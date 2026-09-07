"""On-page SEO: structured data and keyword placement.

Two jobs, both previously missing:

1. **NewsArticle structured data.** The pipeline emitted FAQPage JSON-LD and
   nothing else. For a news site that is the wrong half — NewsArticle is what
   makes a page eligible for Top Stories and rich results, and it carries the
   publisher, date and image that a search engine otherwise has to guess at.

2. **Focus keyword placement.** ``focus_keyword`` was collected from the model
   and then never used. A keyword that appears in no heading and not in the
   opening paragraph is not a focus keyword, it is a label.

On authorship
-------------
``author`` is the publication as an Organization, never an invented person.
Google's publisher policies prohibit content that "misrepresents... the content
creator", so a fabricated human byline on automated output would create a
violation rather than satisfy E-E-A-T. An organisation author is accurate and
schema.org treats it as a first-class author type.
"""
from __future__ import annotations

import html as html_lib
import json
import re
from datetime import datetime, timezone

from . import ArticlePackage

# Words too generic to demand in a heading — requiring "the news" in an <h2>
# would push the writer into keyword stuffing, which is the opposite of the goal.
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "at", "to", "for",
    "with", "from", "by", "as", "is", "are", "was", "were", "be", "been", "it",
    "this", "that", "these", "those", "new", "news", "latest", "update", "updates",
    "uk", "2025", "2026",
}


# --------------------------------------------------------------------------- #
# Structured data
# --------------------------------------------------------------------------- #
def news_article_jsonld(
    package: ArticlePackage,
    site: dict,
    published_at: datetime | None = None,
    page_url: str | None = None,
) -> str:
    """Build a NewsArticle JSON-LD <script> block for one article.

    Called at publish time rather than write time because the image URL and the
    final page URL are only known once the media is uploaded and the slug is
    settled.
    """
    site_url = str(site.get("url") or "").rstrip("/")
    site_name = str(site.get("name") or site.get("id") or "").strip()
    url = page_url or (f"{site_url}/{package.slug}/" if site_url and package.slug else site_url)
    when = (published_at or datetime.now(timezone.utc)).astimezone(timezone.utc)

    data: dict = {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": package.title[:110],   # schema.org caps headline at 110 chars
        "description": package.meta_description,
        "datePublished": when.isoformat(timespec="seconds"),
        "dateModified": when.isoformat(timespec="seconds"),
        "inLanguage": "en-GB",
        # An organisation, not a person — see the module docstring.
        "author": {"@type": "Organization", "name": site_name, "url": site_url or None},
        "publisher": {"@type": "Organization", "name": site_name, "url": site_url or None},
    }
    if url:
        data["mainEntityOfPage"] = {"@type": "WebPage", "@id": url}
    if package.featured_image_url:
        data["image"] = [package.featured_image_url]
    if package.category:
        data["articleSection"] = package.category
    if package.tags:
        data["keywords"] = ", ".join(package.tags)
    if package.sources:
        # Declare what the report is based on; honest attribution, and it is the
        # property search engines read for sourced reporting.
        data["citation"] = [{"@type": "CreativeWork", "url": u} for u in package.sources[:5]]

    return _script(_prune(data))


def _prune(value):
    """Drop None/empty members so the emitted JSON-LD has no dangling nulls."""
    if isinstance(value, dict):
        return {k: _prune(v) for k, v in value.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        return [_prune(v) for v in value if v not in (None, "", [], {})]
    return value


def _script(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    # "</" inside a <script> block would terminate it early.
    payload = payload.replace("</", "<\\/")
    return f'<script type="application/ld+json">{payload}</script>'


# --------------------------------------------------------------------------- #
# Keyword placement
# --------------------------------------------------------------------------- #
def significant_terms(keyword: str) -> list[str]:
    """The content words of a focus keyword, ignoring generic filler."""
    words = re.findall(r"[a-z0-9']+", str(keyword).lower())
    return [w for w in words if w not in _STOPWORDS and len(w) > 2]


def keyword_errors(title: str, html: str, keyword: str) -> list[str]:
    """Check the focus keyword actually lands where it counts.

    Placement, not density: the keyword needs to be in the title, the opening,
    and one subheading. Beyond that, repeating it is stuffing — which is why
    there is a ceiling here as well as a floor.
    """
    terms = significant_terms(keyword)
    if not terms:
        return []  # no usable keyword to check; the title rules already apply

    text = _plain_text(html)
    lower_text = text.lower()
    lower_title = str(title).lower()
    errors: list[str] = []

    if not any(t in lower_title for t in terms):
        errors.append(f"focus keyword {keyword!r} does not appear in the title")

    opening = " ".join(text.split()[:120]).lower()
    if not any(t in opening for t in terms):
        errors.append(f"focus keyword {keyword!r} is missing from the opening paragraph")

    headings = " ".join(re.findall(r"<h[23][^>]*>(.*?)</h[23]>", html,
                                   re.IGNORECASE | re.DOTALL)).lower()
    headings = _plain_text(headings).lower()
    if headings and not any(t in headings for t in terms):
        errors.append(f"focus keyword {keyword!r} appears in no <h2> subheading")

    # Stuffing guard: the leading term should not exceed ~3% of the body.
    # Only meaningful on a real article — on 30 words, two uses is 7% and says
    # nothing. Matched on whole words so "bus" does not count "business".
    words = re.findall(r"[a-z0-9']+", lower_text)
    if len(words) >= 150:
        lead = terms[0]
        density = words.count(lead) / len(words)
        if density > 0.03:
            errors.append(f"keyword {lead!r} used in {density:.1%} of words — "
                          f"reduce it to under 3%, this reads as stuffing")
    return errors


def _plain_text(html: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", html))).strip()
