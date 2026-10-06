#!/usr/bin/env python3
"""Idempotent layout patcher for BookGateway review pages (posts/*.html).

What it does on every post:
  1. Replaces the giant 'Browse by Genre' sidebar list with a statically
     generated 'More like this' box (4 related reviews with cover thumbnails)
     plus a compact 'Popular genres' pill row.
  2. Adds a book aside at the top of the review: cover + understated
     'Buy on Amazon' button + quiet Amazon Associates disclosure
     (book reviews only).
  3. Replaces the old cream 'Find on Amazon' box at the end of the review with
     a plain secondary text link.

All generated regions are wrapped in <!-- bg:NAME:start --> / <!-- bg:NAME:end -->
markers so re-running the script just regenerates them. Run patch_ads.py
afterwards (it is a no-op when ads are already present).

Usage: python3 patch_layout.py [--report related_report.json]
"""
from __future__ import annotations

import hashlib
import html as htmlmod
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
POSTS = ROOT / "posts"
TAG = "bookgateway02-20"
REL_AMAZON = 'target="_blank" rel="sponsored nofollow noopener"'
DISCLOSURE = "Paid link"
N_RELATED = 4
N_GENRES = 8

META_CATS = {
    "Featured", "Uncategorized", "Giveaway", "Preview", "Reviewers",
    "Matthew Scott", "Becky Freyenhagen", "Myra Ovalle", "Brittney Dodson",
    "Nicole L. Wright", "Patrick Tierney", "Michael Krauszer", "Arieltopia",
    "Sunshine", "Asher Boys", "Fantasy, Fiction, Scott Asher",
}
GAME_CATS = {"Video Games", "Board Games"}
MEDIA_CATS = {"Movies & Entertainment", "Movie", "Tech", "Family Fun"}
EVENT_RE = re.compile(r"booky|live-blog|showcase|comic-con|^interview", re.I)
BUYBOX_KINDS = {"book", "other"}

GENRE_BOX_RE = re.compile(
    r'<div class="sidebar-box"><h3>Browse by Genre</h3><ul>.*?</ul></div>', re.S)
OLD_BUY_RE = re.compile(r'<div class="amazon-btn-wrap">.*?</div>', re.S)
OLD_BUY_HREF_RE = re.compile(r'<a href="([^"]+)" class="amazon-btn"')
BUYLINE_HREF_RE = re.compile(r'<p class="buy-line">[^<]*<a href="([^"]+)"')


def region(name: str, body: str) -> str:
    return f"<!-- bg:{name}:start -->{body}<!-- bg:{name}:end -->"


def region_re(name: str) -> re.Pattern:
    return re.compile(rf"<!-- bg:{name}:start -->.*?<!-- bg:{name}:end -->", re.S)


def esc(s: str) -> str:
    return htmlmod.escape(s, quote=True)


def cat_slug(name: str) -> str:
    s = name.lower().replace("&", " ")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def real_cats(cats: list[str]) -> list[str]:
    return [c for c in cats if c not in META_CATS]


def author_key(slug: str) -> str | None:
    if "-by-" not in slug:
        return None
    return slug.rsplit("-by-", 1)[1] or None


def split_title(title: str, slug: str) -> tuple[str, str]:
    """'Lucky by Kade' -> ('Lucky', 'Kade') when the slug carries -by-<author>."""
    title = re.sub(r"(?i)\s*(?:\band\s+|-\s*)?giveaway!*\s*$", "", title).strip() or title
    title = re.sub(r"(?i)^review:\s*", "", title)
    if author_key(slug) and " by " in title:
        t, a = title.rsplit(" by ", 1)
        if t.strip() and a.strip():
            return t.strip(), a.strip()
    return title.strip(), ""


def ensure_tag(url: str) -> str:
    if TAG in url:
        return url
    url = re.sub(r"([?&])tag=[^&]*", rf"\1tag={TAG}", url)
    if TAG in url:
        return url
    return url + ("&" if "?" in url else "?") + f"tag={TAG}"


def is_real_cover(url: str | None) -> bool:
    return bool(url) and "logo" not in url


def stable(*parts: str) -> str:
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def kind_of(row: dict, cov: dict) -> str:
    cats = set(row.get("categories") or [])
    if cats & GAME_CATS:
        return "game"
    if cats & MEDIA_CATS or cov.get("youtube_id"):
        return "media"
    if "Interviews" in cats or EVENT_RE.search(row["slug"]):
        return "event"   # interviews, award round-ups, con live blogs: no product
    if cov.get("non_book"):
        return "other"   # no confident ISBN match (mostly books, some CDs/giveaways)
    return "book"


