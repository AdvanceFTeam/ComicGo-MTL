import os
import re
import time
import json
import mimetypes
import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from bs4 import BeautifulSoup
import threading
from urllib.parse import quote, urlparse, urljoin, unquote
from pathlib import Path
import zipfile
import base64
import io
import argparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

PLAYWRIGHT_AVAILABLE = False
PIL_AVAILABLE = False
EPUB_AVAILABLE = False

mimetypes.add_type("image/webp", ".webp")

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    pass

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    pass

try:
    import ebooklib
    from ebooklib import epub
    EPUB_AVAILABLE = True
except ImportError:
    pass


#  Image compression core
#  Strategy: always output WebP @ quality=82, method=6
#  WebP beats JPEG by ~30% at same perceptual quality; method=6 = slowest
#  encoder but best compression ratio (still fast enough for this use case)
WEBP_QUALITY   = 82   # sweet spot: HD quality, minimum bytes
WEBP_METHOD    = 6    # 0=fast/big → 6=slow/small
WEBP_LOSSLESS  = False

def compress_to_webp(data: bytes) -> bytes:
    """Convert any image bytes → compressed WebP bytes. Returns original on failure."""
    if not PIL_AVAILABLE:
        return data
    try:
        img = Image.open(io.BytesIO(data))
        # flatten transparency onto white before encoding lossy
        if img.mode in ('RGBA', 'LA', 'P'):
            bg = Image.new('RGBA', img.size, (255, 255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = bg.convert('RGB')
        elif img.mode != 'RGB':
            img = img.convert('RGB')

        buf = io.BytesIO()
        img.save(buf, format='WEBP', quality=WEBP_QUALITY, method=WEBP_METHOD, lossless=WEBP_LOSSLESS)
        result = buf.getvalue()
        return result
    except Exception:
        return data


def compress_to_webp_for_cbz(img_path: Path) -> bytes | None:
    """Read image file, return compressed WebP bytes for CBZ packing."""
    try:
        return compress_to_webp(img_path.read_bytes())
    except Exception:
        return None


class SupabaseStorageUploader:
    def __init__(self, supabase_url: str, api_key: str, bucket: str, prefix: str = "", log=None):
        self.supabase_url = (supabase_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.bucket = bucket or ""
        self.prefix = self._clean_prefix(prefix)
        self.log = log or (lambda msg, tag="info": None)
        self.session = requests.Session()

    @classmethod
    def from_env(cls, bucket: str = "", prefix: str = "", log=None):
        return cls(
            supabase_url=os.getenv("SUPABASE_URL", ""),
            api_key=os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY", ""),
            bucket=bucket or os.getenv("SUPABASE_BUCKET", ""),
            prefix=prefix or os.getenv("SUPABASE_PREFIX", ""),
            log=log,
        )

    def missing_config(self) -> list[str]:
        missing = []
        if not self.supabase_url:
            missing.append("SUPABASE_URL")
        if not self.api_key:
            missing.append("SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY")
        if not self.bucket:
            missing.append("SUPABASE_BUCKET or GUI bucket")
        return missing

    def upload_chapter(self, chapter_url: str, output_dir: Path, image_paths: list[Path], export_paths: list[Path] | None = None) -> dict:
        comic_title = output_dir.parent.name
        chapter_title = output_dir.name
        chapter_prefix = self._join_storage_path(
            self.prefix,
            self._storage_segment(comic_title),
            self._storage_segment(chapter_title),
        )

        pages = []
        sorted_images = sorted(image_paths, key=self._path_sort_key)
        total = len(sorted_images)
        self.log(f"Uploading {total} pages to Supabase bucket '{self.bucket}'…", "info")

        for index, image_path in enumerate(sorted_images, 1):
            storage_path = self._join_storage_path(chapter_prefix, "pages", image_path.name)
            self._upload_file(image_path, storage_path)
            pages.append(
                {
                    "page": index,
                    "filename": image_path.name,
                    "storage_path": storage_path,
                    "public_url": self.public_url(storage_path),
                }
            )
            self.log(f"  ✓ Uploaded page {index}/{total}: {image_path.name}", "ok")

        exports = []
        for export_path in sorted(export_paths or [], key=lambda path: path.name.lower()):
            storage_path = self._join_storage_path(chapter_prefix, "exports", export_path.name)
            self._upload_file(export_path, storage_path)
            exports.append(
                {
                    "filename": export_path.name,
                    "storage_path": storage_path,
                    "public_url": self.public_url(storage_path),
                }
            )
            self.log(f"  ✓ Uploaded export: {export_path.name}", "ok")

        manifest = {
            "comic_title": comic_title,
            "chapter_title": chapter_title,
            "source_url": chapter_url,
            "storage_bucket": self.bucket,
            "storage_prefix": chapter_prefix,
            "page_count": len(pages),
            "pages": pages,
            "exports": exports,
            "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        manifest_path = self._join_storage_path(chapter_prefix, "chapter.json")
        self._upload_bytes(
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            manifest_path,
            "application/json; charset=utf-8",
        )
        manifest["manifest_path"] = manifest_path
        manifest["manifest_url"] = self.public_url(manifest_path)
        self.log(f"  ✓ Uploaded manifest: {manifest_path}", "ok")

        self._insert_metadata_if_configured(manifest)
        return manifest

    def _upload_file(self, local_path: Path, storage_path: str):
        content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        self._upload_bytes(local_path.read_bytes(), storage_path, content_type)

    def _upload_bytes(self, data: bytes, storage_path: str, content_type: str):
        content_type = content_type.split(";")[0].strip()
        encoded_path = quote(storage_path, safe="/")
        url = f"{self.supabase_url}/storage/v1/object/{self.bucket}/{encoded_path}"
        response = self.session.post(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "apikey": self.api_key,
                "Content-Type": content_type,
                "Cache-Control": "3600",
                "x-upsert": "true",
            },
            timeout=90,
        )
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Supabase upload failed ({response.status_code}): {response.text[:240]}")

    def _insert_metadata_if_configured(self, manifest: dict):
        table = os.getenv("SUPABASE_CHAPTERS_TABLE", "").strip()
        if not table:
            return
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", table):
            self.log("Skipped metadata insert: SUPABASE_CHAPTERS_TABLE is not a safe table name", "warn")
            return

        record = {
            "comic_title": manifest["comic_title"],
            "chapter_title": manifest["chapter_title"],
            "source_url": manifest["source_url"],
            "storage_bucket": manifest["storage_bucket"],
            "storage_prefix": manifest["storage_prefix"],
            "manifest_path": manifest["manifest_path"],
            "page_count": manifest["page_count"],
            "pages": manifest["pages"],
        }
        url = f"{self.supabase_url}/rest/v1/{table}?on_conflict=storage_bucket,storage_prefix"
        response = self.session.post(
            url,
            data=json.dumps(record, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "apikey": self.api_key,
                "Content-Type": "application/json",
                "Prefer": "return=minimal,resolution=merge-duplicates",
            },
            timeout=30,
        )
        if response.status_code in (200, 201, 204):
            self.log(f"  ✓ Inserted metadata into Supabase table '{table}'", "ok")
        else:
            self.log(f"  ⚠ Metadata insert failed ({response.status_code}): {response.text[:180]}", "warn")

    def public_url(self, storage_path: str) -> str:
        encoded_path = quote(storage_path, safe="/")
        return f"{self.supabase_url}/storage/v1/object/public/{self.bucket}/{encoded_path}"

    @staticmethod
    def _path_sort_key(path: Path):
        match = re.search(r"(\d+)", path.stem)
        return (int(match.group(1)) if match else 999999, path.name.lower())

    @staticmethod
    def _storage_segment(value: str) -> str:
        value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value or "").strip()
        value = re.sub(r"\s+", "-", value)
        return value[:120] or "chapter"

    @classmethod
    def _clean_prefix(cls, prefix: str) -> str:
        parts = [cls._storage_segment(part) for part in (prefix or "").split("/") if part.strip()]
        return "/".join(parts)

    @staticmethod
    def _join_storage_path(*parts: str) -> str:
        return "/".join(str(part).strip("/") for part in parts if str(part).strip("/"))


class PlainVar:
    def __init__(self, value=None):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value




class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text   = text
        self.tip    = None
        widget.bind("<Enter>", self.show)
        widget.bind("<Leave>", self.hide)

    def show(self, _=None):
        x = self.widget.winfo_rootx() + 25
        y = self.widget.winfo_rooty() + 25
        self.tip = tk.Toplevel(self.widget)
        self.tip.wm_overrideredirect(True)
        self.tip.wm_geometry(f"+{x}+{y}")
        tk.Label(self.tip, text=self.text, background="#ffffe0",
                 relief="solid", borderwidth=1, padx=6, pady=4).pack()

    def hide(self, _=None):
        if self.tip:
            self.tip.destroy()
            self.tip = None


#  Universal image-density scorer
#  Given a tag, returns (score, urls[])  higher score = more likely a reader
READER_KEYWORDS = re.compile(
    r'(reader|viewer|chapter|page|scan|manga|manhwa|manhua|comic|strip|episode|webtoon)',
    re.I
)

def _score_container(tag) -> int:
    """Heuristic score for how likely a container holds comic pages."""
    score = 0
    imgs  = tag.find_all('img', recursive=True)
    score += len(imgs) * 10
    text = ' '.join([tag.get('class', [''])[0] if tag.get('class') else '',
                     tag.get('id', ''),
                     tag.name])
    if READER_KEYWORDS.search(text):
        score += 50
    if tag.get('id') and READER_KEYWORDS.search(tag.get('id', '')):
        score += 30
    return score


class UniversalComicDownloader:
    def __init__(self, root):
        self.root = root
        self.root.title("Comic Downloader  Universal Edition")
        self.root.geometry("980x730")
        self.root.resizable(True, True)

        self.bg_color      = "#f0f4f8"
        self.accent_color  = "#667eea"
        self.root.configure(bg=self.bg_color)

        self.url_var                = tk.StringVar()
        self.output_var             = tk.StringVar(value=str(Path.home() / "Downloads" / "Comics"))
        self.use_browser_var        = tk.BooleanVar(value=PLAYWRIGHT_AVAILABLE)
        self.exclude_gifs_var       = tk.BooleanVar(value=True)
        self.skip_tiny_var          = tk.BooleanVar(value=True)
        self.aggressive_comments_var= tk.BooleanVar(value=True)
        self.compress_webp_var      = tk.BooleanVar(value=True)
        self.generate_cbz_var       = tk.BooleanVar(value=True)
        self.generate_pdf_var       = tk.BooleanVar(value=False)
        self.generate_epub_var      = tk.BooleanVar(value=False)
        self.upload_supabase_var    = tk.BooleanVar(value=os.getenv("SUPABASE_UPLOAD", "").lower() in ("1", "true", "yes", "on"))
        self.supabase_bucket_var    = tk.StringVar(value=os.getenv("SUPABASE_BUCKET", "comics"))
        self.supabase_prefix_var    = tk.StringVar(value=os.getenv("SUPABASE_PREFIX", "comics"))

        self.running         = False
        self.total_images    = 0
        self.current_status  = tk.StringVar(value="Ready")
        self.progress_value  = tk.DoubleVar(value=0)
        self.progress_label  = tk.StringVar(value="0%")
        self.images_found    = tk.StringVar(value="Images found: 0")
        self.images_downloaded = tk.StringVar(value="Downloaded: 0/0")

        self.setup_ui()
        self._startup_log()

    #  UI
    def setup_ui(self):
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('Title.TLabel',    font=('Segoe UI', 14, 'bold'), foreground='#667eea')
        style.configure('Subtitle.TLabel', font=('Segoe UI', 9),          foreground='#718096')
        style.configure('TButton',         font=('Segoe UI', 9),          padding=10)
        style.configure('Primary.TButton', font=('Segoe UI', 10, 'bold'), padding=12)
        style.configure('TLabelframe',     borderwidth=2, relief='flat')
        style.configure('TLabelframe.Label', font=('Segoe UI', 9, 'bold'), foreground='#4a5568')

        main = ttk.Frame(self.root, padding="20")
        main.pack(fill=tk.BOTH, expand=True)

        # header
        hf = ttk.Frame(main)
        hf.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 15))
        ttk.Label(hf, text="Comic Downloader", style='Title.TLabel').pack(side=tk.LEFT)
        ttk.Label(hf, text=" • Universal Edition  WebP compressed, HD",
                  style='Subtitle.TLabel').pack(side=tk.LEFT, padx=(8, 0))

        # URL
        uf = ttk.LabelFrame(main, text=" 🔗 Chapter URL ", padding=8)
        uf.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Entry(uf, textvariable=self.url_var, font=("Segoe UI", 10)).pack(fill="x")

        # Save
        sf = ttk.LabelFrame(main, text=" 💾 Save Location ", padding=8)
        sf.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        pf = ttk.Frame(sf)
        pf.pack(fill="x")
        ttk.Entry(pf, textvariable=self.output_var, font=("Segoe UI", 9)).pack(
            side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(pf, text="Browse", command=self.choose_folder, width=10).pack(side="right")

        # Supabase
        supa = ttk.LabelFrame(main, text=" ☁️ Supabase Upload ", padding=8)
        supa.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        ttk.Checkbutton(
            supa,
            text="Upload downloaded chapter to Supabase Storage after download",
            variable=self.upload_supabase_var,
        ).pack(anchor="w", pady=(0, 6))
        supa_grid = ttk.Frame(supa)
        supa_grid.pack(fill="x")
        ttk.Label(supa_grid, text="Bucket").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(supa_grid, textvariable=self.supabase_bucket_var, width=22).grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(supa_grid, text="Path prefix").grid(row=0, column=1, sticky="w")
        ttk.Entry(supa_grid, textvariable=self.supabase_prefix_var).grid(row=1, column=1, sticky="ew")
        supa_grid.columnconfigure(1, weight=1)
        ttk.Label(
            supa,
            text="Requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY in your environment.",
            style='Subtitle.TLabel',
        ).pack(anchor="w", pady=(6, 0))

        # Options
        opt = ttk.LabelFrame(main, text=" ⚙️ Options ", padding=8)
        opt.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        lc = ttk.Frame(opt)
        lc.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 10))
        ttk.Checkbutton(lc, text="Use Browser Mode (recommended for protected/JS sites)",
                        variable=self.use_browser_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(lc, text="Compress images → WebP HD (saves ~30-40% disk space)",
                        variable=self.compress_webp_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(lc, text="Skip GIF images",
                        variable=self.exclude_gifs_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(lc, text="Skip small images (emojis, icons, ads)",
                        variable=self.skip_tiny_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(lc, text="Aggressive filtering (strip comments/widgets)",
                        variable=self.aggressive_comments_var).pack(anchor="w", pady=1)

        rc = ttk.Frame(opt)
        rc.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        ttk.Label(rc, text="Export as:", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(0, 2))
        ttk.Checkbutton(rc, text="CBZ (Comic Book Archive  WebP inside)",
                        variable=self.generate_cbz_var).pack(anchor="w", pady=1)
        ttk.Checkbutton(rc, text="PDF Document",
                        variable=self.generate_pdf_var,
                        state="normal" if PIL_AVAILABLE else "disabled").pack(anchor="w", pady=1)
        ttk.Checkbutton(rc, text="EPUB eBook",
                        variable=self.generate_epub_var,
                        state="normal" if EPUB_AVAILABLE and PIL_AVAILABLE else "disabled").pack(anchor="w", pady=1)

        # Buttons
        bf = ttk.Frame(main)
        bf.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(5, 15))
        self.test_btn   = ttk.Button(bf, text="🔍 Test URL",        command=self.test_url,        width=15)
        self.start_btn  = ttk.Button(bf, text="▶️ Start Download",  command=self.start_download,  style='Primary.TButton', width=18)
        self.cancel_btn = ttk.Button(bf, text="⏹️ Cancel",          command=self.cancel,          width=12, state="disabled")
        self.test_btn.pack(side="left", padx=(0, 8))
        self.start_btn.pack(side="left", padx=(0, 8))
        self.cancel_btn.pack(side="left", padx=(0, 15))
        ttk.Button(bf, text="📁 Open Folder", command=self.open_folder, width=14).pack(side="left", padx=(0, 8))
        ttk.Button(bf, text="🗑️ Clear Log",  command=self.clear_log,   width=12).pack(side="left")

        # Progress
        prf = ttk.LabelFrame(main, text=" 📊 Progress ", padding=8)
        prf.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        sl = ttk.Frame(prf)
        sl.pack(fill="x", pady=(0, 8))
        ttk.Label(sl, text="Status:", font=("Segoe UI", 9, "bold")).pack(side="left", padx=(0, 8))
        ttk.Label(sl, textvariable=self.current_status, foreground="#1976D2",
                  font=("Segoe UI", 9)).pack(side="left")
        pc = ttk.Frame(prf)
        pc.pack(fill="x", pady=(0, 8))
        self.progress = ttk.Progressbar(pc, mode="determinate", variable=self.progress_value)
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Label(pc, textvariable=self.progress_label, width=8,
                  font=("Segoe UI", 9, "bold")).pack(side="right")
        il = ttk.Frame(prf)
        il.pack(fill="x")
        ttk.Label(il, textvariable=self.images_found,      foreground="#666", font=("Segoe UI", 8)).pack(side="left", padx=(0, 15))
        ttk.Label(il, textvariable=self.images_downloaded, foreground="#666", font=("Segoe UI", 8)).pack(side="left")

        # Log
        lf = ttk.LabelFrame(main, text=" 📝 Activity Log ", padding=8)
        lf.grid(row=7, column=0, columnspan=2, sticky="nsew")
        self.log = scrolledtext.ScrolledText(lf, height=14, font=("Consolas", 9),
                                             bg="#ffffff", wrap="word", relief="flat", borderwidth=1)
        self.log.pack(fill="both", expand=True)
        self.log.tag_config("info",  foreground="#4a5568")
        self.log.tag_config("ok",    foreground="#48bb78")
        self.log.tag_config("warn",  foreground="#ed8936")
        self.log.tag_config("error", foreground="#f56565")

        main.columnconfigure(0, weight=1)
        main.rowconfigure(7, weight=1)

    def _startup_log(self):
        self.log_message("=" * 60, "info")
        self.log_message("Comic Downloader  Universal Edition", "ok")
        self.log_message("Images saved as compressed HD WebP (~30-40% smaller)", "info")
        self.log_message("=" * 60, "info")
        self.log_message(f"{'✓' if PLAYWRIGHT_AVAILABLE else '⚠'} Browser mode {'ready' if PLAYWRIGHT_AVAILABLE else 'MIA  pip install playwright && playwright install'}", "ok" if PLAYWRIGHT_AVAILABLE else "warn")
        self.log_message(f"{'✓' if PIL_AVAILABLE else '⚠'} Pillow {'loaded' if PIL_AVAILABLE else 'missing  pip install pillow'}", "ok" if PIL_AVAILABLE else "warn")
        self.log_message(f"{'✓' if EPUB_AVAILABLE else '⚠'} EPUB {'ready' if EPUB_AVAILABLE else 'missing  pip install ebooklib'}", "ok" if EPUB_AVAILABLE else "warn")
        if os.getenv("SUPABASE_URL") and (os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY")):
            self.log_message("✓ Supabase environment detected", "ok")
        else:
            self.log_message("⚠ Supabase upload needs SUPABASE_URL and a Supabase key", "warn")
        self.log_message("-" * 60, "info")

    #  Helpers
    def log_message(self, msg: str, tag: str = "info"):
        ts = time.strftime("%H:%M:%S")
        self.log.insert(tk.END, f"[{ts}] {msg}\n", tag)
        self.log.see(tk.END)
        self.root.update_idletasks()

    def update_status(self, text: str):
        self.current_status.set(text)
        self.root.update_idletasks()

    def clear_log(self):
        self.log.delete("1.0", tk.END)
        self.progress_value.set(0)
        self.progress_label.set("0%")
        self.update_status("Ready")

    def choose_folder(self):
        f = filedialog.askdirectory()
        if f:
            self.output_var.set(f)

    def open_folder(self):
        path = Path(self.output_var.get().strip() or "~/Downloads/Comics").expanduser()
        path.mkdir(parents=True, exist_ok=True)
        try:
            if os.name == "nt":
                os.startfile(path)
            elif "darwin" in os.sys.platform:
                os.system(f'open "{path}"')
            else:
                os.system(f'xdg-open "{path}"')
        except Exception as e:
            self.log_message(f"Couldn't open folder: {e}", "error")

    def cancel(self):
        self.running = False
        self.update_status("Cancelling…")
        self.log_message("Cancel requested  finishing current image then stopping", "warn")

    #  Test / Download entry points
    def test_url(self):
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Error", "Enter a URL first.")
            return
        threading.Thread(target=self._test_task, args=(url,), daemon=True).start()

    def _test_task(self, url):
        self.update_status("Testing URL…")
        self.start_btn["state"] = self.test_btn["state"] = "disabled"
        try:
            html = self.fetch_page(url, self.use_browser_var.get())
            if self.is_naver_series_url(url):
                series = self.parse_naver_series_page(html, url)
                self.log_message(f"✓ Naver series: {series['title']}", "ok")
                self.log_message(f"  Episodes found: {len(series['episodes'])}", "info")
                for episode in series["episodes"][:10]:
                    self.log_message(f"  {episode['number']:03d}. {episode['title']} - {episode['url']}", "info")
                if len(series["episodes"]) > 10:
                    self.log_message(f"  … and {len(series['episodes'])-10} more", "info")
            else:
                imgs = self.extract_image_urls(html, url)
                self.log_message(f"✓ Found {len(imgs)} images", "ok")
                self.images_found.set(f"Images found: {len(imgs)}")
                for i, u in enumerate(imgs[:10], 1):
                    self.log_message(f"  {i:02d}. {u[:120]}", "info")
                if len(imgs) > 10:
                    self.log_message(f"  … and {len(imgs)-10} more", "info")
        except Exception as e:
            self.log_message(f"✗ Test failed: {e}", "error")
        finally:
            self.start_btn["state"] = self.test_btn["state"] = "normal"
            self.update_status("Ready")

    def start_download(self):
        if self.running:
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showwarning("Error", "Please enter a chapter URL.")
            return
        self.running = True
        self.start_btn["state"] = self.test_btn["state"] = "disabled"
        self.cancel_btn["state"] = "normal"
        self.log.delete("1.0", tk.END)
        self.progress_value.set(0)
        self.progress_label.set("0%")
        self.update_status("Starting…")
        target = self.download_series_task if self.is_naver_series_url(url) else self.download_task
        threading.Thread(target=target, args=(url, self.output_var.get().strip()), daemon=True).start()

    @staticmethod
    def is_naver_series_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.endswith("comic.naver.com") and "/webtoon/list" in parsed.path

    @staticmethod
    def is_naver_detail_url(url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.endswith("comic.naver.com") and "/webtoon/detail" in parsed.path

    def parse_naver_series_page(self, html: str, series_url: str) -> dict:
        source_html = html if html and len(html) > 3000 else self._scrap_html_for(series_url) or html
        soup = BeautifulSoup(source_html, "html.parser")

        title = self._selector_text(soup, '[class*="EpisodeListInfo__title"]') or self._meta_content(soup, "og:title") or "Naver Webtoon"
        author = self._selector_text(soup, '[class*="ContentMetaInfo__category"] a') or ""
        summary = self._selector_text(soup, '[class*="EpisodeListInfo__summary"]') or ""
        cover = ""
        cover_tag = soup.select_one('[class*="Poster__type193x250"] img, [class*="EpisodeListInfo"] img')
        if cover_tag:
            cover = self.normalize_url(cover_tag.get("src", ""), series_url)
        tags = [
            self._clean_text(tag.get_text(" ", strip=True)).lstrip("#")
            for tag in soup.select('[class*="TagGroup__tag"]')
        ]
        tags = [tag for tag in dict.fromkeys(tags) if tag]

        episodes = []
        seen_urls = set()
        for link in soup.select('a[href*="/webtoon/detail"][href*="titleId="][href*="no="]'):
            href = self.normalize_url(link.get("href", ""), series_url)
            if not href or href in seen_urls:
                continue
            seen_urls.add(href)
            parsed = urlparse(href)
            no_match = re.search(r"(?:\?|&)no=(\d+)", "?" + parsed.query)
            number = int(no_match.group(1)) if no_match else len(episodes) + 1
            title_text = self._selector_text(link, '[class*="EpisodeListList__title"]') or self._selector_text(link, '[class*="EpisodeListUser__text"]') or f"Episode {number}"
            date_text = self._selector_text(link, ".date")
            rating_text = self._selector_text(link, ".text")
            thumbnail_tag = link.select_one("img")
            thumbnail = self.normalize_url(thumbnail_tag.get("src", ""), series_url) if thumbnail_tag else ""
            episodes.append(
                {
                    "number": number,
                    "title": title_text,
                    "date": date_text,
                    "rating": rating_text,
                    "thumbnail": thumbnail,
                    "url": href,
                }
            )

        episodes.sort(key=lambda item: item["number"])
        return {
            "title": title,
            "author": author,
            "summary": summary,
            "cover": cover,
            "tags": tags,
            "source_url": series_url,
            "episodes": episodes,
        }

    def _scrap_html_for(self, url: str) -> str:
        path = Path("scrap.html")
        if not path.exists():
            return ""
        try:
            html = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            html = path.read_text(encoding="utf-8-sig")
        title_id = self._query_value(url, "titleId")
        if not title_id or f"titleId={title_id}" in html:
            return html
        return ""

    @staticmethod
    def _query_value(url: str, name: str) -> str:
        match = re.search(rf"(?:\?|&){re.escape(name)}=([^&]+)", url)
        return unquote(match.group(1)) if match else ""

    def _selector_text(self, root, selector: str) -> str:
        element = root.select_one(selector)
        return self._clean_text(element.get_text(" ", strip=True)) if element else ""

    @staticmethod
    def _meta_content(soup, property_name: str) -> str:
        tag = soup.find("meta", property=property_name)
        return tag.get("content", "").strip() if tag else ""

    @staticmethod
    def _clean_text(value: str) -> str:
        value = re.sub(r"\s+", " ", value or "").strip()
        if not value:
            return ""
        parts = value.split(" ")
        half = len(parts) // 2
        if len(parts) % 2 == 0 and parts[:half] == parts[half:]:
            value = " ".join(parts[:half])
        value = re.sub(r"(.{3,120})\s+\1$", r"\1", value)
        return value.strip()

    def download_series_task(self, series_url: str, base_dir: str):
        self._series_mode = True
        try:
            use_browser = self.use_browser_var.get() and PLAYWRIGHT_AVAILABLE
            self.update_status("Loading Naver series…")
            html = self.fetch_page(series_url, use_browser)
            series = self.parse_naver_series_page(html, series_url)
            if not series["episodes"]:
                self.log_message("✗ No Naver episodes found on series page.", "error")
                return

            self.log_message(f"✓ Naver series: {series['title']}", "ok")
            self.log_message(f"  Author: {series['author'] or 'Unknown'}", "info")
            self.log_message(f"  Episodes queued: {len(series['episodes'])}", "info")
            self._write_series_metadata(base_dir, series)

            for index, episode in enumerate(series["episodes"], 1):
                if not self.running:
                    self.log_message("Series download cancelled.", "warn")
                    break
                self.log_message("=" * 60, "info")
                self.log_message(f"Episode {index}/{len(series['episodes'])}: {episode['title']}", "info")
                self.download_task(episode["url"], base_dir)
                time.sleep(0.4)
        except Exception as e:
            self.log_message(f"Fatal Naver series error: {e}", "error")
        finally:
            self._series_mode = False
            self._finish()

    def _write_series_metadata(self, base_dir: str, series: dict):
        series_dir = Path(base_dir) / self._sanitize(series["title"])
        series_dir.mkdir(parents=True, exist_ok=True)
        metadata_path = series_dir / "series.json"
        metadata_path.write_text(json.dumps(series, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log_message(f"  Series metadata: {metadata_path}", "info")

    #  Main download task
    def download_task(self, chapter_url: str, base_dir: str):
        saved_paths = []
        try:
            use_browser = self.use_browser_var.get() and PLAYWRIGHT_AVAILABLE
            do_compress = self.compress_webp_var.get() and PIL_AVAILABLE

            self.update_status(f"Loading page via {'Browser' if use_browser else 'HTTP'}…")
            html = self.fetch_page(chapter_url, use_browser)
            self.log_message("✓ Page loaded", "ok")

            self.update_status("Extracting image URLs…")
            image_urls = self.extract_image_urls(html, chapter_url)

            if not image_urls:
                self.log_message("✗ Zero images found. Try Browser Mode.", "error")
                return

            output_dir = self.get_output_directory(html, chapter_url, base_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
            self.log_message(f"Saving to: {output_dir}", "info")

            self.total_images = len(image_urls)
            self.images_found.set(f"Images found: {self.total_images}")
            self.log_message(f"✓ {self.total_images} images queued", "ok")

            # batch browser capture
            browser_cache: dict[str, bytes] = {}
            if use_browser and PLAYWRIGHT_AVAILABLE:
                self.log_message("Batch-capturing images via browser…", "info")
                browser_cache = self.batch_download_with_browser(chapter_url, image_urls)
                if browser_cache:
                    self.log_message(f"✓ Browser captured {len(browser_cache)} images", "ok")

            success = 0
            questionable_dir = output_dir / "_questionable"

            for i, img_url in enumerate(image_urls, 1):
                if not self.running:
                    self.log_message(f"✗ Cancelled at {i}/{self.total_images}", "warn")
                    break

                self.update_status(f"Downloading {i}/{self.total_images}…")
                self.images_downloaded.set(f"Downloaded: {success}/{self.total_images}")

                # always save as .webp if compression enabled, else keep original ext
                orig_ext = Path(urlparse(img_url).path).suffix.lower() or '.jpg'
                out_ext  = '.webp' if do_compress else orig_ext
                filename = f"{i:03d}{out_ext}"
                save_path = output_dir / filename

                self.log_message(f"[{i:03d}/{self.total_images}] {filename}", "info")

                try:
                    # get raw bytes
                    content = None
                    if img_url in browser_cache:
                        content = browser_cache[img_url]
                        self.log_message(f"  ← browser cache ({len(content)//1024} KB raw)", "info")
                    else:
                        content = self._http_get_image(img_url, chapter_url)
                        if content is None and use_browser:
                            content = self.download_image_with_browser(img_url, chapter_url)

                    if not content:
                        raise ValueError("Empty response")

                    # tiny-image guard
                    if self._is_suspicious(content, filename):
                        if self.skip_tiny_var.get():
                            self.log_message("  ⚠ Skipped (tiny/suspect)", "warn")
                            continue
                        else:
                            questionable_dir.mkdir(parents=True, exist_ok=True)
                            (questionable_dir / filename).write_bytes(content)
                            self.log_message(f"  ⚠ Quarantined ({len(content)//1024} KB)", "warn")
                            continue

                    # compress → WebP
                    if do_compress:
                        before = len(content)
                        content = compress_to_webp(content)
                        after   = len(content)
                        saving  = 100 - (after * 100 // before)
                        self.log_message(f"  ✓ Compressed: {before//1024} KB → {after//1024} KB (saved {saving}%)", "ok")
                    else:
                        self.log_message(f"  Size: {len(content)//1024} KB", "info")

                    save_path.write_bytes(content)
                    saved_paths.append(save_path)
                    success += 1

                    perc = i / self.total_images * 100
                    self.progress_value.set(perc)
                    self.progress_label.set(f"{int(perc)}%")
                    self.images_downloaded.set(f"Downloaded: {success}/{self.total_images}")
                    self.log_message(f"  ✓ Saved → {filename}", "ok")
                    time.sleep(0.05)

                except Exception as e:
                    self.log_message(f"  ✗ Failed: {str(e)[:120]}", "error")

            self.log_message("=" * 60, "info")
            self.log_message(f"✓ Done  {success}/{self.total_images} images saved", "ok")
            self.log_message(f"  Location: {output_dir}", "info")
            self.log_message("=" * 60, "info")

            # exports
            exports = []
            if self.running and self.generate_cbz_var.get():
                if self.generate_cbz(output_dir, saved_paths):
                    exports.append("CBZ")
            if self.running and self.generate_pdf_var.get() and PIL_AVAILABLE:
                if self.generate_pdf(output_dir):
                    exports.append("PDF")
            if self.running and self.generate_epub_var.get() and EPUB_AVAILABLE and PIL_AVAILABLE:
                if self.generate_epub(output_dir):
                    exports.append("EPUB")
            if exports:
                self.log_message(f"✓ Exports: {', '.join(exports)}", "ok")

            if self.running and success and self.upload_supabase_var.get():
                self.update_status("Uploading to Supabase…")
                uploader = SupabaseStorageUploader.from_env(
                    bucket=self.supabase_bucket_var.get().strip(),
                    prefix=self.supabase_prefix_var.get().strip(),
                    log=self.log_message,
                )
                missing = uploader.missing_config()
                if missing:
                    self.log_message(f"✗ Supabase upload skipped. Missing: {', '.join(missing)}", "error")
                    self.log_message("  Set environment variables, then restart the downloader.", "info")
                else:
                    export_paths = [
                        path for path in output_dir.iterdir()
                        if path.is_file() and path.suffix.lower() in (".cbz", ".pdf", ".epub")
                    ]
                    manifest = uploader.upload_chapter(chapter_url, output_dir, saved_paths, export_paths)
                    self.log_message(f"✓ Supabase manifest: {manifest['manifest_url']}", "ok")

            self.update_status("All done!")

        except Exception as e:
            self.log_message(f"Fatal error: {e}", "error")
        finally:
            self._finish()

    _HEADERS = {
        "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept":          "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection":      "keep-alive",
        "Sec-Fetch-Dest":  "image",
        "Sec-Fetch-Mode":  "no-cors",
        "Sec-Fetch-Site":  "cross-site",
    }

    def _http_get_image(self, img_url: str, referer: str) -> bytes | None:
        headers = {**self._HEADERS, "Referer": referer}
        try:
            r = requests.get(img_url, headers=headers, timeout=25, stream=True, allow_redirects=True)
            r.raise_for_status()
            return r.content
        except Exception:
            return None

    def _is_suspicious(self, data: bytes, filename: str) -> bool:
        if len(data) < 15 * 1024:
            return True
        if len(data) < 50 * 1024 and PIL_AVAILABLE and filename.lower().endswith('.png'):
            try:
                img = Image.open(io.BytesIO(data))
                w, h = img.size
                if w < 200 or h < 200 or w / h > 8 or h / w > 8:
                    return True
            except Exception:
                return True
        return False

    def fetch_page(self, url: str, use_browser: bool) -> str:
        if use_browser and PLAYWRIGHT_AVAILABLE:
            for attempt in range(1, 4):
                try:
                    with sync_playwright() as p:
                        browser = p.chromium.launch(
                            headless=True,
                            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                        )
                        ctx = browser.new_context(
                            user_agent=self._HEADERS["User-Agent"],
                            viewport={"width": 1280, "height": 900}
                        )
                        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                        page = ctx.new_page()
                        page.goto(url, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(2500)
                        page.evaluate("""() => {
                            document.querySelectorAll('img[data-src],img[data-lazy],img[data-lazy-src],img[data-original]').forEach(img => {
                                ['data-src','data-lazy','data-lazy-src','data-original'].forEach(a => {
                                    if (img.dataset[a.replace('data-','').replace(/-./,c=>c[1].toUpperCase())])
                                        img.src = img.getAttribute(a);
                                });
                            });
                        }""")
                        ph = page.evaluate("document.body.scrollHeight")
                        vh = page.evaluate("window.innerHeight")
                        for i in range(max(25, ph // vh + 5)):
                            page.evaluate(f"window.scrollTo(0,{i * vh * 0.75})")
                            page.wait_for_timeout(450)
                        page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                        page.wait_for_timeout(2000)
                        page.evaluate("window.scrollTo(0,0)")
                        page.wait_for_timeout(800)
                        html = page.content()
                        ctx.close(); browser.close()
                        if len(html) < 4000:
                            raise ValueError("Page too short, probably blocked")
                        return html
                except Exception as e:
                    self.log_message(f"Browser attempt {attempt}/3: {str(e)[:80]}", "warn")
                    time.sleep(2.5)
            raise RuntimeError("Browser failed after 3 attempts")

        r = requests.get(url, headers={"User-Agent": self._HEADERS["User-Agent"]}, timeout=25)
        r.raise_for_status()
        return r.text

    #  Universal image URL extractor  reworked
    #
    #  Extraction pipeline (in order):
    #   1. JSON-LD / Next.js __NEXT_DATA__ / window.__DATA__ embedded JSON
    #   2. Site-specific selectors (fallback fast-path for known sites)
    #   3. Density scoring: rank ALL containers, pick best 3
    #   4. All <img> tags + every lazy-load attribute variant
    #   5. Raw regex sweep for image URLs in JS/JSON blobs
    #   6. Sequential pattern completion (fill gaps in numbered series)
    def extract_image_urls(self, html: str, base_url: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        candidates: set[str] = set()
        domain = urlparse(base_url).netloc.lower()
        self.log_message(f"Scraping: {domain}", "info")

        # 1. Embedded JSON (Next.js, Nuxt, custom loaders) 
        json_found = self._extract_from_embedded_json(html, base_url)
        if json_found:
            self.log_message(f"  JSON extraction: {len(json_found)} URLs", "info")
            candidates.update(json_found)

        # 2. Site-specific selectors (cheap, fast) 
        SELECTORS = [
            # generic readers
            "#readerarea img", ".reading-content img", ".read-container img",
            ".viewer-wrapper img", ".read-viewer img", ".chapter-content img",
            ".page-break img", "div.page-break img", ".wt_viewer img",
            # wordpress manga
            ".reading-content img.wp-manga-chapter-img",
            ".entry-content img.wp-manga-chapter-img",
            # webtoon / long-strip
            "section[aria-label*='Chapter'] img",
            "figure[data-index] img",
            "article.prose img",
            # specific platforms kept from original
            ".mantine-Stack-root img[alt*='Chapter']",
            ".m_6d731127 img",
            "img.manga-image", "img.lazy-load", "img.lazy-loaded",
            ".container img[alt*='Chapter']",
            ".container .flex img",
            "#chapter_area img", "#chapter_boxImages img", "#toon_img img",
            ".image_story img", ".imageChap img",
            ".img-responsive.image-chapter",
            "main#main-content img",
        ]
        for sel in SELECTORS:
            els = soup.select(sel)
            if els:
                for img in els:
                    for src in self._get_img_sources(img, base_url):
                        candidates.add(src)
                if els:
                    self.log_message(f"  Selector '{sel[:40]}' → {len(els)} imgs", "info")

        # 3. Density scoring  find the container most likely to be the reader
        if len(candidates) < 5:
            self.log_message("  Density scoring all containers…", "info")
            all_containers = soup.find_all(["div", "section", "article", "main"])
            scored = sorted(all_containers, key=_score_container, reverse=True)
            for container in scored[:3]:
                for img in container.find_all("img"):
                    for src in self._get_img_sources(img, base_url):
                        candidates.add(src)
            self.log_message(f"  After density pass: {len(candidates)} candidates", "info")

        # 4. All img tags  full attribute sweep 
        if len(candidates) < 5:
            self.log_message("  Full img sweep…", "info")
            for img in soup.find_all("img"):
                for src in self._get_img_sources(img, base_url):
                    candidates.add(src)

        # 5. Regex sweep over raw HTML/JS (catches JSON arrays, bg-image CSS, etc.)
        if len(candidates) < 5:
            self.log_message("  Regex sweep on raw HTML…", "warn")
            rx = re.findall(
                r'https?://[^\s"\'<>\)\(]+\.(?:jpe?g|png|webp|avif)(?:\?[^\s"\'<>\)\(]*)?',
                html, re.I
            )
            candidates.update(rx)
            # also grab srcset entries
            for m in re.finditer(r'srcset\s*=\s*["\']([^"\']+)', html, re.I):
                for part in m.group(1).split(','):
                    url_part = part.strip().split()[0]
                    full = self.normalize_url(url_part, base_url)
                    if full:
                        candidates.add(full)

        filtered = []
        seen: set[str] = set()
        for u in candidates:
            if u not in seen:
                seen.add(u)
                if self._is_valid_image_url(u, base_url):
                    filtered.append(u)

        filtered.sort(key=self._num_sort_key)

        completed = self._complete_sequential_patterns(filtered, base_url, soup)
        if len(completed) > len(filtered):
            self.log_message(f"  Pattern fill: +{len(completed)-len(filtered)} images", "ok")
            filtered = completed

        self.log_message(f"✓ {len(filtered)} images ready", "ok")
        return filtered

    def _extract_from_embedded_json(self, html: str, base_url: str) -> list[str]:
        found: list[str] = []

        # Next.js
        m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.S)
        if m:
            found.extend(self._walk_json_for_images(m.group(1), base_url))

        # window.__DATA__ / window.__STORE__ / window.chapterData etc.
        for m in re.finditer(r'window\.__\w+\s*=\s*(\{.*?\});', html, re.S):
            found.extend(self._walk_json_for_images(m.group(1), base_url))

        # JSON-LD
        for script in re.findall(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.S | re.I):
            found.extend(self._walk_json_for_images(script, base_url))

        # chapter image arrays: "images":["url1","url2"] or "pages":["url1"]
        for m in re.finditer(r'"(?:images?|pages?|chapter_images?|imgs?)"\s*:\s*(\[[^\]]{10,}\])', html, re.S | re.I):
            found.extend(self._walk_json_for_images(m.group(1), base_url))

        return found

    def _walk_json_for_images(self, text: str, base_url: str) -> list[str]:
        results: list[str] = []
        try:
            obj = json.loads(text)
        except Exception:
            # try to extract bare strings that look like image URLs
            for m in re.finditer(r'"(https?://[^"]+\.(?:jpe?g|png|webp|avif)[^"]*)"', text, re.I):
                u = self.normalize_url(m.group(1), base_url)
                if u:
                    results.append(u)
            return results

        stack = [obj]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                for v in node.values():
                    stack.append(v)
            elif isinstance(node, list):
                for v in node:
                    stack.append(v)
            elif isinstance(node, str):
                if re.search(r'\.(jpe?g|png|webp|avif)(\?|$)', node, re.I):
                    u = self.normalize_url(node, base_url)
                    if u:
                        results.append(u)
        return results

    @staticmethod
    def _num_sort_key(url: str) -> int:
        patterns = [
            r'ch[_-]?\d+[_-](\d+)', r'/(\d+)\.(?:jpe?g|png|webp)',
            r'page[_-]?(\d+)', r'c_\d+_(\d+)',
            r'(\d+)(?:-\d+)?\.(?:jpe?g|png|webp)',
        ]
        for pat in patterns:
            m = re.search(pat, url.lower())
            if m:
                return int(m.group(1))
        return 999999

    def _get_img_sources(self, img, base: str) -> list[str]:
        """Extract all possible src variants from an <img> tag."""
        attrs = [
            "src", "data-src", "data-lazy-src", "data-original",
            "data-lazy", "data-url", "data-image", "data-full-src",
            "data-srcset", "srcset", "data-hi-res-src",
        ]
        srcs: list[str] = []
        for a in attrs:
            v = img.get(a, "")
            if not v or not v.strip():
                continue
            skip = ('/1x1.', 'placeholder', 'loading', 'lazy.', 'data:image',
                    'blank.', 'transparent.')
            if any(p in v.lower() for p in skip):
                continue
            if a in ("srcset", "data-srcset"):
                # take highest-res entry (last in srcset, or the one with largest width descriptor)
                parts = [p.strip() for p in v.split(',') if p.strip()]
                # sort by width descriptor if present
                def _srcset_w(p):
                    m = re.search(r'(\d+)w', p)
                    return int(m.group(1)) if m else 0
                parts.sort(key=_srcset_w, reverse=True)
                v = parts[0].split()[0] if parts else ""
            full = self.normalize_url(v, base)
            if full and full.startswith(('http://', 'https://')):
                srcs.append(full)
        return srcs

    def normalize_url(self, src: str, base: str) -> str:
        src = (src or "").strip()
        if not src:             return ""
        if src.startswith("//"): return "https:" + src
        if not src.startswith("http"):
            return urljoin(base, src)
        return src

    def _is_valid_image_url(self, url: str, chapter_url: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        low = url.lower()
        if self.exclude_gifs_var.get() and low.endswith(".gif"):
            return False
        placeholders = ('/1x1.', 'placeholder', 'loading.', 'lazy.', 'blank.', 'transparent.')
        if any(p in low for p in placeholders):
            return False
        junk = [
            "logo", "banner", "icon", "avatar", "thumb", "cover.webp", "cover.jpg",
            "ad-", "advert", "emoji", "discord.webp", "facebook", "twitter",
            "instagram", "patreon", "kofi", "paypal", "donate", "sprite", "button",
            "commission", "reaction", "sticker", "emote", "smil",
        ]
        if self.aggressive_comments_var.get():
            junk += ["comment", "disqus", "reply", "fb_", "social", "share", "widget"]
        if any(k in low for k in junk):
            return False
        good = [
            ".jpg", ".jpeg", ".png", ".webp", ".avif",
            "cdn", "scans", "storage", "media", "image", "chapter",
            "manga", "manhwa", "manhua", "comic", "webtoon", "page",
        ]
        return any(x in low for x in good)

    #  Sequential gap fill  unchanged logic, just cleaned up
    def _complete_sequential_patterns(self, urls: list, base_url: str, soup) -> list:
        if len(urls) < 3:
            return urls
        m = re.search(r'^(.*?)(\d{2,4})(\.[\w]+)(?:\?.*)?$', urls[0])
        if not m:
            return urls
        base_part, ext_part = m.group(1), m.group(3)
        num_strs, nums = [], []
        for u in urls[:6]:
            mm = re.match(r'^' + re.escape(base_part) + r'(\d{2,4})' + re.escape(ext_part), u)
            if mm:
                num_strs.append(mm.group(1))
                nums.append(int(mm.group(1)))
            else:
                return urls  # pattern broken
        if len(nums) < 2:
            return urls
        expected = self._estimate_total_images(soup)
        max_num  = max(max(nums), expected)
        min_num  = min(nums)
        pad      = len(num_strs[0])
        existing = set(urls)
        result   = []
        for n in range(min_num, max_num + 1):
            candidate = f"{base_part}{n:0{pad}d}{ext_part}"
            if candidate in existing:
                result.append(next(u for u in urls if u == candidate))
            else:
                result.append(candidate)
        return result if len(result) > len(urls) else urls

    def _estimate_total_images(self, soup) -> int:
        if not soup:
            return 0
        divs = soup.select(".viewer-wrapper .page, .read-viewer .page, .chapter-content .page")
        if divs:
            self.log_message(f"  Detected {len(divs)} page containers", "info")
            return len(divs)
        prog = soup.select_one(".progress-line, .page-count, [class*='progress']")
        if prog:
            t = prog.get_text(strip=True)
            m = re.search(r'(\d+)\s*/\s*(\d+)', t)
            if m:
                total = int(m.group(2))
                self.log_message(f"  Progress indicator: {total} pages", "info")
                return total
        return 0

    #  Browser helpers
    def batch_download_with_browser(self, chapter_url: str, image_urls: list) -> dict:
        if not PLAYWRIGHT_AVAILABLE:
            return {}
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled", "--no-sandbox"]
                )
                ctx = browser.new_context(
                    user_agent=self._HEADERS["User-Agent"],
                    viewport={"width": 1920, "height": 1080},
                    ignore_https_errors=True
                )
                ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                page = ctx.new_page()
                page.goto(chapter_url, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(3000)
                ph = page.evaluate("document.body.scrollHeight")
                vh = page.evaluate("window.innerHeight")
                for i in range(max(20, ph // vh + 5)):
                    page.evaluate(f"window.scrollTo(0,{i * vh * 0.8})")
                    page.wait_for_timeout(350)
                page.evaluate("window.scrollTo(0,document.body.scrollHeight)")
                page.wait_for_timeout(2500)

                imgs_data = page.evaluate("""
                    async () => {
                        const out = {};
                        for (const img of document.querySelectorAll('img')) {
                            try {
                                const src = img.src || img.dataset.src;
                                if (!src || src.includes('data:image')) continue;
                                if (!img.complete) await new Promise(r=>{img.onload=r;img.onerror=r;setTimeout(r,2000)});
                                if (!img.naturalWidth) continue;
                                const c = document.createElement('canvas');
                                c.width = img.naturalWidth; c.height = img.naturalHeight;
                                c.getContext('2d').drawImage(img, 0, 0);
                                out[src] = c.toDataURL('image/png').split(',')[1];
                            } catch(e) {}
                        }
                        return out;
                    }
                """)
                ctx.close(); browser.close()
                result = {}
                for url, b64 in imgs_data.items():
                    try:
                        result[url] = base64.b64decode(b64)
                    except Exception:
                        pass
                return result
        except Exception as e:
            self.log_message(f"Batch browser capture failed: {str(e)[:100]}", "warn")
            return {}

    def download_image_with_browser(self, img_url: str, referer: str) -> bytes | None:
        if not PLAYWRIGHT_AVAILABLE:
            return None
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=[
                    "--disable-blink-features=AutomationControlled", "--no-sandbox",
                    "--disable-web-security"
                ])
                ctx = browser.new_context(
                    user_agent=self._HEADERS["User-Agent"],
                    ignore_https_errors=True
                )
                ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
                page = ctx.new_page()
                try:
                    page.goto(referer, wait_until="networkidle", timeout=30000)
                    page.wait_for_timeout(1500)
                except Exception:
                    pass
                resp = page.goto(img_url, wait_until="domcontentloaded", timeout=20000)
                content = resp.body() if resp and resp.ok else None
                ctx.close(); browser.close()
                return content
        except Exception as e:
            self.log_message(f"Browser single-image fetch failed: {str(e)[:80]}", "warn")
            return None

    #  Output path
    def get_output_directory(self, html: str, url: str, base_dir: str) -> Path:
        if self.is_naver_detail_url(url):
            title_id = self._query_value(url, "titleId")
            episode_no = self._query_value(url, "no")
            list_url = f"https://comic.naver.com/webtoon/list?titleId={title_id}" if title_id else url
            series_html = self._scrap_html_for(list_url)
            if series_html:
                series = self.parse_naver_series_page(series_html, list_url)
                episode = next((item for item in series["episodes"] if str(item["number"]) == str(episode_no)), None)
                comic = series["title"] or "Naver Webtoon"
                chapter = episode["title"] if episode else f"Episode {episode_no or 'Unknown'}"
                return Path(base_dir) / self._sanitize(comic) / self._sanitize(chapter)

        soup = BeautifulSoup(html, "html.parser")
        og   = soup.find("meta", property="og:title")
        title = (og["content"] if og else getattr(soup.title, "string", "") or "").strip()

        if " - " in title:
            parts   = [p.strip() for p in title.split(" - ")]
            comic   = parts[0]
            chapter = " - ".join(parts[1:])
        elif re.search(r'Chapter|Episode', title, re.I):
            sep     = "Chapter" if "Chapter" in title else "Episode"
            comic, chapter = title.split(sep, 1)
            comic   = comic.strip()
            chapter = sep + " " + chapter.strip()
        else:
            comic   = "Unknown Comic"
            chapter = title or "Chapter"

        comic   = re.sub(r'\s*(Manhwa|Manga|Manhua|Read|Online|Latest).*', '', comic, flags=re.I).strip() or "Comic"
        chapter = re.sub(r'(Chapter|Episode|Ch\.?|Ep\.?)\s*', 'Ch. ', chapter, flags=re.I).strip() or "Chapter"
        return Path(base_dir) / self._sanitize(comic) / self._sanitize(chapter)

    @staticmethod
    def _sanitize(s: str) -> str:
        s = re.sub(r'[<>:"/\\|?*]', '', s)
        return re.sub(r'\s+', ' ', s).strip()[:85] or "Unknown"

    #  Export generators
    def generate_cbz(self, output_dir: Path, image_paths: list) -> bool:
        try:
            cbz_path = output_dir / f"{output_dir.name}.cbz"
            with zipfile.ZipFile(cbz_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for img_path in sorted(image_paths):
                    if PIL_AVAILABLE and img_path.suffix.lower() not in ('.webp',):
                        # compress non-webp to webp for smallest CBZ
                        data = compress_to_webp_for_cbz(img_path)
                        if data:
                            zf.writestr(img_path.stem + ".webp", data)
                            continue
                    zf.write(img_path, img_path.name)
            self.log_message(f"✓ CBZ: {cbz_path.name}", "ok")
            return True
        except Exception as e:
            self.log_message(f"✗ CBZ failed: {e}", "error")
            return False

    def generate_pdf(self, output_dir: Path) -> bool:
        try:
            images = sorted(
                [p for p in output_dir.iterdir()
                 if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')],
                key=lambda x: int(re.search(r'(\d+)', x.stem).group(1))
                              if re.search(r'(\d+)', x.stem) else 0
            )
            if not images:
                raise ValueError("No images for PDF")
            imgs = [Image.open(p).convert("RGB") for p in images]
            pdf_path = output_dir / f"{output_dir.name}.pdf"
            imgs[0].save(pdf_path, "PDF", resolution=100.0, save_all=True, append_images=imgs[1:])
            self.log_message(f"✓ PDF: {pdf_path.name}", "ok")
            return True
        except Exception as e:
            self.log_message(f"✗ PDF failed: {e}", "error")
            return False

    def generate_epub(self, output_dir: Path) -> bool:
        try:
            book = epub.EpubBook()
            book.set_identifier(f"comic-{time.time()}")
            book.set_title(f"{output_dir.parent.name} - {output_dir.name}")
            book.add_author("Comic Downloader")
            book.set_language('en')
            images = sorted(
                [p for p in output_dir.iterdir()
                 if p.suffix.lower() in ('.jpg', '.jpeg', '.png', '.webp')],
                key=lambda x: int(re.search(r'(\d+)', x.stem).group(1))
                              if re.search(r'(\d+)', x.stem) else 0
            )
            chapters = []
            for i, img_path in enumerate(images, 1):
                mt = "image/webp" if img_path.suffix.lower() == '.webp' else \
                     "image/jpeg" if img_path.suffix.lower() in ('.jpg', '.jpeg') else "image/png"
                item = epub.EpubItem(uid=f"img{i}",
                                     file_name=f"images/page_{i:03d}{img_path.suffix}",
                                     media_type=mt, content=img_path.read_bytes())
                book.add_item(item)
                ch = epub.EpubHtml(title=f"Page {i}", file_name=f"page_{i:03d}.xhtml")
                ch.content = f'<div><img src="{item.file_name}" style="max-width:100%;height:auto;"/></div>'
                book.add_item(ch)
                chapters.append(ch)
            book.toc   = chapters
            book.spine = ['nav'] + chapters
            book.add_item(epub.EpubNcx())
            book.add_item(epub.EpubNav())
            ep = output_dir / f"{output_dir.name}.epub"
            epub.write_epub(str(ep), book)
            self.log_message(f"✓ EPUB: {ep.name}", "ok")
            return True
        except Exception as e:
            self.log_message(f"✗ EPUB failed: {e}", "error")
            return False

    def _finish(self):
        if getattr(self, "_series_mode", False):
            return
        self.running = False
        self.start_btn["state"] = self.test_btn["state"] = "normal"
        self.cancel_btn["state"] = "disabled"
        self.update_status("Ready")
        if self.total_images:
            self.progress_value.set(100)
            self.progress_label.set("100%")


class HeadlessComicDownloader(UniversalComicDownloader):
    def __init__(self, upload: bool = True, output_dir: str | None = None, use_browser: bool | None = None):
        self.root = None
        self.url_var = PlainVar("")
        self.output_var = PlainVar(output_dir or str(Path.cwd()))
        self.use_browser_var = PlainVar(PLAYWRIGHT_AVAILABLE if use_browser is None else use_browser)
        self.exclude_gifs_var = PlainVar(True)
        self.skip_tiny_var = PlainVar(True)
        self.aggressive_comments_var = PlainVar(True)
        self.compress_webp_var = PlainVar(True)
        self.generate_cbz_var = PlainVar(True)
        self.generate_pdf_var = PlainVar(False)
        self.generate_epub_var = PlainVar(False)
        self.upload_supabase_var = PlainVar(upload)
        self.supabase_bucket_var = PlainVar(os.getenv("SUPABASE_BUCKET", "comics"))
        self.supabase_prefix_var = PlainVar(os.getenv("SUPABASE_PREFIX", "comics"))
        self.running = True
        self.total_images = 0
        self.current_status = PlainVar("Ready")
        self.progress_value = PlainVar(0)
        self.progress_label = PlainVar("0%")
        self.images_found = PlainVar("Images found: 0")
        self.images_downloaded = PlainVar("Downloaded: 0/0")

    def log_message(self, msg: str, tag: str = "info"):
        encoding = os.sys.stdout.encoding or "utf-8"
        print(str(msg).encode(encoding, errors="replace").decode(encoding, errors="replace"), flush=True)

    def update_status(self, text: str):
        self.current_status.set(text)
        encoding = os.sys.stdout.encoding or "utf-8"
        print(f"Status: {text}".encode(encoding, errors="replace").decode(encoding, errors="replace"), flush=True)

    def _finish(self):
        if getattr(self, "_series_mode", False):
            return
        self.running = False
        if self.total_images:
            self.progress_value.set(100)
            self.progress_label.set("100%")


def run_cli():
    parser = argparse.ArgumentParser(description="Download comics and optionally upload to Supabase.")
    parser.add_argument("--url", default="https://comic.naver.com/webtoon/list?titleId=842699")
    parser.add_argument("--output", default=str(Path.cwd()))
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    downloader = HeadlessComicDownloader(
        upload=not args.no_upload,
        output_dir=args.output,
        use_browser=False if args.no_browser else None,
    )
    if downloader.is_naver_series_url(args.url):
        downloader.download_series_task(args.url, args.output)
    else:
        downloader.download_task(args.url, args.output)


if __name__ == "__main__":
    if len(os.sys.argv) > 1:
        run_cli()
    else:
        root = tk.Tk()
        app  = UniversalComicDownloader(root)
        root.mainloop()
