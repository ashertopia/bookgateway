#!/usr/bin/env python3
"""
BookGateway cover generator v2.
Improvements over v1:
- Extracts ISBN_10 and ISBN_13 from Google Books
- Extracts ISBNs from Open Library search results
- Converts ISBN-13 to ISBN-10 for direct Amazon /dp/ links
- Better search strategies for hard-to-find books
- Re-processes entries missing ISBNs even if cover exists
"""

import json, time, re, sys
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote
from urllib.error import URLError

REVIEWS_FILE   = "reviews.json"
COVERS_FILE    = "covers.json"
AFFILIATE_TAG  = "bookgateway02-20"
DELAY_SEC      = 0.4

NON_BOOK_SLUGS = re.compile(
    r"asher-dad|football-gm|retro-bowl|amazon-luna|cbs-franchise|hey-mr-president|"
    r"solitaire-grand|booky-award|showcase|2011-booky|2012-booky|2013-book|2014-book|"
    r"2016-booky|4159|4570|4764|6606|6612|6617",
    re.I
)
NON_BOOK_CATS = re.compile(
    r"video.games|board.games|tech|movies|interviews|giveaway|"
    r"asher.boys|arieltopia|matthew.scott|reviewers", re.I
)

def is_non_book(slug, categories):
    if NON_BOOK_SLUGS.search(slug):
        return True
    return any(NON_BOOK_CATS.search(c) for c in categories)

def isbn13_to_isbn10(isbn13):
    """Convert ISBN-13 to ISBN-10 for Amazon /dp/ links."""
    if not isbn13 or len(isbn13) != 13:
        return None
    if not isbn13.startswith("978"):
        return None  # 979-prefix books don't have ISBN-10
    core = isbn13[3:12]  # 9 digits
    check = sum((10 - i) * int(d) for i, d in enumerate(core))
    check = (11 - (check % 11)) % 11
    check_char = "X" if check == 10 else str(check)
    return core + check_char

def amazon_url(isbn10=None, isbn13=None, title=""):
    """Build direct /dp/ link if we have ISBN-10, else ISBN-13 search, else title search."""
    tag = AFFILIATE_TAG
    if isbn10:
        return f"https://www.amazon.com/dp/{isbn10}/?tag={tag}"
    if isbn13:
        return f"https://www.amazon.com/s?k={quote(isbn13)}&tag={tag}"
    q = re.sub(r'\s+by\s+.*', '', title, flags=re.I).strip()
    return f"https://www.amazon.com/s?k={quote(q)}&tag={tag}"

def fetch_json(url, timeout=10):
    try:
        req = Request(url, headers={"User-Agent": "BookGateway/2.0"})
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None

def query_google_books(query, max_results=5):
    """Return (cover_url, isbn13, isbn10) or (None, None, None)."""
    params = urlencode({
        "q": query,
        "maxResults": str(max_results),
        "fields": "items(volumeInfo(title,imageLinks,industryIdentifiers))"
    })
    url = f"https://www.googleapis.com/books/v1/volumes?{params}"
    data = fetch_json(url)
    if not data or "items" not in data:
        return None, None, None
    for item in data["items"]:
        vi = item.get("volumeInfo", {})
        il = vi.get("imageLinks", {})
        src = il.get("thumbnail") or il.get("smallThumbnail")
        isbn13, isbn10 = None, None
        for ident in vi.get("industryIdentifiers", []):
            if ident.get("type") == "ISBN_13":
                isbn13 = ident["identifier"]
            elif ident.get("type") == "ISBN_10":
                isbn10 = ident["identifier"]
        if src:
            src = src.replace("http://", "https://").replace("zoom=1", "zoom=2")
            # Derive isbn10 from isbn13 if not given directly
            if isbn13 and not isbn10:
                isbn10 = isbn13_to_isbn10(isbn13)
            return src, isbn13, isbn10
    return None, None, None

def query_open_library(title, author=""):
    """Return (cover_url, isbn13, isbn10) or (None, None, None)."""
    params = {"title": title, "limit": "3", "fields": "cover_i,isbn"}
    if author:
        params["author"] = author
    url = "https://openlibrary.org/search.json?" + urlencode(params)
    data = fetch_json(url)
    if not data:
        return None, None, None
    for doc in data.get("docs", []):
        cover_url = None
        if doc.get("cover_i"):
            cover_url = f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-M.jpg"
        isbn13, isbn10 = None, None
        for isbn in doc.get("isbn", []):
            if len(isbn) == 13 and not isbn13:
                isbn13 = isbn
            elif len(isbn) == 10 and not isbn10:
                isbn10 = isbn
        if not isbn10 and isbn13:
            isbn10 = isbn13_to_isbn10(isbn13)
        if cover_url:
            return cover_url, isbn13, isbn10
    return None, None, None

