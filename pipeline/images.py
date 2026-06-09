"""Stage 4 — attach a free-licensed featured image to each ArticlePackage.

Public entry point:
    ``attach_images(packages, sites, settings, db, dry_run) -> dict``
    returns {(site_id, trend_id): media_id | None}.

Sourcing strategy (all images must be licensed for commercial reuse):
  1. Pexels search on the article's image_query (cached once per trend_id so a
     single API call serves every site covering that trend — free tier is
     rate-limited).
  2. Each site covering the same trend gets a DIFFERENT photo (tracked via the
     used_images table + an in-run set) to reduce the network's footprint; if
     the photo pool is exhausted, reuse is allowed but logged.
  3. The chosen photo is downloaded, resized to <=1200px wide and re-encoded as
     JPEG q80 (Pillow), then uploaded to WordPress with a correct attribution
     caption. The returned media id is stored on the package.

Fallback order when a Pexels search yields nothing:
  (a) simplify the query to its first word and re-search Pexels;
  (b) query Openverse (license_type=commercial) and use its CC attribution;
  (c) proceed with no image and flag it (None in the returned map / log).

We NEVER use the trend's RSS picture_url — those are news-publisher images and
are not licensed for reuse. Nothing here raises to the orchestrator: any error
degrades to "no image" for that package.
"""
from __future__ import annotations

import html as html_lib
import io
import logging
import os
from dataclasses import dataclass

import requests

from core.wp import WPClient
from . import ArticlePackage

logger = logging.getLogger(__name__)

PEXELS_SEARCH_URL = "https://api.pexels.com/v1/search"
OPENVERSE_SEARCH_URL = "https://api.openverse.org/v1/images/"
PER_PAGE = 15
MAX_WIDTH = 1200
JPEG_QUALITY = 80
SEARCH_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 30
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # safety cap against oversized/hostile images
USER_AGENT = "trendpress/1.0 (+https://github.com/trendpress)"


@dataclass
class _Candidate:
    """A normalized image candidate from either provider."""

    photo_id: str          # str so Pexels ints and Openverse uuids unify
    download_url: str
    caption_html: str      # provider-appropriate attribution
    source: str            # "pexels" | "openverse"


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def attach_images(
    packages: list[ArticlePackage],
    sites: list[dict],
    settings: dict,
    db,
    dry_run: bool = False,
) -> dict:
    """Attach a featured image to each package; return {(site,trend): media_id|None}."""
    site_by_id = {s["id"]: s for s in sites}
    api_key = os.environ.get("PEXELS_API_KEY", "")
    if not api_key:
        logger.warning("images: PEXELS_API_KEY not set; relying on Openverse fallback only")

    results: dict[tuple[str, str], int | None] = {}
    search_cache: dict[str, list[_Candidate]] = {}
    used_by_trend: dict[str, set[str]] = {}

    for package in packages:
        key = (package.site_id, package.trend_id)
        try:
            results[key] = _attach_one(
                package, site_by_id, search_cache, used_by_trend, api_key, db, dry_run
            )
        except Exception as exc:  # absolute backstop — never crash the orchestrator
            logger.warning("images: [%s] unexpected error for %r (%s); no image",
                           package.site_id, package.title, exc)
            results[key] = None

    attached = sum(1 for v in results.values() if v)
    logger.info("images: %d attached, %d without image (dry_run=%s)",
                attached, len(results) - attached, dry_run)
    return results


def _attach_one(
    package: ArticlePackage,
    site_by_id: dict[str, dict],
    search_cache: dict[str, list[_Candidate]],
    used_by_trend: dict[str, set[str]],
    api_key: str,
    db,
    dry_run: bool,
) -> int | None:
    """Source, (optionally) process and upload one package's image; return media id."""
    site = site_by_id.get(package.site_id)
    if site is None:
        logger.warning("images: no site config for %r; skipping", package.site_id)
        return None

    candidates = _candidates_for_trend(package, search_cache, api_key)
    used = used_by_trend.setdefault(package.trend_id, _seed_used(db, package.trend_id))
    chosen = _pick_unused(candidates, used)
    if chosen is None:
        logger.warning("images: [%s] no licensed image found for %r — FLAG for digest",
                       package.site_id, package.title)
        return None

    # Reserve the photo in-memory at SELECTION time (not after upload) so other
    # sites covering this trend pick a different one — in both dry-run and real
    # runs. Dry-run needs this for per-site variety; the DB (the cross-run source
    # of truth) is written only after a real successful upload, below.
    used.add(chosen.photo_id)

    if dry_run:
        logger.info("images: [%s] would use %s photo %s -> %s",
                    package.site_id, chosen.source, chosen.photo_id, chosen.download_url)
        return None

    media_id = _process_and_upload(chosen, package, site)
    if media_id is not None:
        package.featured_media_id = media_id
        _record_used(db, package, chosen)
    return media_id


