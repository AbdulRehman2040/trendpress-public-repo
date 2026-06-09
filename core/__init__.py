"""Shared core utilities: config loading and common filesystem paths.

The client classes (GeminiClient, WPClient) and the db helpers live in their
own modules and are imported directly (e.g. ``from core.wp import WPClient``)
so this package init stays dependency-light and free of circular imports.
"""
from __future__ import annotations

from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"


def load_settings() -> dict:
    """Load global settings from config/settings.yaml."""
    with open(CONFIG_DIR / "settings.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_sites() -> list[dict]:
    """Load site profiles from config/sites.yaml as a list of dicts.

    Accepts either a top-level list (the documented format) or a mapping with a
    ``sites:`` key, and ignores any non-mapping entries (e.g. stray comments).
    """
    with open(CONFIG_DIR / "sites.yaml", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or []
    if isinstance(data, dict):
        data = data.get("sites", [])
    return [site for site in data if isinstance(site, dict)]


__all__ = ["PROJECT_ROOT", "CONFIG_DIR", "DATA_DIR", "load_settings", "load_sites"]