def needs_processing(entry):
    """Re-process if missing cover OR missing isbn."""
    if not entry:
        return True
    if entry.get("non_book"):
        return False
    # Process if no cover, or if has cover but no isbn
    return not entry.get("cover") or not entry.get("isbn10")

def main():
    with open(REVIEWS_FILE, encoding="utf-8") as f:
        reviews = json.load(f)

    try:
        with open(COVERS_FILE, encoding="utf-8") as f:
            covers = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        covers = {}

    total = len(reviews)
    updated = 0
    skipped = 0
    failed = 0

    for i, review in enumerate(reviews):
        slug = review["slug"]
        title = review.get("title", "")
        cats = review.get("categories", [])
        existing = covers.get(slug)

        # Skip if non-book
        if existing and existing.get("non_book"):
            continue

        # Mark non-books
        if is_non_book(slug, cats):
            covers[slug] = {"cover": None, "isbn13": None, "isbn10": None, "amazon": None, "non_book": True}
            continue

        # Skip if already has both cover and isbn10
        if existing and existing.get("cover") and existing.get("isbn10"):
            skipped += 1
            continue

        if not title:
            covers[slug] = {"cover": None, "isbn13": None, "isbn10": None, "amazon": None}
            failed += 1
            continue

        # Parse title / author
        m = re.match(r'^(.+?)\s+by\s+(.+)$', title, re.I)
        book_title = m.group(1).strip() if m else title
        author     = m.group(2).strip() if m else ""

        # Keep existing cover if we have one, just try to get ISBN
        existing_cover = existing.get("cover") if existing else None

        cover, isbn13, isbn10 = None, None, None

        # Strategy 1: Google Books — full query
        if not (existing_cover and isbn10):
            q = f"{book_title} {author}".strip()
            cover, isbn13, isbn10 = query_google_books(q)
            time.sleep(DELAY_SEC)

        # Strategy 2: Google Books — title only
        if not cover and not existing_cover:
            cover, isbn13, isbn10 = query_google_books(book_title)
            time.sleep(DELAY_SEC)

        # Strategy 3: Google Books — intitle search
        if not cover and not existing_cover:
            cover, isbn13, isbn10 = query_google_books(f"intitle:{book_title}")
            time.sleep(DELAY_SEC)

        # Strategy 4: Open Library
        if not cover and not existing_cover:
            cover, isbn13, isbn10 = query_open_library(book_title, author)
            time.sleep(DELAY_SEC)

        # Strategy 5: Open Library title only
        if not cover and not existing_cover:
            cover, isbn13, isbn10 = query_open_library(book_title)
            time.sleep(DELAY_SEC)

        # Use existing cover if we didn't find a new one
        if not cover and existing_cover:
            cover = existing_cover

        # Build Amazon link
        amz = amazon_url(isbn10, isbn13, title)

        covers[slug] = {
            "cover":  cover,
            "isbn13": isbn13,
            "isbn10": isbn10,
            "amazon": amz,
        }

        status = "✓" if cover else "✗"
        isbn_tag = f" ISBN10={isbn10}" if isbn10 else ""
        print(f"[{i+1}/{total}] {status}{isbn_tag} {title[:55]}")

        if cover:
            updated += 1
        else:
            failed += 1

        if (i + 1) % 50 == 0:
            with open(COVERS_FILE, "w", encoding="utf-8") as f:
                json.dump(covers, f, indent=2, ensure_ascii=False)
            print(f"  → Saved ({updated} covers, {failed} failed, {skipped} skipped)")

    # Final save
    with open(COVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(covers, f, indent=2, ensure_ascii=False)

    isbn_count = sum(1 for v in covers.values() if v.get("isbn10"))
    direct_links = sum(1 for v in covers.values() if v.get("amazon","").startswith("https://www.amazon.com/dp/"))
    print(f"\nDone.")
    print(f"  Covers: {updated} found, {failed} failed, {skipped} skipped")
    print(f"  ISBNs (ISBN-10): {isbn_count}")
    print(f"  Direct Amazon /dp/ links: {direct_links}")

if __name__ == "__main__":
    main()
