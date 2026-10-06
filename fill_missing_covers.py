#!/usr/bin/env python3
"""
Fill missing book covers via Open Library + Amazon CDN + optional Google Books.
Requires author match — never accepts title-only fallbacks.
Uses HTML-parsed title/author/publisher from book_meta.
"""
from __future__ import annotations

import json
import re
import time
from urllib.request import urlopen, Request
from urllib.parse import urlencode, quote
from urllib.error import HTTPError, URLError

from book_meta import (
    AFFILIATE_TAG,
    amazon_dp_url,
    amazon_search_url,
    author_match,
    build_meta_cache,
    publisher_match,
    title_from_card,
    title_similarity,
)

REVIEWS_FILE = "reviews.json"
COVERS_FILE = "covers.json"
DELAY = 0.75
GB_DELAY = 1.6
CHECKPOINT_EVERY = 15
UA = "BookGateway/3.2 (fill-missing-covers)"

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

ASIN_RE = re.compile(r"/dp/([A-Z0-9]{10})", re.I)
ROLE_PREFIX = re.compile(
    r"^(?:written\s+by|illustrated\s+by|edited\s+by|by:?|artists?:|art\s+by)\s+",
    re.I,
)
GIVEAWAY_PAREN = re.compile(r"\s*\((?:give\s*away!?|contest)[^)]*\)\s*", re.I)
PROSE_HINT = re.compile(
    r"\b(review|narration|boyfriend|shouldn|continues the|feel-good|"
    r"excellent|film that|dvd)\b",
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


def clean_author(author: str) -> str:
    if not author:
        return ""
    a = author.strip()
    a = ROLE_PREFIX.sub("", a)
    a = GIVEAWAY_PAREN.sub("", a).strip()
    a = re.sub(r"\s*\(Give Away!?\)\s*", "", a, flags=re.I).strip()
    # Drop trailing role notes
    a = re.sub(r"\s*\(.*\)\s*$", "", a).strip()
    if len(a) > 90 or PROSE_HINT.search(a):
        return ""
    # "Mary Asher, the Golden Reviewer" style
    if re.search(r"\b(reviewer|golden reviewer)\b", a, re.I):
        return ""
    return a


def resolve_title_author(meta: dict, review: dict) -> tuple[str, str, str]:
    """Return (title, author, publisher) preferring clean bib + card fallbacks."""
    book_title = (meta.get("title") or "").strip()
    author = clean_author(meta.get("author") or "")
    publisher = (meta.get("publisher") or "").strip()
    card_title, card_author = title_from_card(review.get("title", ""))
    card_author = clean_author(card_author)

    if not book_title and card_title:
        book_title = card_title
    # If bib title looks like review prose, prefer card
    if book_title and (
        len(book_title) > 100
        or PROSE_HINT.search(book_title)
        or book_title.lower().endswith(" review")
    ):
        if card_title and not card_title.lower().endswith(" review"):
            book_title = card_title

    if not author and card_author:
        author = card_author
    # Prefer card author when bib is empty/bad and card has one
    if card_author and (not author or len(card_author) < len(author) * 0.5):
        # keep fuller bib author when it matches card surname
        if author and author_match(card_author, [author]):
            pass
        elif not author:
            author = card_author

    # Strip series clutter from title for search? keep as-is; OL handles
    return book_title, author, publisher


def fetch_json(url, retries=4):
    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                wait = 35 * (2 ** attempt)
                print(f"  Rate limited ({e.code}), waiting {wait}s...")
                time.sleep(wait)
            elif e.code in (500, 502, 503, 504):
                time.sleep(5 * (attempt + 1))
            else:
                print(f"  HTTP {e.code} for {url[:90]}")
                return None
        except (URLError, TimeoutError, OSError) as e:
            print(f"  fetch error: {e}")
            time.sleep(3 * (attempt + 1))
        except Exception as e:
            print(f"  fetch error: {e}")
            time.sleep(2)
    return None


def url_ok(url, min_bytes=2000):
    for method in ("HEAD", "GET"):
        try:
            req = Request(url, method=method, headers={"User-Agent": UA})
            with urlopen(req, timeout=14) as r:
                if r.status != 200:
                    continue
                ctype = (r.headers.get("Content-Type") or "").lower()
                clen = r.headers.get("Content-Length")
                if clen is not None:
                    try:
                        if int(clen) < min_bytes:
                            continue
                    except ValueError:
                        pass
                if method == "GET":
                    data = r.read()
                    if len(data) < min_bytes:
                        continue
                    # reject tiny gif placeholders
                    if ctype.startswith("image/gif") and len(data) < min_bytes:
                        continue
                if ctype.startswith("image/") or "octet-stream" in ctype or not ctype:
                    return True
        except Exception:
            continue
    return False


def title_variants(title: str) -> list[str]:
    """Generate alternate titles for stubborn OL lookups."""
    variants = []
    t = (title or "").strip()
    if not t:
        return variants
    variants.append(t)
    # A/The swap
    m = re.match(r"^(a|an|the)\s+(.+)$", t, re.I)
    if m:
        rest = m.group(2)
        variants.append(rest)
        for art in ("The", "A"):
            alt = f"{art} {rest}"
            if alt.lower() != t.lower():
                variants.append(alt)
    else:
        variants.append(f"The {t}")
        variants.append(f"A {t}")
    # Drop subtitle after colon/emdash
    for sep in (":", "—", "–", " - "):
        if sep in t:
            variants.append(t.split(sep, 1)[0].strip())
    # Dedupe preserving order
    seen = set()
    out = []
    for v in variants:
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def score_ol_doc(title, author, publisher, doc):
    doc_title = doc.get("title") or ""
    doc_authors = doc.get("author_name") or []
    doc_pubs = doc.get("publisher") or []

    if author and not author_match(author, doc_authors):
        return None

    tscore = title_similarity(title, doc_title)
    if tscore < 0.55:
        return None

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
    return (score, cover, i13, i10, doc_title, doc_authors)


def open_library_search(title, author="", publisher=""):
    """
    Return best matching (cover_url, isbn13, isbn10) with author verification.
    Never accepts a result without author match when author is provided.
    """
    if not title or not author:
        return None, None, None

    attempts = []
    for tv in title_variants(title)[:4]:
        attempts.append({"title": tv, "author": author, "limit": "12",
                         "fields": "cover_i,isbn,title,author_name,publisher"})
    # Combined free-text query often finds stubborn titles
    attempts.append({
        "q": f"{title} {author}",
        "limit": "12",
        "fields": "cover_i,isbn,title,author_name,publisher",
    })

    best = None
    for params in attempts:
        data = fetch_json("https://openlibrary.org/search.json?" + urlencode(params))
        time.sleep(DELAY)
        if not data:
            continue
        for doc in data.get("docs", []):
            scored = score_ol_doc(title, author, publisher, doc)
            if not scored:
                # Also try matching against variant title if title param differed
                qt = params.get("title") or title
                if qt != title:
                    scored = score_ol_doc(qt, author, publisher, doc)
            if scored and (best is None or scored[0] > best[0]):
                best = scored
        if best and best[0] >= 0.9 and best[1]:
            break

    if not best:
        return None, None, None
    return best[1], best[2], best[3]


def google_books_search(title, author="", publisher=""):
    """Author-verified Google Books lookup. Returns (cover, isbn13, isbn10)."""
    if not title or not author:
        return None, None, None

    queries = [
        f'intitle:"{title}" inauthor:"{author.split(",")[0].split(" and ")[0].strip()}"',
        f"{title} {author}",
    ]
    best = None
    for q in queries:
        params = urlencode({
            "q": q,
            "maxResults": "5",
            "fields": (
                "items(volumeInfo(title,authors,publisher,imageLinks,"
                "industryIdentifiers))"
            ),
        })
        data = fetch_json(f"https://www.googleapis.com/books/v1/volumes?{params}")
        time.sleep(GB_DELAY)
        if not data or "items" not in data:
            continue
        for item in data["items"]:
            vi = item.get("volumeInfo") or {}
            doc_title = vi.get("title") or ""
            doc_authors = vi.get("authors") or []
            doc_pub = vi.get("publisher") or ""

            if not author_match(author, doc_authors):
                continue
            tscore = title_similarity(title, doc_title)
            if tscore < 0.55:
                continue

            il = vi.get("imageLinks") or {}
            src = il.get("thumbnail") or il.get("smallThumbnail")
            if src:
                src = src.replace("http://", "https://").replace("zoom=1", "zoom=2")
                # Prefer larger
                src = re.sub(r"zoom=\d", "zoom=2", src)

            isbn13 = isbn10 = None
            for ident in vi.get("industryIdentifiers") or []:
                if ident.get("type") == "ISBN_13":
                    isbn13 = ident.get("identifier")
                if ident.get("type") == "ISBN_10":
                    isbn10 = ident.get("identifier")
            if isbn13 and not isbn10:
                isbn10 = isbn13_to_isbn10(isbn13)

            pscore = 0.1 if publisher and doc_pub and publisher_match(publisher, doc_pub) else 0
            score = tscore + pscore + (0.05 if src else 0)
            cand = (score, src, isbn13, isbn10)
            if best is None or score > best[0]:
                best = cand
        if best and best[0] >= 0.9 and best[1]:
            break

    if not best or not best[1]:
        return (None, best[2] if best else None, best[3] if best else None)
    return best[1], best[2], best[3]


def try_cover_by_isbn(isbn10=None, isbn13=None):
    candidates = []
    for isbn in (isbn13, isbn10):
        if isbn:
            candidates.append(f"https://covers.openlibrary.org/b/isbn/{isbn}-L.jpg")
    if isbn10 and not isbn10.upper().startswith("B"):
        candidates.append(
            f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01.LZZZZZZZ.jpg"
        )
        candidates.append(
            f"https://images-na.ssl-images-amazon.com/images/P/{isbn10}.01._SCLZZZZZZZ_.jpg"
        )
    for url in candidates:
        if url_ok(url):
            return url
        time.sleep(0.15)
    return None


def try_amazon_asin_cover(asin: str) -> str | None:
    """Try Amazon CDN product images for a known ASIN/ISBN10."""
    if not asin or len(asin) != 10:
        return None
    asin = asin.upper()
    candidates = [
        f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01.MAIN._SCRM_.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg",
        f"https://m.media-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg",
    ]
    for url in candidates:
        if url_ok(url, min_bytes=2500):
            return url
        time.sleep(0.15)
    return None


def verify_amazon_asin(asin: str, title: str, author: str) -> bool:
    """
    Soft-verify ASIN via Amazon product HTML title/author signals.
    Returns True if page suggests a match, False if clear mismatch,
    and True (permissive) if page unreadable but we already trust known ASIN + author.
    """
    if not asin or not title or not author:
        return False
    url = f"https://www.amazon.com/dp/{asin}"
    try:
        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; BookGateway/3.2)",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urlopen(req, timeout=18) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  ASIN page fetch failed ({asin}): {e}")
        # CDN-only path: allow if we have author (ASIN already on this review)
        return True

    # Extract title-ish signals
    signals = []
    for pat in [
        r"<title>([^<]+)</title>",
        r'id="productTitle"[^>]*>\s*([^<]+)<',
        r'property="og:title"\s+content="([^"]+)"',
    ]:
        m = re.search(pat, html, re.I)
        if m:
            signals.append(re.sub(r"\s+", " ", m.group(1)).strip())

    page_blob = " ".join(signals).lower()
    # Author often in byline
    byline = ""
    m = re.search(
        r'id="bylineInfo"[^>]*>(.*?)</div>',
        html,
        re.I | re.S,
    )
    if m:
        byline = re.sub(r"<[^>]+>", " ", m.group(1))
        byline = re.sub(r"\s+", " ", byline).strip()

    t_ok = any(title_similarity(title, s) >= 0.5 for s in signals) if signals else False
    # Also token check against page title
    if not t_ok and signals:
        nt = set(re.findall(r"[a-z0-9]+", title.lower()))
        ns = set(re.findall(r"[a-z0-9]+", page_blob))
        if nt and len(nt & ns) / len(nt) >= 0.5:
            t_ok = True

    a_ok = author_match(author, [byline]) if byline else False
    if not a_ok:
        # author tokens in page blob / byline area
        for part in re.split(r"[,&]| and ", author):
            part = part.strip()
            if len(part) >= 4 and part.lower() in (page_blob + " " + byline.lower()):
                a_ok = True
                break

    if signals and not t_ok and not a_ok:
        print(f"  ASIN mismatch {asin}: page={signals[0][:60]!r}")
        return False
    if t_ok or a_ok:
        return True
    # No clear signals — permissive keep for known ASIN + author present
    return True