def build_index():
    reviews = json.loads((ROOT / "reviews.json").read_text(encoding="utf-8"))
    covers = json.loads((ROOT / "covers.json").read_text(encoding="utf-8"))
    idx = {}
    for row in reviews:
        slug = row["slug"]
        cov = covers.get(slug) or {}
        cats = real_cats(row.get("categories") or [])
        amazon = cov.get("amazon") or ""
        idx[slug] = {
            "slug": slug,
            "title": htmlmod.unescape(row.get("title") or slug),
            "cats": cats,
            "primary": cats[0] if cats else None,
            "author": author_key(slug),
            "cover": cov.get("cover") if is_real_cover(cov.get("cover")) else None,
            "dp": amazon if "/dp/" in amazon else None,
            "kind": kind_of(row, cov),
            "date": row.get("date") or "",
            "asher": "Asher Boys" in (row.get("categories") or []),
        }
    # genre counts for the pill row (non-meta, non-game categories)
    counts = Counter(c for row in reviews for c in (row.get("categories") or []))
    genres = [c for c, _ in counts.most_common()
              if c not in META_CATS and c not in GAME_CATS and c not in MEDIA_CATS
              and (ROOT / "category" / f"{cat_slug(c)}.html").exists()][:N_GENRES]
    return idx, genres


# Which kinds of posts may be suggested for a given source kind.
#   POOL_FOR: candidates allowed when related by author or category.
#   FALLBACK_FOR: candidates allowed as filler when nothing closer exists.
# Book reviews never suggest game/video posts; game posts only suggest games.
POOL_FOR = {"book": ("book",), "other": ("book",), "event": ("book",), "game": ("game",),
            "media": ("media", "game", "book")}
FALLBACK_FOR = {"book": ("book",), "other": ("book",), "event": ("book",), "game": ("game",),
                "media": ("media", "game")}
KIND_RANK = {"media": 2, "game": 1, "book": 0, "other": 0, "event": 0}


def pick_related(src: dict, idx: dict) -> list[tuple[dict, str]]:
    kinds = POOL_FOR[src["kind"]]
    fallback_kinds = FALLBACK_FOR[src["kind"]]
    src_title = src["title"].lower()
    scored = []
    for c in idx.values():
        if c["slug"] == src["slug"] or not c["cover"] or c["kind"] not in kinds:
            continue
        if c["title"].lower() == src_title:
            continue
        same_author = bool(src["author"]) and c["author"] == src["author"]
        if src["primary"] and c["primary"] == src["primary"]:
            cat_score = 3
        elif src["primary"] and src["primary"] in c["cats"]:
            cat_score = 2
        elif set(src["cats"]) & set(c["cats"]):
            cat_score = 1
        else:
            cat_score = 0
        family = src["asher"] and c["asher"]  # Asher Boys family channel posts
        if same_author:
            reason = "author"
        elif cat_score:
            reason = "category"
        elif family and c["kind"] != "book":
            reason = "asher-boys"
        elif c["kind"] in fallback_kinds:
            reason = "fallback"
        else:
            continue
        if c["kind"] == "book" and src["kind"] == "media" and reason not in ("author", "category"):
            continue
        kind_score = KIND_RANK[c["kind"]] if src["kind"] == "media" else 0
        key = (same_author, cat_score, family, kind_score, bool(c["dp"]),
               stable(src["slug"], c["slug"]))
        scored.append((key, c, reason))
    scored.sort(key=lambda t: t[0], reverse=True)
    out, seen_covers = [], set()
    for _, c, reason in scored:
        if c["cover"] in seen_covers:
            continue
        seen_covers.add(c["cover"])
        out.append((c, reason))
        if len(out) == N_RELATED:
            break
    return out


def related_html(src: dict, idx: dict, genres: list[str]) -> tuple[str, list]:
    picks = pick_related(src, idx)
    parts = []
    if picks:
        items = []
        for c, _ in picks:
            t, a = split_title(c["title"], c["slug"])
            meta = f"by {a}" if a else (c["primary"] or "")
            meta_html = f'<span class="related-meta">{esc(meta)}</span>' if meta else ""
            items.append(
                f'<li><a class="related-item" href="{esc(c["slug"])}.html">'
                f'<img class="related-thumb" src="{esc(c["cover"])}" alt="" width="52" height="78" '
                f'loading="lazy" decoding="async" onerror="this.style.visibility=\'hidden\'">'
                f'<span class="related-text"><span class="related-title">{esc(t)}</span>{meta_html}</span></a></li>'
            )
        parts.append('<div class="sidebar-box related-box"><h3>More like this</h3>'
                     '<ul class="related-list">' + "".join(items) + "</ul></div>")
    pills = "".join(f'<a href="../category/{cat_slug(g)}.html">{esc(g)}</a>' for g in genres)
    parts.append('<div class="sidebar-box genre-box"><h3>Popular genres</h3>'
                 f'<div class="genre-pills">{pills}</div></div>')
    return region("related", "".join(parts)), picks


