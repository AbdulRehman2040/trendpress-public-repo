"""Stage 6 (housekeeping) — sync Search Console metrics and delete dead posts.

Two public entry points, both driven from main.py:

    ``sync_metrics(sites, settings) -> dict``   # python main.py --sync-metrics
    ``prune(sites, settings, dry_run) -> dict`` # python main.py --prune

Why deletion, not just archiving: a post that has been live for two months with
no clicks and no impressions is not "waiting to rank" — it is dead weight in
wp_posts, its revisions, its postmeta and (mostly) in wp-content/uploads. WP
REST deletes use ``force=true`` so the row is removed rather than trashed;
trashing reclaims nothing.

Safety model — every one of these must pass before a single post is deleted:
  * metrics must be FRESH. A site whose last sync is missing or older than
    ``require_fresh_metrics_days`` is skipped entirely. A failed sync must never
    read as "nothing gets traffic".
  * a candidate must have a post_metrics row. Absent data means "not measured",
    not "no traffic" — see core/gsc.py on zero-impression pages.
  * age >= ``min_age_days`` AND clicks <= ``max_clicks`` AND
    impressions <= ``max_impressions``.
  * at most ``max_deletes_per_site_per_week`` deletions per site per rolling
    7 days, counted from prune_log. A wrong threshold costs 10 posts, not 3000.
  * attachments are only deleted when no other live post references them.
  * every attempt is written to prune_log, deleted or not.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from core import db, gsc
from core.wp import WPClient, WPError

logger = logging.getLogger(__name__)

DEFAULTS = {
    "enabled": True,
    "min_age_days": 60,
    "max_clicks": 0,
    "max_impressions": 2,
    "metrics_window_days": 28,
    "max_deletes_per_site_per_week": 10,
    "require_fresh_metrics_days": 7,
    "delete_media": True,
}


def config(settings: dict) -> dict:
    """Merge the ``prune:`` block from settings.yaml over the built-in defaults."""
    cfg = dict(DEFAULTS)
    cfg.update(settings.get("prune") or {})
    return cfg


# --------------------------------------------------------------------------- #
# Metrics sync
# --------------------------------------------------------------------------- #
def sync_metrics(sites: list[dict], settings: dict) -> dict:
    """Pull Search Console performance for every site into post_metrics.

    For each site we fetch the pages that earned impressions, then write a row
    for EVERY live post — the ones GSC returned get their real numbers, the ones
    it did not get explicit zeros. That distinction is what makes
    ``prune_candidates(require_metrics=True)`` safe: a post with no row means
    the sync never covered it, not that it has no traffic.
    """
    cfg = config(settings)
    window = int(cfg["metrics_window_days"])
    summary: dict = {"sites": 0, "posts": 0, "with_traffic": 0, "errors": []}

    for site in sites:
        site_id = site.get("id", "?")
        try:
            page_metrics = gsc.fetch_page_metrics(site, window_days=window)
        except Exception as exc:  # auth / permission / transport — never kill the sweep
            logger.warning("[%s] metrics sync skipped: %s", site_id, exc)
            summary["errors"].append(f"[{site_id}] {str(exc)[:300]}")
            continue

        live = db.live_posts_for_metrics(site_id)
        matched = 0
        for post in live:
            found = page_metrics.get(gsc.normalize_url(post.get("url")))
            db.upsert_post_metrics(
                site_id=site_id,
                wp_post_id=int(post["wp_post_id"]),
                url=post.get("url"),
                clicks=(found or {}).get("clicks", 0),
                impressions=(found or {}).get("impressions", 0),
                position=(found or {}).get("position"),
                window_days=window,
            )
            if found:
                matched += 1

        summary["sites"] += 1
        summary["posts"] += len(live)
        summary["with_traffic"] += matched
        logger.info("[%s] metrics: %d live post(s), %d with impressions in %dd",
                    site_id, len(live), matched, window)

    logger.info("metrics sync complete | sites=%d posts=%d with_traffic=%d errors=%d",
                summary["sites"], summary["posts"], summary["with_traffic"],
                len(summary["errors"]))
    return summary


# --------------------------------------------------------------------------- #
# Pruning
# --------------------------------------------------------------------------- #
def prune(sites: list[dict], settings: dict, dry_run: bool = False) -> dict:
    """Delete dead posts (and their images) from WordPress, capped per site."""
    cfg = config(settings)
    result: dict = {"deleted": [], "errors": [], "skipped_sites": [], "considered": 0}

    if not cfg.get("enabled", True):
        logger.info("pruner: disabled in settings (prune.enabled = false)")
        result["skipped_sites"].append("all: prune.enabled is false")
        return result

    for site in sites:
        site_id = site.get("id", "?")
        reason = _site_blocked(site_id, cfg)
        if reason:
            logger.warning("[%s] prune skipped: %s", site_id, reason)
            result["skipped_sites"].append(f"{site_id}: {reason}")
            continue

        allowance = _allowance(site_id, cfg)
        if allowance <= 0:
            msg = f"weekly cap reached ({cfg['max_deletes_per_site_per_week']})"
            logger.info("[%s] prune skipped: %s", site_id, msg)
            result["skipped_sites"].append(f"{site_id}: {msg}")
            continue

        candidates = db.prune_candidates(
            site_id=site_id,
            min_age_days=int(cfg["min_age_days"]),
            max_clicks=int(cfg["max_clicks"]),
            max_impressions=int(cfg["max_impressions"]),
            limit=allowance,
            require_metrics=True,
        )
        result["considered"] += len(candidates)
        if not candidates:
            logger.info("[%s] prune: nothing dead enough to delete", site_id)
            continue
        logger.info("[%s] prune: %d candidate(s), allowance %d",
                    site_id, len(candidates), allowance)

        wp = None
        if not dry_run:
            try:
                wp = WPClient(site)
            except WPError as exc:  # missing app password — skip, do not crash
                logger.warning("[%s] prune skipped: %s", site_id, exc)
                result["skipped_sites"].append(f"{site_id}: {exc}")
                continue

        for row in candidates:
            outcome = _prune_one(row, site_id, wp, cfg, dry_run)
            if outcome["outcome"] == "error":
                result["errors"].append(
                    f"[{site_id}] {row.get('title')}: {outcome['detail']}")
            else:
                result["deleted"].append(outcome)

    logger.info("prune complete | considered=%d deleted=%d errors=%d skipped_sites=%d",
                result["considered"], len(result["deleted"]), len(result["errors"]),
                len(result["skipped_sites"]))
    return result


def _site_blocked(site_id: str, cfg: dict) -> str | None:
    """Return a reason string if this site must not be pruned, else None."""
    synced_at = db.metrics_synced_at(site_id)
    if synced_at is None:
        return ("no Search Console metrics have ever synced — run "
                "`python main.py --sync-metrics` first")
    if synced_at.tzinfo is None:
        synced_at = synced_at.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - synced_at).days
    limit = int(cfg["require_fresh_metrics_days"])
    if age_days > limit:
        return (f"metrics are {age_days}d stale (limit {limit}d) — "
                f"refusing to prune on old data")
    return None


def _allowance(site_id: str, cfg: dict) -> int:
    """Remaining deletions for this site in the rolling 7-day window."""
    cap = int(cfg["max_deletes_per_site_per_week"])
    return max(0, cap - db.count_pruned_since(site_id, days=7))


def _prune_one(row: dict, site_id: str, wp, cfg: dict, dry_run: bool) -> dict:
    """Delete one post (+ its attachment) and record the attempt. Never raises."""
    post_id = int(row["id"])
    wp_post_id = int(row["wp_post_id"])
    title = row.get("title")
    url = row.get("url")
    clicks = int(row.get("clicks") or 0)
    impressions = int(row.get("impressions") or 0)
    position = row.get("position")
    age_days = int(row.get("age_days") or 0)
    media_id = row.get("featured_media_id")

    def record(outcome: str, detail: str | None = None) -> dict:
        # A dry run writes nothing, matching the rest of the pipeline: the
        # preview must be observable without leaving a trace in prune_log.
        if not dry_run:
            db.add_prune_log(site_id, wp_post_id, title, url, clicks, impressions,
                             position, age_days, media_id, outcome, detail)
        return {"site_id": site_id, "wp_post_id": wp_post_id, "title": title,
                "url": url, "clicks": clicks, "impressions": impressions,
                "age_days": age_days, "outcome": outcome, "detail": detail}

    if dry_run:
        logger.info("[%s] DRY-RUN: would delete post %d %r (%dd old, %d clicks, %d impr)",
                    site_id, wp_post_id, title, age_days, clicks, impressions)
        return record("would-delete")

    try:
        # Rows created before featured_media_id existed — ask WordPress once.
        if media_id is None:
            try:
                media_id = int(wp.get_post(wp_post_id).get("featured_media") or 0) or None
                if media_id:
                    db.set_post_media_id(post_id, media_id)
            except WPError as exc:
                logger.debug("[%s] could not read featured_media for %d (%s)",
                             site_id, wp_post_id, exc)

        wp.delete_post(wp_post_id, force=True)

        detail = None
        if media_id and cfg.get("delete_media", True):
            if db.media_in_use_elsewhere(site_id, int(media_id), post_id):
                detail = f"kept media {media_id} (still used by another live post)"
            else:
                try:
                    wp.delete_media(int(media_id))
                    detail = f"media {media_id} deleted"
                except WPError as exc:  # the post is gone; a stuck image is not fatal
                    detail = f"post deleted, media {media_id} failed: {str(exc)[:200]}"
                    logger.warning("[%s] %s", site_id, detail)

        db.mark_post_deleted(post_id)
        logger.info("[%s] deleted post %d %r (%dd, %d clicks, %d impr)%s",
                    site_id, wp_post_id, title, age_days, clicks, impressions,
                    f" - {detail}" if detail else "")
        return record("deleted", detail)
    except Exception as exc:
        logger.warning("[%s] delete failed for post %d (%s)", site_id, wp_post_id, exc)
        return record("error", str(exc)[:300])


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def render_report(result: dict, dry_run: bool = False) -> str:
    """Human-readable summary for the log and the email digest."""
    deleted = result.get("deleted") or []
    verb = "Would delete" if dry_run else "Deleted"
    lines = [f"{verb} {len(deleted)} post(s) (considered {result.get('considered', 0)})"]
    for d in deleted:
        lines.append(f"- [{d['site_id']}] {d.get('title') or '(untitled)'} "
                     f"- {d['age_days']}d, {d['clicks']} clicks, {d['impressions']} impr")
        if d.get("url"):
            lines.append(f"    {d['url']}")
    for key, label in (("skipped_sites", "Skipped sites"), ("errors", "Errors")):
        items = result.get(key) or []
        if items:
            lines.append(f"\n{label} ({len(items)}):")
            lines.extend(f"- {i}" for i in items)
    return "\n".join(lines)
