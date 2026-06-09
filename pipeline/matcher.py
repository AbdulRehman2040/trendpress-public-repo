"""Stage 2 — assign trends to sites WITHOUT broadcasting.

Public entry point: ``assign_topics(trends, sites, settings, gemini) -> list[Assignment]``.

Why this stage exists: publishing the same trending topic across the whole site
network is textbook "scaled content abuse" and gets networks deindexed by
Google. So we score how naturally each topic fits each site (one Gemini call),
then deterministically spread topics across a *subset* of sites — capping both
how many sites may run a single topic and how many posts a site may take per
day. It is expected and healthy that some sites get nothing and some trends are
never used.

Failure policy: scoring requires Gemini. If Gemini is unavailable or the call
fails, this stage returns NO assignments (it refuses to broadcast blindly)
rather than guessing — a loud warning is logged. Nothing is written to
used_topics here; that happens only after a successful publish.
"""
from __future__ import annotations

import json
import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from core import db
from . import Assignment, Trend

if TYPE_CHECKING:  # avoid importing the google-genai SDK at module load time
    from core.gemini import GeminiClient

logger = logging.getLogger(__name__)

_SCORING_SYSTEM = (
    "You are a UK content strategist deciding which niche websites should cover "
    "which trending topics, protecting the network from scaled-content-abuse "
    "penalties by avoiding broadcasting one topic to every site."
)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def assign_topics(
    trends: list[Trend],
    sites: list[dict],
    settings: dict,
    gemini: "GeminiClient | None",
) -> list[Assignment]:
    """Score topic/site fit (one Gemini call) and spread topics across sites."""
    sites = [s for s in sites if s.get("status") != "paused"]
    if not trends or not sites:
        logger.info("matcher: nothing to assign (%d trends, %d sites)", len(trends), len(sites))
        return []

    scores = _score_with_gemini(trends, sites, gemini)
    if not scores:
        return []  # warning already logged by _score_with_gemini

    assignments = _select(scores, trends, sites, settings)
    for line in _summary_lines(assignments):
        logger.info("%s", line)
    return assignments


def fill_uncovered(
    assignments: list[Assignment], trends: list[Trend], sites: list[dict], settings: dict,
) -> list[Assignment]:
    """Guarantee every active site gets at least ONE assignment so a run is never
    empty. Any site the AI matcher left uncovered is given the currently
    least-used trend (round-robin), with a local angle. Used by the staggered run.
    """
    active = [s for s in sites if s.get("status") != "paused"]
    if not trends or not active:
        return assignments
    covered = {a.site_id for a in assignments}
    uncovered = [s for s in active if s.get("id") not in covered]
    if not uncovered:
        return assignments

    use_count: Counter = Counter(a.trend.trend_id for a in assignments)
    for trend in trends:
        use_count.setdefault(trend.trend_id, 0)
    for site in uncovered:
        trend = min(trends, key=lambda t: use_count[t.trend_id])  # least-used -> spread
        use_count[trend.trend_id] += 1
        angle = (site.get("angle_pool") or ["news report"])[0]
        assignments.append(Assignment(trend=trend, site_id=site["id"],
                                      score=int(settings.get("min_match_score", 5)), angle=angle))
    logger.info("matcher: round-robin filled %d uncovered site(s) (never run empty)",
                len(uncovered))
    return assignments


# --------------------------------------------------------------------------- #
# Scoring — exactly one Gemini call
# --------------------------------------------------------------------------- #
def _score_with_gemini(
    trends: list[Trend], sites: list[dict], gemini: "GeminiClient | None"
) -> list[dict]:
    """Ask Gemini to score every (trend, site) pair; return [] if unavailable."""
    if gemini is None:
        logger.warning(
            "matcher: Gemini unavailable — cannot score topic/site fit; making no "
            "assignments (refusing to broadcast blindly)"
        )
        return []
    try:
        raw = gemini.generate_json(_scoring_prompt(trends, sites), system=_SCORING_SYSTEM)
    except Exception as exc:  # network / quota / parse — never crash the run
        logger.warning("matcher: Gemini scoring failed (%s); making no assignments", exc)
        return []
    return _extract_scores(raw)


def _scoring_prompt(trends: list[Trend], sites: list[dict]) -> str:
    trend_payload = [
        {"trend_id": t.trend_id, "title": t.title, "top_snippet": _top_snippet(t)}
        for t in trends
    ]
    site_payload = [
        {"site_id": s["id"], "niche": s.get("niche", ""), "audience": s.get("audience", "")}
        for s in sites
    ]
    return (
        "Score how naturally each trending topic fits each website's niche and "
        "audience, on a 0-10 scale.\n\n"
        "Be STRICT: a generic national news story does NOT automatically fit a "
        "niche site. Reserve a score of 8 or higher for strong, natural fits "
        "where the site's specific readers would genuinely expect this story. "
        "Score weak or merely-tangential fits low.\n\n"
        "Return ONLY strict JSON of the form:\n"
        '{"scores": [{"trend_id": "...", "site_id": "...", "score": 0-10, '
        '"angle": "one short editorial angle tailored to this site", '
        '"reason": "one line"}]}\n'
        "Include an entry for every topic/site pair you consider relevant; you "
        "may omit clearly irrelevant pairs. No markdown, no commentary.\n\n"
        "TOPICS:\n" + json.dumps(trend_payload, ensure_ascii=False) + "\n\n"
        "SITES:\n" + json.dumps(site_payload, ensure_ascii=False)
    )


