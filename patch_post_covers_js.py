#!/usr/bin/env python3
"""Replace client-side Google/OL cover scripts with covers.json lookup."""
from pathlib import Path
import re

NEW = r"""<script>
/* BookGateway post cover v2 — prefer covers.json, float beside text */
(function(){
  var h1 = document.querySelector('.post-full h1');
  var pc = document.querySelector('.post-content');
  if (!h1 || !pc) return;
  if (pc.querySelector('.book-cover-img')) return;
  var title = h1.textContent.trim();
  var path = (location.pathname || '').replace(/\\/g, '/');
  var slugMatch = path.match(/\/posts\/([^\/]+?)(?:\.html)?$/);
  var slug = slugMatch ? slugMatch[1] : '';
  if (!slug) {
    var canon = document.querySelector('link[rel="canonical"]');
    if (canon) {
      var href = canon.getAttribute('href') || '';
      var m2 = href.match(/\/posts\/([^\/]+?)(?:\.html)?$/);
      if (m2) slug = m2[1];
    }
  }
  function insertCover(src) {
    if (!src || pc.querySelector('.book-cover-img')) return;
    var img = document.createElement('img');
    img.src = src;
    img.alt = title;
    img.className = 'book-cover-img';
    img.loading = 'lazy';
    img.onerror = function(){ this.style.display = 'none'; };
    pc.insertBefore(img, pc.firstChild);
  }
  fetch('/covers.json?v=' + Date.now())
    .then(function(r){ return r.json(); })
    .then(function(data){
      var entry = data && data[slug];
      if (entry && entry.cover) insertCover(entry.cover);
    })
    .catch(function(){});
})();
</script>"""

pat = re.compile(
    r"<script>\s*\(function\(\)\{.*?insertCover.*?\}\)\(\);\s*</script>",
    re.S,
)

n = 0
already = 0
failed = []
for path in sorted(Path("posts").glob("*.html")):
    if path.name == "test.html":
        continue
    text = path.read_text(encoding="utf-8", errors="ignore")
    if "post cover v2" in text:
        already += 1
        continue
    m = pat.search(text)
    if not m:
        failed.append(path.name)
        continue
    path.write_text(text[: m.start()] + NEW + text[m.end() :], encoding="utf-8")
    n += 1

print(f"replaced={n} already={already} failed={len(failed)}")
if failed:
    print("failed sample:", failed[:15])
