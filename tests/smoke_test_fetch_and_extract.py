#!/usr/bin/env python3
"""
Live smoke test for fetch_and_extract.py.

Run from the project root (or anywhere -- the script locates ../src
relative to its own location, not the current working directory):
    python3 tests/smoke_test_fetch_and_extract.py

Downloads and extracts a few real items from the Magna theme set --
the primary global theme itself (small) plus one icon pack (larger,
exercises a bigger zip/tar than anything tested so far) -- verifying
md5 checksums and inspecting the extracted structure.

Writes everything into ./test_cache/ in the current directory so it's
easy to inspect afterward and easy to delete when done.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ocs_client
import fetch_and_extract as fe

MAGNA_GLOBAL_THEME_URL = "https://store.kde.org/p/2134200"
MAGNA_DARK_ICONS_ID = "2102240"  # ~6 MB icon pack, good size test

CACHE_DIR = Path("./test_cache")


def run_one(label: str, entry: ocs_client.ContentEntry):
    print(f"--- {label}: {entry.name} ---")
    dl = entry.primary_download
    if dl is None:
        print("  SKIP: no downloads available for this entry")
        return None

    print(f"  fetching {dl.filename} ({dl.size_kb} KB)...")
    try:
        result = fe.fetch_and_extract(entry, CACHE_DIR)
    except fe.FetchError as exc:
        print(f"  FAIL: {exc}")
        return None

    print(f"  OK: cache_dir={result.cache_dir}")
    print(f"      raw_file={result.raw_file} (exists: {result.raw_file.exists()}, "
          f"size: {result.raw_file.stat().st_size} bytes)")
    print(f"      archive_format={result.archive_format}")
    print(f"      md5_verified={result.md5_verified}")

    extracted_items = list(result.extracted_dir.rglob("*"))
    print(f"      extracted_dir={result.extracted_dir} ({len(extracted_items)} files/dirs)")
    # Show a small sample of what's in there
    for item in sorted(extracted_items)[:8]:
        rel = item.relative_to(result.extracted_dir)
        print(f"        {'[dir] ' if item.is_dir() else '       '}{rel}")
    if len(extracted_items) > 8:
        print(f"        ... and {len(extracted_items) - 8} more")

    # Write the manifest too, exercising that code path
    manifest_path = fe.write_manifest(
        result.cache_dir, entry, result, bucket="auto_install"
    )
    print(f"      manifest written to {manifest_path}")
    print()
    return result


def main():
    print(f"Cache directory: {CACHE_DIR.resolve()}")
    print()

    provider_base = ocs_client.get_provider_base_url()

    print("Fetching primary theme metadata...")
    primary_entry = ocs_client.get_content_from_url(
        MAGNA_GLOBAL_THEME_URL, provider_base=provider_base
    )
    print()

    run_one("Primary Global Theme", primary_entry)

    print("Fetching icon pack metadata...")
    icon_entry = ocs_client.get_content(MAGNA_DARK_ICONS_ID, provider_base=provider_base)
    print()

    run_one("Icon Pack", icon_entry)

    print("=" * 60)
    print("Smoke test complete. Inspect ./test_cache/ to look around.")
    print("Delete it with: rm -rf ./test_cache")
    print("=" * 60)


if __name__ == "__main__":
    main()