def _extract_scores(raw: Any) -> list[dict]:
    """Normalize Gemini output to a flat list of score dicts."""
    if isinstance(raw, dict):
        for key in ("scores", "results", "assignments", "data"):
            if isinstance(raw.get(key), list):
                return [s for s in raw[key] if isinstance(s, dict)]
        return [raw] if raw.get("trend_id") else []
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict)]
    return []


# --------------------------------------------------------------------------- #
# Selection — deterministic Python (not the LLM)
# --------------------------------------------------------------------------- #
def _select(
    scores: list[dict], trends: list[Trend], sites: list[dict], settings: dict
) -> list[Assignment]:
    """Greedily assign highest-scoring pairs within the per-trend & per-site caps."""
    min_score = int(settings.get("min_match_score", 7))
    max_sites = int(settings.get("max_sites_per_topic", 4))
    trend_by_id = {t.trend_id: t for t in trends}
    site_by_id = {s["id"]: s for s in sites}
    remaining = {s["id"]: _remaining_cap(s, settings) for s in sites}

    candidates = _valid_candidates(scores, trend_by_id, site_by_id, min_score)
    # Deterministic: highest score wins first, ties broken by ids.
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))

    per_trend: dict[str, int] = {}
    assignments: list[Assignment] = []
    for score, trend_id, site_id, entry in candidates:
        if per_trend.get(trend_id, 0) >= max_sites:
            continue  # this topic already on enough sites
        if remaining.get(site_id, 0) <= 0:
            continue  # this site is at its daily cap
        angle = _resolve_angle(entry.get("angle"), site_by_id[site_id])
        assignments.append(
            Assignment(trend=trend_by_id[trend_id], site_id=site_id, score=score, angle=angle)
        )
        per_trend[trend_id] = per_trend.get(trend_id, 0) + 1
        remaining[site_id] -= 1
    return assignments


def _valid_candidates(
    scores: list[dict], trend_by_id: dict, site_by_id: dict, min_score: int
) -> list[tuple[int, str, str, dict]]:
    """Filter raw scores to known, on-threshold (trend, site) pairs."""
    out: list[tuple[int, str, str, dict]] = []
    for entry in scores:
        trend_id = entry.get("trend_id")
        site_id = entry.get("site_id")
        if not isinstance(trend_id, str) or not isinstance(site_id, str):
            continue
        score = _as_int(entry.get("score"))
        if trend_id in trend_by_id and site_id in site_by_id and score >= min_score:
            out.append((score, trend_id, site_id, entry))
    return out


def _remaining_cap(site: dict, settings: dict) -> int:
    """Per-site remaining capacity today = daily cap minus posts already made."""
    cap = int(site.get("posts_per_day_cap", settings.get("posts_per_day_cap_default", 2)))
    return max(0, cap - db.count_posts_today(site["id"]))


def _resolve_angle(suggested: Any, site: dict) -> str:
    """Prefer the Gemini angle; else rotate through the site's angle_pool."""
    if suggested and str(suggested).strip():
        return str(suggested).strip()
    pool = site.get("angle_pool") or []
    if not pool:
        return "general explainer"
    return pool[db.next_angle_index(site["id"]) % len(pool)]


# --------------------------------------------------------------------------- #
# Helpers & reporting
# --------------------------------------------------------------------------- #
def _top_snippet(trend: Trend) -> str:
    if not trend.news_items:
        return ""
    first = trend.news_items[0]
    return first.snippet or first.title


def _as_int(value: Any) -> int:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return -1


def _summary_lines(assignments: list[Assignment]) -> list[str]:
    """Readable plan: one line per topic plus a per-site count line (requirement 3)."""
    if not assignments:
        return ["matcher: no assignments this run"]
    by_trend: dict[str, list[Assignment]] = {}
    for a in assignments:
        by_trend.setdefault(a.trend.trend_id, []).append(a)

    lines = []
    for group in by_trend.values():
        group.sort(key=lambda a: (-a.score, a.site_id))
        parts = ", ".join(f"{a.site_id} ({a.score}, {a.angle})" for a in group)
        lines.append(f"TOPIC '{group[0].trend.title}' → {parts}")

    counts: dict[str, int] = {}
    for a in assignments:
        counts[a.site_id] = counts.get(a.site_id, 0) + 1
    per_site = ", ".join(f"{sid}: {n}" for sid, n in sorted(counts.items()))
    lines.append(f"per-site counts: {per_site}")
    return lines


def render_plan(assignments: list[Assignment]) -> str:
    """Render the assignment plan for --dry-run output."""
    return "\n".join(_summary_lines(assignments))


# --------------------------------------------------------------------------- #
# Standalone smoke test:  python -m pipeline.matcher
# --------------------------------------------------------------------------- #
def _make_console_utf8() -> None:
    """Keep stdout/stderr UTF-8 so the '→' in the plan is safe on Windows."""
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass


def _main() -> None:
    _make_console_utf8()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from core import load_settings, load_sites
    from pipeline import trends as trends_mod

    gemini = None
    try:
        from core.gemini import GeminiClient
        gemini = GeminiClient()
    except Exception as exc:  # SDK missing or no API key
        logging.getLogger(__name__).warning("Gemini unavailable for smoke test: %s", exc)

    db.init_db()
    settings = load_settings()
    sites = [s for s in load_sites() if s.get("status") != "paused"]
    found = trends_mod.get_safe_trends(settings, db, gemini)
    plan = assign_topics(found, sites, settings, gemini)
    print(render_plan(plan))


if __name__ == "__main__":
    _main()
