#!/usr/bin/env python3
"""
Parse bibliographic metadata from BookGateway review HTML.
Cache: book_meta.json  {slug: {title, author, publisher, series, date, source}}
"""
from __future__ import annotations

import html as html_lib
import json
import re
import unicodedata
from pathlib import Path
from difflib import SequenceMatcher

POSTS_DIR = Path("posts")
META_CACHE = Path("book_meta.json")
AFFILIATE_TAG = "bookgateway02-20"

MONTH_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{4}$",
    re.I,
)
YEAR_RE = re.compile(r"^(19|20)\d{2}$")
BY_RE = re.compile(
    r"^(?:written\s+by|illustrated\s+by|by)\s+(.+)$",
    re.I,
)
SERIES_RE = re.compile(
    r"\bBook\s*#?\s*\d|\bSeries\b|#\s*\d|–\s*Book|—\s*Book|- Book|\bVol\.?\s*\d",
    re.I,
)
STOP_AUTHOR = {
    "the", "a", "an", "and", "of", "dr", "mr", "mrs", "ms", "jr", "sr", "phd",
}


def normalize(s: str) -> str:
    s = (s or "").lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def tokens(s: str) -> set[str]:
    return {t for t in normalize(s).split() if t and t not in STOP_AUTHOR and len(t) > 1}


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    # one contains the other (series subtitles etc.)
    if na in nb or nb in na:
        return 0.92
    ta, tb = set(na.split()), set(nb.split())
    if not ta or not tb:
        return 0.0
    overlap = len(ta & tb) / max(len(ta), len(tb))
    seq = SequenceMatcher(None, na, nb).ratio()
    return max(overlap, seq)


def _fuzzy_token_hit(a: str, bset: set[str]) -> bool:
    if a in bset:
        return True
    if len(a) < 5:
        return False
    for b in bset:
        if abs(len(a) - len(b)) > 1 or len(b) < 5:
            continue
        if SequenceMatcher(None, a, b).ratio() >= 0.85:
            return True
    return False


def author_match(review_author: str, candidate_authors) -> bool:
    """
    Require author token overlap.
    Surname-only review titles (e.g. 'Kade') must appear in 'Savannah Kade'.
    Allows minor spelling variants (Yancy/Yancey) and accent folding via normalize.
    """
    if isinstance(candidate_authors, str):
        candidate_authors = [candidate_authors]
    cand_list = [c for c in (candidate_authors or []) if c]
    if not review_author or not cand_list:
        return False

    rev = tokens(review_author)
    if not rev:
        return False

    for cand in cand_list:
        ct = tokens(cand)
        if not ct:
            continue
        hits = sum(1 for t in rev if _fuzzy_token_hit(t, ct))
        if hits:
            if len(rev) >= 2:
                if hits / len(rev) >= 0.5:
                    return True
            else:
                return True
        cand_parts = normalize(cand).split()
        if cand_parts and _fuzzy_token_hit(cand_parts[-1], rev):
            return True
    return False


def publisher_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return False
    return bool(ta & tb) and (len(ta & tb) / min(len(ta), len(tb)) >= 0.4)


def title_from_card(card_title: str) -> tuple[str, str]:
    """Split 'Enhancer by Kane' → ('Enhancer', 'Kane')."""
    m = re.match(r"^(.+?)\s+by\s+(.+)$", card_title or "", re.I)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return (card_title or "").strip(), ""


def amazon_search_url(title: str = "", author: str = "") -> str:
    from urllib.parse import quote
    parts = [p for p in [title, author] if p]
    q = " ".join(parts).strip() or "book"
    return f"https://www.amazon.com/s?k={quote(q)}&tag={AFFILIATE_TAG}"


def amazon_dp_url(isbn10: str) -> str:
    return f"https://www.amazon.com/dp/{isbn10}/?tag={AFFILIATE_TAG}"


def _post_content_lines(html: str) -> list[str]:
    m = re.search(r'class="post-content"[^>]*>(.*?)</div>', html, re.S | re.I)
    if not m:
        return []
    chunk = m.group(1)
    chunk = re.sub(r"<br\s*/?>", "\n", chunk, flags=re.I)
    chunk = re.sub(r"</p\s*>", "\n", chunk, flags=re.I)
    chunk = re.sub(r"<[^>]+>", "", chunk)
    chunk = html_lib.unescape(chunk)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in chunk.splitlines()]
    return [ln for ln in lines if ln]


