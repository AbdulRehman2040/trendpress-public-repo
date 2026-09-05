"""Search Console setup checker — run this before the first --sync-metrics.

Answers three questions in one command:

  1. Do the credentials work at all?
  2. Which of your sites has the service account actually been granted on?
  3. For each match, is it a URL-prefix or a domain property — i.e. what should
     ``sites.gsc_property`` be set to?

A site that shows MISSING here can never be pruned: the pruner refuses to act
without metrics, and metrics for that site will never arrive.

Usage:
    python scripts/gsc_check.py            # report only, changes nothing
    python scripts/gsc_check.py --apply    # also write gsc_property for matches
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running as `python scripts/gsc_check.py` from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from core import db, gsc


def match_property(site: dict, properties: list[dict]) -> tuple[str | None, str]:
    """Best property for a site: (siteUrl or None, kind).

    A domain property (``sc-domain:example.com``) covers every subdomain and
    both schemes, so it is preferred when both kinds exist — it is the one that
    will not silently miss traffic on a www/non-www variant.
    """
    host = gsc.host_of(site.get("url"))
    if not host:
        return None, "no url"
    domain_hit = url_hit = None
    for entry in properties:
        prop = entry.get("siteUrl", "")
        if gsc.host_of(prop) != host:
            continue
        if prop.startswith("sc-domain:"):
            domain_hit = prop
        else:
            url_hit = prop
    if domain_hit:
        return domain_hit, "domain"
    if url_hit:
        return url_hit, "url-prefix"
    return None, "MISSING"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check Search Console access for every site.")
    parser.add_argument("--apply", action="store_true",
                        help="write the matched property into sites.gsc_property")
    args = parser.parse_args()

    load_dotenv()
    db.init_db()

    try:
        properties = gsc.list_properties()
    except gsc.GSCError as exc:
        print(f"FAILED: {exc}\n")
        print("Fix the credentials first — see the header of core/gsc.py.")
        return 1

    print(f"Service account can read {len(properties)} Search Console propert"
          f"{'y' if len(properties) == 1 else 'ies'}:")
    for entry in properties:
        print(f"  - {entry.get('siteUrl'):<45} {entry.get('permissionLevel', '')}")

    sites = db.load_sites_from_db()
    if not sites:
        print("\nNo sites in the database. Run `python scripts/import_sites.py` first.")
        return 1

    print(f"\nMatching {len(sites)} configured site(s):\n")
    print(f"  {'SITE':<10} {'HOST':<28} {'KIND':<12} PROPERTY")
    matched, missing = 0, []
    for site in sites:
        site_id = site.get("id", "?")
        prop, kind = match_property(site, properties)
        host = gsc.host_of(site.get("url")) or "-"
        print(f"  {site_id:<10} {host:<28} {kind:<12} {prop or '-'}")
        if prop:
            matched += 1
            # Only URL-prefix properties are the default; a domain property must
            # be stored explicitly or fetch_page_metrics would query the wrong one.
            if args.apply and (kind == "domain" or site.get("gsc_property") != prop):
                site["gsc_property"] = prop
                db.upsert_site(site)
        else:
            missing.append(site_id)

    print(f"\n{matched}/{len(sites)} site(s) reachable.")
    if missing:
        print(f"\nMISSING ({len(missing)}): {', '.join(missing)}")
        print("These will never produce metrics, so they can never be pruned.")
        print("In Search Console open each property -> Settings -> Users and permissions")
        print("-> Add user -> paste the service account's client_email -> Full or Restricted.")
    if args.apply:
        print("\ngsc_property written for every matched site.")
    elif matched:
        print("\nRe-run with --apply to save these properties onto the sites table.")
    return 0 if matched else 1


if __name__ == "__main__":
    raise SystemExit(main())
