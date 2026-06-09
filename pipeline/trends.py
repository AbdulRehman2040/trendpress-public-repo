"""Stage 1 — discover safe UK Google Trends topics.

Public entry point: ``get_safe_trends(settings, db, gemini) -> list[Trend]``.

Pipeline within this stage:
  1. fetch the official Google Trends RSS feed for ``settings['geo']``
  2. parse items into ``Trend`` objects (xml.etree, namespace read from the doc)
  3. drop trends already used within ``settings['dedupe_window_days']`` days
  4. drop unsafe trends via a cheap keyword pre-filter, then ONE batched
     Gemini classification call for everything that survives
  5. return the survivors sorted by approximate traffic (desc)

Failure policy: a network/parse failure returns an empty list (never crashes
the orchestrator). If the Gemini safety call is unavailable, the stage fails
OPEN — it keeps the keyword-filtered trends and logs a loud warning — because
the keyword pre-filter still applies and publishing is gated downstream by
``review_mode`` (default "pending" = human review). Per-trend, a missing
verdict fails CLOSED (the topic is dropped).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any, Protocol

import requests

from . import NewsItem, Trend

if TYPE_CHECKING:  # avoid importing the google-genai SDK at module load time
    from core.gemini import GeminiClient

logger = logging.getLogger(__name__)

TRENDS_RSS_URL = "https://trends.google.com/trending/rss?geo={geo}"
FALLBACK_NAMESPACE = "https://trends.google.com/trending/rss"
FETCH_TIMEOUT = 20  # seconds
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Cheap, no-API pre-filter (requirement 4a). Matched case-insensitively as whole
# words/phrases. Edit freely — over-dropping a borderline topic is acceptable.
UNSAFE_KEYWORDS: tuple[str, ...] = (
    "dies", "dead", "death", "killed", "murder", "stabbing", "shooting",
    "missing person", "arrested", "charged", "court", "trial", "verdict",
    "inquest", "suicide", "overdose", "cancer diagnosis", "crash victims",
)
_UNSAFE_RE = re.compile(
    r"\b(" + "|".join(re.escape(k) for k in UNSAFE_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

_SAFETY_SYSTEM = (
    "You are a cautious UK news editor screening trending topics for an "
    "automated publisher. Your job is to avoid harm and defamation under UK law."
)


class _TopicStore(Protocol):
    """The subset of core.db this stage depends on (for dedupe)."""

    def is_topic_used(self, trend_id: str, within_days: int | None = ...) -> bool: ...


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def get_safe_trends(settings: dict, db: _TopicStore, gemini: "GeminiClient | None") -> list[Trend]:
    """Return today's safe, unused trends for the configured geo, by traffic."""
    trends = _fetch_and_parse(settings)
    if not trends:
        return []
    logger.info("trends: parsed %d topic(s) from feed", len(trends))

    trends = _drop_used(trends, db, settings)
    trends = _keyword_filter(trends)
    trends = _gemini_safety_filter(trends, gemini)

    trends.sort(key=lambda t: _parse_traffic(t.traffic), reverse=True)
    logger.info("trends: %d safe topic(s) for geo=%s", len(trends), settings.get("geo", "GB"))
    return trends


# --------------------------------------------------------------------------- #
# 1 + 2. Fetch & parse
# --------------------------------------------------------------------------- #
def _fetch_and_parse(settings: dict) -> list[Trend]:
    """Fetch the RSS feed and parse it; return [] on any network/parse error."""
    url = TRENDS_RSS_URL.format(geo=settings.get("geo", "GB"))
    try:
        resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=FETCH_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("trends fetch failed (%s): %s", url, exc)
        return []
    try:
        return _parse_feed(resp.content)
    except ET.ParseError as exc:
        logger.error("trends feed parse failed: %s", exc)
        return []


def _parse_feed(xml_bytes: bytes) -> list[Trend]:
    """Parse the RSS bytes into Trend objects using the document's namespace."""
    root = ET.fromstring(xml_bytes)
    nsmap = {"ht": _detect_namespace(root)}
    trends = [_parse_item(item, nsmap) for item in root.iter("item")]
    return [t for t in trends if t is not None]


