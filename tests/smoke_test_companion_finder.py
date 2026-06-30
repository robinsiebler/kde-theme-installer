#!/usr/bin/env python3
"""
Live smoke test for companion_finder.py.

Run from the project root (or anywhere -- the script locates ../src
relative to its own location, not the current working directory):
    python3 tests/smoke_test_companion_finder.py

Fetches the real Magna-Dark-Global-6 theme, extracts its companion
links, resolves every one via the live OCS API, and prints out the
final bucketed result -- this is essentially the full pipeline for
"what would get downloaded and what would get installed" end to end.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ocs_client
import companion_finder

MAGNA_URL = "https://store.kde.org/p/2134200"


def main():
    print(f"Fetching primary theme: {MAGNA_URL}")
    provider_base = ocs_client.get_provider_base_url()
    primary = ocs_client.get_content_from_url(MAGNA_URL, provider_base=provider_base)
    print(f"  name={primary.name!r} typeid={primary.typeid} typename={primary.typename!r}")
    print()

    print("Extracting companion links from description...")
    links = companion_finder.extract_companion_links(primary.description_html)
    print(f"  found {len(links)} links")
    print()

    print("Resolving each companion link via the OCS API (this makes "
          f"{len(links)} live requests, may take a moment)...")
    resolved, failed = companion_finder.resolve_companions(links, provider_base=provider_base)
    print(f"  resolved: {len(resolved)}   failed: {len(failed)}")
    print()

    print("=" * 70)
    print("AUTO-INSTALL BUCKET")
    print("=" * 70)
    auto_items = [r for r in resolved if r.bucket == companion_finder.BUCKET_AUTO_INSTALL]
    for r in auto_items:
        dl = r.entry.primary_download
        size = f"{dl.size_kb} KB" if dl and dl.size_kb else "size unknown"
        print(f"  [{r.entry.typename:30}] {r.entry.name:35} ({size})  (label: {r.link.label})")
    print(f"  -> {len(auto_items)} items")
    print()

    print("=" * 70)
    print("DOWNLOAD-ONLY (v2) BUCKET")
    print("=" * 70)
    download_only_items = [r for r in resolved if r.bucket == companion_finder.BUCKET_DOWNLOAD_ONLY]
    for r in download_only_items:
        dl = r.entry.primary_download
        size = f"{dl.size_kb} KB" if dl and dl.size_kb else "size unknown"
        print(f"  [{r.entry.typename:30}] {r.entry.name:35} ({size})  (label: {r.link.label})")
    print(f"  -> {len(download_only_items)} items")
    print()

    print("=" * 70)
    print("UNKNOWN TYPE BUCKET (needs review -- new typeid we haven't seen)")
    print("=" * 70)
    unknown_items = [r for r in resolved if r.bucket == companion_finder.BUCKET_UNKNOWN]
    for r in unknown_items:
        print(f"  [typeid={r.entry.typeid} typename={r.entry.typename!r}] {r.entry.name}  (label: {r.link.label})")
    print(f"  -> {len(unknown_items)} items")
    print()

    if failed:
        print("=" * 70)
        print("FAILED LOOKUPS")
        print("=" * 70)
        for f in failed:
            print(f"  id={f.link.content_id}  label={f.link.label!r}  error={f.error}")
        print()

    print("Smoke test complete.")


if __name__ == "__main__":
    main()
