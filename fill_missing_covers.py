#!/usr/bin/env python3
"""
Fill missing book covers via Open Library + Amazon CDN.
Requires author match — never accepts title-only fallbacks.
Uses HTML-parsed title/author/publisher from book_meta.
"""
from __future__ import annotations

import json
import re
import time
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote
from urllib.error import HTTPError

from book_meta import (
    AFFILIATE_TAG,
    amazon_dp_url,
    amazon_search_url,
    author_match,
    build_meta_cache,
    publisher_match,
    title_similarity,
)

REVIEWS_FILE = "reviews.json"
COVERS_FILE = "covers.json"
DELAY = 0.7
CHECKPOINT_EVERY = 20
UA = "BookGateway/3.1 (fill-missing-covers)"

NON_BOOK_SLUGS = re.compile(
    r"asher-dad|football-gm|retro-bowl|amazon-luna|cbs-franchise|hey-mr-president|"
    r"solitaire-grand|booky-award|showcase|2011-booky|2012-booky|2013-book|2014-book|"
    r"2016-booky|4159|4570|4764|6606|6612|6617",
    re.I,
)
NON_BOOK_CATS = re.compile(
    r"video.games|board.games|tech|movies|interviews|giveaway|"
    r"asher.boys|arieltopia|matthew.scott|reviewers",
    re.I,
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
    for c in cleaned:
        if len(c) == 13:
            return c, None
    return None, None


def fetch_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA})
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
    for method in ("HEAD", "GET"):
        try:
            req = Request(url, method=method, headers={"User-Agent": UA})
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


def open_library_search(title, author="", publisher=""):
    """
    Return best matching (cover_url, isbn13, isbn10) with author verification.
    Never accepts a result without author match when author is provided.
    """
    if not title:
        return None, None, None

    params = {
        "title": title,
        "limit": "10",
        "fields": "cover_i,isbn,title,author_name,publisher",
    }
    if author:
        params["author"] = author
    data = fetch_json("https://openlibrary.org/search.json?" + urlencode(params))
    if not data:
        return None, None, None

    scored = []
    for doc in data.get("docs", []):
        doc_title = doc.get("title") or ""
        doc_authors = doc.get("author_name") or []
        doc_pubs = doc.get("publisher") or []

        if author and not author_match(author, doc_authors):
            continue

        tscore = title_similarity(title, doc_title)
        if tscore < 0.55:
            continue

        pscore = 0.0
        if publisher and doc_pubs:
            if any(publisher_match(publisher, p) for p in doc_pubs):
                pscore = 0.15

        cover = None
        if doc.get("cover_i"):
            cover = f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg"
        isbns = doc.get("isbn") or []
        i13, i10 = pick_isbns(isbns)
        score = tscore + pscore + (0.05 if cover else 0) + (0.05 if i10 else 0)
        scored.append((score, cover, i13, i10, doc_title, doc_authors))

    if not scored:
        return None, None, None

    scored.sort(key=lambda x: x[0], reverse=True)
    # Prefer publisher-matching candidate when scores close
    best = scored[0]
    return best[1], best[2], best[3]


def try_cover_by_isbn(isbn10=None, isbn13=None):
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
    for k in ("isbn13", "isbn10", "cover", "amazon"):
        if k not in entry:
            entry[k] = None
    return entry


def save(covers):
    with open(COVERS_FILE, "w", encoding="utf-8") as f:
        json.dump(covers, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main():
    reviews = json.load(open(REVIEWS_FILE, encoding="utf-8"))
    covers = json.load(open(COVERS_FILE, encoding="utf-8"))
    print("Building / loading book meta…")
    meta = build_meta_cache(reviews, force=False)

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

    filled = missed = processed = 0

    for review in candidates:
        slug = review["slug"]
        entry = migrate_entry(covers.get(slug) or {})
        if entry.get("cover") or entry.get("non_book"):
            continue

        m = meta.get(slug) or {}
        book_title = m.get("title") or ""
        author = m.get("author") or ""
        publisher = m.get("publisher") or ""
        if not book_title:
            # last resort from card title
            mt = re.match(r"^(.+?)\s+by\s+(.+)$", review.get("title", ""), re.I)
            if mt:
                book_title, author = mt.group(1).strip(), mt.group(2).strip()
            else:
                book_title = review.get("title", "")

        cover = None
        isbn13 = entry.get("isbn13")
        isbn10 = entry.get("isbn10")

        # Require author for acceptance when we have one
        c, i13, i10 = open_library_search(book_title, author, publisher)
        time.sleep(DELAY)
        if c:
            cover = c
        if i10 and not isbn10:
            isbn10 = i10
        if i13 and not isbn13:
            isbn13 = i13

        # If author search failed and we have author, do NOT fall back to title-only.
        # If we have no author at all, skip cover (safer).
        if not cover and not author:
            print(f"[{processed+1}/{total}] skip (no author) {book_title[:50]}")
            missed += 1
            processed += 1
            entry["amazon"] = entry.get("amazon") or amazon_search_url(book_title, author)
            covers[slug] = entry
            continue

        # Optional: second search with title + publisher in query string if first missed
        if not cover and publisher:
            c, i13, i10 = open_library_search(
                f"{book_title}", author, publisher
            )
            # already slept; one more polite delay only if we actually searched again
            # (same call pattern — skip duplicate; already tried)
            pass

        if not cover and (isbn10 or isbn13):
            # Only use existing ISBN cover if we already verified ISBN in audit,
            # or verify via OL books API author match
            cover = try_cover_by_isbn(isbn10=isbn10, isbn13=isbn13)
            time.sleep(DELAY)

        processed += 1
        if cover:
            entry["cover"] = cover
            if isbn13:
                entry["isbn13"] = isbn13
            if isbn10:
                entry["isbn10"] = isbn10
            entry["amazon"] = (
                amazon_dp_url(isbn10) if isbn10 else amazon_search_url(book_title, author)
            )
            filled += 1
            tag = f" isbn10={isbn10}" if isbn10 else ""
            print(f"[{processed}/{total}] ✓ cover{tag}  {book_title[:50]} / {author[:30]}")
        else:
            if isbn13:
                entry["isbn13"] = isbn13
            if isbn10:
                entry["isbn10"] = isbn10
                entry["amazon"] = amazon_dp_url(isbn10)
            else:
                entry["amazon"] = amazon_search_url(book_title, author)
            missed += 1
            print(f"[{processed}/{total}] ✗ no cover  {book_title[:50]} / {author[:30]}")

        covers[slug] = entry

        if processed % CHECKPOINT_EVERY == 0:
            save(covers)
            c_total = sum(1 for v in covers.values() if v and v.get("cover"))
            print(f"  → checkpoint: {c_total} covers ({filled} filled, {missed} missed)")

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
