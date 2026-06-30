#!/usr/bin/env python3
"""
Live smoke test for ocs_client.py.

Run from the project root (or anywhere -- the script locates ../src
relative to its own location, not the current working directory):
    python3 tests/smoke_test_ocs_client.py

Exercises the real network paths that couldn't be tested offline:
provider discovery and a live content/data lookup.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ocs_client


def main():
    print("=== Test 1: provider discovery ===")
    try:
        base = ocs_client.get_provider_base_url()
        print(f"OK: provider base = {base}")
        if base == ocs_client.FALLBACK_PROVIDER_BASE:
            print("  (note: this matches the hardcoded fallback -- could mean "
                  "providers.xml fetch failed and we silently fell back, "
                  "or it could just mean the live value still matches. "
                  "Not necessarily a problem.)")
    except ocs_client.OcsError as e:
        print(f"FAIL: {e}")
        return

    print()
    print("=== Test 2: known-good content lookup (Magna-Dark-Global-6) ===")
    try:
        entry = ocs_client.get_content("2134200", provider_base=base)
        print(f"OK: name={entry.name!r} typeid={entry.typeid} typename={entry.typename!r}")
        print(f"    downloads found: {len(entry.downloads)}")
        if entry.primary_download:
            d = entry.primary_download
            print(f"    primary: {d.filename} ({d.size_kb} KB, md5={d.md5sum})")
        print(f"    description length: {len(entry.description_html)} chars")
    except ocs_client.OcsError as e:
        print(f"FAIL: {e}")
        return

    print()
    print("=== Test 3: URL-based lookup convenience wrapper ===")
    try:
        entry2 = ocs_client.get_content_from_url(
            "https://store.kde.org/p/2134200", provider_base=base
        )
        print(f"OK: resolved URL to id={entry2.content_id}, name={entry2.name!r}")
    except ocs_client.OcsError as e:
        print(f"FAIL: {e}")
        return

    print()
    print("=== Test 4: nonexistent content id (should fail cleanly, not crash) ===")
    try:
        ocs_client.get_content("99999999999", provider_base=base)
        print("UNEXPECTED: lookup of a bogus id succeeded?")
    except ocs_client.OcsApiError as e:
        print(f"OK: got expected OcsApiError: {e}")
    except ocs_client.OcsError as e:
        print(f"OK (different OcsError subclass, still handled cleanly): {e}")

    print()
    print("All smoke tests completed.")


if __name__ == "__main__":
    main()
