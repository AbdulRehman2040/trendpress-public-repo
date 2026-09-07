"""Fetch the body text of a source news article.

Why this exists
---------------
The writer is told to use ONLY facts present in its source material, and the
only source material it used to get was a Google Trends RSS snippet — roughly
20-40 words. Asked for a 900-word article on that, a model can only pad, which
is precisely the "thin content with little or no added value" Google penalises.

Giving the writer the actual article text turns the same honest constraint into
a strength: it can now report substance instead of announcing that substance
exists somewhere else.

Deliberately dependency-free: html.parser from the stdlib, not a readability
library. We need paragraph text, not a general-purpose DOM.
"""
from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

TIMEOUT = 15
MAX_BYTES = 2_000_000        # ignore anything enormous; news pages are far smaller
MIN_PARAGRAPH_CHARS = 60     # below this a <p> is a caption, byline or nav crumb
MAX_BODY_WORDS = 900         # plenty of grounding without flooding the prompt

# Blocks whose text is never article body.
_SKIP_TAGS = {"script", "style", "nav", "header", "footer", "aside", "form",
              "noscript", "figcaption", "button", "svg"}

# Boilerplate that survives tag filtering on most news sites.
_BOILERPLATE = re.compile(
    r"(cookie|subscribe|sign up|newsletter|advertisement|share this|read more|"
    r"follow us|all rights reserved|terms of service|privacy policy|"
    r"related articles|most read|trending now)",
    re.I,
)

_UA = ("Mozilla/5.0 (compatible; trendpress/1.0; +https://github.com/) "
       "Python-requests")


class _ParagraphExtractor(HTMLParser):
    """Collect the text of <p> elements outside boilerplate containers."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paragraphs: list[str] = []
        self._depth_skipped = 0
        self._in_p = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_TAGS:
            self._depth_skipped += 1
        elif tag == "p" and not self._depth_skipped:
            self._in_p = True
            self._buf = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._depth_skipped = max(0, self._depth_skipped - 1)
        elif tag == "p" and self._in_p:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            if len(text) >= MIN_PARAGRAPH_CHARS and not _BOILERPLATE.search(text):
                self.paragraphs.append(text)
            self._in_p = False
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_p and not self._depth_skipped:
            self._buf.append(data)


def extract_article(url: str, max_words: int = MAX_BODY_WORDS) -> str:
    """Return the readable body text of a news page, or "" if unavailable.

    Never raises: a source that blocks us, times out or renders via JavaScript
    simply yields "", and the caller falls back to the RSS snippet. A failed
    fetch must degrade the article, never break the run.
    """
    if not url or not url.startswith(("http://", "https://")):
        return ""
    try:
        resp = requests.get(
            url, timeout=TIMEOUT, stream=True,
            headers={"User-Agent": _UA, "Accept": "text/html,application/xhtml+xml"},
        )
        if not resp.ok:
            logger.debug("extract: %s -> HTTP %s", url, resp.status_code)
            return ""
        if "html" not in resp.headers.get("Content-Type", "").lower():
            return ""
        html = resp.raw.read(MAX_BYTES, decode_content=True)
        text = html.decode(resp.encoding or "utf-8", errors="replace")
    except Exception as exc:
        logger.debug("extract: %s failed (%s)", url, exc)
        return ""

    parser = _ParagraphExtractor()
    try:
        parser.feed(text)
    except Exception:  # malformed markup — keep whatever parsed cleanly
        pass

    body = " ".join(parser.paragraphs).strip()
    words = body.split()
    if len(words) > max_words:
        body = " ".join(words[:max_words]) + " ..."
    return body


def domain_of(url: str) -> str:
    """Bare hostname for attribution when the feed gives no source name."""
    try:
        host = urlsplit(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def word_count(text: str) -> int:
    return len(text.split()) if text else 0