# --------------------------------------------------------------------------- #
# Candidate sourcing (cached once per trend) + fallback chain
# --------------------------------------------------------------------------- #
def _candidates_for_trend(
    package: ArticlePackage, cache: dict[str, list[_Candidate]], api_key: str
) -> list[_Candidate]:
    """Return image candidates for the trend, searching (and caching) on first use."""
    if package.trend_id in cache:
        return cache[package.trend_id]

    query = (package.image_query or package.focus_keyword or package.title).strip()
    candidates = _pexels_search(query, api_key)

    if not candidates:  # fallback (a): simplify to the first word
        first_word = query.split()[0] if query.split() else ""
        if first_word and first_word.lower() != query.lower():
            logger.info("images: Pexels empty for %r; retrying with %r", query, first_word)
            candidates = _pexels_search(first_word, api_key)

    if not candidates:  # fallback (b): Openverse (CC-licensed)
        logger.info("images: Pexels empty for trend %s; trying Openverse", package.trend_id)
        candidates = _openverse_search(query)

    cache[package.trend_id] = candidates
    return candidates


def _pick_unused(candidates: list[_Candidate], used: set[str]) -> _Candidate | None:
    """First candidate not yet used for this trend; reuse the first if exhausted."""
    for candidate in candidates:
        if candidate.photo_id not in used:
            return candidate
    if candidates:
        logger.info("images: photo pool exhausted for trend; reusing a photo")
        return candidates[0]
    return None


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
def _pexels_search(query: str, api_key: str) -> list[_Candidate]:
    if not api_key or not query:
        return []
    try:
        resp = requests.get(
            PEXELS_SEARCH_URL,
            headers={"Authorization": api_key},
            params={"query": query, "per_page": PER_PAGE, "orientation": "landscape"},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        photos = resp.json().get("photos", [])
    except (requests.RequestException, ValueError) as exc:  # ValueError = bad JSON
        logger.warning("images: Pexels search failed for %r (%s)", query, exc)
        return []
    out: list[_Candidate] = []
    for photo in photos:
        src = photo.get("src") or {}
        url = src.get("large2x") or src.get("large") or src.get("original")
        if not url:
            continue
        out.append(_Candidate(
            photo_id=str(photo.get("id")),
            download_url=url,
            caption_html=_pexels_caption(photo),
            source="pexels",
        ))
    return out


def _openverse_search(query: str) -> list[_Candidate]:
    if not query:
        return []
    try:
        resp = requests.get(
            OPENVERSE_SEARCH_URL,
            params={"q": query, "license_type": "commercial", "page_size": PER_PAGE},
            headers={"User-Agent": USER_AGENT},
            timeout=SEARCH_TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("results", [])
    except (requests.RequestException, ValueError) as exc:  # ValueError = bad JSON
        logger.warning("images: Openverse search failed for %r (%s)", query, exc)
        return []
    out: list[_Candidate] = []
    for item in items:
        url = item.get("url")
        if not url:
            continue
        out.append(_Candidate(
            photo_id=str(item.get("id")),
            download_url=url,
            caption_html=_openverse_caption(item),
            source="openverse",
        ))
    return out


def _pexels_caption(photo: dict) -> str:
    name = html_lib.escape(photo.get("photographer", "Pexels"))
    photographer = _link(photo.get("url", "https://www.pexels.com"), name)
    return f'Photo by {photographer} on {_link("https://www.pexels.com", "Pexels")}'


def _openverse_caption(item: dict) -> str:
    """Build a clickable CC attribution caption (Title - Author - Source - License)."""
    title = html_lib.escape(item.get("title") or "Image")
    creator = html_lib.escape(item.get("creator") or "Unknown")
    landing = item.get("foreign_landing_url") or item.get("url") or ""
    license_label = _cc_license_label(item.get("license"), item.get("license_version"))
    license_url = item.get("license_url") or ""

    work = _link(landing, title) if landing else title
    author = _link(item.get("creator_url"), creator) if item.get("creator_url") else creator
    license_html = _link(license_url, license_label) if license_url else html_lib.escape(license_label)
    if not (item.get("creator") or item.get("license")):  # last resort: provider's string
        return html_lib.escape(item.get("attribution") or f"Image via {item.get('provider', 'Openverse')}")
    return f'{work} by {author} is licensed under {license_html}.'


def _cc_license_label(code: str | None, version: str | None) -> str:
    """Turn an Openverse license code into a proper label, e.g. 'CC BY-SA 4.0', 'CC0 1.0'."""
    code = (code or "").lower()
    base = "CC0" if code == "cc0" else f"CC {code.upper()}" if code else "CC"
    return f"{base} {version}".strip() if version else base


def _link(url: str | None, text: str) -> str:
    href = html_lib.escape(url or "", quote=True)
    return f'<a href="{href}" target="_blank" rel="nofollow noopener">{text}</a>'


# --------------------------------------------------------------------------- #
# Download, process (Pillow), upload
# --------------------------------------------------------------------------- #
def _process_and_upload(
    candidate: _Candidate, package: ArticlePackage, site: dict
) -> int | None:
    """Download + resize + upload; return the WP media id, or None on any failure."""
    try:
        raw = _download(candidate.download_url)
        jpeg = _to_jpeg(raw)
    except Exception as exc:  # network / decode / Pillow — degrade to no image
        logger.warning("images: [%s] processing failed for %s (%s)",
                       package.site_id, candidate.download_url, exc)
        return None
    try:
        media = WPClient(site).upload_media(
            jpeg, f"{package.slug}.jpg", "image/jpeg",
            alt_text=package.title, caption=candidate.caption_html,
        )
        media_id = int(media["id"])
        logger.info("images: [%s] uploaded media %d (%s, %d bytes)",
                    package.site_id, media_id, candidate.source, len(jpeg))
        return media_id
    except Exception as exc:
        logger.warning("images: [%s] upload failed (%s)", package.site_id, exc)
        return None


def _download(url: str) -> bytes:
    """Stream the image with a hard size cap so a hostile/oversized URL can't OOM us."""
    with requests.get(url, headers={"User-Agent": USER_AGENT},
                      timeout=DOWNLOAD_TIMEOUT, stream=True) as resp:
        resp.raise_for_status()
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
            raise ValueError(f"image too large: {declared} bytes")
        buffer = bytearray()
        for chunk in resp.iter_content(chunk_size=65536):
            buffer.extend(chunk)
            if len(buffer) > MAX_DOWNLOAD_BYTES:
                raise ValueError("image exceeded max download size")
        return bytes(buffer)


def _to_jpeg(raw: bytes) -> bytes:
    """Resize to <=MAX_WIDTH wide (downscale only) and encode as JPEG q80."""
    from PIL import Image  # lazy: Pillow only needed on the real (non-dry-run) path

    with Image.open(io.BytesIO(raw)) as img:
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.width > MAX_WIDTH:
            height = round(img.height * MAX_WIDTH / img.width)
            img = img.resize((MAX_WIDTH, height), Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        return buffer.getvalue()


# --------------------------------------------------------------------------- #
# used_images bookkeeping
# --------------------------------------------------------------------------- #
def _seed_used(db, trend_id: str) -> set[str]:
    try:
        return {str(pid) for pid in db.images_used_for_trend(trend_id)}
    except Exception:  # never block image selection on a db read error
        return set()


def _record_used(db, package: ArticlePackage, candidate: _Candidate) -> None:
    try:
        db.add_used_image(package.trend_id, package.site_id, candidate.photo_id)
    except Exception as exc:
        logger.debug("images: could not record used image (%s)", exc)
