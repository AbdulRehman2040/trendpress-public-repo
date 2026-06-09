"""trendpress CLI orchestrator.

Runs the full daily pipeline — trends → matcher → writer → images → publisher →
digest — and a weekly --health kill-switch. Owns config loading, logging, site
selection (incl. skipping kill-switch-paused sites) and the digest assembly.

Exit code is non-zero only on total failure (an unhandled exception), so a cron
wrapper alerts on real breakage but not on isolated per-site publish errors.

Examples:
    python main.py --dry-run
    python main.py --sites site1,site2
    python main.py --canary-only
    python main.py --health
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv

from core import DATA_DIR, load_settings, load_sites
from core import db, notify
from pipeline import images, matcher, publisher, trends, writer

LOG_PATH = DATA_DIR / "trendpress.log"
logger = logging.getLogger("trendpress")


def setup_logging(verbose: bool = False) -> None:
    """Configure root logging: console + rotating file, with timestamps."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:  # trend titles contain unicode (smart quotes); keep the console safe
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


def select_sites(args: argparse.Namespace, skip_paused: bool = True) -> list[dict]:
    """Resolve which sites this run targets, applying CLI + kill-switch filters."""
    sites = load_sites()
    if args.sites:
        wanted = {s.strip() for s in args.sites.split(",") if s.strip()}
        sites = [s for s in sites if s.get("id") in wanted]
    if args.canary_only:
        sites = [s for s in sites if s.get("status") == "canary"]
    else:
        sites = [s for s in sites if s.get("status") != "paused"]
    if skip_paused:  # kill-switch: skip sites flagged paused in site_health
        kept = []
        for s in sites:
            if db.is_site_paused(s.get("id", "")):
                logger.warning("[%s] skipped: flagged paused by the kill-switch", s.get("id"))
            else:
                kept.append(s)
        sites = kept
    return sites


def paused_site_ids() -> list[str]:
    """All configured sites currently flagged paused (for the digest)."""
    return [s["id"] for s in load_sites() if db.is_site_paused(s.get("id", ""))]


def build_gemini():
    """Construct a GeminiClient, or return None if the SDK/key is unavailable.

    Imported lazily so the orchestrator still runs (e.g. --dry-run) when the
    google-genai SDK isn't installed or GEMINI_API_KEY isn't set.
    """
    try:
        from core.gemini import GeminiClient
        return GeminiClient()
    except Exception as exc:
        logger.warning("Gemini client unavailable (%s); safety filter will fail open", exc)
        return None


def run_daily(args: argparse.Namespace) -> None:
    """Run the full pipeline: trends → matcher → writer → images → publisher → digest."""
    settings = load_settings()
    db.init_db()
    sites = select_sites(args)
    gemini = build_gemini()
    logger.info(
        "Daily run start | dry_run=%s | sites=[%s]",
        args.dry_run, ", ".join(s.get("id", "?") for s in sites) or "none",
    )

    found = trends.get_safe_trends(settings, db, gemini)
    if args.dry_run:
        logger.info("Safe trends (%d):\n%s", len(found), trends.render_trends_table(found))
    assignments = matcher.assign_topics(found, sites, settings, gemini)
    if args.dry_run:
        logger.info("Assignment plan (%d):\n%s", len(assignments), matcher.render_plan(assignments))
    packages = writer.write_articles(assignments, sites, settings, gemini, db)
    media_map = images.attach_images(packages, sites, settings, db, dry_run=args.dry_run)
    if args.dry_run and packages:
        paths = [writer.write_preview(p) for p in packages]
        logger.info("Wrote %d preview file(s) to %s", len(paths), writer.PREVIEW_DIR)
    results = publisher.publish(packages, media_map, sites, settings, db, dry_run=args.dry_run)

    digest = _build_digest(args, found, assignments, packages, results, media_map, sites)
    notify.send_digest(digest, settings)

    logger.info(
        "Daily run complete | trends=%d assignments=%d articles=%d results=%d",
        len(found), len(assignments), len(packages), len(results),
    )