def _detect_namespace(root: ET.Element) -> str:
    """Read the real ht namespace URI from the document; fall back to constant."""
    for el in root.iter():
        if el.tag.startswith("{"):
            uri = el.tag[1:].split("}", 1)[0]
            if "trends.google" in uri or uri.rstrip("/").endswith("trending/rss"):
                return uri
    return FALLBACK_NAMESPACE


def _parse_item(item: ET.Element, nsmap: dict) -> Trend | None:
    """Build a Trend from one <item>, or None if it has no usable news facts."""
    title = (item.findtext("title") or "").strip()
    if not title:
        return None
    news_items = _parse_news_items(item, nsmap)
    if not news_items:  # need source facts to write from
        logger.debug("trends: skipping %r (no news items)", title)
        return None
    return Trend(
        trend_id=_trend_id(title),
        title=title,
        traffic=(item.findtext("ht:approx_traffic", "", nsmap) or "").strip(),
        news_items=news_items,
        picture_url=(item.findtext("ht:picture", "", nsmap) or "").strip() or None,
    )


def _parse_news_items(item: ET.Element, nsmap: dict) -> list[NewsItem]:
    """Extract the namespaced <ht:news_item> children of an item."""
    out: list[NewsItem] = []
    for node in item.findall("ht:news_item", nsmap):
        title = (node.findtext("ht:news_item_title", "", nsmap) or "").strip()
        url = (node.findtext("ht:news_item_url", "", nsmap) or "").strip()
        if not title or not url:
            continue
        out.append(NewsItem(
            title=title,
            snippet=(node.findtext("ht:news_item_snippet", "", nsmap) or "").strip(),
            url=url,
            source=(node.findtext("ht:news_item_source", "", nsmap) or "").strip(),
        ))
    return out


