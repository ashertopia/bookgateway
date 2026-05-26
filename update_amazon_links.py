#!/usr/bin/env python3
"""
Update Amazon links on all post pages using isbn10 (direct /dp/ link) from covers.json.
Run after generate_covers.py.
"""
import json, re
from pathlib import Path

covers    = json.load(open("covers.json", encoding="utf-8"))
posts_dir = Path("posts")
updated   = 0

for fpath in sorted(posts_dir.glob("*.html")):
    slug  = fpath.stem
    entry = covers.get(slug)
    if not entry or not entry.get("amazon"):
        continue

    new_url = entry["amazon"]
    html    = fpath.read_text(encoding="utf-8", errors="ignore")
    orig    = html

    # Replace any amazon link already in the page
    html = re.sub(
        r'https://www\.amazon\.com/(?:s\?k=[^"&\s]*|dp/[A-Z0-9]+/?\?)[^"]*tag=bookgateway02-20',
        new_url,
        html
    )

    if html != orig:
        fpath.write_text(html, encoding="utf-8")
        updated += 1

print(f"Updated Amazon links on {updated} post pages")
