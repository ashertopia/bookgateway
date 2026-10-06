#!/usr/bin/env python3
"""Idempotent ad patcher for BookGateway static site.

- Ensures ads.txt / robots.txt exist (does not overwrite if already correct)
- Creates Asher Arcade house ad if missing
- Listing pages: replace 'Ad space' placeholder with 2nd AdSense unit;
  swap house ad (KeepsakeDrop vs Asher Arcade) by category rules
- Post pages: inject AdSense script + auto unit + house ad above genre box
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PUB = "ca-pub-2147479795144668"
PUB_ADS_TXT = "pub-2147479795144668"

ADSENSE_SCRIPT = (
    f'<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js'
    f'?client={PUB}" crossorigin="anonymous"></script>'
)

ADSENSE_UNIT = f"""  <div class="ad-unit">
    <div class="ad-unit-label">Advertisement</div>
    <ins class="adsbygoogle"
         style="display:block;min-height:100px;"
         data-ad-client="{PUB}"
         data-ad-format="auto"
         data-full-width-responsive="true"></ins>
    <script>(adsbygoogle = window.adsbygoogle || []).push({{}});</script>
  </div>"""

KD_HOUSE = """  <div class="ad-unit">
    <div class="ad-unit-label">Advertisement</div>
    <a href="https://keepsakedrop.com/?utm_source=bookgateway&utm_medium=sidebar&utm_campaign=house-ad"
       target="_blank" rel="noopener sponsored" style="display:block;line-height:0;text-decoration:none;">
      <img src="/ads/bookgateway_sidebar_ad.png"
           width="280" height="200"
           alt="KeepsakeDrop — guest wedding photos saved to one Google Drive folder. $39 flat. No guest app."
           style="border:0;border-radius:12px;display:block;width:100%;height:auto;" />
    </a>
  </div>"""

AA_HOUSE = """  <div class="ad-unit">
    <div class="ad-unit-label">Advertisement</div>
    <a href="https://www.asherarcade.com/?utm_source=bookgateway&utm_medium=sidebar&utm_campaign=house-ad"
       target="_blank" rel="noopener sponsored" style="display:block;line-height:0;text-decoration:none;">
      <img src="/ads/asher_arcade_sidebar_ad.png"
           width="280" height="200"
           alt="Asher Arcade — custom games for birthdays, weddings and showers. Ready in 7 days, from $39."
           style="border:0;border-radius:12px;display:block;width:100%;height:auto;" />
    </a>
  </div>"""

# Category slug stems that get Asher Arcade on category listing pages
ASHER_CAT_SLUGS = {
    "video-games",
    "asher-boys",
    "board-games",
    "tech",
    "movies-entertainment",
    "movie",
}

# Category display names / slugs that prefer Asher Arcade on posts
ASHER_CAT_NAMES = {
    "video games",
    "asher boys",
    "board games",
    "tech",
    "movies & entertainment",
    "movies and entertainment",
    "movie",
}

# Women-skewed / wedding-adjacent — KeepsakeDrop (also the default)
KD_CAT_NAMES = {
    "romance & chick lit",
    "romance and chick lit",
    "relationships",
    "arieltopia",
    "gift books",
    "historical fiction",
    "young adult",
}

# litRPG / gamer keywords for SF/Fantasy posts (slug or title)
GAMER_KW = re.compile(
    r"litrpg|lit-rpg|mmorpg|\bvr\b|virtual|dungeon|troll|panga|sentenced|"
    r"enhancer|leveled|apocalypse|\bonline\b|\bgame\b|\bgames\b|system\b",
    re.I,
)

SF_FANTASY_NAMES = {"science fiction", "sci-fi", "fantasy", "fantasy fiction"}

PLACEHOLDER_RE = re.compile(
    r'<div class="ad-unit"[^>]*>\s*'
    r'<div class="ad-unit-label">Advertisement</div>\s*'
    r'(?:<!--[^>]*-->\s*)?'
    r'<p style="color:#ccc;font-size:0\.8rem;">Ad space</p>\s*'
    r'</div>',
    re.I,
)

# Match either existing house ad block (KD or AA)
HOUSE_AD_RE = re.compile(
    r'<div class="ad-unit">\s*'
    r'<div class="ad-unit-label">Advertisement</div>\s*'
    r'<a href="https://(?:keepsakedrop\.com|www\.asherarcade\.com)/\?utm_source=bookgateway[^"]*"'
    r'[\s\S]*?</a>\s*'
    r'</div>',
    re.I,
)

ADSENSE_SCRIPT_RE = re.compile(
    r'<script\s+async\s+src="https://pagead2\.googlesyndication\.com/pagead/js/adsbygoogle\.js\?client=ca-pub-[0-9]+"[^>]*></script>\s*',
    re.I,
)

# Detect already-injected post ad block (marker: house ad OR adsbygoogle ins in sidebar before genre)
POST_HAS_ADS_RE = re.compile(
    r'<div class="sidebar">\s*<div class="ad-unit">',
    re.I,
)


def category_stem_from_path(path: Path) -> str | None:
    """e.g. romance-chick-lit-page-3.html -> romance-chick-lit; video-games.html -> video-games"""
    if path.parent.name != "category":
        return None
    name = path.stem  # without .html
    m = re.match(r"^(.+)-page-\d+$", name)
    return m.group(1) if m else name


def choose_house_for_listing(path: Path) -> str:
    """Return 'aa' or 'kd' for listing pages."""
    if path.name == "index.html" or path.name.startswith("page-"):
        return "kd"
    stem = category_stem_from_path(path)
    if stem and stem in ASHER_CAT_SLUGS:
        return "aa"
    return "kd"


def normalize_cat(name: str) -> str:
    return re.sub(r"\s+", " ", name.replace("&amp;", "&").strip().lower())


def choose_house_for_post(cats: list[str], slug: str, title: str) -> str:
    norms = [normalize_cat(c) for c in cats]
    # Explicit Asher categories win
    if any(c in ASHER_CAT_NAMES for c in norms):
        return "aa"
    # litRPG / gamer SF-Fantasy heuristic
    blob = f"{slug} {title}"
    if any(c in SF_FANTASY_NAMES for c in norms) and GAMER_KW.search(blob):
        return "aa"
    # Uncategorized posts with strong litRPG/gamer slug signals (no KD cats present)
    if not norms and GAMER_KW.search(slug):
        return "aa"
    return "kd"


def ensure_adsense_script(html: str) -> tuple[str, bool]:
    if "pagead2.googlesyndication.com/pagead/js/adsbygoogle.js" in html:
        return html, False
    # Insert after favicon link if present, else after <head>
    m = re.search(r'(<link rel="icon"[^>]*>\s*)', html, re.I)
    if m:
        pos = m.end()
        return html[:pos] + ADSENSE_SCRIPT + "\n" + html[pos:], True
    m = re.search(r"(<head>\s*)", html, re.I)
    if m:
        pos = m.end()
        return html[:pos] + ADSENSE_SCRIPT + "\n" + html[pos:], True
    return html, False


def replace_placeholder(html: str) -> tuple[str, bool]:
    if "Ad space" not in html:
        return html, False
    new, n = PLACEHOLDER_RE.subn(ADSENSE_UNIT, html, count=1)
    return new, n > 0


def swap_house_ad(html: str, which: str) -> tuple[str, bool]:
    target = AA_HOUSE if which == "aa" else KD_HOUSE
    # Already correct?
    if which == "aa" and "asher_arcade_sidebar_ad.png" in html and "asherarcade.com/?utm_source=bookgateway" in html:
        # Still normalize markup if old KD remnants? If AA present, OK.
        if "bookgateway_sidebar_ad.png" not in html:
            return html, False
    if which == "kd" and "bookgateway_sidebar_ad.png" in html and "keepsakedrop.com/?utm_source=bookgateway" in html:
        if "asher_arcade_sidebar_ad.png" not in html:
            return html, False
    m = HOUSE_AD_RE.search(html)
    if not m:
        return html, False
    new = html[: m.start()] + target + html[m.end() :]
    return new, new != html


def patch_listing(path: Path) -> dict:
    html = path.read_text(encoding="utf-8")
    orig = html
    changed = {}
    html, c = ensure_adsense_script(html)
    changed["script"] = c
    which = choose_house_for_listing(path)
    html, c = swap_house_ad(html, which)
    changed["house"] = c
    changed["house_which"] = which
    html, c = replace_placeholder(html)
    changed["placeholder"] = c
    if html != orig:
        path.write_text(html, encoding="utf-8")
        changed["wrote"] = True
    else:
        changed["wrote"] = False
    return changed


def extract_post_cats(html: str) -> list[str]:
    m = re.search(r'<div class="card-cats">(.*?)</div>', html, re.S | re.I)
    if not m:
        return []
    return re.findall(r">([^<>]+)</a>", m.group(1))


def extract_post_title(html: str) -> str:
    m = re.search(r"<h1>(.*?)</h1>", html, re.S | re.I)
    if not m:
        m = re.search(r"<title>(.*?)\s*-\s*BookGateway</title>", html, re.I)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def inject_post_sidebar_ads(html: str, which: str) -> tuple[str, bool]:
    if POST_HAS_ADS_RE.search(html):
        # Already has ads; maybe just swap house
        html2, c = swap_house_ad(html, which)
        return html2, c
    house = AA_HOUSE if which == "aa" else KD_HOUSE
    block = ADSENSE_UNIT + "\n" + house + "\n"
    # Prefer inserting right after <div class="sidebar">
    m = re.search(r'(<div class="sidebar">\s*)', html, re.I)
    if not m:
        return html, False
    # Insert before existing sidebar-box content
    new = html[: m.end()] + block + html[m.end() :]
    return new, True


def patch_post(path: Path, reviews_by_slug: dict) -> dict:
    html = path.read_text(encoding="utf-8")
    orig = html
    slug = path.stem
    cats = extract_post_cats(html)
    title = extract_post_title(html)
    if not cats and slug in reviews_by_slug:
        cats = reviews_by_slug[slug].get("categories") or []
        if not title:
            title = reviews_by_slug[slug].get("title") or ""
    which = choose_house_for_post(cats, slug, title)
    changed = {"house_which": which, "cats": cats}
    html, c = ensure_adsense_script(html)
    changed["script"] = c
    html, c = inject_post_sidebar_ads(html, which)
    changed["sidebar"] = c
    if html != orig:
        path.write_text(html, encoding="utf-8")
        changed["wrote"] = True
    else:
        changed["wrote"] = False
    return changed


def main() -> None:
    # Load reviews for category fallback
    reviews_by_slug = {}
    rj = ROOT / "reviews.json"
    if rj.exists():
        data = json.loads(rj.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for row in data:
                s = row.get("slug")
                if s:
                    reviews_by_slug[s] = row

    listing_files = [ROOT / "index.html"]
    listing_files += sorted(ROOT.glob("page-*.html"))
    listing_files += sorted((ROOT / "category").glob("*.html"))

    stats = {
        "listing_wrote": 0,
        "listing_placeholder": 0,
        "listing_house_swap": 0,
        "listing_aa": 0,
        "listing_kd": 0,
        "post_wrote": 0,
        "post_script": 0,
        "post_sidebar": 0,
        "post_aa": 0,
        "post_kd": 0,
        "post_with_adsense": 0,
    }

    for path in listing_files:
        if not path.exists():
            continue
        r = patch_listing(path)
        if r.get("wrote"):
            stats["listing_wrote"] += 1
        if r.get("placeholder"):
            stats["listing_placeholder"] += 1
        if r.get("house"):
            stats["listing_house_swap"] += 1
        if r.get("house_which") == "aa":
            stats["listing_aa"] += 1
        else:
            stats["listing_kd"] += 1

    posts_dir = ROOT / "posts"
    for path in sorted(posts_dir.glob("*.html")):
        r = patch_post(path, reviews_by_slug)
        if r.get("wrote"):
            stats["post_wrote"] += 1
        if r.get("script"):
            stats["post_script"] += 1
        if r.get("sidebar"):
            stats["post_sidebar"] += 1
        if r.get("house_which") == "aa":
            stats["post_aa"] += 1
        else:
            stats["post_kd"] += 1

    # Recount posts that now have adsbygoogle
    for path in posts_dir.glob("*.html"):
        t = path.read_text(encoding="utf-8")
        if "adsbygoogle" in t:
            stats["post_with_adsense"] += 1

    print("STATS", json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
