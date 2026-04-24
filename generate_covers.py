#!/usr/bin/env python3
"""
BookGateway cover generator.
Reads reviews.json, queries Google Books + Open Library APIs,
and produces covers.json: { slug: { cover, isbn, amazon } }

Run from repo root. Requires no external packages (stdlib only).
Rate-limited to be polite to free APIs.
"""

import json, time, re, sys
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote
from urllib.error import URLError

REVIEWS_FILE   = "reviews.json"
COVERS_FILE    = "covers.json"
AFFILIATE_TAG  = "bookgateway02-20"
DELAY_SEC      = 0.4   # seconds between requests (~2.5/sec — well within limits)

# Categories / slug patterns that are NOT books
NON_BOOK_SLUGS = re.compile(
    r"asher-dad|football-gm|retro-bowl|amazon-luna|cbs-franchise|hey-mr-president|"
    r"solitaire-grand|booky-award|showcase|2011-booky|2012-booky|2013-book|2014-book|"
    r"2016-booky|4159|4570|4764|6606|6612|6617",
    re.I
)
NON_BOOK_CATS = re.compile(r"video.games|board.games|tech|movies|interviews|giveaway|"
                            r"asher.boys|arieltopia|matthew.scott|reviewers", re.I)

def is_non_book(slug, categories):
    if NON_BOOK_SLUGS.search(slug):
        return True
    return any(NON_BOOK_CATS.search(c) for c in categories)

def clean_query(title):
    """Turn 'The Great Gatsby by Fitzgerald' into 'The Great Gatsby Fitzgerald'."""
    q = re.sub(r'\s+by\s+', ' ', title, flags=re.I).strip()
    q = re.sub(r'[^\w\s]', ' ', q)
    q = re.sub(r'\s+', ' ', q).strip()
    return q

def fetch_json(url, timeout=8):
    try:
        req = Request(url, headers={"User-Agent": "BookGateway/1.0"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def query_google_books(query):
    """Return (cover_url, isbn13) or (None, None)."""
    params = urlencode({"q": query, "maxResults": "3", "fields": "items(volumeInfo(imageLinks,industryIdentifiers))"})
    url = f"https://www.googleapis.com/books/v1/volumes?{params}"
    data = fetch_json(url)
    if not data or "items" not in data:
        return None, None
    for item in data["items"]:
        vi = item.get("volumeInfo", {})
        il = vi.get("imageLinks", {})
        src = il.get("thumbnail") or il.get("smallThumbnail")
        isbn = None
        for ident in vi.get("industryIdentifiers", []):
            if ident.get("type") == "ISBN_13":
                isbn = ident["identifier"]
                break
        if src:
            src = src.replace("http://", "https://").replace("zoom=1", "zoom=2")
            return src, isbn
    return None, None

def query_open_library(title, author=""):
    """Return cover_url or None."""
    params = {"title": title, "limit": "1", "fields": "cover_i"}
    if author:
        params["author"] = author
    url = "https://openlibrary.org/search.json?" + urlencode(params)
    data = fetch_json(url)
    if not data:
        return None
    docs = data.get("docs", [])
    if docs and docs[0].get("cover_i"):
        return f"https://covers.openlibrary.org/b/id/{docs[0]['cover_i']}-M.jpg"
    return None

def amazon_url(isbn=None, title=""):
    tag = AFFILIATE_TAG
    if isbn:
        return f"https://www.amazon.com/s?k={quote(isbn)}&tag={tag}"
    # Better search: just title words, no "by"
    q = re.sub(r'\s+by\s+.*', '', title, flags=re.I).strip()
    return f"https://www.amazon.com/s?k={quote(q)}&tag={tag}"

def main():
    with open(REVIEWS_FILE, encoding="utf-8") as f:
        reviews = json.load(f)

    # Load existing covers to skip already-done
    try:
        with open(COVERS_FILE, encoding="utf-8") as f:
            covers = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        covers = {}

    total = len(reviews)
    updated = 0
    skipped_non_book = 0
    failed = 0

    for i, review in enumerate(reviews):
        slug = review["slug"]
        title = review.get("title", "")
        cats = review.get("categories", [])

        # Skip if already resolved
        if slug in covers and covers[slug].get("cover"):
            continue

        # Mark non-books immediately
        if is_non_book(slug, cats):
            covers[slug] = {"cover": None, "isbn": None, "amazon": None, "non_book": True}
            skipped_non_book += 1
            continue

        if not title:
            covers[slug] = {"cover": None, "isbn": None, "amazon": None}
            failed += 1
            continue

        # Parse title / author
        m = re.match(r'^(.+?)\s+by\s+(.+)$', title, re.I)
        book_title = m.group(1).strip() if m else title
        author     = m.group(2).strip() if m else ""

        # Try Google Books first
        query = clean_query(title)
        cover, isbn = query_google_books(query)
        time.sleep(DELAY_SEC)

        # Fallback: try just the book title (no author)
        if not cover and book_title != query:
            cover, isbn = query_google_books(book_title)
            time.sleep(DELAY_SEC)

        # Fallback: Open Library
        if not cover:
            cover = query_open_library(book_title, author)
            time.sleep(DELAY_SEC)

        amz = amazon_url(isbn, title)

        covers[slug] = {
            "cover": cover,
            "isbn":  isbn,
            "amazon": amz,
        }

        status = "✓" if cover else "✗"
        print(f"[{i+1}/{total}] {status} {title[:50]}")

        if cover:
            updated += 1
        else:
            failed += 1

        # Save incrementally every 50 books
        if (i + 1) % 50 == 0:
            with open(COVERS_FILE, "w", encoding="utf-8") as f:
                json.dump(covers, f, indent=2, ensure_ascii=False)
            print(f"  → Saved progress ({updated} covers, {failed} failed, {skipped_non_book} non-books)")

    # Final save
    with open(COVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(covers, f, indent=2, ensure_ascii=False)

    print(f"\nDone. {updated} covers found, {failed} failed, {skipped_non_book} non-books marked.")
    print(f"covers.json saved.")

if __name__ == "__main__":
    main()
