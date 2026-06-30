#!/usr/bin/env python3
"""
Live smoke test for installer.py.

Run from the project root:
    python3 tests/smoke_test_installer.py

Fetches and extracts representative real items from the Magna theme set
and installs them into a FAKE XDG_DATA_HOME (NOT your real
~/.local/share). Tests both install paths:

  - Folder-based (Global Themes, Plasma styles, Aurorae, icon themes):
    content goes into its own named subfolder under the XDG path.
  - Flat-file (Plasma color schemes, Konsole color schemes): the actual
    .colors/.colorscheme file(s) go directly into the XDG directory with
    no wrapping subfolder, which is what Konsole/System Settings actually
    scan for. This is the path added to fix the real-world bug where
    installed Konsole/color scheme entries didn't show up in pickers.

Checks:
  - install_path exists and is the right kind (file vs directory)
  - display_name read from real metadata, not store name fallback
  - flat-file items are genuinely flat (not nested one level too deep)
"""

import os
import shutil
import sys
from pathlib import Path

FAKE_XDG_DATA_HOME = Path("./test_fake_xdg_data_home").resolve()
os.environ["XDG_DATA_HOME"] = str(FAKE_XDG_DATA_HOME)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ocs_client
import fetch_and_extract as fe
import installer

MAGNA_GLOBAL_THEME_URL = "https://store.kde.org/p/2134200"
MAGNA_VIOLET_COLORSCHEME_ID = "2102231"   # typeid 112, flat-file
MAGNA_DARK_KONSOLE_ID = "2102220"         # typeid 462, flat-file
MAGNA_DARK_PLASMA_ID = "2102246"          # typeid 104, folder-based
MAGNA_AURORAE_ID = "2134193"              # typeid 717, folder-based

CACHE_DIR = Path("./test_cache")

all_pass = True


def run_one(label: str, entry: ocs_client.ContentEntry, checks: list) -> bool:
    print(f"--- {label}: {entry.name} (typeid={entry.typeid}) ---")

    fetch_result = fe.fetch_and_extract(entry, CACHE_DIR)
    print(f"  fetched + extracted to {fetch_result.extracted_dir}")

    try:
        result = installer.install_content(entry, fetch_result)
    except installer.InstallError as exc:
        print(f"  FAIL: InstallError: {exc}")
        return False

    print(f"  install_path: {result.install_path}")
    print(f"  display_name: {result.display_name!r} (confirmed={result.display_name_confirmed})")

    passed = True
    for check_name, check_fn in checks:
        ok = check_fn(result)
        status = "OK" if ok else "FAIL"
        print(f"  {status}: {check_name}")
        if not ok:
            passed = False

    print()
    return passed


def main():
    global all_pass
    print(f"FAKE XDG_DATA_HOME for this test: {FAKE_XDG_DATA_HOME}")
    print("(your real ~/.local/share is NOT touched by this test)")
    print()

    if FAKE_XDG_DATA_HOME.exists():
        shutil.rmtree(FAKE_XDG_DATA_HOME)

    provider_base = ocs_client.get_provider_base_url()

    # --- Global Theme (folder-based) ---
    entry = ocs_client.get_content_from_url(MAGNA_GLOBAL_THEME_URL, provider_base=provider_base)
    all_pass &= run_one("Global Theme (folder-based)", entry, [
        ("install_path is a directory",
            lambda r: r.install_path.is_dir()),
        ("metadata.json present at install path",
            lambda r: (r.install_path / "metadata.json").exists()),
        ("display_name_confirmed from real metadata",
            lambda r: r.display_name_confirmed),
    ])

    # --- Plasma Color Scheme (flat-file) ---
    entry = ocs_client.get_content(MAGNA_VIOLET_COLORSCHEME_ID, provider_base=provider_base)
    all_pass &= run_one("Plasma Color Scheme (flat-file)", entry, [
        ("install_path is a file (not a directory)",
            lambda r: r.install_path.is_file()),
        ("install_path has .colors extension",
            lambda r: r.install_path.suffix == ".colors"),
        ("file is directly in color-schemes/, not nested",
            lambda r: r.install_path.parent == FAKE_XDG_DATA_HOME / "color-schemes"),
        ("display_name_confirmed from real metadata",
            lambda r: r.display_name_confirmed),
    ])

    # --- Konsole Color Scheme (flat-file) ---
    entry = ocs_client.get_content(MAGNA_DARK_KONSOLE_ID, provider_base=provider_base)
    all_pass &= run_one("Konsole Color Scheme (flat-file)", entry, [
        ("install_path is a file (not a directory)",
            lambda r: r.install_path.is_file()),
        ("install_path has .colorscheme extension",
            lambda r: r.install_path.suffix == ".colorscheme"),
        ("file is directly in konsole/, not nested",
            lambda r: r.install_path.parent == FAKE_XDG_DATA_HOME / "konsole"),
    ])

    # --- Plasma Theme (folder-based) ---
    entry = ocs_client.get_content(MAGNA_DARK_PLASMA_ID, provider_base=provider_base)
    all_pass &= run_one("Plasma Theme (folder-based)", entry, [
        ("install_path is a directory",
            lambda r: r.install_path.is_dir()),
        ("installed under plasma/desktoptheme/",
            lambda r: r.install_path.parent == FAKE_XDG_DATA_HOME / "plasma" / "desktoptheme"),
    ])

    # --- Aurorae Window Decoration (folder-based) ---
    entry = ocs_client.get_content(MAGNA_AURORAE_ID, provider_base=provider_base)
    all_pass &= run_one("Aurorae Window Decoration (folder-based)", entry, [
        ("install_path is a directory",
            lambda r: r.install_path.is_dir()),
        ("installed under aurorae/themes/",
            lambda r: r.install_path.parent == FAKE_XDG_DATA_HOME / "aurorae" / "themes"),
    ])

    print("=" * 60)
    print(f"RESULT: {'ALL PASS' if all_pass else 'SOME FAILED'}")
    print(f"Inspect results under: {FAKE_XDG_DATA_HOME}")
    print(f"Clean up with: rm -rf {FAKE_XDG_DATA_HOME} {CACHE_DIR}")
    print("=" * 60)


if __name__ == "__main__":
    main()