def _trend_id(title: str) -> str:
    """Stable id: first 16 hex chars of sha1(lowercased, whitespace-normalized)."""
    normalized = re.sub(r"\s+", " ", title.strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 3. Dedupe
# --------------------------------------------------------------------------- #
def _drop_used(trends: list[Trend], db: _TopicStore, settings: dict) -> list[Trend]:
    """Drop trends already used within the dedupe window."""
    window = settings.get("dedupe_window_days", 7)
    kept = []
    for t in trends:
        if db.is_topic_used(t.trend_id, within_days=window):
            logger.info("dedupe: skipping already-used %r (%s)", t.title, t.trend_id)
        else:
            kept.append(t)
    return kept


# --------------------------------------------------------------------------- #
# 4a. Keyword pre-filter
# --------------------------------------------------------------------------- #
def _keyword_filter(trends: list[Trend]) -> list[Trend]:
    """Drop trends whose title or news titles hit an unsafe keyword."""
    kept = []
    for t in trends:
        hit = _keyword_hit(t)
        if hit:
            logger.info("safety[keyword]: dropping %r (matched %r)", t.title, hit)
        else:
            kept.append(t)
    return kept


def _keyword_hit(trend: Trend) -> str | None:
    haystack = " ".join([trend.title, *(n.title for n in trend.news_items)])
    match = _UNSAFE_RE.search(haystack)
    return match.group(0) if match else None


# --------------------------------------------------------------------------- #
# 4b. Batched Gemini safety classification
# --------------------------------------------------------------------------- #
def _gemini_safety_filter(trends: list[Trend], gemini: "GeminiClient | None") -> list[Trend]:
    """Classify all remaining trends in ONE Gemini call; drop the unsafe ones."""
    if not trends:
        return []
    if gemini is None:
        logger.warning(
            "safety[gemini]: client unavailable; failing OPEN with %d keyword-"
            "filtered trend(s) (publishing is gated by review_mode)", len(trends),
        )
        return trends

    payload = [
        {"trend_id": t.trend_id, "title": t.title,
         "top_snippet": (t.news_items[0].snippet or t.news_items[0].title)}
        for t in trends
    ]
    try:
        raw = gemini.generate_json(_safety_prompt(payload), system=_SAFETY_SYSTEM)
    except Exception as exc:  # network / quota / parse — keep going, never crash
        logger.warning(
            "safety[gemini]: classification failed (%s); failing OPEN with %d "
            "keyword-filtered trend(s)", exc, len(trends),
        )
        return trends

    verdicts = _verdict_map(raw)
    kept = []
    for t in trends:
        verdict = verdicts.get(t.trend_id)
        if verdict is None:
            logger.warning("safety[gemini]: no verdict for %r; dropping to be safe", t.title)
        elif verdict.get("safe") is True:
            kept.append(t)
        else:
            logger.info("safety[gemini]: dropping %r — %s",
                        t.title, verdict.get("reason", "marked unsafe"))
    return kept


def _safety_prompt(payload: list[dict]) -> str:
    return (
        "Classify each trending topic below as safe or unsafe for an automated "
        "publisher to write a news article about.\n\n"
        "Mark a topic UNSAFE if it is centred on any of:\n"
        "- a person's death\n"
        "- an ongoing criminal case or a named suspect\n"
        "- a personal tragedy\n"
        "- medical claims about a named individual\n"
        "- adult content\n"
        "- anything where a factual error about a real person could be "
        "defamatory under UK law.\n\n"
        "Treat as SAFE: sports results, entertainment, product launches, "
        "weather, finance, lifestyle, and ordinary politics-as-news.\n\n"
        "Return ONLY a strict JSON array with one object per input topic, each "
        'with exactly these keys: {"trend_id": string, "safe": boolean, '
        '"reason": short string}. No markdown, no commentary.\n\n'
        "Topics:\n" + json.dumps(payload, ensure_ascii=False)
    )


def _verdict_map(raw: Any) -> dict[str, dict]:
    """Normalize Gemini output (array, or object wrapping one) to id -> verdict."""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = next((v for v in raw.values() if isinstance(v, list)), None)
        if items is None:
            items = [raw] if raw.get("trend_id") else []
    else:
        items = []
    return {v["trend_id"]: v for v in items if isinstance(v, dict) and v.get("trend_id")}


# --------------------------------------------------------------------------- #
# 5. Traffic parsing & table rendering
# --------------------------------------------------------------------------- #
def _parse_traffic(traffic: str) -> int:
    """Parse strings like '200K+', '1,000+', '20M' into an int for sorting."""
    match = re.search(r"([\d.,]+)\s*([KMB]?)", (traffic or "").upper())
    if not match:
        return 0
    number = float(match.group(1).replace(",", ""))
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}[match.group(2)]
    return int(number * multiplier)


def render_trends_table(trends: list[Trend]) -> str:
    """Render a clean fixed-width table for --dry-run output."""
    header = ("TITLE", "TRAFFIC", "NEWS", "SAMPLE SOURCE")
    rows = [header] + [
        (_truncate(t.title, 50), t.traffic or "-",
         str(len(t.news_items)), _truncate(_sample_source(t), 28))
        for t in trends
    ]
    if len(rows) == 1:
        return "(no safe trends)"
    widths = [max(len(row[i]) for row in rows) for i in range(len(header))]
    lines = []
    for i, row in enumerate(rows):
        lines.append("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)))
        if i == 0:
            lines.append("  ".join("-" * widths[j] for j in range(len(header))))
    return "\n".join(lines)


def _sample_source(trend: Trend) -> str:
    return trend.news_items[0].source if trend.news_items else ""


def _truncate(text: str, width: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= width else text[: width - 1] + "…"


# --------------------------------------------------------------------------- #
# Standalone smoke test:  python -m pipeline.trends
# --------------------------------------------------------------------------- #
def _main() -> None:
    import sys
    for _stream in (sys.stdout, sys.stderr):  # keep '…'/unicode titles safe on Windows
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from core import load_settings  # local import: keep module import side-effect free
    from core import db

    gemini = None
    try:
        from core.gemini import GeminiClient
        gemini = GeminiClient()
    except Exception as exc:  # SDK missing or no API key
        logging.getLogger(__name__).warning("Gemini unavailable for smoke test: %s", exc)

    db.init_db()
    trends = get_safe_trends(load_settings(), db, gemini)
    print(render_trends_table(trends))


if __name__ == "__main__":
    _main()