def _build_digest(args, found, assignments, packages, results, media_map, sites) -> dict:
    """Assemble the post-run summary for notify.send_digest."""
    site_by_id = {s.get("id"): s for s in sites}
    posts, errors = [], []
    for package, result in zip(packages, results):  # publisher returns 1 result per package, in order
        site_url = str(site_by_id.get(result.site_id, {}).get("url", "")).rstrip("/")
        edit_url = (f"{site_url}/wp-admin/post.php?post={result.wp_post_id}&action=edit"
                    if result.wp_post_id else None)
        posts.append({"site": result.site_id, "title": package.title,
                      "status": result.status, "edit_url": edit_url})
        if result.status == "error":
            errors.append(f"[{result.site_id}] {package.title}: {result.error}")

    assigned_ids = {a.trend.trend_id for a in assignments}
    skipped_trends = [t.title for t in found if t.trend_id not in assigned_ids]
    # In dry-run nothing is uploaded, so 'missing images' is not meaningful.
    missing_images = ([f"[{sid}] {tid}" for (sid, tid), mid in (media_map or {}).items() if mid is None]
                      if not args.dry_run else [])

    prefix = "[DRY-RUN] " if args.dry_run else ""
    return {
        "title": f"{prefix}trendpress daily — {len(posts)} post(s)",
        "posts": posts,
        "errors": errors,
        "skipped_trends": skipped_trends,
        "missing_images": missing_images,
        "paused_sites": paused_site_ids(),
    }


def run_staggered(args: argparse.Namespace) -> None:
    """Publish to each site one at a time, waiting --gap-minutes between sites.

    Trends are fetched once; then matcher->writer->images->publisher runs per
    site with a gap in between, so the network never posts simultaneously.
    used_topics dedup means each trend is used by at most one site per batch.
    ONE digest email is sent at the end with every link and any failures.
    """
    settings = load_settings()
    db.init_db()
    sites = select_sites(args)
    gemini = build_gemini()
    gap = max(0, int(getattr(args, "gap_minutes", 3)))
    dedupe_days = settings.get("dedupe_window_days", 7)
    logger.info("Staggered run | dry_run=%s | sites=%d | gap=%dmin",
                args.dry_run, len(sites), gap)

    # One safety call + ONE matcher call for the whole network (fewer Gemini calls);
    # then write/image/publish per site with a gap, so they don't post simultaneously.
    found = trends.get_safe_trends(settings, db, gemini)
    assignments = matcher.assign_topics(found, sites, settings, gemini)
    # Never run empty: give every active site a trend even if the AI matcher
    # scored few/none (e.g. niche-mismatched trends, or a Gemini outage).
    assignments = matcher.fill_uncovered(assignments, found, sites, settings)
    by_site: dict[str, list] = {}
    for assignment in assignments:
        by_site.setdefault(assignment.site_id, []).append(assignment)
    targets = [s for s in sites if by_site.get(s.get("id", ""))]  # only sites with work
    logger.info("Staggered: %d trend(s) -> %d assignment(s) across %d site(s)",
                len(found), len(assignments), len(targets))

    posts: list[dict] = []
    errors: list[str] = []
    for i, site in enumerate(targets):
        sid = site.get("id", "?")
        site_assignments = by_site.get(sid, [])
        logger.info("=== staggered [%d/%d] site %s (%d article(s)) ===",
                    i + 1, len(targets), sid, len(site_assignments))
        packages = writer.write_articles(site_assignments, [site], settings, gemini, db)
        media_map = images.attach_images(packages, [site], settings, db, dry_run=args.dry_run)
        if args.dry_run:
            for pkg in packages:
                writer.write_preview(pkg)
        results = publisher.publish(packages, media_map, [site], settings, db, dry_run=args.dry_run)

        site_url = str(site.get("url", "")).rstrip("/")
        for pkg, res in zip(packages, results):
            if res.status == "error":
                errors.append(f"[{sid}] {pkg.title}: {res.error}")
            else:
                posts.append({
                    "site": sid, "site_name": site.get("name"), "title": pkg.title,
                    "status": res.status, "url": res.url,
                    "edit_url": (f"{site_url}/wp-admin/post.php?post={res.wp_post_id}&action=edit"
                                 if res.wp_post_id else None),
                })
        if gap and not args.dry_run and i < len(targets) - 1:
            logger.info("waiting %d min before the next site...", gap)
            time.sleep(gap * 60)

    skipped = ([t.title for t in found if not db.is_topic_used(t.trend_id, dedupe_days)]
               if not args.dry_run else [])
    digest = {
        "title": f"{'[DRY-RUN] ' if args.dry_run else ''}trendpress — "
                 f"{len(posts)} post(s), {len(errors)} error(s)",
        "posts": posts,
        "errors": errors,
        "skipped_trends": skipped,
        "missing_images": [],
        "paused_sites": paused_site_ids(),
    }
    notify.send_digest(digest, settings)
    logger.info("Staggered run complete | posts=%d errors=%d", len(posts), len(errors))


