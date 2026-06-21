"""One-time seeder: copy config/sites.yaml into the Neon `sites` table.

Run this ONCE after the Neon migration so the pipeline (which now reads sites
from the DB via core.load_sites) keeps your existing 10 site profiles. It is
idempotent — re-running upserts, so it is safe to run again after editing
config/sites.yaml.

The WordPress application password is NEVER stored in the DB; only the NAME of
its env var (app_password_env) is copied. Those secrets stay in .env / GitHub
Actions secrets exactly as before.

Usage:
    python scripts/import_sites.py            # requires DATABASE_URL in env/.env
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running as `python scripts/import_sites.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from core import load_sites_from_yaml
from core import db


def main() -> int:
    load_dotenv()
    db.init_db()

    sites = load_sites_from_yaml()
    if not sites:
        print("No sites found in config/sites.yaml — nothing to import.")
        return 1

    imported = 0
    for site in sites:
        site_id = site.get("id")
        if not site_id:
            print(f"  ! skipping entry without an id: {site!r}")
            continue
        db.upsert_site(site)
        imported += 1
        print(f"  + {site_id:<8} {site.get('name', ''):<24} [{site.get('status', 'active')}]")

    total = len(db.load_sites_from_db())
    print(f"\nImported/updated {imported} site(s). The sites table now holds {total} site(s).")
    print("The pipeline will read these on its next run (config/sites.yaml is no longer read at runtime).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
