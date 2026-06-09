"""WordPress REST API client — one instance per site.

Constructed from a site config dict (see config/sites.yaml). Authenticates with
HTTP Basic using the site's wp_username and an application password read from
the env var named by ``app_password_env``. All calls use a 30s timeout and
raise WPError (with the response body) on non-2xx responses.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import requests
from requests.auth import HTTPBasicAuth

logger = logging.getLogger(__name__)

TIMEOUT = 30  # seconds, applied to every request

# Map common taxonomy names/slugs to their WP REST endpoint.
_TAXONOMY_ENDPOINTS = {
    "category": "categories",
    "categories": "categories",
    "tag": "tags",
    "tags": "tags",
    "post_tag": "tags",
}


class WPError(RuntimeError):
    """Raised when a WordPress REST call fails or auth is misconfigured."""


class WPClient:
    """Minimal WordPress REST client for posts, media and taxonomy terms."""

    def __init__(self, site: dict) -> None:
        self.site_id = site.get("id", "unknown")
        self.base = site["url"].rstrip("/") + "/wp-json/wp/v2"
        password = os.environ.get(site["app_password_env"], "").replace(" ", "")
        if not password:
            raise WPError(
                f"[{self.site_id}] no application password in env "
                f"{site['app_password_env']!r}"
            )
        self._auth = HTTPBasicAuth(site["wp_username"], password)

    # ---- internal ----------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base}/{path.lstrip('/')}"
        resp = requests.request(method, url, auth=self._auth, timeout=TIMEOUT, **kwargs)
        if not resp.ok:
            raise WPError(
                f"[{self.site_id}] {method} {url} -> {resp.status_code}: {resp.text[:500]}"
            )
        return resp

    # ---- posts -------------------------------------------------------------
    def create_post(self, payload: dict) -> dict:
        """Create a post. ``payload`` follows the WP /posts schema."""
        return self._request("POST", "posts", json=payload).json()

    def search_posts(self, query: str, per_page: int = 3) -> list[dict]:
        """Search posts; return a list of {title, link} for internal linking."""
        resp = self._request("GET", "posts", params={"search": query, "per_page": per_page})
        return [
            {"title": p.get("title", {}).get("rendered", ""), "link": p.get("link", "")}
            for p in resp.json()
        ]

    def list_recent_posts(self, days: int = 7, per_page: int = 100) -> list[dict]:
        """List posts published on this site in the last `days` (health check / audit)."""
        after = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        resp = self._request("GET", "posts", params={
            "after": after, "per_page": per_page, "status": "publish",
            "_fields": "id,link,date",
        })
        return [{"id": p.get("id"), "link": p.get("link"), "date": p.get("date")}
                for p in resp.json()]

    # ---- media -------------------------------------------------------------
    def upload_media(
        self,
        binary: bytes,
        filename: str,
        mime: str,
        alt_text: str = "",
        caption: str = "",
    ) -> dict:
        """Upload binary media; optionally set alt text/caption afterward."""
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Type": mime,
        }
        media = self._request("POST", "media", headers=headers, data=binary).json()
        if alt_text or caption:
            media = self._request(
                "POST", f"media/{media['id']}",
                json={"alt_text": alt_text, "caption": caption},
            ).json()
        return media

    # ---- taxonomy ----------------------------------------------------------
    def list_categories(self, per_page: int = 100) -> list[dict]:
        """List existing categories as [{id, name}], most-used first, for reuse."""
        resp = self._request("GET", "categories", params={
            "per_page": per_page, "_fields": "id,name", "orderby": "count", "order": "desc",
        })
        return [{"id": c.get("id"), "name": c.get("name", "")} for c in resp.json()]

    def get_or_create_term(self, name: str, taxonomy: str) -> int:
        """Return the term id for ``name`` in ``taxonomy``, reusing an existing
        term on a case-insensitive name match, and only creating one if none exists."""
        path = _TAXONOMY_ENDPOINTS.get(taxonomy, "tags")
        target = name.strip().lower()
        for term in self._request("GET", path, params={"search": name}).json():
            if term.get("name", "").strip().lower() == target:
                return int(term["id"])
        return int(self._request("POST", path, json={"name": name}).json()["id"])
