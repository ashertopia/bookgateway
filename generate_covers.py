#!/usr/bin/env python3
"""
BookGateway cover generator v3.
- Separate passes: covers first, then ISBNs for already-covered books
- Exponential backoff on rate limits
- Saves every 10 books
- No heredoc issues — called directly from workflow
"""
import json, time, re, sys
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote
from urllib.error import URLError, HTTPError

REVIEWS_FILE  = "reviews.json"
COVERS_FILE   = "covers.json"
AFFILIATE_TAG = "bookgateway02-20"
DELAY         = 0.5   # seconds between requests

NON_BOOK_SLUGS = re.compile(
    r"asher-dad|football-gm|retro-bowl|amazon-luna|cbs-franchise|hey-mr-president|"
    r"solitaire-grand|booky-award|showcase|2011-booky|2012-booky|2013-book|2014-book|"
    r"2016-booky|4159|4570|4764|6606|6612|6617", re.I
)
NON_BOOK_CATS = re.compile(
    r"video.games|board.games|tech|movies|interviews|giveaway|"
    r"asher.boys|arieltopia|matthew.scott|reviewers", re.I
)

def is_non_book(slug, cats):
    if NON_BOOK_SLUGS.search(slug): return True
    return any(NON_BOOK_CATS.search(c) for c in cats)

def isbn13_to_isbn10(isbn13):
    if not isbn13 or len(isbn13) != 13 or not isbn13.startswith("978"):
        return None
    core = isbn13[3:12]
    total = sum((10 - i) * int(d) for i, d in enumerate(core))
    check = (11 - (total % 11)) % 11
    return core + ("X" if check == 10 else str(check))

def amazon_url(isbn10=None, isbn13=None, title=""):
    if isbn10:
        return f"https://www.amazon.com/dp/{isbn10}/?tag={AFFILIATE_TAG}"
    if isbn13:
        return f"https://www.amazon.com/s?k={quote(isbn13)}&tag={AFFILIATE_TAG}"
    q = re.sub(r'\s+by\s+.*', '', title, flags=re.I).strip()
    return f"https://www.amazon.com/s?k={quote(q)}&tag={AFFILIATE_TAG}"

def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "BookGateway/3.0"})
            with urlopen(req, timeout=12) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                wait = 30 * (2 ** attempt)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                return None
        except Exception:
            time.sleep(2)
    return None

def google_books(query):
    """Return (cover, isbn13, isbn10)."""
    params = urlencode({"q": query, "maxResults": "3",
                        "fields": "items(volumeInfo(imageLinks,industryIdentifiers))"})
    data = fetch_json(f"https://www.googleapis.com/books/v1/volumes?{params}")
    if not data or "items" not in data:
        return None, None, None
    for item in data["items"]:
        vi = item.get("volumeInfo", {})
        il = vi.get("imageLinks", {})
        src = il.get("thumbnail") or il.get("smallThumbnail")
        isbn13 = isbn10 = None
        for ident in vi.get("industryIdentifiers", []):
            if ident["type"] == "ISBN_13": isbn13 = ident["identifier"]
            if ident["type"] == "ISBN_10": isbn10 = ident["identifier"]
        if src:
            src = src.replace("http://", "https://").replace("zoom=1", "zoom=2")
            if isbn13 and not isbn10:
                isbn10 = isbn13_to_isbn10(isbn13)
            return src, isbn13, isbn10
    return None, None, None

def open_library(title, author=""):
    """Return (cover, isbn13, isbn10)."""
    params = {"title": title, "limit": "3", "fields": "cover_i,isbn"}
    if author: params["author"] = author
    data = fetch_json("https://openlibrary.org/search.json?" + urlencode(params))
    if not data: return None, None, None
    for doc in data.get("docs", []):
        cover = f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-M.jpg" if doc.get("cover_i") else None
        isbn13 = isbn10 = None
        for isbn in doc.get("isbn", []):
            if len(isbn) == 13 and not isbn13: isbn13 = isbn
            if len(isbn) == 10 and not isbn10: isbn10 = isbn
        if not isbn10 and isbn13: isbn10 = isbn13_to_isbn10(isbn13)
        if cover:
            return cover, isbn13, isbn10
    return None, None, None

