"""Stage 5 — publish each ArticlePackage to its WordPress site.

Public entry point:
    ``publish(packages, media_map, sites, settings, db, dry_run) -> list[PublishResult]``

Safety model (this is what makes unattended runs safe):
  * review_mode decides the post status:
      - "pending" → status=pending (a human approves in wp-admin). DEFAULT.
      - "sample"  → settings.sample_rate of posts stay pending, the rest scheduled.
      - "auto"    → all scheduled.
  * Scheduled posts use status=future with a UTC date_gmt staggered inside
    settings.publish_window (Europe/London), >=20 minutes apart per site and
    never on the same minute network-wide, so the sites don't all post at once.
  * used_topics is written ONLY after a post is created, so a failed run can
    retry the same trend tomorrow.
  * Per-site failures are caught and recorded; the loop CONTINUES — one broken
    site never stops the network.

We send only date_gmt (UTC) for scheduling: if both date and date_gmt are sent
the WP REST API ignores date_gmt, and we cannot know each site's configured
timezone, so UTC is the unambiguous choice.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, time, timedelta, timezone

from core.wp import WPClient
from . import ArticlePackage, PublishResult

logger = logging.getLogger(__name__)

SCHEDULE_GAP_MINUTES = 20    # minimum spacing between a single site's scheduled posts
LEAD_MINUTES = 10            # never schedule closer than this to "now"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def publish(
    packages: list[ArticlePackage],
    media_map: dict,
    sites: list[dict],
    settings: dict,
    db,
    dry_run: bool = False,
) -> list[PublishResult]:
    """Publish/schedule every package; return one PublishResult per package, in order."""
    if not packages:
        return []
    site_by_id = {s["id"]: s for s in sites}
    review_mode = settings.get("review_mode", "pending")
    sample_rate = float(settings.get("sample_rate", 0.1))
    scheduler = _Scheduler(settings.get("publish_window") or {})

    results: list[PublishResult] = []
    for package in packages:
        site = site_by_id.get(package.site_id)
        if site is None:
            results.append(PublishResult(package.site_id, None, None, "error", "no site config"))
            continue
        status = _decide_status(review_mode, sample_rate)
        media_id = (media_map or {}).get((package.site_id, package.trend_id)) or package.featured_media_id
        try:
            results.append(_publish_one(package, site, status, media_id, scheduler, db, dry_run))
        except Exception as exc:  # one broken site must never stop the network
            logger.warning("[%s] publish failed for %r (%s)", package.site_id, package.title, exc)
            results.append(PublishResult(package.site_id, None, None, "error", str(exc)[:300]))

    _log_summary(results)
    return results


def _decide_status(review_mode: str, sample_rate: float) -> str:
    """Map review_mode to a WordPress post status."""
    if review_mode == "live":
        return "publish"   # publish immediately, public (no human review)
    if review_mode == "auto":
        return "future"    # scheduled inside the publish window
    if review_mode == "sample":
        return "pending" if random.random() < sample_rate else "future"
    return "pending"  # default / unknown → safest (human approval)


# --------------------------------------------------------------------------- #
# Single post
# --------------------------------------------------------------------------- #
def _publish_one(
    package: ArticlePackage, site: dict, status: str, media_id, scheduler, db, dry_run: bool
) -> PublishResult:
    scheduled = scheduler.next_time(package.site_id) if status == "future" else None

    if dry_run:
        when = scheduled.isoformat(timespec="minutes") if scheduled else "n/a"
        logger.info("[%s] DRY-RUN: would create %s post %r (media=%s, when=%s)",
                    package.site_id, status, package.title, media_id, when)
        return PublishResult(package.site_id, None, None, f"would-{status}", None)

    wp = WPClient(site)
    categories, tags = _resolve_terms(wp, package)
    payload = {
        "title": package.title,
        "slug": package.slug,
        "content": package.html_content,
        "excerpt": package.meta_description,
        "status": status,
        "categories": categories,
        "tags": tags,
    }
    if media_id:
        payload["featured_media"] = int(media_id)
    if scheduled is not None:
        payload["date_gmt"] = (scheduled.astimezone(timezone.utc)
                               .replace(tzinfo=None).isoformat(timespec="seconds"))

    resp = wp.create_post(payload)
    wp_post_id = int(resp.get("id"))
    link = resp.get("link") or f"{site['url'].rstrip('/')}/?p={wp_post_id}"

    # Record success FIRST in posts, then mark the trend used (only now, so a
    # failed run can retry the trend tomorrow).
    db.add_post(package.site_id, package.trend_id, wp_post_id, package.title, link, status)
    db.add_used_topic(package.trend_id, package.title)
    logger.info("[%s] created %s post %d: %s", package.site_id, status, wp_post_id, link)
    return PublishResult(package.site_id, wp_post_id, link, status, None)


def _resolve_terms(wp: WPClient, package: ArticlePackage) -> tuple[list[int], list[int]]:
    """Resolve category + tags to term ids, best-effort (skip any that fail)."""
    categories: list[int] = []
    if package.category:
        try:
            categories.append(wp.get_or_create_term(package.category, "category"))
        except Exception as exc:
            logger.warning("[%s] category %r failed (%s)", package.site_id, package.category, exc)
    tags: list[int] = []
    for tag in package.tags:
        try:
            tags.append(wp.get_or_create_term(tag, "post_tag"))
        except Exception as exc:
            logger.warning("[%s] tag %r failed (%s)", package.site_id, tag, exc)
    return categories, tags


def _log_summary(results: list[PublishResult]) -> None:
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r.status] = by_status.get(r.status, 0) + 1
    summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_status.items()))
    logger.info("publisher: %d package(s) processed | %s", len(results), summary or "(none)")


# --------------------------------------------------------------------------- #
# Staggered scheduling inside the publish window
# --------------------------------------------------------------------------- #
class _Scheduler:
    """Hands out timezone-aware publish datetimes inside the window, staggered."""

    def __init__(self, window: dict) -> None:
        self.tz = _resolve_tz(window.get("timezone", "Europe/London"))
        self.start = _parse_hhmm(window.get("start", "09:00"))
        self.end = _parse_hhmm(window.get("end", "18:00"))
        self._per_site: dict[str, list[datetime]] = {}
        self._taken_minutes: set[int] = set()  # epoch-minute keys, network-wide

    def next_time(self, site_id: str) -> datetime:
        earliest, latest = self._bounds()
        site_times = self._per_site.setdefault(site_id, [])
        candidate = None
        for _ in range(60):
            guess = _random_dt(earliest, latest)
            spaced = all(abs((guess - t).total_seconds()) >= SCHEDULE_GAP_MINUTES * 60
                         for t in site_times)
            if spaced and _minute_key(guess) not in self._taken_minutes:
                candidate = guess
                break
        if candidate is None:  # window too crowded → step past the site's latest slot
            anchor = max(site_times) if site_times else earliest
            candidate = min(anchor + timedelta(minutes=SCHEDULE_GAP_MINUTES), latest)
        site_times.append(candidate)
        self._taken_minutes.add(_minute_key(candidate))
        return candidate

    def _bounds(self) -> tuple[datetime, datetime]:
        """Return (earliest, latest) for the next usable window day, earliest < latest."""
        now = datetime.now(self.tz)
        for day_offset in (0, 1, 2):
            day = (now + timedelta(days=day_offset)).date()
            start = datetime.combine(day, self.start, self.tz)
            end = datetime.combine(day, self.end, self.tz)
            earliest = max(start, now + timedelta(minutes=LEAD_MINUTES))
            if earliest < end:
                return earliest, end
        fallback = now + timedelta(minutes=LEAD_MINUTES)
        return fallback, fallback + timedelta(hours=1)


def _resolve_tz(name: str):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # tzdata missing (e.g. bare Windows) — degrade to UTC
        logger.warning("publisher: timezone %r unavailable (install tzdata); using UTC", name)
        return timezone.utc


def _parse_hhmm(value: str) -> time:
    try:
        hour, minute = (int(x) for x in str(value).split(":")[:2])
        return time(hour, minute)
    except (ValueError, TypeError):
        return time(9, 0)


def _random_dt(start: datetime, end: datetime) -> datetime:
    span = max(0, int((end - start).total_seconds()))
    return start + timedelta(seconds=random.randint(0, span)) if span else start


def _minute_key(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() // 60)
