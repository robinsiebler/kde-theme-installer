#!/usr/bin/env python3
"""
Live smoke test for preview thumbnail downloading.

Run from the project root:
    python3 tests/smoke_test_thumbnails.py

Fetches the real Magna-Dark-Global-6 theme and confirms its preview
thumbnail downloads correctly alongside the main archive.
"""

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ocs_client
import fetch_and_extract as fe

MAGNA_URL = "https://store.kde.org/p/2134200"
CACHE_DIR = Path("./test_cache")


def main():
    provider_base = ocs_client.get_provider_base_url()

    print(f"Fetching metadata: {MAGNA_URL}")
    entry = ocs_client.get_content_from_url(MAGNA_URL, provider_base=provider_base)
    print(f"  name: {entry.name}")
    print(f"  preview_image_urls: {len(entry.preview_image_urls)} found")
    for url in entry.preview_image_urls:
        print(f"    {url}")
    print(f"  primary_preview_url: {entry.primary_preview_url}")
    print()

    print("Downloading + extracting (with thumbnail)...")
    result = fe.fetch_and_extract(entry, CACHE_DIR, fetch_thumbnail=True)
    print(f"  thumbnail_file: {result.thumbnail_file}")

    if result.thumbnail_file is None:
        print("  FAIL: expected a thumbnail to be downloaded (entry has preview URLs)")
        return

    if not result.thumbnail_file.exists():
        print("  FAIL: thumbnail_file path set but file doesn't actually exist")
        return

    size = result.thumbnail_file.stat().st_size
    print(f"  thumbnail file size: {size} bytes")
    if size == 0:
        print("  FAIL: thumbnail file is empty")
        return

    print()
    print(f"OK: real thumbnail downloaded successfully to {result.thumbnail_file}")
    print(f"Open it to confirm it's a real image: {result.thumbnail_file}")


if __name__ == "__main__":
    main()
