#!/usr/bin/env python3
"""
Fill missing book covers via Open Library + Amazon CDN.
Skips non_book entries and entries that already have a cover.
Prefer ISBN-13 9780/9781 → isbn10; set amazon /dp/{isbn10}/ affiliate URL.
"""
import json
import re
import time
from urllib.request import urlopen, Request
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

REVIEWS_FILE = "reviews.json"
COVERS_FILE = "covers.json"
AFFILIATE_TAG = "bookgateway02-20"
DELAY = 0.7
CHECKPOINT_EVERY = 20

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
    if NON_BOOK_SLUGS.search(slug):
        return True
    return any(NON_BOOK_CATS.search(c) for c in cats)


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
    """Prefer ISBN-13 9780/9781 → ISBN-10; else any ISBN-10; else other 978*."""
    cleaned = []
    for raw in isbn_list or []:
        c = clean_isbn(raw)
        if c:
            cleaned.append(c)

    for c in cleaned:
        if len(c) == 13 and c.startswith(("9780", "9781")):
            i10 = isbn13_to_isbn10(c)
            if i10:
                return c, i10

    for c in cleaned:
        if len(c) == 10:
            i13 = None
            for other in cleaned:
                if len(other) == 13 and other.startswith("978") and other[3:12] == c[:9]:
                    i13 = other
                    break
            return i13, c.upper() if c[-1] in "Xx" else c

    for c in cleaned:
        if len(c) == 13 and c.startswith("978"):
            i10 = isbn13_to_isbn10(c)
            if i10:
                return c, i10

    # Keep a bare isbn13 if nothing convertible
    for c in cleaned:
        if len(c) == 13:
            return c, None

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
            req = Request(url, headers={"User-Agent": "BookGateway/3.0 (fill-missing-covers)"})
            with urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                wait = 30 * (2 ** attempt)
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  HTTP {e.code} for {url[:90]}")
                return None
        except Exception as e:
            print(f"  fetch error: {e}")
            time.sleep(2)
    return None


def url_ok(url):
    """HEAD (fallback GET) — True if status 200 and looks like a real image (not OL/Amazon placeholder)."""
    for method in ("HEAD", "GET"):
        try:
            req = Request(url, method=method, headers={"User-Agent": "BookGateway/3.0"})
            with urlopen(req, timeout=12) as r:
                if r.status != 200:
                    continue
                ctype = (r.headers.get("Content-Type") or "").lower()
                clen = r.headers.get("Content-Length")
                if clen is not None:
                    try:
                        if int(clen) < 2000:
                            continue
                    except ValueError:
                        pass
                if method == "GET":
                    data = r.read(4096)
                    rest = r.read() if len(data) < 4096 else b""
                    total = len(data) + len(rest)
                    if total < 2000:
                        continue
                if ctype.startswith("image/") or "octet-stream" in ctype or not ctype:
                    return True
        except Exception:
            continue
    return False


def open_library_search(title, author=""):
    """
    Return best (cover_url, isbn13, isbn10, isbn_list) from OL search.
    cover_url from cover_i when present; isbns always collected when present.
    """
    params = {"title": title, "limit": "5", "fields": "cover_i,isbn,title,author_name"}
    if author:
        params["author"] = author
    data = fetch_json("https://openlibrary.org/search.json?" + urlencode(params))
    if not data:
        return None, None, None, []

    best_cover = None
    best_i13 = best_i10 = None
    best_isbns = []

    for doc in data.get("docs", []):
        isbns = doc.get("isbn") or []
        i13, i10 = pick_isbns(isbns)
        cover = None
        if doc.get("cover_i"):
            cover = f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg"

        # Prefer a doc that has a cover
        if cover and not best_cover:
            best_cover, best_i13, best_i10, best_isbns = cover, i13, i10, isbns
            # good enough if we also have isbn10
            if i10:
                return best_cover, best_i13, best_i10, best_isbns
        elif not best_cover and i10 and not best_i10:
            best_i13, best_i10, best_isbns = i13, i10, isbns

    if best_cover or best_i10:
        return best_cover, best_i13, best_i10, best_isbns
    return None, None, None, []


