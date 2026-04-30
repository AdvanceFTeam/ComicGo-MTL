import io
import json
import os
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import Flask, abort, jsonify, render_template, request, send_from_directory
from PIL import Image, ImageFilter, ImageOps, ImageEnhance

from data.catalog import get_comic, load_comics
#using EasyOCR for OCR, which is a bit heavy to load
_ocr_reader  = None
_ocr_ready   = False
_ocr_lock    = threading.Lock()
_ocr_sem     = threading.Semaphore(1)   # only 1 OCR job at a time


def _load_ocr_background():
    global _ocr_reader, _ocr_ready
    try:
        import easyocr
        print("[OCR] Loading model…")
        reader = easyocr.Reader(['ko', 'en'], gpu=False, verbose=False)
        with _ocr_lock:
            _ocr_reader = reader
            _ocr_ready  = True
        print("[OCR] Ready")
    except Exception:
        print("[OCR] Load failed:")
        traceback.print_exc()


threading.Thread(target=_load_ocr_background, daemon=True).start()


def _get_ocr_reader():
    with _ocr_lock:
        return _ocr_reader if _ocr_ready else None


# Flask 
app = Flask(__name__, static_folder=None)


def filtered_comics(args):
    comics = load_comics()
    query  = args.get("q", "").strip().lower()
    sel_format = args.get("format", "all")
    sel_origin = args.get("origin", "all")
    sel_status = args.get("status", "all")
    sel_sort   = args.get("sort",   "updated")
    r = comics
    if query:
        r = [c for c in r if query in c["title"].lower()
             or query in c["summary"].lower()
             or any(query in g.lower() for g in c["genres"])]
    if sel_format != "all": r = [c for c in r if c["format"] == sel_format]
    if sel_origin != "all": r = [c for c in r if c["origin"] == sel_origin]
    if sel_status != "all": r = [c for c in r if c["status"] == sel_status]
    key = {"rating": lambda c: c["rating"],
           "title":  lambda c: c["title"],
           "chapters": lambda c: len(c["chapters"])}.get(sel_sort, lambda c: c["updated_at"])
    return sorted(r, key=key, reverse=(sel_sort != "title"))


@app.context_processor
def inject_filters():
    comics = load_comics()
    return {
        "formats":  sorted({c["format"] for c in comics}),
        "origins":  sorted({c["origin"] for c in comics}),
        "statuses": sorted({c["status"] for c in comics}),
    }


@app.route("/")
def home():
    comics = filtered_comics(request.args)
    featured = comics[:3]
    latest_chapters = sorted(
        [{**ch, "comic": comic} for comic in comics for ch in comic["chapters"][:2]],
        key=lambda ch: ch["date"], reverse=True)[:6]
    return render_template("index.html", comics=comics, featured=featured,
                           latest_chapters=latest_chapters, filters=request.args)


@app.route("/comic/<slug>")
def comic_detail(slug):
    comic = get_comic(slug)
    if not comic: abort(404)
    return render_template("comic.html", comic=comic)


@app.route("/read/<slug>/<int:chapter_number>")
def reader(slug, chapter_number):
    comic = get_comic(slug)
    if not comic: abort(404)
    chapter = next((ch for ch in comic["chapters"] if ch["number"] == chapter_number), None)
    if not chapter: abort(404)
    chapters = comic["chapters"]
    idx = chapters.index(chapter)
    return render_template("reader.html", comic=comic, chapter=chapter,
                           previous_chapter=chapters[idx+1] if idx+1 < len(chapters) else None,
                           next_chapter=chapters[idx-1] if idx-1 >= 0 else None)


# Supabase 
def _sb_headers():
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
    return {"apikey": key, "Authorization": f"Bearer {key}",
            "Content-Type": "application/json", "Prefer": "return=representation"}


def _get_cached(url):
    sb = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not sb: return None
    try:
        r = requests.get(f"{sb}/rest/v1/page_translations",
                         params={"image_url": f"eq.{url}", "select": "translations", "limit": 1},
                         headers=_sb_headers(), timeout=8)
        r.raise_for_status()
        rows = r.json()
        return rows[0]["translations"] if rows else None
    except Exception:
        return None


def _save_cached(url, translations):
    sb = os.getenv("SUPABASE_URL", "").rstrip("/")
    if not sb: return
    try:
        requests.post(f"{sb}/rest/v1/page_translations",
                      json={"image_url": url, "translations": translations},
                      headers={**_sb_headers(), "Prefer": "resolution=merge-duplicates"},
                      timeout=8)
    except Exception:
        pass


# OCR + translation 
OCR_MAX_W = 1000   # resize to this width before OCR (saves memory; plenty for Korean text)


def _preprocess(img: Image.Image):
    """Resize, sharpen, boost contrast - improves EasyOCR accuracy on webtoons."""
    orig_w, orig_h = img.size
    if orig_w > OCR_MAX_W:
        new_h = int(orig_h * OCR_MAX_W / orig_w)
        img = img.resize((OCR_MAX_W, new_h), Image.LANCZOS)
    img = img.convert("RGB")
    img = ImageOps.autocontrast(img, cutoff=2)
    img = ImageEnhance.Sharpness(img).enhance(1.6)
    img = ImageEnhance.Contrast(img).enhance(1.2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), img.size   # (bytes, (w, h))


