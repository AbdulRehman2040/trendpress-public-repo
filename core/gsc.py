"""Google Search Console client — trailing-window performance per URL.

Answers the only question the pruner cares about: for each published page, how
many clicks and impressions did it earn in the last N days, and at what average
position. That maps directly onto "is it ranked, does anybody read it".

Auth
----
A Google Cloud **service account** with the Search Console API enabled. Put the
JSON key in the ``GSC_CREDENTIALS_JSON`` env var (the whole JSON blob, or a path
to the file). Then add the service account's ``client_email`` as a user on each
Search Console property (Settings -> Users and permissions -> Add user, Full or
Restricted). A property the service account cannot see returns 403 and that site
is skipped — it is never treated as "zero traffic".

Zero-impression pages
---------------------
The Search Console API only returns rows for pages with at least one
impression; a page nobody ever saw is simply absent from the response. That is
exactly the signal the pruner wants, so ``fetch_page_metrics`` returns the map
of pages that DID get impressions and the caller records explicit zeros for
every live post missing from it. Absent-from-GSC and never-synced must not look
the same in the database, or a failed sync would read as "delete everything".
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, timedelta
from urllib.parse import urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

API = "https://searchconsole.googleapis.com/webmasters/v3/sites/{property}/searchAnalytics/query"
SCOPES = ["https://www.googleapis.com/auth/webmasters.readonly"]
ROW_LIMIT = 25000   # API maximum per page
TIMEOUT = 60
# GSC data lags real time; asking for the last two days returns nothing useful.
LAG_DAYS = 3


class GSCError(RuntimeError):
    """Raised when Search Console is unreachable, unauthorised, or misconfigured."""


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
def _credentials():
    """Build service-account credentials from GSC_CREDENTIALS_JSON (blob or path)."""
    raw = os.environ.get("GSC_CREDENTIALS_JSON", "").strip()
    if not raw:
        raise GSCError(
            "GSC_CREDENTIALS_JSON is not set. Create a Google Cloud service account, "
            "enable the Search Console API, and put its JSON key in this env var "
            "(locally in .env, in CI as a GitHub Actions secret)."
        )
    if not raw.lstrip().startswith("{"):  # treat as a path to the key file
        try:
            raw = open(raw, encoding="utf-8").read()
        except OSError as exc:
            raise GSCError(f"GSC_CREDENTIALS_JSON looks like a path but could not be read: {exc}")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GSCError(f"GSC_CREDENTIALS_JSON is not valid JSON: {exc}")

    try:
        from google.oauth2 import service_account
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise GSCError(
            "google-auth is not installed. Add `google-auth>=2.30` to requirements.txt."
        ) from exc
    return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)


def _access_token() -> str:
    """Mint a short-lived OAuth access token for the service account."""
    from google.auth.transport.requests import Request

    creds = _credentials()
    creds.refresh(Request())
    if not creds.token:
        raise GSCError("Search Console auth succeeded but returned no access token")
    return creds.token


# --------------------------------------------------------------------------- #
# URL normalisation — post.url and the GSC page key must compare equal
# --------------------------------------------------------------------------- #
def normalize_url(url: str | None) -> str:
    """Canonical form for matching: scheme/host lowercased, no query, no fragment,
    no trailing slash, and http/https treated as the same page."""
    if not url:
        return ""
    parts = urlsplit(str(url).strip())
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = (parts.path or "").rstrip("/")
    return urlunsplit(("https", host, path, "", ""))


def property_for(site: dict) -> str:
    """The Search Console property to query for a site.

    Uses the site's explicit ``gsc_property`` when set (needed for domain
    properties, which look like ``sc-domain:example.com``); otherwise falls back
    to the site URL as a URL-prefix property, which is what most people have.
    """
    explicit = (site.get("gsc_property") or "").strip()
    if explicit:
        return explicit
    url = str(site.get("url") or "").strip()
    if not url:
        raise GSCError(f"[{site.get('id')}] site has no url and no gsc_property")
    return url if url.endswith("/") else url + "/"


# --------------------------------------------------------------------------- #
# Query
# --------------------------------------------------------------------------- #
def fetch_page_metrics(site: dict, window_days: int = 28) -> dict[str, dict]:
    """Return {normalized_url: {clicks, impressions, position}} for the window.

    Only pages with at least one impression appear — see the module docstring.
    Raises GSCError on auth/permission/transport failure so the caller can skip
    the site rather than misread silence as zero traffic.
    """
    prop = property_for(site)
    end = date.today() - timedelta(days=LAG_DAYS)
    start = end - timedelta(days=int(window_days))
    token = _access_token()
    url = API.format(property=requests.utils.quote(prop, safe=""))

    metrics: dict[str, dict] = {}
    start_row = 0
    while True:
        body = {
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
            "dimensions": ["page"],
            "rowLimit": ROW_LIMIT,
            "startRow": start_row,
            "dataState": "final",
        }
        resp = requests.post(
            url, json=body, timeout=TIMEOUT,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        if resp.status_code in (401, 403):
            raise GSCError(
                f"[{site.get('id')}] Search Console denied access to {prop!r} "
                f"({resp.status_code}). Add the service account as a user on that "
                f"property, and check gsc_property matches it exactly."
            )
        if not resp.ok:
            raise GSCError(f"[{site.get('id')}] Search Console {resp.status_code}: {resp.text[:300]}")

        rows = resp.json().get("rows") or []
        for row in rows:
            keys = row.get("keys") or []
            if not keys:
                continue
            metrics[normalize_url(keys[0])] = {
                "clicks": int(row.get("clicks") or 0),
                "impressions": int(row.get("impressions") or 0),
                "position": float(row["position"]) if row.get("position") is not None else None,
            }
        if len(rows) < ROW_LIMIT:
            break
        start_row += ROW_LIMIT

    logger.info("[%s] GSC %s: %d page(s) with impressions over %d days (%s..%s)",
                site.get("id"), prop, len(metrics), window_days, start, end)
    return metrics


def list_properties() -> list[dict]:
    """Every Search Console property the service account can read.

    This is the setup check: if a property is missing here, the service account
    has not been added as a user on it, and that site can never be pruned.
    Returns [{siteUrl, permissionLevel}] sorted by siteUrl.
    """
    token = _access_token()
    resp = requests.get(
        "https://searchconsole.googleapis.com/webmasters/v3/sites",
        headers={"Authorization": f"Bearer {token}"},
        timeout=TIMEOUT,
    )
    if not resp.ok:
        raise GSCError(f"Search Console {resp.status_code}: {resp.text[:300]}")
    entries = resp.json().get("siteEntry") or []
    return sorted(entries, key=lambda e: e.get("siteUrl", ""))


def host_of(url: str | None) -> str:
    """Bare hostname, lowercased, without a www. prefix."""
    if not url:
        return ""
    raw = str(url).strip()
    if raw.startswith("sc-domain:"):
        host = raw[len("sc-domain:"):]
    else:
        host = urlsplit(raw if "//" in raw else "https://" + raw).netloc
    host = host.lower()
    return host[4:] if host.startswith("www.") else host
