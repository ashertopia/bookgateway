#\!/usr/bin/env python3
"""
social_post_generator.py — BookGateway.com Social Media Post Generator
=======================================================================
Reads reviews.json, selects the highest-value unposted review (prioritizing
Romance, Thriller, Fantasy, and Historical Fiction), generates platform-specific
post copy for Instagram and Pinterest, writes output to social_queue/YYYY-MM-DD.json,
updates social_posted.json, and prints everything to stdout.

Usage:
    python social_post_generator.py [--category "Romance & Chick Lit"]

Designed to run inside the repo root (GitHub Actions context), so all file
paths are relative to the working directory.
"""

import json
import os
import sys
import argparse
import textwrap
from datetime import date, datetime
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

REVIEWS_FILE   = "reviews.json"
POSTED_FILE    = "social_posted.json"
QUEUE_DIR      = Path("social_queue")
AMAZON_TAG     = "bookgateway02-20"
SITE_URL       = "https://bookgateway.com"

# Priority order for category selection.
# Reviews whose primary (or best) category appears earlier are picked first.
CATEGORY_PRIORITY = [
    "Romance & Chick Lit",
    "Thriller & Suspense",
    "Fantasy",
    "Historical Fiction",
    "Science Fiction",
    "Children & Teens",
    "General Fiction",
    "Christian Living",
    "Graphic Novels",
]

# ---------------------------------------------------------------------------
# Hashtag sets — genre-specific, always appended with the site tags
# ---------------------------------------------------------------------------

SITE_TAGS = "#BookGateway #bookreviews"

