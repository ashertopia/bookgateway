#!/usr/bin/env python3
"""
Backfill ISBN-10s from Open Library for book entries that already have covers
but lack isbn10. Sets amazon to /dp/{isbn10}/ affiliate URL.
Idempotent: skips entries that already have isbn10.
"""
import json
import re
import time
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import HTTPError

REVIEWS_FILE = "reviews.json"
COVERS_FILE = "covers.json"
AFFILIATE_TAG = "bookgateway02-20"
DELAY = 0.7  # seconds between requests (0.5–1s)
CHECKPOINT_EVERY = 20


def isbn13_to_isbn10(isbn13):
    if not isbn13 or len(isbn13) != 13 or not isbn13.startswith("978"):
        return None
    core = isbn13[3:12]
    if not core.isdigit():
        return None
    total = sum((10 - i) * int(d) for i, d in enumerate(core))
    check = (11 - (total % 11)) % 11
    return core + ("X" if check == 10 else str(check))


def clean_isbn(raw):
    return re.sub(r"[^0-9Xx]", "", raw or "")


def pick_isbns(isbn_list):
    """
    Prefer ISBN-13 starting 9780/9781 converted to ISBN-10;
    else any ISBN-10. Also return a related isbn13 when available.
    Returns (isbn13, isbn10).
    """
    cleaned = []
    for raw in isbn_list or []:
        c = clean_isbn(raw)
        if c:
            cleaned.append(c)

    # Prefer 9780 / 9781 ISBN-13 → ISBN-10
    for c in cleaned:
        if len(c) == 13 and c.startswith(("9780", "9781")):
            i10 = isbn13_to_isbn10(c)
            if i10:
                return c, i10

    # Else any ISBN-10
    for c in cleaned:
        if len(c) == 10:
            # Pair with a matching 978…13 if present
            i13 = None
            for other in cleaned:
                if len(other) == 13 and other.startswith("978") and other[3:12] == c[:9]:
                    i13 = other
                    break
            return i13, c.upper() if c[-1] in "Xx" else c

    # Fallback: any other 978* ISBN-13 → ISBN-10
    for c in cleaned:
        if len(c) == 13 and c.startswith("978"):
            i10 = isbn13_to_isbn10(c)
            if i10:
                return c, i10

    return None, None


def amazon_url(isbn10=None, title=""):
    if isbn10:
        return f"https://www.amazon.com/dp/{isbn10}/?tag={AFFILIATE_TAG}"
    from urllib.parse import quote
    q = re.sub(r"\s+by\s+.*", "", title, flags=re.I).strip()
    return f"https://www.amazon.com/s?k={quote(q)}&tag={AFFILIATE_TAG}"


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": "BookGateway/3.0 (isbn-backfill)"})
            with urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                wait = 30 * (2 ** attempt)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  HTTP {e.code} for {url[:80]}")
                return None
        except Exception as e:
            print(f"  fetch error: {e}")
            time.sleep(2)
    return None


def open_library_isbns(title, author=""):
    params = {"title": title, "limit": "5", "fields": "cover_i,isbn"}
    if author:
        params["author"] = author
    data = fetch_json("https://openlibrary.org/search.json?" + urlencode(params))
    if not data:
        return None, None
    for doc in data.get("docs", []):
        isbn13, isbn10 = pick_isbns(doc.get("isbn", []))
        if isbn10:
            return isbn13, isbn10
    return None, None


def migrate_entry(entry):
    """Normalize schema: isbn → isbn13; ensure isbn10/isbn13 keys exist."""
    if entry is None:
        entry = {}
    if "isbn" in entry and "isbn13" not in entry:
        entry["isbn13"] = entry.pop("isbn")
    elif "isbn" in entry:
        entry.pop("isbn", None)
    if "isbn13" not in entry:
        entry["isbn13"] = None
    if "isbn10" not in entry:
        entry["isbn10"] = None
    return entry


def save(covers):
    with open(COVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(covers, f, indent=2, ensure_ascii=False)


def main():
    reviews = json.load(open(REVIEWS_FILE, encoding="utf-8"))
    covers = json.load(open(COVERS_FILE, encoding="utf-8"))
    title_by_slug = {r["slug"]: r.get("title", "") for r in reviews}

    # Migrate all entries first
    for slug, entry in list(covers.items()):
        covers[slug] = migrate_entry(entry or {})

    candidates = [
        slug
        for slug, entry in covers.items()
        if entry.get("cover")
        and not entry.get("isbn10")
        and not entry.get("non_book")
    ]
    total = len(candidates)
    print(f"Candidates to backfill: {total}")

    found = 0
    missed = 0
    processed = 0

    for slug in candidates:
        entry = covers[slug]
        title_full = title_by_slug.get(slug, "")
        m = re.match(r"^(.+?)\s+by\s+(.+)$", title_full, re.I)
        book_title = m.group(1).strip() if m else title_full
        author = m.group(2).strip() if m else ""

        isbn13, isbn10 = open_library_isbns(book_title, author)
        time.sleep(DELAY)

        if not isbn10 and author:
            isbn13, isbn10 = open_library_isbns(book_title, "")
            time.sleep(DELAY)

        processed += 1
        if isbn10:
            entry["isbn10"] = isbn10
            if isbn13:
                entry["isbn13"] = isbn13
            entry["amazon"] = amazon_url(isbn10)
            found += 1
            print(f"[{processed}/{total}] + isbn10={isbn10}  {book_title[:50]}")
        else:
            missed += 1
            print(f"[{processed}/{total}] - no isbn  {book_title[:55]}")

        covers[slug] = entry

        if processed % CHECKPOINT_EVERY == 0:
            save(covers)
            i_total = sum(1 for v in covers.values() if v and v.get("isbn10"))
            print(f"  → checkpoint: {i_total} isbn10s ({found} found, {missed} missed this run)")

    save(covers)

    c_total = sum(1 for v in covers.values() if v and v.get("cover"))
    i_total = sum(1 for v in covers.values() if v and v.get("isbn10"))
    dp_total = sum(
        1
        for v in covers.values()
        if v and (v.get("amazon") or "").startswith("https://www.amazon.com/dp/")
    )
    remaining = sum(
        1
        for v in covers.values()
        if v
        and v.get("cover")
        and not v.get("isbn10")
        and not v.get("non_book")
    )
    print(f"\nFinished.")
    print(f"  Covers: {c_total}  |  ISBN-10s: {i_total}  |  Direct /dp/ links: {dp_total}")
    print(f"  This run: found={found} missed={missed}  |  Remaining cover-no-isbn10: {remaining}")


if __name__ == "__main__":
    main()