def run_health(args: argparse.Namespace) -> None:
    """Weekly kill-switch: probe each site, set/clear its paused flag in site_health.

    We NEVER rewrite sites.yaml. The paused flag lives in the DB; select_sites()
    skips flagged sites on the daily run and the digest reports them.

    TODO (optional upgrade — Google Search Console auto-pause):
      Authenticate with a GSC service-account JSON (env GSC_CREDENTIALS_JSON) and
      call the Search Analytics API (searchanalytics.query) per verified site for
      the trailing 7 days vs the prior 7 days. If a site's total impressions drop
      by >50% week-over-week, treat it as a quality/penalty signal and auto-pause
      it here (db.add_site_health(site_id, note, paused=True)), surfacing it in the
      digest. Keep the existing reachability/failure checks as a fast first line.
    """
    settings = load_settings()
    db.init_db()
    sites = select_sites(args, skip_paused=False)  # evaluate all, incl. already-paused
    logger.info("Weekly health check start | sites=%d", len(sites))

    newly_paused = []
    for site in sites:
        site_id = site.get("id", "")
        try:  # one broken site must not stop the weekly sweep over the others
            note, paused = _check_site_health(site)
        except Exception as exc:
            note, paused = f"health check error: {str(exc)[:160]}", True
        try:
            db.add_site_health(site_id, note, paused=paused)
        except Exception as exc:
            logger.warning("[%s] could not record health (%s)", site_id, exc)
        logger.info("[%s] %s%s", site_id, "PAUSED — " if paused else "", note)
        if paused:
            newly_paused.append(f"{site_id}: {note}")

    notify.send_digest({
        "title": f"trendpress health check — {len(newly_paused)} paused",
        "posts": [], "errors": [], "skipped_trends": [], "missing_images": [],
        "paused_sites": newly_paused or paused_site_ids(),
    }, settings)
    logger.info("Weekly health check complete | %d site(s) paused", len(newly_paused))


def _check_site_health(site: dict) -> tuple[str, bool]:
    """Return (note, should_pause) for one site based on reachability + recent failures."""
    from core.wp import WPClient  # local import: only needed for the health check

    site_id = site.get("id", "")
    successes, errors = db.recent_post_stats(site_id, days=7)
    try:
        live = WPClient(site).list_recent_posts(days=7)
    except Exception as exc:
        return f"API unreachable: {str(exc)[:160]}", True
    if errors >= 3 and errors >= successes:
        return f"repeated publish failures: {errors} errors / {successes} ok (7d)", True
    return f"ok: {len(live)} live post(s), {successes} ok / {errors} errors (7d)", False


def build_parser() -> argparse.ArgumentParser:
    """Define the command-line interface."""
    p = argparse.ArgumentParser(
        prog="trendpress",
        description="Trend-based auto-publisher for a WordPress site network.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="run the pipeline but never POST to WordPress")
    p.add_argument("--sites", metavar="IDS",
                   help="comma-separated site ids to limit the run")
    p.add_argument("--canary-only", action="store_true",
                   help="only run sites with status=canary")
    p.add_argument("--health", action="store_true",
                   help="run the weekly kill-switch health check and exit")
    p.add_argument("--stagger", action="store_true",
                   help="publish to each site one at a time with a gap between them")
    p.add_argument("--gap-minutes", type=int,
                   default=int(os.environ.get("STAGGER_GAP_MIN", "3") or 3), metavar="N",
                   help="minutes to wait between sites in --stagger mode (env STAGGER_GAP_MIN, default 3)")
    p.add_argument("--verbose", action="store_true",
                   help="enable debug-level logging")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv()
    setup_logging(args.verbose)
    try:
        if args.health:
            run_health(args)
        elif args.stagger:
            run_staggered(args)
        else:
            run_daily(args)
    except Exception:
        logger.exception("Run failed")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