def parse_post_html(html: str, title_hint: str = "") -> dict | None:
    """
    Extract title/author/publisher/series/date from bibliographic block.
    title_hint: card/H1 book title (without 'by Author') to disambiguate.
    """
    lines = _post_content_lines(html)
    if not lines:
        return None

    hint_title, hint_author = title_from_card(title_hint) if title_hint else ("", "")
    # Also accept bare title hint
    if title_hint and not hint_title:
        hint_title = title_hint

    by_idx = None
    author = None
    for i, ln in enumerate(lines[:60]):
        bm = BY_RE.match(ln)
        if not bm:
            continue
        # skip illustrated-by if a written-by / by follows soon? Prefer first real by
        low = ln.lower()
        if low.startswith("illustrated by"):
            continue
        author = bm.group(1).strip()
        # strip trailing roles
        author = re.sub(r"\s*\(.*\)\s*$", "", author).strip()
        by_idx = i
        break

    if by_idx is None:
        # Fallback: Title / AuthorName / Publisher / Date (no 'by')
        # Look for short-line clusters early in content
        return _parse_no_by_block(lines, hint_title, hint_author)

    before = lines[max(0, by_idx - 4) : by_idx]
    after = lines[by_idx + 1 : by_idx + 5]

    # Pick title from before lines using hint
    title = None
    series = None
    candidates = [ln for ln in before if len(ln) < 120]
    if hint_title and candidates:
        scored = sorted(
            candidates,
            key=lambda ln: title_similarity(ln, hint_title),
            reverse=True,
        )
        if title_similarity(scored[0], hint_title) >= 0.5:
            title = scored[0]
        # series = other short line that looks like series
        for ln in candidates:
            if ln != title and SERIES_RE.search(ln):
                series = ln
                break
    if not title and candidates:
        # Prefer non-series line closest to by
        for ln in reversed(candidates):
            if SERIES_RE.search(ln):
                series = series or ln
                continue
            title = ln
            break
        if not title:
            title = candidates[-1]

    publisher = None
    date = None
    for a in after:
        if MONTH_RE.match(a) or YEAR_RE.match(a):
            date = a
            continue
        if BY_RE.match(a):
            continue
        if not publisher and 2 < len(a) < 80:
            publisher = a

    # Prefer fuller author from bib over surname hint
    if hint_author and author and not author_match(hint_author, [author]):
        # keep bib author; hint may be truncated surname that still matches
        if hint_author.lower() not in author.lower() and author.lower() not in hint_author.lower():
            # if no overlap at all, prefer bib
            pass

    if not title and hint_title:
        title = hint_title
    if not author and hint_author:
        author = hint_author

    if not title and not author:
        return None

    return {
        "title": title,
        "author": author,
        "publisher": publisher,
        "series": series,
        "date": date,
        "source": "html_bib",
    }


def _parse_no_by_block(lines, hint_title, hint_author):
    """Handle Title / Author / Publisher / Date without 'by'."""
    # Find a date line; walk backward for short lines
    for i, ln in enumerate(lines[:40]):
        if not (MONTH_RE.match(ln) or YEAR_RE.match(ln)):
            continue
        # expect: title, author, publisher, date OR title, publisher, date
        window = lines[max(0, i - 4) : i]
        short = [w for w in window if len(w) < 100]
        if len(short) < 2:
            continue
        date = ln
        publisher = short[-1] if short else None
        maybe_author = short[-2] if len(short) >= 2 else None
        maybe_title = short[-3] if len(short) >= 3 else short[0]

        # If hint_title matches one of the shorts, use that as title
        title = maybe_title
        author = maybe_author
        if hint_title:
            for s in short:
                if title_similarity(s, hint_title) >= 0.6:
                    title = s
                    break
        # Author line shouldn't look like a publisher corp name if we have hint
        if hint_author and author and not author_match(hint_author, [author]):
            # try other short lines
            for s in short:
                if author_match(hint_author, [s]):
                    author = s
                    break

        if title and (author or hint_author):
            return {
                "title": title,
                "author": author or hint_author,
                "publisher": publisher,
                "series": None,
                "date": date,
                "source": "html_noby",
            }
    return None


def parse_post_file(slug: str, title_hint: str = "") -> dict | None:
    path = POSTS_DIR / f"{slug}.html"
    if not path.exists():
        return None
    html = path.read_text(encoding="utf-8", errors="ignore")
    meta = parse_post_html(html, title_hint=title_hint)
    if meta:
        meta["slug"] = slug
    return meta


def load_meta_cache() -> dict:
    if META_CACHE.exists():
        return json.loads(META_CACHE.read_text(encoding="utf-8"))
    return {}


def save_meta_cache(cache: dict) -> None:
    META_CACHE.write_text(
        json.dumps(cache, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def build_meta_cache(reviews: list, force: bool = False) -> dict:
    cache = {} if force else load_meta_cache()
    for r in reviews:
        slug = r["slug"]
        if not force and slug in cache and cache[slug].get("author"):
            continue
        hint = r.get("title", "")
        meta = parse_post_file(slug, title_hint=hint)
        if not meta:
            t, a = title_from_card(hint)
            meta = {
                "title": t or hint,
                "author": a,
                "publisher": None,
                "series": None,
                "date": None,
                "source": "card_title",
                "slug": slug,
            }
        else:
            # If bib title looks wrong vs hint, prefer hint title but keep bib author
            t_hint, a_hint = title_from_card(hint)
            if t_hint and meta.get("title"):
                if title_similarity(meta["title"], t_hint) < 0.45:
                    meta["title"] = t_hint
            if a_hint and not meta.get("author"):
                meta["author"] = a_hint
            # Prefer full bib author over surname-only card author always
        cache[slug] = meta
    save_meta_cache(cache)
    return cache


def extract_youtube_id(html: str) -> str | None:
    m = re.search(
        r"(?:youtube\.com/embed/|youtu\.be/)([A-Za-z0-9_-]{6,})",
        html,
    )
    return m.group(1) if m else None


if __name__ == "__main__":
    import sys

    reviews = json.loads(Path("reviews.json").read_text(encoding="utf-8"))
    cache = build_meta_cache(reviews, force=True)
    with_author = sum(1 for v in cache.values() if v.get("author"))
    print(f"Cached {len(cache)} entries, {with_author} with author")
    for slug in ("enhancer-by-kane", "ask-me-to-stay-by-kade", "12-rules-for-life-by-peterson", "rebel-by-kade"):
        print(slug, cache.get(slug))