HASHTAGS = {
    "Romance & Chick Lit": (
        "#bookstagram #romancebooks #romancereads #bookrecommendation "
        "#chicklit #bookclub"
    ),
    "Thriller & Suspense": (
        "#thrillerbooks #suspensereads #bookstagram #crimefiction "
        "#bookrecommendation"
    ),
    "Fantasy": (
        "#fantasybooks #fantasybookstagram #epicfantasy #bookrecommendation "
        "#bookstagram"
    ),
    "Historical Fiction": (
        "#historicalfiction #historicalromance #bookstagram #bookrecommendation"
    ),
    "Christian Living": (
        "#christianbooks #christianfiction #faithbooks #bookstagram"
    ),
    "Christian Fiction": (
        "#christianbooks #christianfiction #faithbooks #bookstagram"
    ),
    "Science Fiction": (
        "#scifibooks #sciencefiction #scifireads #bookstagram #bookrecommendation"
    ),
    "Children & Teens": (
        "#kidsbooks #yabooks #youngadult #bookstagram #bookrecommendation"
    ),
    "Graphic Novels": (
        "#graphicnovel #comicbooks #bookstagram #bookrecommendation #comicsofinstagram"
    ),
    "General Fiction": (
        "#bookstagram #bookrecommendation #bookclub #readingcommunity #booklovers"
    ),
    # Fallback — used when no known category matches
    "_default": (
        "#bookstagram #bookrecommendation #bookclub #readingcommunity #booklovers"
    ),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_json(path, default=None):
    """Load a JSON file, returning `default` if the file does not exist."""
    p = Path(path)
    if not p.exists():
        return default
    with p.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path, data, indent=2):
    """Write `data` as pretty-printed JSON to `path`."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=indent, ensure_ascii=False)


def priority_score(review):
    """
    Return a sort key for a review based on its categories.
    Lower score = higher priority (sorted ascending).
    Reviews not in CATEGORY_PRIORITY get score len(CATEGORY_PRIORITY).
    When a review belongs to multiple priority categories, the best
    (lowest) score wins.
    """
    cats = review.get("categories", [])
    scores = [
        CATEGORY_PRIORITY.index(c)
        for c in cats
        if c in CATEGORY_PRIORITY
    ]
    return min(scores) if scores else len(CATEGORY_PRIORITY)


def pick_best_hashtag_set(categories):
    """
    Return the most relevant hashtag block for the given category list.
    Uses CATEGORY_PRIORITY order to break ties.
    Always appends SITE_TAGS.
    """
    for cat in CATEGORY_PRIORITY:
        if cat in categories and cat in HASHTAGS:
            return "{} {}".format(HASHTAGS[cat], SITE_TAGS)
    # Fall back to first category that has a specific set
    for cat in categories:
        if cat in HASHTAGS:
            return "{} {}".format(HASHTAGS[cat], SITE_TAGS)
    return "{} {}".format(HASHTAGS["_default"], SITE_TAGS)


def truncate(text, max_len, suffix="…"):
    """Truncate `text` to at most `max_len` characters, adding `suffix`."""
    if len(text) <= max_len:
        return text
    return text[: max_len - len(suffix)].rstrip() + suffix


# ---------------------------------------------------------------------------
# Post generators
# ---------------------------------------------------------------------------

def generate_instagram(review):
    """
    Build an Instagram caption.

    Structure:
        [Hook — title with book emoji]
        [blank line]
        [Excerpt snippet — up to ~200 chars]
        [blank line]
        [CTA with review URL]
        [blank line]
        [hashtag line]

    Returns a dict with:
        caption      — body text above hashtags (copy into the caption field)
        hashtags     — hashtag line only (can post as first comment instead)
        full_caption — caption + hashtags combined
        char_count   — total length of full_caption
    """
    title      = review["title"]
    excerpt    = review.get("excerpt", "")
    url        = review.get("url", SITE_URL)
    categories = review.get("categories", [])

    hook    = "📖 {}".format(title)
    snippet = truncate(excerpt, 200)
    cta     = "Full review at BookGateway.com 👇\n{}".format(url)

    caption_body = "\n".join([hook, "", snippet, "", cta])
    hashtag_line = pick_best_hashtag_set(categories)
    full_caption = "{}\n\n{}".format(caption_body, hashtag_line)

    return {
        "caption":      caption_body,
        "hashtags":     hashtag_line,
        "full_caption": full_caption,
        "char_count":   len(full_caption),
    }


def generate_pinterest(review):
    """
    Build a Pinterest pin.

    Pinterest best practices:
    - Title:       keyword-rich, ≤ 100 chars
    - Description: 150–500 chars, natural language with keywords,
                   ends with a CTA and the review link.

    Returns a dict with pin_title, description, link_url, char_count.
    """
    title      = review["title"]
    excerpt    = review.get("excerpt", "")
    url        = review.get("url", SITE_URL)
    categories = review.get("categories", [])
    reviewer   = review.get("reviewer", "")

    genre_label  = categories[0] if categories else "fiction"
    pin_title    = truncate("Book Review: {}".format(title), 100)
    snippet      = truncate(excerpt, 300)

    if snippet and snippet[-1] not in ".\!?":
        snippet += "."

    reviewer_line = " {}.".format(reviewer) if reviewer else ""

    description = " ".join(filter(None, [
        snippet,
        "A {} book review from BookGateway.com.{}".format(genre_label, reviewer_line),
        "Click through for the full review and Amazon link → {}".format(url),
    ]))
    description = truncate(description, 500)

    return {
        "pin_title":   pin_title,
        "description": description,
        "link_url":    url,
        "char_count":  len(description),
    }


def generate_todays_pick(review):
    """
    Plain-text "today's pick" summary — useful for newsletters, Slack,
    team stand-ups, or reading the GitHub Actions job summary.
    """
    title      = review["title"]
    categories = ", ".join(review.get("categories", []))
    reviewer   = review.get("reviewer", "")
    date_str   = review.get("date", "")
    excerpt    = review.get("excerpt", "")
    url        = review.get("url", SITE_URL)
    amazon_url = review.get("amazon_url", "")

    lines = [
        "=" * 60,
        "TODAY'S BOOKGATEWAY PICK",
        "=" * 60,
        "Title    : {}".format(title),
        "Category : {}".format(categories),
        "Reviewer : {}".format(reviewer),
        "Date     : {}".format(date_str),
        "",
        "Excerpt:",
        textwrap.fill(excerpt, width=60) if excerpt else "(no excerpt)",
        "",
        "Review URL : {}".format(url),
    ]
    if amazon_url:
        lines.append("Amazon     : {}".format(amazon_url))
    lines.append("=" * 60)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core pipeline
# ---------------------------------------------------------------------------

def select_review(reviews, posted, category_filter=None):
    """
    Pick the highest-priority unposted review.

    1. Exclude already-posted slugs.
    2. If category_filter is set, restrict to reviews in that category.
       Falls back to all categories if no match is found.
    3. Sort by (priority_score ASC, date DESC) — newest high-priority first.
    4. Return the first result, or None if the pool is empty.
    """
    candidates = [r for r in reviews if r["slug"] not in posted]

    if category_filter:
        filtered = [
            r for r in candidates
            if category_filter.lower() in [c.lower() for c in r.get("categories", [])]
        ]
        if filtered:
            candidates = filtered
        else:
            print(
                "[warn] No unposted reviews found for category '{}'. "
                "Falling back to all categories.".format(category_filter),
                file=sys.stderr,
            )

    if not candidates:
        return None

    def sort_key(r):
        score = priority_score(r)
        # Negate date ordinal for descending date sort
        try:
            date_ord = datetime.strptime(r["date"], "%Y-%m-%d").toordinal()
        except (KeyError, ValueError):
            date_ord = 0
        return (score, -date_ord)

    candidates.sort(key=sort_key)
    return candidates[0]


def run(category_filter=None):
    """Main pipeline: load → select → generate → save → print."""

    # ------------------------------------------------------------------
    # 1. Load data
    # ------------------------------------------------------------------
    print("[info] Loading reviews from {} …".format(REVIEWS_FILE))
    reviews = load_json(REVIEWS_FILE)
    if not reviews:
        print("[error] Could not load {}. Aborting.".format(REVIEWS_FILE), file=sys.stderr)
        sys.exit(1)
    print("[info] Loaded {} reviews.".format(len(reviews)))

    posted = load_json(POSTED_FILE, default={})
    print("[info] {} reviews already posted.".format(len(posted)))

    # ------------------------------------------------------------------
    # 2. Select the next review
    # ------------------------------------------------------------------
    review = select_review(reviews, posted, category_filter=category_filter)
    if review is None:
        print("[info] All reviews have been posted\! Nothing to do.", file=sys.stderr)
        sys.exit(0)

    slug = review["slug"]
    print("[info] Selected: '{}' ({})".format(review["title"], slug))

    # ------------------------------------------------------------------
    # 3. Generate post content
    # ------------------------------------------------------------------
    instagram    = generate_instagram(review)
    pinterest    = generate_pinterest(review)
    todays_pick  = generate_todays_pick(review)

    # ------------------------------------------------------------------
    # 4. Write to social_queue/YYYY-MM-DD.json
    # ------------------------------------------------------------------
    today      = date.today().isoformat()
    queue_file = QUEUE_DIR / "{}.json".format(today)

    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "date":         today,
        "review":       review,
        "posts": {
            "instagram":   instagram,
            "pinterest":   pinterest,
            "todays_pick": todays_pick,
        },
    }
    save_json(queue_file, output)
    print("[info] Queue file written → {}".format(queue_file))

    # ------------------------------------------------------------------
    # 5. Mark as posted in social_posted.json
    # ------------------------------------------------------------------
    posted[slug] = {
        "posted_date": today,
        "title":       review["title"],
        "categories":  review.get("categories", []),
    }
    save_json(POSTED_FILE, posted)
    print("[info] Marked '{}' as posted in {}".format(slug, POSTED_FILE))

    # ------------------------------------------------------------------
    # 6. Print generated posts to stdout for human / automation use
    # ------------------------------------------------------------------
    divider = "-" * 60

    print("\n" + todays_pick)

    print("\n" + divider)
    print("INSTAGRAM CAPTION")
    print(divider)
    print(instagram["full_caption"])
    print("\n[{} chars]".format(instagram["char_count"]))

    print("\n" + divider)
    print("PINTEREST PIN")
    print(divider)
    print("Title: {}".format(pinterest["pin_title"]))
    print("\nDescription:\n{}".format(pinterest["description"]))
    print("\nLink: {}".format(pinterest["link_url"]))
    print("\n[{} chars in description]".format(pinterest["char_count"]))

    print("\n" + divider)
    print("Queue file : {}".format(queue_file))
    print("Posted log : {}".format(POSTED_FILE))
    print(divider)

    return output


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Generate social media posts for the next BookGateway review."
    )
    parser.add_argument(
        "--category",
        metavar="CATEGORY",
        default=None,
        help=(
            "Filter to a specific category, e.g. 'Romance & Chick Lit'. "
            "Falls back to all categories if no unposted match is found."
        ),
    )
    args = parser.parse_args()
    run(category_filter=args.category)
