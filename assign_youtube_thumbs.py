#!/usr/bin/env python3
"""Assign YouTube thumbnail URLs to covers.json for posts with embeds."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.request import Request, urlopen

from book_meta import extract_youtube_id

COVERS = Path("covers.json")
POSTS = Path("posts")
UA = "BookGateway/3.1"


def thumb_url(vid: str) -> str:
    # Prefer hqdefault (always exists). maxresdefault often 404 for older videos.
    return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg"


def main():
    covers = json.loads(COVERS.read_text(encoding="utf-8"))
    assigned = 0
    examples = []
    for path in sorted(POSTS.glob("*.html")):
        slug = path.stem
        if slug == "test":
            continue
        html = path.read_text(encoding="utf-8", errors="ignore")
        vid = extract_youtube_id(html)
        if not vid:
            continue
        entry = covers.get(slug) or {}
        url = thumb_url(vid)
        entry["youtube_id"] = vid
        # Do not overwrite Amazon CDN / Google Books / local branding covers
        existing = entry.get("cover") or ""
        is_book = bool(entry.get("isbn10") or entry.get("isbn13")) and not entry.get("non_book")
        prefer_keep = (
            is_book
            or existing.startswith("/assets/")
            or "images-amazon.com" in existing
            or "books.google" in existing
            or entry.get("cover_source") in ("game_branding", "amazon_cdn", "amazon_asin", "gb", "publisher_site")
        )
        if not existing or not prefer_keep:
            entry["cover"] = url
        # Keep/mark non_book for video posts (never force a real book to non_book)
        if is_book:
            entry["non_book"] = False
        elif entry.get("non_book") is not False:
            if not entry.get("isbn10") and not entry.get("isbn13"):
                entry["non_book"] = True
        covers[slug] = entry
        assigned += 1
        if slug == "asher-dad-plays-hey-mr-president" or assigned <= 5:
            examples.append((slug, vid, url))

    COVERS.write_text(json.dumps(covers, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Assigned YouTube thumbs: {assigned}")
    for s, v, u in examples:
        print(f"  {s}: {v} -> {u}")
    e = covers.get("asher-dad-plays-hey-mr-president")
    print("asher-dad-plays-hey-mr-president:", e)


if __name__ == "__main__":
    main()