def try_cover_by_isbn(isbn10=None, isbn13=None):
    """Try OL ISBN cover then Amazon CDN. Return cover URL or None."""
    candidates = []
    for isbn in (isbn13, isbn10):
        if isbn:
            candidates.append(f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg")
    if isbn10:
        candidates.append(
            f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.LZZZZZZZ.jpg"
        )

    for url in candidates:
        if url_ok(url):
            return url
        time.sleep(0.2)
    return None


def migrate_entry(entry):
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
    if "cover" not in entry:
        entry["cover"] = None
    if "amazon" not in entry:
        entry["amazon"] = None
    return entry


def save(covers):
    with open(COVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(covers, f, indent=2, ensure_ascii=False)


def main():
    reviews = json.load(open(REVIEWS_FILE, encoding="utf-8"))
    covers = json.load(open(COVERS_FILE, encoding="utf-8"))

    for slug, entry in list(covers.items()):
        covers[slug] = migrate_entry(entry or {})

    candidates = []
    for r in reviews:
        slug = r["slug"]
        cats = r.get("categories", [])
        entry = covers.get(slug) or {}
        if entry.get("non_book") or is_non_book(slug, cats):
            if not entry.get("non_book"):
                covers[slug] = {
                    "cover": None,
                    "isbn13": None,
                    "isbn10": None,
                    "amazon": None,
                    "non_book": True,
                }
            continue
        if entry.get("cover"):
            continue
        candidates.append(r)

    total = len(candidates)
    print(f"Candidates missing cover: {total}")

    filled = 0
    missed = 0
    processed = 0

    for review in candidates:
        slug = review["slug"]
        title_full = review.get("title", "")
        entry = migrate_entry(covers.get(slug) or {})

        # Skip if somehow already filled (checkpoint resume)
        if entry.get("cover") or entry.get("non_book"):
            continue

        m = re.match(r"^(.+?)\s+by\s+(.+)$", title_full, re.I)
        book_title = m.group(1).strip() if m else title_full
        author = m.group(2).strip() if m else ""

        cover = None
        isbn13 = entry.get("isbn13")
        isbn10 = entry.get("isbn10")

        # 1) OL search title+author
        c, i13, i10, isbns = open_library_search(book_title, author)
        time.sleep(DELAY)
        if c:
            cover = c
        if i10 and not isbn10:
            isbn10 = i10
        if i13 and not isbn13:
            isbn13 = i13

        # 2) OL search title only
        if not cover:
            c, i13, i10, isbns2 = open_library_search(book_title, "")
            time.sleep(DELAY)
            if c:
                cover = c
            if i10 and not isbn10:
                isbn10 = i10
            if i13 and not isbn13:
                isbn13 = i13
            if not isbns and isbns2:
                isbns = isbns2

        # Derive isbn from any leftover list
        if not isbn10 and isbns:
            i13, i10 = pick_isbns(isbns)
            if i10:
                isbn10 = i10
            if i13 and not isbn13:
                isbn13 = i13

        # 3) If we have ISBN but no cover, try OL isbn cover + Amazon CDN
        if not cover and (isbn10 or isbn13):
            cover = try_cover_by_isbn(isbn10=isbn10, isbn13=isbn13)
            time.sleep(DELAY)

        # 4) If still no cover but we got isbns from a no-cover OL doc earlier,
        #    still try CDN with whatever we have
        if not cover and not isbn10:
            # one more OL pass already done; nothing else without Google Books
            pass

        processed += 1
        if cover:
            entry["cover"] = cover
            if isbn13:
                entry["isbn13"] = isbn13
            if isbn10:
                entry["isbn10"] = isbn10
            entry["amazon"] = amazon_url(isbn10, title_full)
            filled += 1
            tag = f" isbn10={isbn10}" if isbn10 else ""
            print(f"[{processed}/{total}] ✓ cover{tag}  {title_full[:55]}")
        else:
            # Still save any ISBNs we found even without cover
            if isbn13:
                entry["isbn13"] = isbn13
            if isbn10:
                entry["isbn10"] = isbn10
                entry["amazon"] = amazon_url(isbn10, title_full)
            elif not entry.get("amazon"):
                entry["amazon"] = amazon_url(None, title_full)
            missed += 1
            print(f"[{processed}/{total}] ✗ no cover  {title_full[:55]}")

        covers[slug] = entry

        if processed % CHECKPOINT_EVERY == 0:
            save(covers)
            c_total = sum(1 for v in covers.values() if v and v.get("cover"))
            print(f"  → checkpoint: {c_total} covers ({filled} filled, {missed} missed this run)")

    save(covers)

    c_total = sum(1 for v in covers.values() if v and v.get("cover"))
    i_total = sum(1 for v in covers.values() if v and v.get("isbn10"))
    dp_total = sum(
        1
        for v in covers.values()
        if v and (v.get("amazon") or "").startswith("https://www.amazon.com/dp/")
    )
    still_missing = sum(
        1
        for r in reviews
        if not (covers.get(r["slug"]) or {}).get("non_book")
        and not is_non_book(r["slug"], r.get("categories", []))
        and not (covers.get(r["slug"]) or {}).get("cover")
    )
    print(f"\nFinished.")
    print(f"  Covers: {c_total}  |  ISBN-10s: {i_total}  |  Direct /dp/ links: {dp_total}")
    print(f"  This run: filled={filled} missed={missed}  |  Still missing: {still_missing}")


if __name__ == "__main__":
    main()