def extract_asin(entry: dict) -> str | None:
    for key in ("isbn10",):
        v = entry.get(key)
        if v and re.fullmatch(r"[A-Z0-9]{10}", str(v), re.I):
            return str(v).upper()
    am = entry.get("amazon") or ""
    m = ASIN_RE.search(am)
    if m:
        return m.group(1).upper()
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
    sources = {"ol": 0, "isbn": 0, "gb": 0, "asin": 0}

    for review in candidates:
        slug = review["slug"]
        entry = migrate_entry(covers.get(slug) or {})
        if entry.get("cover") or entry.get("non_book"):
            continue

        m = meta.get(slug) or {}
        book_title, author, publisher = resolve_title_author(m, review)

        cover = None
        source = None
        isbn13 = entry.get("isbn13")
        isbn10 = entry.get("isbn10")

        if not book_title or not author:
            print(f"[{processed+1}/{total}] skip (no author/title) {book_title[:50]!r}")
            missed += 1
            processed += 1
            entry["amazon"] = entry.get("amazon") or amazon_search_url(book_title, author)
            covers[slug] = entry
            continue

        # 1) Open Library (author-required)
        c, i13, i10 = open_library_search(book_title, author, publisher)
        if c:
            cover, source = c, "ol"
        if i10 and not isbn10:
            isbn10 = i10
        if i13 and not isbn13:
            isbn13 = i13

        # 2) Existing ISBN cover URLs (only if we have ISBN from this verified search
        #    or previously stored — still require that OL/GB matched author above,
        #    or try OL books API lightly via cover URL only when ISBN came from OL)
        if not cover and (isbn10 or isbn13):
            # Prefer ISBN obtained from author-matched OL above
            cover = try_cover_by_isbn(isbn10=isbn10, isbn13=isbn13)
            time.sleep(0.25)
            if cover:
                source = "isbn"

        # 3) Google Books (author-verified) — only if still missing
        if not cover:
            c, i13, i10 = google_books_search(book_title, author, publisher)
            if c:
                cover, source = c, "gb"
            if i10 and not isbn10:
                isbn10 = i10
            if i13 and not isbn13:
                isbn13 = i13
            # If GB gave ISBN but weak/no image, try ISBN CDN
            if not cover and (isbn10 or isbn13):
                cover = try_cover_by_isbn(isbn10=isbn10, isbn13=isbn13)
                if cover:
                    source = "isbn"

        # 4) Known ASIN / ISBN10 in amazon field (ebook/audiobook)
        if not cover:
            asin = extract_asin(entry)
            if asin and verify_amazon_asin(asin, book_title, author):
                cover = try_amazon_asin_cover(asin)
                if cover:
                    source = "asin"
                    # Keep Kindle ASINs as amazon dp; don't call them isbn10 unless numeric
                    if asin[0].isdigit() or asin.upper().startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")):
                        if not isbn10 and re.fullmatch(r"[0-9X]{10}", asin, re.I):
                            isbn10 = asin
                    entry["amazon"] = amazon_dp_url(asin)
                time.sleep(0.3)

        processed += 1
        if cover:
            entry["cover"] = cover
            if isbn13:
                entry["isbn13"] = isbn13
            if isbn10 and re.fullmatch(r"[0-9X]{10}", str(isbn10), re.I):
                entry["isbn10"] = isbn10
                entry["amazon"] = amazon_dp_url(isbn10)
            elif extract_asin(entry):
                entry["amazon"] = amazon_dp_url(extract_asin(entry))
            else:
                entry["amazon"] = amazon_search_url(book_title, author)
            filled += 1
            sources[source or "ol"] = sources.get(source or "ol", 0) + 1
            tag = f" [{source}]"
            if isbn10:
                tag += f" isbn10={isbn10}"
            print(f"[{processed}/{total}] ✓ cover{tag}  {book_title[:48]} / {author[:28]}")
        else:
            if isbn13:
                entry["isbn13"] = isbn13
            if isbn10 and re.fullmatch(r"[0-9X]{10}", str(isbn10), re.I):
                entry["isbn10"] = isbn10
                entry["amazon"] = amazon_dp_url(isbn10)
            else:
                asin = extract_asin(entry)
                if asin:
                    entry["amazon"] = amazon_dp_url(asin)
                else:
                    entry["amazon"] = amazon_search_url(book_title, author)
            missed += 1
            print(f"[{processed}/{total}] ✗ no cover  {book_title[:48]} / {author[:28]}")

        covers[slug] = entry

        if processed % CHECKPOINT_EVERY == 0:
            save(covers)
            c_total = sum(1 for v in covers.values() if v and v.get("cover"))
            print(
                f"  → checkpoint: {c_total} covers "
                f"({filled} filled, {missed} missed) sources={sources}"
            )

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
    print(f"  Sources: {sources}")


if __name__ == "__main__":
    main()
