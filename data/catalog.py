import os
import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import quote

import requests

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def slugify(value):
    value = re.sub(r"[^a-zA-Z0-9]+", "-", value or "").strip("-").lower()
    return value or "comic"


def load_comics():
    rows = _fetch_supabase_chapters()
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("comic_title") or "Untitled"].append(row)

    comics = []
    for title, chapters in grouped.items():
        chapters = sorted(chapters, key=_chapter_sort_key, reverse=True)
        comic_slug = slugify(title)
        first_pages = chapters[0].get("pages") or []
        cover = first_pages[0].get("public_url") if first_pages else "/assets/covers/iron-lotus.svg"
        latest_created = chapters[0].get("created_at") or ""
        comics.append(
            {
                "title": title,
                "slug": comic_slug,
                "format": "manhwa",
                "origin": "Korean",
                "status": "Ongoing",
                "rating": 0,
                "updated_at": latest_created[:10] or datetime.utcnow().date().isoformat(),
                "cover": cover,
                "genres": ["MTL", "Naver"],
                "translator": "Uploaded MTL",
                "summary": "Uploaded chapter metadata from Supabase.",
                "chapters": [_chapter_from_row(row) for row in chapters],
            }
        )

    return sorted(comics, key=lambda comic: comic["updated_at"], reverse=True)


def get_comic(slug):
    return next((comic for comic in load_comics() if comic["slug"] == slug), None)


def _fetch_supabase_chapters():
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    api_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_ANON_KEY", "")
    table = os.getenv("SUPABASE_CHAPTERS_TABLE", "comic_chapters")

    if not supabase_url or not api_key or not table:
        return []

    try:
        response = requests.get(
            f"{supabase_url}/rest/v1/{table}",
            params={
                "select": "id,comic_title,chapter_title,source_url,storage_bucket,storage_prefix,manifest_path,page_count,pages,created_at",
                "order": "created_at.desc",
            },
            headers={
                "apikey": api_key,
                "Authorization": f"Bearer {api_key}",
            },
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return []


def _chapter_from_row(row):
    number = _chapter_number(row.get("chapter_title") or "")
    pages = row.get("pages") or []
    return {
        "number": number,
        "title": row.get("chapter_title") or f"Chapter {number}",
        "date": (row.get("created_at") or "")[:10],
        "quality": "Supabase",
        "source_url": row.get("source_url") or "",
        "pages": pages,
        "manifest_path": row.get("manifest_path") or "",
        "storage_prefix": row.get("storage_prefix") or "",
    }


def _chapter_number(value):
    match = re.search(r"(\d+)", value or "")
    return int(match.group(1)) if match else 0


def _chapter_sort_key(row):
    return (_chapter_number(row.get("chapter_title") or ""), row.get("created_at") or "")


def storage_public_url(storage_path):
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    bucket = os.getenv("SUPABASE_BUCKET", "comics")
    return f"{supabase_url}/storage/v1/object/public/{bucket}/{quote(storage_path, safe='/')}"
