"""Stage 3 — turn each Assignment into a publish-ready ArticlePackage.

Public entry point:
    ``write_articles(assignments, sites, settings, gemini, db) -> list[ArticlePackage]``

One Gemini call per assignment. The model does NOT know today's news: the only
facts permitted in an article are those present in the trend's news_items
(title/snippet/source/url). The generation prompt enforces this hard, and a
post-generation validator checks structure, lengths, source links and word
count, retrying once with the validation errors before skipping an assignment.

Failure policy: generation needs Gemini. If it is unavailable, this stage makes
no articles and logs a warning (consistent with the matcher). Nothing is
written to used_topics here — that happens only after a successful publish.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import random
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from core import DATA_DIR
from core.wp import WPClient
from . import ArticlePackage, Assignment, FaqItem, Trend

if TYPE_CHECKING:  # avoid importing the google-genai SDK at module load time
    from core.gemini import GeminiClient

logger = logging.getLogger(__name__)

PREVIEW_DIR = DATA_DIR / "preview"
REQUIRED_KEYS = (
    "title", "slug", "meta_description", "focus_keyword",
    "tags", "category", "html_content", "image_query", "faq",
)
WORD_TOLERANCE = 0.25  # +/- 25% of the target word count

_SYSTEM = (
    "You are an experienced UK news writer producing original, factually-grounded "
    "articles for a specific niche website. You write careful UK English and you "
    "never invent facts beyond the source material you are given."
)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def write_articles(
    assignments: list[Assignment],
    sites: list[dict],
    settings: dict,
    gemini: "GeminiClient | None",
    db,
) -> list[ArticlePackage]:
    """Generate one ArticlePackage per assignment (sequential Gemini calls)."""
    if not assignments:
        return []
    if gemini is None:
        logger.warning("writer: Gemini unavailable; generating no articles")
        return []

    site_by_id = {s["id"]: s for s in sites}
    category_cache: dict[str, list[str]] = {}  # site_id -> existing category names
    packages: list[ArticlePackage] = []
    for assignment in assignments:
        site = site_by_id.get(assignment.site_id)
        if site is None:
            logger.warning("writer: no site config for %r; skipping", assignment.site_id)
            continue
        if _already_posted(db, assignment.site_id, assignment.trend.trend_id):
            logger.info("[%s] already has a post for %s; skipping",
                        assignment.site_id, assignment.trend.trend_id)
            continue
        package = _write_one(assignment, site, gemini, category_cache)
        if package is not None:
            packages.append(package)
    logger.info("writer: produced %d/%d article(s)", len(packages), len(assignments))
    return packages


def _already_posted(db, site_id: str, trend_id: str) -> bool:
    """True if a non-errored post already exists for this site+trend (re-run guard)."""
    try:
        return any(
            row["trend_id"] == trend_id and row["status"] != "error"
            for row in db.list_posts(site_id)
        )
    except Exception:  # db optional here; never block generation on a read error
        return False


# --------------------------------------------------------------------------- #
# Per-assignment generation (1 call + up to 1 validation retry)
# --------------------------------------------------------------------------- #
def _write_one(
    assignment: Assignment, site: dict, gemini: "GeminiClient",
    category_cache: dict[str, list[str]],
) -> ArticlePackage | None:
    trend = assignment.trend
    target_words = _target_word_count(site)
    internal_links = _fetch_internal_links(site, trend)
    existing_categories = _existing_categories(site, category_cache)
    base_prompt = _build_prompt(assignment, site, target_words, internal_links, existing_categories)
    site_id = assignment.site_id

    errors: list[str] = []
    for attempt in range(2):  # initial attempt + one retry with the errors appended
        prompt = base_prompt if not errors else base_prompt + _retry_suffix(errors)
        try:
            raw = gemini.generate_json(prompt, system=_SYSTEM)
        except Exception as exc:  # call failed (network/quota/parse) — skip cleanly
            logger.warning("[%s] generation call failed for %r (%s)",
                           site_id, trend.title, exc)
            return None
        raw = _repair(raw, trend)  # cheap local fixes (title length, source links) — avoid a regen
        errors = _validate(raw, trend, target_words)
        if not errors:
            return _build_package(raw, assignment)
        logger.info("[%s] validation failed for %r (attempt %d/2): %s",
                    site_id, trend.title, attempt + 1, "; ".join(errors))

    logger.warning("[%s] skipping %r after 2 attempts: %s",
                   site_id, trend.title, "; ".join(errors))
    return None


def _target_word_count(site: dict) -> int:
    word_range = site.get("word_range") or [700, 1100]
    try:
        low, high = int(word_range[0]), int(word_range[1])
    except (TypeError, ValueError, IndexError):
        low, high = 700, 1100
    if low > high:
        low, high = high, low
    return random.randint(low, high)


def _fetch_internal_links(site: dict, trend: Trend) -> list[dict]:
    """Fetch up to 3 related posts from this site; [] for new/unreachable sites."""
    try:
        links = WPClient(site).search_posts(trend.title, per_page=3)
        return [lk for lk in links if lk.get("link")][:3]
    except Exception as exc:  # missing creds, new site, network — internal links optional
        logger.debug("[%s] internal-link search skipped (%s)", site.get("id"), exc)
        return []


def _existing_categories(site: dict, cache: dict[str, list[str]]) -> list[str]:
    """Existing category names for the site (cached per run) so the writer reuses them."""
    site_id = site.get("id", "")
    if site_id not in cache:
        try:
            cache[site_id] = [c["name"] for c in WPClient(site).list_categories() if c.get("name")]
        except Exception as exc:  # new/unreachable site or no creds — let the AI choose
            logger.debug("[%s] category fetch skipped (%s)", site_id, exc)
            cache[site_id] = []
    return cache[site_id]


# --------------------------------------------------------------------------- #
# The generation prompt (the heart of the system)
# --------------------------------------------------------------------------- #
def _build_prompt(
    assignment: Assignment, site: dict, target_words: int,
    internal_links: list[dict], existing_categories: list[str],
) -> str:
    trend = assignment.trend
    name = site.get("name", site.get("id", ""))
    niche = site.get("niche", "")
    audience = site.get("audience") or "this site's readers"
    tone = site.get("tone", "")
    return (
        "Write ONE complete web article and return it as STRICT JSON.\n\n"
        "SITE IDENTITY\n"
        f"- Name: {name}\n"
        f"- Niche: {niche}\n"
        f"- Audience: {audience}\n"
        f"- Tone: {tone}\n"
        f"- Editorial angle for THIS article: {assignment.angle}\n"
        f"- Target length: about {target_words} words (must stay within +/-25%).\n\n"
        f"TOPIC: {trend.title}\n\n"
        "SOURCE MATERIAL — the only permitted source of facts:\n"
        f"{_format_sources(trend)}\n\n"
        "HARD RULES (follow exactly):\n"
        "- Write in UK English.\n"
        "- Use ONLY facts, quotes, numbers and names that appear in the SOURCE "
        "MATERIAL above. Do NOT add anything from your own knowledge. If a detail "
        "is not in the source material, do not state it.\n"
        "- Attribute key claims to their source by name (e.g. \"according to "
        "<source>\").\n"
        "- Use a neutral news register suited to the angle. No clickbait.\n"
        "- The angle is the lens for framing and the takeaway only — never a "
        "licence to invent facts.\n\n"
        "REQUIRED ARTICLE STRUCTURE (inside html_content):\n"
        "1. Opening paragraph(s) covering the development.\n"
        "2. An <h2>Background</h2> context section.\n"
        "3. One or two <h2> sections on the main developments.\n"
        "4. An <h2> FAQ section with 3-4 genuinely useful Q&As (the same Q&As "
        "must also appear in the \"faq\" JSON field).\n"
        f"5. A closing <h2>What this means for you</h2> takeaway written "
        f"specifically for: {audience}.\n"
        "6. REQUIRED: include at least TWO <a href=\"...\"> links pointing to the "
        "SOURCE MATERIAL urls, woven naturally into the body.\n"
        f"{_format_internal_links(internal_links)}\n\n"
        f"{_format_categories(existing_categories)}"
        "HTML RULES: html_content must use ONLY these tags: h2, h3, p, ul, li, a, "
        "strong. No <h1>. No markdown. No inline styles. No <html> or <body> "
        "wrapper.\n\n"
        "OUTPUT — return STRICT JSON only (no markdown fences), with EXACTLY these "
        "keys:\n"
        '{"title": "MUST be under 60 characters, no clickbait", "slug": "kebab-case", '
        '"meta_description": "<=155 chars", "focus_keyword": "main keyword phrase", '
        '"tags": ["3 to 6 tags"], "category": "an existing category if one fits, '
        'else a new short one", '
        '"html_content": "the article HTML", '
        '"image_query": "2-3 generic English words for a stock photo, NEVER a '
        'person\'s name or brand", "faq": [{"q": "...", "a": "..."}]}'
    )


def _format_sources(trend: Trend) -> str:
    blocks = []
    for i, item in enumerate(trend.news_items, start=1):
        blocks.append(
            f"[{i}] TITLE: {item.title}\n"
            f"    SNIPPET: {item.snippet or '(no snippet provided)'}\n"
            f"    SOURCE: {item.source or 'unknown'}\n"
            f"    URL: {item.url}"
        )
    return "\n".join(blocks)


def _format_internal_links(internal_links: list[dict]) -> str:
    if not internal_links:
        return ("7. This site has no related internal posts yet — do NOT invent "
                "internal links.")
    listed = "; ".join(f'"{lk.get("title", "")}" ({lk["link"]})' for lk in internal_links)
    return ("7. Weave these existing internal links in naturally where relevant "
            f"(<a href>): {listed}")


def _format_categories(existing_categories: list[str]) -> str:
    """Tell the writer to reuse an existing category, creating one only if none fit."""
    if not existing_categories:
        return "CATEGORY: choose one concise, relevant category name for the article.\n\n"
    listed = "; ".join(existing_categories[:100])  # most-used first
    return (
        "CATEGORY: set \"category\" to the SINGLE most relevant of this site's "
        "EXISTING categories below, copied EXACTLY (same spelling and case). Prefer "
        "a BROAD, established category (e.g. a top-level topic) over a narrow one, "
        "and reuse rather than duplicate. Invent a new short category ONLY if none "
        "of these is a reasonable fit.\n"
        f"Existing categories (most used first): {listed}\n\n"
    )


def _retry_suffix(errors: list[str]) -> str:
    bullets = "\n".join(f"- {e}" for e in errors)
    return ("\n\nYOUR PREVIOUS OUTPUT FAILED VALIDATION. Fix every problem below "
            f"and return corrected STRICT JSON:\n{bullets}")


# --------------------------------------------------------------------------- #
# Local auto-repair (avoid wasting a Gemini regeneration on cheap problems)
# --------------------------------------------------------------------------- #
def _repair(raw: Any, trend: Trend) -> Any:
    """Fix cheap validation issues in place so we don't burn a regeneration:
      * trim an over-length title (<=60) / meta_description (<=155)
      * if the body lacks the required source links, append a 'Sources' section.
    """
    if not isinstance(raw, dict):
        return raw
    if raw.get("title"):
        raw["title"] = _trim(str(raw["title"]), 60)
    if raw.get("meta_description"):
        raw["meta_description"] = _trim(str(raw["meta_description"]), 155)

    html = str(raw.get("html_content", ""))
    source_urls = [item.url for item in trend.news_items if item.url]
    if source_urls:
        present = sum(1 for url in set(source_urls) if url in html)
        if present < min(2, len(source_urls)):
            block = _sources_block(trend)
            if block:
                raw["html_content"] = html + block
    return raw


def _sources_block(trend: Trend) -> str:
    """A simple <h2>Sources</h2> list linking every source URL (dedup, in order)."""
    seen: set[str] = set()
    items = []
    for item in trend.news_items:
        if item.url and item.url not in seen:
            seen.add(item.url)
            label = html_lib.escape(item.source or item.title or "Source")
            href = html_lib.escape(item.url, quote=True)
            items.append(f'<li><a href="{href}">{label}</a></li>')
    if not items:
        return ""
    return "\n<h2>Sources</h2>\n<ul>\n" + "\n".join(items) + "\n</ul>"


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate(raw: Any, trend: Trend, target_words: int) -> list[str]:
    """Return a list of human-readable validation errors ([] means valid)."""
    if not isinstance(raw, dict):
        return ["output was not a JSON object"]

    errors = [f"missing or empty key: {k}"
              for k in REQUIRED_KEYS if not raw.get(k)]
    if "html_content" in [e.split(": ")[-1] for e in errors]:
        return errors  # no html to inspect further

    # NOTE: title / meta_description length are NOT validated here — they are
    # trimmed to spec in _build_package so an otherwise-good article is never
    # skipped over a few extra characters.
    html = str(raw.get("html_content", ""))
    h2_count = len(re.findall(r"<h2[\s>]", html, re.IGNORECASE))
    if h2_count < 3:
        errors.append(f"html_content needs at least 3 <h2> sections, found {h2_count}")

    source_urls = {item.url for item in trend.news_items if item.url}
    needed = min(2, len(source_urls))
    present = sum(1 for url in source_urls if url in html)
    if present < needed:
        errors.append(f"html_content needs at least {needed} source links, found {present}")

    words = _word_count(html)
    low = int(target_words * (1 - WORD_TOLERANCE))
    high = int(target_words * (1 + WORD_TOLERANCE))
    if not low <= words <= high:
        errors.append(f"word count {words} outside {low}-{high} (target {target_words})")

    faq = raw.get("faq")
    if isinstance(faq, list) and not 3 <= len(faq) <= 4:
        errors.append(f"faq must have 3-4 items, found {len(faq)}")
    return errors


def _word_count(html: str) -> int:
    text = html_lib.unescape(re.sub(r"<[^>]+>", " ", html))
    return len(text.split())


def _trim(text: str, limit: int) -> str:
    """Trim to <= limit chars on a word boundary (so titles/metas never get skipped)."""
    text = " ".join(str(text).split())  # collapse whitespace
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rfind(" ")]
    return cut.rstrip(" ,;:-—")


# --------------------------------------------------------------------------- #
# Build the ArticlePackage (+ FAQ JSON-LD)
# --------------------------------------------------------------------------- #
def _build_package(raw: dict, assignment: Assignment) -> ArticlePackage:
    faq = [
        FaqItem(q=str(item.get("q", "")).strip(), a=str(item.get("a", "")).strip())
        for item in raw.get("faq", []) if isinstance(item, dict) and item.get("q")
    ]
    html = str(raw["html_content"]).strip() + "\n" + _faq_jsonld(faq)
    tags = [str(t).strip() for t in raw.get("tags", []) if str(t).strip()][:6]
    sources = [item.url for item in assignment.trend.news_items if item.url]
    return ArticlePackage(
        site_id=assignment.site_id,
        trend_id=assignment.trend.trend_id,
        title=_trim(str(raw["title"]), 60),
        slug=_slugify(raw.get("slug") or raw["title"]),
        meta_description=_trim(str(raw["meta_description"]), 155),
        focus_keyword=str(raw.get("focus_keyword", "")).strip(),
        tags=tags,
        category=str(raw.get("category", "")).strip() or "News",
        html_content=html,
        image_query=str(raw.get("image_query", "")).strip(),
        faq=faq,
        sources=sources,
    )


def _faq_jsonld(faq: list[FaqItem]) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": f.q,
             "acceptedAnswer": {"@type": "Answer", "text": f.a}}
            for f in faq
        ],
    }
    return ('<script type="application/ld+json">'
            + json.dumps(data, ensure_ascii=False) + "</script>")


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return slug[:80] or "article"


# --------------------------------------------------------------------------- #
# Dry-run preview output
# --------------------------------------------------------------------------- #
def write_preview(package: ArticlePackage) -> Path:
    """Write a self-contained, browser-openable preview of the article."""
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = PREVIEW_DIR / f"{_slugify(package.site_id)}--{_slugify(package.slug)}.html"
    path.write_text(_render_preview_html(package), encoding="utf-8")
    return path


def _render_preview_html(p: ArticlePackage) -> str:
    esc = html_lib.escape
    meta = (
        f"site: {esc(p.site_id)} &middot; trend: {esc(p.trend_id)} &middot; "
        f"category: {esc(p.category)} &middot; focus: {esc(p.focus_keyword)} &middot; "
        f"image_query: {esc(p.image_query)} &middot; words: {_word_count(p.html_content)}"
    )
    tags = ", ".join(esc(t) for t in p.tags)
    return (
        "<!doctype html>\n<html lang=\"en-GB\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        f"<title>{esc(p.title)}</title>\n"
        f"<meta name=\"description\" content=\"{esc(p.meta_description)}\">\n"
        "<style>body{max-width:760px;margin:2rem auto;padding:0 1rem;"
        "font:17px/1.6 Georgia,serif;color:#1a1a1a}"
        ".tp-meta{font:13px/1.5 system-ui,sans-serif;color:#666;background:#f4f4f5;"
        "padding:.6rem .8rem;border-radius:6px;margin-bottom:1.5rem}"
        "h1{font-size:1.9rem;line-height:1.2}h2{margin-top:1.8rem}"
        "a{color:#0b5fff}</style>\n</head>\n<body>\n"
        f"<div class=\"tp-meta\">{meta}<br>tags: {tags}</div>\n"
        f"<h1>{esc(p.title)}</h1>\n"
        f"{p.html_content}\n"
        "</body>\n</html>\n"
    )