def current_amazon_href(html: str) -> str | None:
    m = OLD_BUY_HREF_RE.search(html) or BUYLINE_HREF_RE.search(html)
    return htmlmod.unescape(m.group(1)) if m else None


def patch_post(path: Path, idx: dict, genres: list[str], report: dict) -> bool:
    html = path.read_text(encoding="utf-8")
    orig = html
    slug = path.stem
    src = idx.get(slug)
    if not src or '<div class="post-content">' not in html:
        report["skipped"].append(slug)
        return False

    # ---- 1. sidebar: related + genre pills -------------------------------
    block, picks = related_html(src, idx, genres)
    if region_re("related").search(html):
        html = region_re("related").sub(lambda m: block, html, count=1)
    elif GENRE_BOX_RE.search(html):
        html = GENRE_BOX_RE.sub(lambda m: block, html, count=1)
    else:
        report["no_sidebar_slot"].append(slug)
    report["related"][slug] = {"n": len(picks), "kind": src["kind"],
                               "reasons": [r for _, r in picks]}

    # ---- 2/3. Amazon links ---------------------------------------------
    page_href = current_amazon_href(html)
    href = src["dp"] or page_href
    title_main, _ = split_title(src["title"], slug)
    if href:
        href = ensure_tag(href)
    h = esc(href) if href else ""

    # top aside (cover + buy box) for book reviews
    aside = ""
    if src["kind"] in BUYBOX_KINDS and (src["cover"] or href):
        inner = ""
        if src["cover"]:
            inner += (f'<img class="book-cover-img" src="{esc(src["cover"])}" alt="Cover of {esc(title_main)}" '
                      f'width="180" height="270" decoding="async" onerror="this.style.display=\'none\'">')
        if href:
            inner += (f'<div class="buy-box"><a class="buy-btn" href="{h}" {REL_AMAZON}>Buy on Amazon</a>'
                      f'<p class="buy-disclosure">{DISCLOSURE}</p></div>')
        cls = "book-aside" + ("" if src["cover"] else " no-cover")
        aside = region("bookaside", f'<aside class="{cls}">{inner}</aside>')
    if region_re("bookaside").search(html):
        html = region_re("bookaside").sub(lambda m: aside, html, count=1)
    elif aside:
        html = html.replace('<div class="post-content">', '<div class="post-content">' + aside, 1)

    # end-of-review secondary line
    line = ""
    if href:
        if src["kind"] in BUYBOX_KINDS:
            text = f'Enjoyed this review? <a href="{h}" {REL_AMAZON}>Buy <cite>{esc(title_main)}</cite> on Amazon&nbsp;&rarr;</a>'
        else:
            text = f'<a href="{h}" {REL_AMAZON}>Search Amazon for <cite>{esc(title_main)}</cite>&nbsp;&rarr;</a>'
        line = region("buyline", f'<p class="buy-line">{text}</p>')
    if region_re("buyline").search(html):
        html = region_re("buyline").sub(lambda m: line, html, count=1)
    elif OLD_BUY_RE.search(html):
        html = OLD_BUY_RE.sub(lambda m: line, html, count=1)

    if not href:
        report["no_amazon"].append(slug)
    if html != orig:
        path.write_text(html, encoding="utf-8")
        return True
    return False


def main() -> None:
    idx, genres = build_index()
    report = {"skipped": [], "no_sidebar_slot": [], "no_amazon": [], "related": {}}
    wrote = 0
    for path in sorted(POSTS.glob("*.html")):
        wrote += patch_post(path, idx, genres, report)
    short = {s: r for s, r in report["related"].items() if r["n"] < N_RELATED}
    print(f"posts written: {wrote}")
    print(f"popular genres: {genres}")
    print(f"skipped: {report['skipped']}")
    print(f"no sidebar slot: {report['no_sidebar_slot']}")
    print(f"posts without any Amazon link: {len(report['no_amazon'])}")
    print(f"posts with < {N_RELATED} related: {len(short)}")
    for s, r in sorted(short.items()):
        print(f"  {s}: {r['n']} ({r['kind']})")
    reasons = Counter(x for r in report["related"].values() for x in r["reasons"])
    print(f"related link reasons: {dict(reasons)}")
    if "--report" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--report") + 1])
        out.write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
