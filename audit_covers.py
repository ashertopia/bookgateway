#!/usr/bin/env python3
"""
Audit covers.json against HTML-parsed bibliographic metadata + Open Library ISBN data.
Clear mismatched cover/isbn fields; reset amazon to search URL.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

from book_meta import (
    AFFILIATE_TAG,
    amazon_search_url,
    author_match,
    build_meta_cache,
    normalize,
    publisher_match,
    title_similarity,
)

COVERS_FILE = Path("covers.json")
REVIEWS_FILE = Path("reviews.json")
POSTS_DIR = Path("posts")
DELAY = 0.35
BATCH = 15
UA = "BookGateway/3.1 (audit-covers)"


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                wait = 25 * (2 ** attempt)
                print(f"  rate limited, sleep {wait}s")
                time.sleep(wait)
            else:
                print(f"  HTTP {e.code}")
                return None
        except Exception as e:
            print(f"  fetch err: {e}")
            time.sleep(2)
    return None


def ol_books_by_isbns(isbns: list[str]) -> dict:
    """Batch ISBN → OL data. Returns {isbn: data}."""
    if not isbns:
        return {}
    keys = ",".join(f"ISBN:{i}" for i in isbns)
    url = (
        "https://openlibrary.org/api/books?"
        f"bibkeys={keys}&jscmd=data&format=json"
    )
    data = fetch_json(url) or {}
    out = {}
    for isbn in isbns:
        key = f"ISBN:{isbn}"
        if key in data:
            out[isbn] = data[key]
    return out


def ol_authors(book: dict) -> list[str]:
    return [a.get("name", "") for a in (book.get("authors") or []) if a.get("name")]


def ol_publishers(book: dict) -> list[str]:
    return [p.get("name", "") for p in (book.get("publishers") or []) if p.get("name")]


def entry_isbn(entry: dict) -> str | None:
    for k in ("isbn10", "isbn13"):
        v = entry.get(k)
        if v:
            return re.sub(r"[^0-9Xx]", "", str(v))
    # try extract from amazon dp
    am = entry.get("amazon") or ""
    m = re.search(r"/dp/([A-Z0-9]{10})", am, re.I)
    if m:
        return m.group(1)
    return None


def clear_entry(entry: dict, title: str = "", author: str = "") -> dict:
    entry["cover"] = None
    entry["isbn10"] = None
    entry["isbn13"] = None
    entry["amazon"] = amazon_search_url(title, author)
    entry.pop("non_book", None)  # keep if was set — don't clear non_book here
    return entry


def update_post_amazon(slug: str, new_url: str) -> bool:
    path = POSTS_DIR / f"{slug}.html"
    if not path.exists() or not new_url:
        return False
    html = path.read_text(encoding="utf-8", errors="ignore")
    orig = html
    html = re.sub(
        r'https://www\.amazon\.com/(?:s\?k=[^"&\s]*|dp/[A-Z0-9]+/?\?)[^"]*tag=bookgateway02-20',
        new_url,
        html,
    )
    # also catch dp without trailing ?
    html = re.sub(
        r'https://www\.amazon\.com/dp/[A-Z0-9]{10}/?(?:\?[^"]*)?',
        new_url,
        html,
    )
    # ensure tag present
    if "tag=bookgateway02-20" not in new_url:
        return False
    if html != orig:
        path.write_text(html, encoding="utf-8")
        return True
    return False


def main():
    reviews = json.loads(REVIEWS_FILE.read_text(encoding="utf-8"))
    covers = json.loads(COVERS_FILE.read_text(encoding="utf-8"))
    review_by_slug = {r["slug"]: r for r in reviews}

    print("Building book_meta cache…")
    meta = build_meta_cache(reviews, force=True)

    # Collect candidates: has cover or isbn, not non_book
    candidates = []
    for slug, entry in covers.items():
        if not entry or entry.get("non_book"):
            continue
        if entry.get("cover") or entry.get("isbn10") or entry.get("isbn13"):
            candidates.append(slug)

    print(f"Auditing {len(candidates)} entries with cover/isbn…")

    audited = cleared = kept = skipped = no_ol = no_meta = 0
    cleared_slugs = []

    # Process in batches for ISBN lookups
    for i in range(0, len(candidates), BATCH):
        batch = candidates[i : i + BATCH]
        isbn_map = {}  # isbn -> [slugs]
        slug_isbn = {}
        for slug in batch:
            entry = covers[slug]
            isbn = entry_isbn(entry)
            if not isbn:
                skipped += 1
                continue
            slug_isbn[slug] = isbn
            isbn_map.setdefault(isbn, []).append(slug)

        ol = ol_books_by_isbns(list(isbn_map.keys()))
        time.sleep(DELAY)

        for slug, isbn in slug_isbn.items():
            audited += 1
            entry = covers[slug]
            m = meta.get(slug) or {}
            our_title = m.get("title") or ""
            our_author = m.get("author") or ""
            our_pub = m.get("publisher") or ""

            if not our_author and not our_title:
                no_meta += 1
                # can't verify — keep
                kept += 1
                continue

            book = ol.get(isbn)
            if not book:
                no_ol += 1
                # No OL data for this ISBN — keep (might still be valid)
                kept += 1
                continue

            ol_title = book.get("title") or ""
            ol_auth = ol_authors(book)
            ol_pubs = ol_publishers(book)

            t_ok = title_similarity(our_title, ol_title) >= 0.55 if our_title else True
            a_ok = author_match(our_author, ol_auth) if our_author else False

            # Prefer publisher match as soft signal; don't require if OL lacks it
            p_ok = True
            if our_pub and ol_pubs:
                # soft: if both have publishers and they clash AND title is marginal, clearer mismatch
                if not any(publisher_match(our_pub, p) for p in ol_pubs):
                    p_ok = False

            mismatch = False
            if our_author and not a_ok:
                mismatch = True
            elif our_title and not t_ok:
                mismatch = True
            elif our_author and a_ok and our_title and not t_ok:
                mismatch = True
            # If author matches and title matches, keep even if publisher differs
            if a_ok and t_ok:
                mismatch = False
            # Author match + weak title but publisher matches → keep
            if a_ok and not t_ok and our_pub and ol_pubs and any(
                publisher_match(our_pub, p) for p in ol_pubs
            ):
                # still suspicious if titles wildly different
                if title_similarity(our_title, ol_title) < 0.35:
                    mismatch = True
                else:
                    mismatch = False

            if mismatch:
                print(
                    f"CLEAR {slug}: ours={our_title!r}/{our_author!r} "
                    f"ol={ol_title!r}/{ol_auth} isbn={isbn}"
                )
                covers[slug] = clear_entry(entry, our_title, our_author)
                # preserve non_book if somehow set
                update_post_amazon(slug, covers[slug]["amazon"])
                cleared += 1
                cleared_slugs.append(slug)
            else:
                kept += 1

        if (i // BATCH) % 10 == 0 and i:
            print(f"  … {i}/{len(candidates)} audited={audited} cleared={cleared}")

    COVERS_FILE.write_text(
        json.dumps(covers, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("\nAudit summary")
    print(f"  candidates: {len(candidates)}")
    print(f"  audited:    {audited}")
    print(f"  cleared:    {cleared}")
    print(f"  kept:       {kept}")
    print(f"  skipped (no isbn): {skipped}")
    print(f"  no OL data: {no_ol}")
    print(f"  thin meta:  {no_meta}")
    print(f"  cleared slugs (first 40): {cleared_slugs[:40]}")
    Path("audit_cleared.json").write_text(
        json.dumps(cleared_slugs, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