def save(covers):
    with open(COVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(covers, f, indent=2, ensure_ascii=False)

def main():
    reviews = json.load(open(REVIEWS_FILE, encoding="utf-8"))
    try:
        covers = json.load(open(COVERS_FILE, encoding="utf-8"))
    except Exception:
        covers = {}

    # Migrate old entries: add isbn10/isbn13 keys if missing
    for slug, v in covers.items():
        if v and not v.get("non_book"):
            if "isbn10" not in v: v["isbn10"] = None
            if "isbn13" not in v: v["isbn13"] = v.pop("isbn", None)

    total = len(reviews)
    covers_added = isbn_added = skipped = 0

    for i, review in enumerate(reviews):
        slug  = review["slug"]
        title = review.get("title", "")
        cats  = review.get("categories", [])
        entry = covers.get(slug, {}) or {}

        # Mark non-books
        if is_non_book(slug, cats):
            if not entry.get("non_book"):
                covers[slug] = {"cover": None, "isbn13": None, "isbn10": None,
                                "amazon": None, "non_book": True}
            continue

        has_cover = bool(entry.get("cover"))
        has_isbn  = bool(entry.get("isbn10"))

        # Fully resolved — skip
        if has_cover and has_isbn:
            skipped += 1
            continue

        m = re.match(r'^(.+?)\s+by\s+(.+)$', title, re.I)
        book_title = m.group(1).strip() if m else title
        author     = m.group(2).strip() if m else ""

        cover  = entry.get("cover")
        isbn13 = entry.get("isbn13")
        isbn10 = entry.get("isbn10")

        # ── PASS 1: find cover if missing ────────────────────────────────────
        if not has_cover:
            # Try Google Books: title + author
            c, i13, i10 = google_books(f"{book_title} {author}".strip())
            time.sleep(DELAY)
            if not c:
                c, i13, i10 = google_books(book_title)
                time.sleep(DELAY)
            if not c:
                c, i13, i10 = open_library(book_title, author)
                time.sleep(DELAY)
            if not c:
                c, i13, i10 = open_library(book_title)
                time.sleep(DELAY)
            if c:
                cover, isbn13, isbn10 = c, i13, i10
                covers_added += 1
                tag = f" [isbn10={isbn10}]" if isbn10 else ""
                print(f"[{i+1}/{total}] ✓ cover{tag} {title[:55]}")
            else:
                print(f"[{i+1}/{total}] ✗ no cover  {title[:55]}")

        # ── PASS 2: find ISBN for already-covered books ───────────────────────
        elif not has_isbn:
            _, i13, i10 = google_books(f"{book_title} {author}".strip())
            time.sleep(DELAY)
            if not i10:
                _, i13, i10 = open_library(book_title, author)
                time.sleep(DELAY)
            if i10:
                isbn13, isbn10 = i13, i10
                isbn_added += 1
                print(f"[{i+1}/{total}] + isbn10={isbn10}  {title[:50]}")
            else:
                print(f"[{i+1}/{total}] - no isbn  {title[:55]}")

        covers[slug] = {
            "cover":  cover,
            "isbn13": isbn13,
            "isbn10": isbn10,
            "amazon": amazon_url(isbn10, isbn13, title),
        }

        # Save every 10
        if (i + 1) % 10 == 0:
            save(covers)
            c_total  = sum(1 for v in covers.values() if v and v.get("cover"))
            i_total  = sum(1 for v in covers.values() if v and v.get("isbn10"))
            print(f"  → checkpoint: {c_total} covers, {i_total} isbn10s")

    save(covers)
    c_total = sum(1 for v in covers.values() if v and v.get("cover"))
    i_total = sum(1 for v in covers.values() if v and v.get("isbn10"))
    dp_total = sum(1 for v in covers.values()
                   if v and (v.get("amazon") or "").startswith("https://www.amazon.com/dp/"))
    print(f"\nFinished.")
    print(f"  Covers: {c_total}  |  ISBN-10s: {i_total}  |  Direct /dp/ links: {dp_total}")
    print(f"  New covers: {covers_added}  |  New ISBNs: {isbn_added}  |  Skipped: {skipped}")

if __name__ == "__main__":
    main()
