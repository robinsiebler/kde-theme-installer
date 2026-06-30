#!/usr/bin/env python3
"""
Live end-to-end smoke test for pipeline.py.

Run from the project root:
    python3 tests/smoke_test_pipeline.py

This is THE integration test -- runs the entire flow for the real
Magna-Dark-Global-6 theme: fetch primary metadata, discover all 21
companion links, resolve each, download+extract everything, install
the auto-install-bucket items, and print the final summary.

Safety note: like smoke_test_installer.py, this sets XDG_DATA_HOME to
a fake local directory before running, so it does NOT touch your real
~/.local/share. This is still a "preview the whole thing" run, not a
real install to your system.

This will make ~22 OCS API calls and download several MB of real
content (the icon packs in particular) -- expect it to take a minute
or two.
"""

import os
import shutil
import sys
from pathlib import Path

FAKE_XDG_DATA_HOME = Path("./test_fake_xdg_data_home").resolve()
os.environ["XDG_DATA_HOME"] = str(FAKE_XDG_DATA_HOME)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pipeline

MAGNA_URL = "https://store.kde.org/p/2134200"
CACHE_DIR = Path("./test_cache")


def progress_printer(stage: str, detail: str) -> None:
    print(f"  [{stage}] {detail}")


def main():
    print(f"FAKE XDG_DATA_HOME for this test: {FAKE_XDG_DATA_HOME}")
    print("(your real ~/.local/share is NOT touched by this test)")
    print(f"Cache: {CACHE_DIR.resolve()}")
    print()
    print(f"Running full pipeline for: {MAGNA_URL}")
    print("(this will take a minute or two -- ~22 API calls + several MB of downloads)")
    print()

    if FAKE_XDG_DATA_HOME.exists():
        shutil.rmtree(FAKE_XDG_DATA_HOME)

    result = pipeline.run_pipeline(
        MAGNA_URL,
        CACHE_DIR,
        install=True,
        progress=progress_printer,
    )

    print()
    print("=" * 70)
    print("FINAL SUMMARY")
    print("=" * 70)
    print(pipeline.format_summary(result))
    print()

    print("=" * 70)
    print("SANITY CHECKS")
    print("=" * 70)
    total_items = len(result.all_items)
    print(f"Total items processed (primary + companions): {total_items}")
    print(f"  installed: {len(result.installed_items)}")
    print(f"  download-only: {len(result.download_only_items)}")
    print(f"  unknown type: {len(result.unknown_type_items)}")
    print(f"  failed: {len(result.failed_items)}")
    print(f"  companion lookup failures: {len(result.companion_lookup_failures)}")

    thumbnails_found = sum(1 for i in result.all_items if i.thumbnail_file is not None)
    print(f"  thumbnails downloaded: {thumbnails_found} / {total_items}")

    # Flat-file install checks: Konsole and Plasma color scheme items
    # must install as flat files directly in their XDG directory, NOT
    # as a nested subfolder -- this is what the real bug fix tested.
    print()
    print("=== Flat-file install checks ===")
    konsole_items = [i for i in result.installed_items if i.typeid == "462"]
    colorscheme_items = [i for i in result.installed_items if i.typeid == "112"]
    flat_file_pass = True
    for item in konsole_items + colorscheme_items:
        if item.install_path is None:
            continue
        is_file = item.install_path.is_file()
        not_nested = item.install_path.parent.name in ("konsole", "color-schemes")
        status = "OK" if (is_file and not_nested) else "FAIL"
        if not (is_file and not_nested):
            flat_file_pass = False
        print(f"  {status}: {item.name} -> {item.install_path} "
              f"(is_file={is_file}, not_nested={not_nested})")
    if not konsole_items and not colorscheme_items:
        print("  (no Konsole/color-scheme items installed in this run)")
    elif flat_file_pass:
        print(f"  All {len(konsole_items) + len(colorscheme_items)} flat-file "
              f"items installed correctly")

    # Expected shape based on our testing against Magna-Dark-Global-6:
    # 1 primary + 21 companions = 22 total, 19 auto-install (18
    # companions + primary), 3 download-only, 0 unknown, 0 failed.
    print()
    if len(result.unknown_type_items) > 0:
        print("NOTE: unknown-typed items found -- this means we hit a "
              "content type not yet in our typeid tables. Worth reviewing.")
    if len(result.failed_items) > 0 or len(result.companion_lookup_failures) > 0:
        print("NOTE: some items failed -- check the summary above for details.")
    if not flat_file_pass:
        print("NOTE: flat-file install check failed -- Konsole/color scheme "
              "items may be nested one level too deep to be found by their "
              "respective pickers. Check installer.py FLAT_FILE_TYPEIDS.")

    print()
    print(f"Inspect installed files under: {FAKE_XDG_DATA_HOME}")
    print(f"Inspect cache/manifests under: {CACHE_DIR.resolve()}")
    print(f"Clean up with: rm -rf {FAKE_XDG_DATA_HOME} {CACHE_DIR}")


if __name__ == "__main__":
    main()
