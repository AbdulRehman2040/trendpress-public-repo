"""Shared data contracts for the trendpress pipeline.

Every stage consumes and produces these types, so the modules stay decoupled
and a stub can be swapped for a real implementation without touching callers.
Lightweight dataclasses only — no third-party dependencies here.

Flow:  Trend -> Assignment -> ArticlePackage -> PublishResult
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NewsItem:
    """A single supporting news story behind a trend."""

    title: str
    snippet: str
    url: str
    source: str


@dataclass
class Trend:
    """A trending topic discovered in stage 1 (trends)."""

    trend_id: str
    title: str
    traffic: str
    news_items: list[NewsItem] = field(default_factory=list)
    picture_url: str | None = None


@dataclass
class Assignment:
    """A trend matched to a single site with a chosen editorial angle."""

    trend: Trend
    site_id: str
    score: int
    angle: str


@dataclass
class FaqItem:
    """One question/answer pair for an article's FAQ block."""

    q: str
    a: str


@dataclass
class ArticlePackage:
    """A fully written, ready-to-publish article for one site."""

    site_id: str
    trend_id: str
    title: str
    slug: str
    meta_description: str
    focus_keyword: str
    tags: list[str]
    category: str
    html_content: str
    image_query: str
    faq: list[FaqItem] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    featured_media_id: int | None = None    # set by the images stage after upload
    featured_image_url: str | None = None   # WP source_url of the uploaded image (for the dashboard)


@dataclass
class PublishResult:
    """Outcome of attempting to publish one ArticlePackage."""

    site_id: str
    wp_post_id: int | None
    url: str | None
    status: str
    error: str | None = None


__all__ = [
    "NewsItem",
    "Trend",
    "Assignment",
    "FaqItem",
    "ArticlePackage",
    "PublishResult",
]