def _group_lines(results, img_w, img_h, v_gap=0.035, h_gap=0.55):
    """
    Merge EasyOCR line detections into logical text blocks.
    Lines are merged when they are:
      - vertically close (gap < v_gap fraction of image height), AND
      - horizontally overlapping or within h_gap fraction of image width
    """
    if not results:
        return []

    gap_v = img_h * v_gap
    gap_h = img_w * h_gap

    def bbox_bounds(bbox):
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        return min(xs), min(ys), max(xs), max(ys)

    # Sort top-to-bottom
    entries = []
    for (bbox, text, conf) in results:
        if conf < 0.35: continue
        text = (text or "").strip()
        if not text: continue
        x0, y0, x1, y1 = bbox_bounds(bbox)
        entries.append((x0, y0, x1, y1, text))
    entries.sort(key=lambda e: (e[1], e[0]))

    groups = []   # list of (texts, x0, y0, x1, y1)

    for (ex0, ey0, ex1, ey1, etxt) in entries:
        merged = False
        for g in groups:
            gx0, gy0, gx1, gy1 = g[1], g[2], g[3], g[4]
            v_close = (ey0 - gy1) < gap_v and (ey0 - gy1) > -gy1 * 0.5
            h_overlap = not (ex1 < gx0 - gap_h or ex0 > gx1 + gap_h)
            if v_close and h_overlap:
                g[0].append(etxt)
                g[1] = min(gx0, ex0); g[2] = min(gy0, ey0)
                g[3] = max(gx1, ex1); g[4] = max(gy1, ey1)
                merged = True
                break
        if not merged:
            groups.append([[etxt], ex0, ey0, ex1, ey1])

    return groups


def _batch_translate(texts):
    """Translate a list of strings in one call using a separator trick."""
    from deep_translator import GoogleTranslator
    SEP = " ||| "
    joined = SEP.join(texts)
    try:
        translated = GoogleTranslator(source="ko", target="en").translate(joined) or joined
        parts = translated.split(SEP)
        if len(parts) == len(texts):
            return parts
        # fallback: translate individually
    except Exception:
        pass
    results = []
    for t in texts:
        try:
            results.append(GoogleTranslator(source="ko", target="en").translate(t) or t)
        except Exception:
            results.append(t)
    return results


def _translate_page(image_url):
    reader = _get_ocr_reader()
    if reader is None:
        return []

    # 1. Download
    try:
        resp = requests.get(image_url, timeout=30)
        resp.raise_for_status()
        img_bytes = resp.content
    except Exception as e:
        print(f"[OCR] Download failed: {e}")
        return []

    # 2. Open + preprocess
    try:
        img = Image.open(io.BytesIO(img_bytes))
        orig_w, orig_h = img.size
        ocr_bytes, (ocr_w, ocr_h) = _preprocess(img)
    except Exception as e:
        print(f"[OCR] Preprocess failed: {e}")
        return []

    # 3. OCR - one job at a time via semaphore
    with _ocr_sem:
        try:
            raw = reader.readtext(ocr_bytes, detail=1, paragraph=False,
                                  contrast_ths=0.1, adjust_contrast=0.5,
                                  text_threshold=0.5, low_text=0.3)
        except Exception as e:
            print(f"[OCR] readtext failed: {e}")
            traceback.print_exc()
            return []

    if not raw:
        return []

    # 4. Group nearby lines
    groups = _group_lines(raw, ocr_w, ocr_h)
    if not groups:
        return []

    # 5. Batch translate
    raw_texts = [" ".join(g[0]) for g in groups]
    translated = _batch_translate(raw_texts)

    # 6. Build bubble list (coords normalised to original image fractions)
    PAD_X, PAD_Y = 0.010, 0.008
    bubbles = []
    for i, g in enumerate(groups):
        _, gx0, gy0, gx1, gy1 = g[0], g[1], g[2], g[3], g[4]
        x0 = max(0.0, gx0 / ocr_w - PAD_X)
        y0 = max(0.0, gy0 / ocr_h - PAD_Y)
        x1 = min(1.0, gx1 / ocr_w + PAD_X)
        y1 = min(1.0, gy1 / ocr_h + PAD_Y)
        bubbles.append({
            "x": round(x0, 4), "y": round(y0, 4),
            "w": round(x1 - x0, 4), "h": round(y1 - y0, 4),
            "text": translated[i],
            "raw":  raw_texts[i],
        })

    print(f"[OCR] {len(bubbles)} bubbles ← {image_url.split('/')[-1]}")
    return bubbles


# API routes

@app.route("/api/translate")
def api_translate():
    url = request.args.get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing url"}), 400

    if not _ocr_ready:
        return jsonify({"url": url, "bubbles": [], "ocr_ready": False,
                        "message": "OCR model loading, retry shortly"})

    cached = _get_cached(url)
    if cached is not None:
        return jsonify({"url": url, "bubbles": cached, "cached": True, "ocr_ready": True})

    try:
        bubbles = _translate_page(url)
    except Exception:
        traceback.print_exc()
        bubbles = []

    _save_cached(url, bubbles)
    return jsonify({"url": url, "bubbles": bubbles, "cached": False, "ocr_ready": True})


@app.route("/api/ocr_status")
def api_ocr_status():
    return jsonify({"ready": _ocr_ready})


@app.route("/api/comics")
def comics_api():
    return jsonify(filtered_comics(request.args))


@app.route("/css/<path:filename>")
def css(filename):
    return send_from_directory("public/css", filename)


@app.route("/js/<path:filename>")
def js(filename):
    return send_from_directory("public/js", filename)


@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory("public/assets", filename)


@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=True, threaded=True, use_reloader=False)
