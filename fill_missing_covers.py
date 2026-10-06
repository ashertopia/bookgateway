#!/usr/bin/env python3
"""
Fill missing book covers: Amazon CDN first (verified ISBN-10 / ASIN), then Google Books.
Open Library is metadata-only (ISBN / id_amazon) — never used as an image source.
Requires author match — never accepts title-only fallbacks.
Uses HTML-parsed title/author/publisher from book_meta.
Affiliate tag: bookgateway02-20
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
DELAY = 1.0
GB_DELAY = 1.5
CHECKPOINT_EVERY = 12
UA = "BookGateway/3.4 (fill-missing-covers)"

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
ISBN10_RE = re.compile(r"^[0-9X]{10}$", re.I)
ASIN_LIKE_RE = re.compile(r"^[A-Z0-9]{10}$", re.I)


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


def pick_asin(amazon_ids, isbn10=None):
    """Prefer numeric ISBN-10 style ASIN; else first B0… Kindle ASIN."""
    ids = [str(a).upper() for a in (amazon_ids or []) if ASIN_LIKE_RE.match(str(a))]
    if isbn10 and ISBN10_RE.match(isbn10):
        return isbn10.upper()
    for a in ids:
        if ISBN10_RE.match(a):
            return a
    for a in ids:
        if a.startswith("B"):
            return a
    return ids[0] if ids else None


def clean_author(author: str) -> str:
    if not author:
        return ""
    a = author.strip()
    a = ROLE_PREFIX.sub("", a)
    a = GIVEAWAY_PAREN.sub("", a).strip()
    a = re.sub(r"\s*\(Give Away!?\)\s*", "", a, flags=re.I).strip()
    a = re.sub(r"\s*\(.*\)\s*$", "", a).strip()
    if len(a) > 90 or PROSE_HINT.search(a):
        return ""
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
    if book_title and (
        len(book_title) > 100
        or PROSE_HINT.search(book_title)
        or book_title.lower().endswith(" review")
    ):
        if card_title and not card_title.lower().endswith(" review"):
            book_title = card_title

    if not author and card_author:
        author = card_author
    if card_author and (not author or len(card_author) < len(author) * 0.5):
        if author and author_match(card_author, [author]):
            pass
        elif not author:
            author = card_author

    return book_title, author, publisher


# Global cooldown timestamps (epoch) after 429s
_RATE_COOLDOWN = {"gb": 0.0, "ol": 0.0}


def fetch_json(url, retries=3, kind="ol"):
    """Fetch JSON; on 429 set a cooldown and bail instead of multi-minute stalls."""
    now = time.time()
    cool = _RATE_COOLDOWN.get(kind, 0.0)
    if cool > now:
        # Still in cooldown — skip without waiting
        return None

    for attempt in range(retries):
        try:
            req = Request(url, headers={"User-Agent": UA})
            with urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except HTTPError as e:
            if e.code == 429:
                wait = min(90, 20 * (2 ** attempt))
                _RATE_COOLDOWN[kind] = time.time() + wait
                print(f"  Rate limited ({kind} {e.code}), cooldown {wait}s — skipping source")
                return None
            elif e.code in (500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
            else:
                print(f"  HTTP {e.code} for {url[:90]}")
                return None
        except (URLError, TimeoutError, OSError) as e:
            print(f"  fetch error: {e}")
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            print(f"  fetch error: {e}")
            time.sleep(1)
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
                    if ctype.startswith("image/gif") and len(data) < min_bytes:
                        continue
                if ctype.startswith("image/") or "octet-stream" in ctype or not ctype:
                    return True
        except Exception:
            continue
    return False


def title_variants(title: str) -> list[str]:
    """Generate alternate titles for stubborn lookups."""
    variants = []
    t = (title or "").strip()
    if not t:
        return variants
    variants.append(t)
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
    for sep in (":", "—", "–", " - "):
        if sep in t:
            variants.append(t.split(sep, 1)[0].strip())
    # Drop edition/volume noise
    t2 = re.sub(r"\s*\((?:revised|updated|expanded|anniversary)[^)]*\)\s*", "", t, flags=re.I)
    if t2 != t:
        variants.append(t2.strip())
    seen = set()
    out = []
    for v in variants:
        key = v.lower()
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out


def score_ol_doc(title, author, publisher, doc):
    """Score OL doc for metadata. Cover URL ignored by callers (policy)."""
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

    isbns = doc.get("isbn") or []
    i13, i10 = pick_isbns(isbns)
    amz_ids = doc.get("id_amazon") or []
    asin = pick_asin(amz_ids, i10)
    score = tscore + pscore + (0.05 if i10 else 0) + (0.04 if asin else 0)
    return (score, i13, i10, asin, doc_title, doc_authors)


def open_library_meta(title, author="", publisher=""):
    """
    Author-verified OL search for ISBN-13/10 + Amazon ASIN only.
    Never returns an OL cover URL (library photos rejected by policy).
    Returns (isbn13, isbn10, asin, extra_ids) where extra_ids is a list of
    alternate ISBN-10 / ASIN candidates to try on Amazon CDN.
    """
    if not title or not author:
        return None, None, None, []

    attempts = []
    for tv in title_variants(title)[:3]:
        attempts.append({
            "title": tv,
            "author": author,
            "limit": "12",
            "fields": "isbn,title,author_name,publisher,id_amazon",
        })
    attempts.append({
        "q": f"{title} {author}",
        "limit": "12",
        "fields": "isbn,title,author_name,publisher,id_amazon",
    })

    scored_docs = []
    for params in attempts:
        if _RATE_COOLDOWN.get("ol", 0) > time.time():
            break
        data = fetch_json("https://openlibrary.org/search.json?" + urlencode(params), kind="ol")
        if not data:
            if _RATE_COOLDOWN.get("ol", 0) > time.time():
                break
            time.sleep(0.3)
            continue
        time.sleep(DELAY)
        for doc in data.get("docs", []):
            scored = score_ol_doc(title, author, publisher, doc)
            if not scored:
                qt = params.get("title") or title
                if qt != title:
                    scored = score_ol_doc(qt, author, publisher, doc)
            if scored:
                scored_docs.append((scored, doc))
        if scored_docs and max(s[0][0] for s in scored_docs) >= 0.9:
            # keep searching one more query for alternate ISBNs, then stop
            if params.get("q"):
                break

    if not scored_docs:
        return None, None, None, []

    scored_docs.sort(key=lambda x: x[0][0], reverse=True)
    best = scored_docs[0][0]
    # Collect alternate ISBN-10 / ASINs from top matching docs
    extras = []
    seen = set()
    for scored, doc in scored_docs[:8]:
        if scored[0] < 0.55:
            continue
        i13, i10 = pick_isbns(doc.get("isbn") or [])
        for cand in [i10] + list(doc.get("id_amazon") or []):
            if not cand:
                continue
            c = str(cand).upper()
            if not ASIN_LIKE_RE.match(c) or c in seen:
                continue
            seen.add(c)
            extras.append(c)
        # Also convert any 978 ISBN-13s in the list
        for raw in (doc.get("isbn") or [])[:12]:
            c = clean_isbn(raw)
            if len(c) == 13 and c.startswith("978"):
                i10b = isbn13_to_isbn10(c)
                if i10b and i10b.upper() not in seen:
                    seen.add(i10b.upper())
                    extras.append(i10b.upper())

    return best[1], best[2], best[3], extras


# Back-compat alias used by upgrade_covers.py
def open_library_search(title, author="", publisher=""):
    """Legacy: returns (cover_url=None, isbn13, isbn10). Cover always None."""
    i13, i10, _asin, _extras = open_library_meta(title, author, publisher)
    return None, i13, i10


def google_books_search(title, author="", publisher=""):
    """Author-verified Google Books lookup. Returns (cover, isbn13, isbn10)."""
    if not title or not author:
        return None, None, None

    author_q = author.split(",")[0].split(" and ")[0].strip()
    queries = [
        f'intitle:"{title}" inauthor:"{author_q}"',
        f"{title} {author}",
    ]
    # Shorter title variant for stubborn matches
    for sep in (":", "—", "–"):
        if sep in title:
            queries.append(f'intitle:"{title.split(sep, 1)[0].strip()}" inauthor:"{author_q}"')
            break

    best = None
    for q in queries:
        if _RATE_COOLDOWN.get("gb", 0) > time.time():
            break
        params = urlencode({
            "q": q,
            "maxResults": "8",
            "printType": "books",
            "fields": (
                "items(volumeInfo(title,authors,publisher,imageLinks,"
                "industryIdentifiers))"
            ),
        })
        data = fetch_json(f"https://www.googleapis.com/books/v1/volumes?{params}", kind="gb")
        if not data or "items" not in data:
            if _RATE_COOLDOWN.get("gb", 0) > time.time():
                break
            time.sleep(0.3)
            continue
        time.sleep(GB_DELAY)
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
                src = src.replace("http://", "https://")
                src = re.sub(r"zoom=\d", "zoom=2", src)
                if "zoom=" not in src:
                    src = src + ("&" if "?" in src else "?") + "zoom=2"

            isbn13 = isbn10 = None
            for ident in vi.get("industryIdentifiers") or []:
                if ident.get("type") == "ISBN_13":
                    isbn13 = ident.get("identifier")
                if ident.get("type") == "ISBN_10":
                    isbn10 = ident.get("identifier")
            if isbn13 and not isbn10:
                isbn10 = isbn13_to_isbn10(isbn13)

            pscore = 0.1 if publisher and doc_pub and publisher_match(publisher, doc_pub) else 0
            score = tscore + pscore + (0.05 if src else 0) + (0.05 if isbn10 else 0)
            cand = (score, src, isbn13, isbn10)
            if best is None or score > best[0]:
                best = cand
        if best and best[0] >= 0.9 and best[1]:
            break

    if not best:
        return None, None, None
    if not best[1]:
        return None, best[2], best[3]
    return best[1], best[2], best[3]


def try_amazon_asin_cover(asin: str) -> str | None:
    """Try Amazon CDN product images for a known ASIN/ISBN10."""
    if not asin or len(asin) != 10:
        return None
    asin = asin.upper()
    candidates = [
        f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01._SCLZZZZZZZ_.jpg",
        f"https://images-na.ssl-images-amazon.com/images/P/{asin}.01.MAIN._SCRM_.jpg",
        f"https://m.media-amazon.com/images/P/{asin}.01.LZZZZZZZ.jpg",
    ]
    for url in candidates:
        if url_ok(url, min_bytes=2500):
            return url
        time.sleep(0.12)
    return None


def try_cover_by_isbn(isbn10=None, isbn13=None):
    """Amazon CDN only — never Open Library cover URLs."""
    if isbn10 and not str(isbn10).upper().startswith("B"):
        cover = try_amazon_asin_cover(isbn10)
        if cover:
            return cover
    if isbn13:
        i10 = isbn13_to_isbn10(clean_isbn(isbn13))
        if i10:
            return try_amazon_asin_cover(i10)
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
                "User-Agent": "Mozilla/5.0 (compatible; BookGateway/3.4)",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urlopen(req, timeout=18) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  ASIN page fetch failed ({asin}): {e}")
        return True

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
    byline = ""
    m = re.search(r'id="bylineInfo"[^>]*>(.*?)</div>', html, re.I | re.S)
    if m:
        byline = re.sub(r"<[^>]+>", " ", m.group(1))
        byline = re.sub(r"\s+", " ", byline).strip()

    t_ok = any(title_similarity(title, s) >= 0.5 for s in signals) if signals else False
    if not t_ok and signals:
        nt = set(re.findall(r"[a-z0-9]+", title.lower()))
        ns = set(re.findall(r"[a-z0-9]+", page_blob))
        if nt and len(nt & ns) / len(nt) >= 0.5:
            t_ok = True

    a_ok = author_match(author, [byline]) if byline else False
    if not a_ok:
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
    return True


def search_amazon_asin(title: str, author: str) -> str | None:
    """
    Discover an ASIN via Amazon search HTML when no ISBN is known.
    Verifies candidate /dp/ links against title+author before accepting.
    """
    if not title or not author:
        return None
    q = f"{title} {author}"
    url = f"https://www.amazon.com/s?k={quote(q)}&i=stripbooks"
    try:
        req = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html",
            },
        )
        with urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  Amazon search failed: {e}")
        return None

    # Collect ordered unique ASINs from search result /dp/ links
    asins = []
    seen = set()
    for m in re.finditer(r"/dp/([A-Z0-9]{10})", html, re.I):
        a = m.group(1).upper()
        if a in seen:
            continue
        seen.add(a)
        asins.append(a)
        if len(asins) >= 6:
            break

    for asin in asins:
        if verify_amazon_asin(asin, title, author):
            return asin
        time.sleep(0.35)
    return None


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
    print("Policy: Amazon CDN (ISBN/ASIN) > Google Books zoom=2; never OL images")

    filled = missed = processed = 0
    sources = {"amazon_cdn": 0, "amazon_asin": 0, "gb": 0}

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
        asin = extract_asin(entry)

        if not book_title or not author:
            print(f"[{processed+1}/{total}] skip (no author/title) {book_title[:50]!r}")
            missed += 1
            processed += 1
            entry["amazon"] = entry.get("amazon") or amazon_search_url(book_title, author)
            covers[slug] = entry
            continue

        # 1) Existing ISBN-10 / ASIN → Amazon CDN
        if isbn10 and ISBN10_RE.match(str(isbn10)):
            cover = try_amazon_asin_cover(isbn10)
            if cover:
                source = "amazon_cdn"
                asin = asin or isbn10.upper()

        if not cover and asin:
            if verify_amazon_asin(asin, book_title, author):
                cover = try_amazon_asin_cover(asin)
                if cover:
                    source = "amazon_asin"
            time.sleep(0.2)

        # 2) Open Library metadata only (ISBN + id_amazon) — never OL image
        extras = []
        if not cover or not (isbn10 or asin):
            i13, i10, ol_asin, extras = open_library_meta(book_title, author, publisher)
            if i10 and not isbn10:
                isbn10 = i10
            if i13 and not isbn13:
                isbn13 = i13
            if ol_asin and not asin:
                asin = ol_asin

        # Try Amazon CDN across primary + alternate IDs from OL
        if not cover:
            candidates = []
            for c in [isbn10, asin] + list(extras):
                if not c:
                    continue
                cu = str(c).upper()
                if cu not in candidates and ASIN_LIKE_RE.match(cu):
                    candidates.append(cu)
            for cand in candidates[:8]:
                cover = try_amazon_asin_cover(cand)
                if cover:
                    if ISBN10_RE.match(cand):
                        isbn10 = cand
                        source = "amazon_cdn"
                    else:
                        asin = cand
                        source = "amazon_asin"
                    break
                time.sleep(0.08)

        # 3) Amazon search scrape for ASIN when still no cover
        #    (even if a dead ISBN was found — CDN may lack that edition)
        if not cover:
            found = search_amazon_asin(book_title, author)
            time.sleep(0.45)
            if found:
                cover = try_amazon_asin_cover(found)
                if cover:
                    asin = found
                    source = "amazon_asin"
                    if ISBN10_RE.match(found) and not isbn10:
                        isbn10 = found

        # 4) Google Books last (author-verified zoom=2); also try Amazon CDN from its ISBN
        if not cover:
            c, i13, i10 = google_books_search(book_title, author, publisher)
            if i10 and not isbn10:
                isbn10 = i10
            if i13 and not isbn13:
                isbn13 = i13
            if i10:
                am = try_amazon_asin_cover(i10)
                if am:
                    cover = am
                    source = "amazon_cdn"
                    isbn10 = i10
                    asin = asin or i10.upper()
            if not cover and c:
                cover = c
                source = "gb"

        processed += 1
        if cover:
            entry["cover"] = cover
            entry["cover_source"] = source
            if isbn13:
                entry["isbn13"] = isbn13
            if isbn10 and ISBN10_RE.match(str(isbn10)):
                entry["isbn10"] = isbn10
                entry["amazon"] = amazon_dp_url(isbn10)
            elif asin:
                entry["amazon"] = amazon_dp_url(asin)
                if ISBN10_RE.match(asin):
                    entry["isbn10"] = asin
            else:
                entry["amazon"] = amazon_search_url(book_title, author)
            filled += 1
            sources[source or "amazon_cdn"] = sources.get(source or "amazon_cdn", 0) + 1
            tag = f" [{source}]"
            if isbn10:
                tag += f" isbn10={isbn10}"
            elif asin:
                tag += f" asin={asin}"
            print(f"[{processed}/{total}] ✓ cover{tag}  {book_title[:48]} / {author[:28]}")
        else:
            if isbn13:
                entry["isbn13"] = isbn13
            if isbn10 and ISBN10_RE.match(str(isbn10)):
                entry["isbn10"] = isbn10
                entry["amazon"] = amazon_dp_url(isbn10)
            elif asin:
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
    print(f"  Affiliate tag: {AFFILIATE_TAG}")


if __name__ == "__main__":
    main()
