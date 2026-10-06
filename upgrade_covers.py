#!/usr/bin/env python3
"""
Upgrade cover sources: prefer clean Amazon CDN / Google Books publisher art.
Reject Open Library library-scan covers when a better source exists.
Also assign local game branding for Asher Dad / video posts.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode

from book_meta import (
    amazon_dp_url,
    amazon_search_url,
    author_match,
    build_meta_cache,
    title_similarity,
)
from fill_missing_covers import (
    clean_author,
    extract_asin,
    isbn13_to_isbn10,
    is_non_book,
    migrate_entry,
    resolve_title_author,
    try_amazon_asin_cover,
    try_cover_by_isbn,
    url_ok,
    google_books_search,
    open_library_search,
    verify_amazon_asin,
    DELAY,
    GB_DELAY,
)

COVERS_FILE = Path("covers.json")
REVIEWS_FILE = Path("reviews.json")
UA = "BookGateway/3.3 (upgrade-covers)"

# Local game branding (repo-relative URLs for GitHub Pages)
GAME_COVERS = {
    "asher-dad-plays-retro-bowl": "/assets/games/retro-bowl.png",
    "asher-dad-plays-elder-scrolls-online": "/assets/games/eso.png",
    "asher-dad-opens-eso-dwarven-crates": "/assets/games/eso.png",
    "asher-dad-reviews-football-gm": "/assets/games/football-gm.png",
    "asher-dad-unboxes-amazon-luna": "/assets/games/amazon-luna.png",
    "asher-dad-plays-solitaire-grand-harvest": "/assets/games/solitaire-grand-harvest.jpg",
    "asher-dad-plays-hey-mr-president": "/assets/games/hey-mr-president.jpg",
    "asher-dad-reviews-cbs-franchise-hockey": "/assets/games/cbs-franchise-hockey.jpg",
    "asher-dad-reviews-canary-security-system": "/assets/games/canary.jpg",
    "asher-dad-watches-avengers-endgame": "/assets/games/avengers-endgame.jpg",
}

# Force-fix known trash OL covers (library photos / wrong series / audiobook cases)
FORCE_REPLACE = {
    "it-can-t-happen-here-by-lewis": {
        "prefer_isbn10": "1720337446",  # Amazon CDN works
    },
    "dead-six-by-correia-kupari": {
        "prefer_isbn10": "1476781850",
    },
    "pilot-x-by-merritt": {
        "prefer_isbn10": "1942645317",
    },
    "everybody-always-by-goff": {
        "prefer_isbn10": "0718078136",  # print edition; 0718078179 is dead CDN / audio
    },
}


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except HTTPError as e:
            if e.code == 429:
                time.sleep(30 * (2 ** attempt))
            else:
                return None
        except Exception:
            time.sleep(2)
    return None


def amazon_cdn(isbn10: str) -> str | None:
    """Fast single-GET check of Amazon ISBN cover CDN."""
    if not isbn10 or len(isbn10) != 10:
        return None
    url = f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.LZZZZZZZ.jpg"
    try:
        req = Request(url, headers={"User-Agent": UA})
        with urlopen(req, timeout=10) as r:
            data = r.read()
            ctype = (r.headers.get("Content-Type") or "").lower()
            if len(data) >= 2500 and ("jpeg" in ctype or "jpg" in ctype or "image/" in ctype):
                return url
    except Exception:
        pass
    return None

    for pattern in (
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.LZZZZZZZ.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01._SCLZZZZZZZ_.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.MAIN._SCRM_.jpg",
    ):
        if url_ok(pattern, min_bytes=2500):
            return pattern
        time.sleep(0.1)
    return None


def is_ol_cover(url: str | None) -> bool:
    return bool(url and "openlibrary.org" in url)


def is_youtube_thumb(url: str | None) -> bool:
    return bool(url and "img.youtube.com" in url)


def save(covers):
    COVERS_FILE.write_text(
        json.dumps(covers, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def assign_game_covers(covers: dict) -> int:
    n = 0
    for slug, path in GAME_COVERS.items():
        entry = migrate_entry(covers.get(slug) or {})
        entry["cover"] = path
        entry["non_book"] = True
        entry["cover_source"] = "game_branding"
        # drop youtube thumb fields as primary
        covers[slug] = entry
        n += 1
        print(f"  game art → {slug}: {path}")
    return n


def force_fix_known_bad(covers, meta, reviews_by_slug) -> int:
    n = 0
    for slug, spec in FORCE_REPLACE.items():
        entry = migrate_entry(covers.get(slug) or {})
        m = meta.get(slug) or {}
        review = reviews_by_slug.get(slug) or {"title": "", "slug": slug}
        title, author, publisher = resolve_title_author(m, review)
        isbn10 = spec.get("prefer_isbn10") or entry.get("isbn10")
        cover = amazon_cdn(isbn10) if isbn10 else None
        if not cover:
            # try GB
            c, i13, i10 = google_books_search(title, author, publisher or "")
            if c:
                cover = c
            if i10:
                isbn10 = i10
                am = amazon_cdn(i10)
                if am:
                    cover = am
        if not cover:
            print(f"  ! still bad (no replacement): {slug}")
            continue
        entry["cover"] = cover
        entry["cover_source"] = "amazon_cdn" if "amazon" in cover else "google_books"
        if isbn10 and re.fullmatch(r"[0-9X]{10}", isbn10, re.I):
            entry["isbn10"] = isbn10
            entry["amazon"] = amazon_dp_url(isbn10)
        covers[slug] = entry
        n += 1
        print(f"  fixed {slug} → {cover[:70]}")
    return n


def upgrade_ol_to_amazon(covers, meta, reviews) -> tuple[int, int]:
    """Replace OL covers with Amazon CDN when isbn10 present and CDN works."""
    replaced = skipped = 0
    candidates = []
    for r in reviews:
        slug = r["slug"]
        entry = covers.get(slug) or {}
        if entry.get("non_book") or is_non_book(slug, r.get("categories", [])):
            continue
        if not is_ol_cover(entry.get("cover")):
            continue
        isbn10 = entry.get("isbn10")
        if not isbn10 or not re.fullmatch(r"[0-9X]{10}", str(isbn10), re.I):
            # try extract from amazon dp if numeric
            asin = extract_asin(entry)
            if asin and re.fullmatch(r"[0-9X]{10}", asin, re.I):
                isbn10 = asin
            else:
                skipped += 1
                continue
        candidates.append((slug, isbn10))

    print(f"OL→Amazon candidates: {len(candidates)}")
    for i, (slug, isbn10) in enumerate(candidates, 1):
        cover = amazon_cdn(isbn10)
        if cover:
            entry = migrate_entry(covers[slug])
            entry["cover"] = cover
            entry["cover_source"] = "amazon_cdn"
            entry["isbn10"] = isbn10
            entry["amazon"] = amazon_dp_url(isbn10)
            covers[slug] = entry
            replaced += 1
            if replaced <= 15 or replaced % 25 == 0:
                print(f"  [{i}/{len(candidates)}] replaced {slug}")
        else:
            skipped += 1
        if i % 40 == 0:
            save(covers)
            print(f"  … checkpoint replaced={replaced}")
        time.sleep(0.05)
    return replaced, skipped


def fill_missing_amazon_first(covers, meta, reviews) -> tuple[int, int]:
    """Fill missing covers: Amazon CDN (via OL/GB ISBN metadata) then GB image; never OL image."""
    filled = missed = 0
    candidates = []
    for r in reviews:
        slug = r["slug"]
        entry = covers.get(slug) or {}
        if entry.get("non_book") or is_non_book(slug, r.get("categories", [])):
            continue
        if entry.get("cover"):
            continue
        candidates.append(r)

    print(f"Missing to fill (amazon-first): {len(candidates)}")
    for i, review in enumerate(candidates, 1):
        slug = review["slug"]
        entry = migrate_entry(covers.get(slug) or {})
        m = meta.get(slug) or {}
        title, author, publisher = resolve_title_author(m, review)
        if not title or not author:
            missed += 1
            print(f"  [{i}/{len(candidates)}] skip no author {slug}")
            continue

        cover = None
        isbn13 = entry.get("isbn13")
        isbn10 = entry.get("isbn10")
        source = None

        # Get verified ISBN from OL search (metadata only — ignore OL cover)
        ol_cover, i13, i10 = open_library_search(title, author, publisher or "")
        # deliberately ignore ol_cover
        if i10 and not isbn10:
            isbn10 = i10
        if i13 and not isbn13:
            isbn13 = i13

        if isbn10:
            cover = amazon_cdn(isbn10)
            if cover:
                source = "amazon_cdn"

        if not cover:
            gb_cover, gi13, gi10 = google_books_search(title, author, publisher or "")
            if gi10 and not isbn10:
                isbn10 = gi10
            if gi13 and not isbn13:
                isbn13 = gi13
            if isbn10 and not cover:
                cover = amazon_cdn(isbn10)
                if cover:
                    source = "amazon_cdn"
            if not cover and gb_cover:
                cover = gb_cover
                source = "google_books"

        if not cover:
            asin = extract_asin(entry)
            if asin and verify_amazon_asin(asin, title, author):
                cover = try_amazon_asin_cover(asin)
                if cover:
                    source = "amazon_asin"
                    entry["amazon"] = amazon_dp_url(asin)

        if cover:
            entry["cover"] = cover
            entry["cover_source"] = source
            if isbn10 and re.fullmatch(r"[0-9X]{10}", str(isbn10), re.I):
                entry["isbn10"] = isbn10
                entry["amazon"] = amazon_dp_url(isbn10)
            elif isbn13:
                entry["isbn13"] = isbn13
            if not entry.get("amazon"):
                entry["amazon"] = amazon_search_url(title, author)
            if isbn13:
                entry["isbn13"] = isbn13
            covers[slug] = entry
            filled += 1
            print(f"  [{i}/{len(candidates)}] ✓ [{source}] {title[:45]} / {author[:25]}")
        else:
            if isbn10 and re.fullmatch(r"[0-9X]{10}", str(isbn10), re.I):
                entry["isbn10"] = isbn10
                entry["amazon"] = amazon_dp_url(isbn10)
            elif isbn13:
                entry["isbn13"] = isbn13
                entry["amazon"] = entry.get("amazon") or amazon_search_url(title, author)
            else:
                entry["amazon"] = entry.get("amazon") or amazon_search_url(title, author)
            covers[slug] = entry
            missed += 1
            print(f"  [{i}/{len(candidates)}] ✗ {title[:45]} / {author[:25]}")

        if i % 15 == 0:
            save(covers)
            print(f"  → checkpoint filled={filled} missed={missed}")

    return filled, missed


def main():
    reviews = json.loads(REVIEWS_FILE.read_text(encoding="utf-8"))
    covers = json.loads(COVERS_FILE.read_text(encoding="utf-8"))
    reviews_by_slug = {r["slug"]: r for r in reviews}
    print("Loading book meta…")
    meta = build_meta_cache(reviews, force=False)

    for slug, entry in list(covers.items()):
        covers[slug] = migrate_entry(entry or {})

    before_missing = sum(
        1
        for r in reviews
        if not (covers.get(r["slug"]) or {}).get("non_book")
        and not is_non_book(r["slug"], r.get("categories", []))
        and not (covers.get(r["slug"]) or {}).get("cover")
    )
    before_ol = sum(
        1 for v in covers.values() if v and is_ol_cover(v.get("cover"))
    )
    print(f"Before: missing={before_missing} ol_covers={before_ol}")

    print("\n1) Game branding for Asher Dad / video posts")
    games = assign_game_covers(covers)
    save(covers)

    print("\n2) Force-fix known trash covers")
    forced = force_fix_known_bad(covers, meta, reviews_by_slug)
    save(covers)

    print("\n3) Upgrade OL covers → Amazon CDN where ISBN works")
    replaced, skipped = upgrade_ol_to_amazon(covers, meta, reviews)
    save(covers)

    print("\n4) Fill remaining missing (Amazon-first, no OL images)")
    filled, missed = fill_missing_amazon_first(covers, meta, reviews)
    save(covers)

    after_missing = sum(
        1
        for r in reviews
        if not (covers.get(r["slug"]) or {}).get("non_book")
        and not is_non_book(r["slug"], r.get("categories", []))
        and not (covers.get(r["slug"]) or {}).get("cover")
    )
    after_ol = sum(1 for v in covers.values() if v and is_ol_cover(v.get("cover")))
    after_am = sum(
        1
        for v in covers.values()
        if v and v.get("cover") and "amazon" in (v.get("cover") or "")
    )
    after_local = sum(
        1
        for v in covers.values()
        if v and v.get("cover") and str(v.get("cover")).startswith("/assets/")
    )

    print("\n=== Summary ===")
    print(f"  game arts assigned: {games}")
    print(f"  known-bad forced:   {forced}")
    print(f"  OL→Amazon replaced: {replaced} (skipped no CDN: {skipped})")
    print(f"  newly filled:       {filled} (still missed this pass: {missed})")
    print(f"  missing books:      {before_missing} → {after_missing}")
    print(f"  OL covers left:     {before_ol} → {after_ol}")
    print(f"  Amazon CDN covers:  {after_am}")
    print(f"  local game assets:  {after_local}")


if __name__ == "__main__":
    main()
