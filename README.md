# KDE Store Theme Installer

A tool to take a KDE Store / Pling-network theme URL, discover and
download every companion piece referenced in its description (icon
packs, color schemes, Aurorae window decorations, wallpapers, etc.),
and install the safe/well-understood ones automatically into the
correct Plasma 6 user-data locations.

Full design rationale, scope decisions, and the OCS API investigation
that got us here live in `design_doc.md` -- read that first if you're
picking this project back up after a break.

## Requirements

```bash
pip install requests beautifulsoup4 Pillow --user
```

`Pillow` is optional (thumbnails won't display in the GUI without it,
but everything else works fine).

Tkinter is required and is usually available as a system package:

```bash
sudo dnf install python3-tkinter   # Nobara/Fedora/RHEL
sudo apt install python3-tk        # Debian/Ubuntu
```

## Running the GUI

```bash
python3 src/gui.py
```

Paste a KDE Store / Pling URL (e.g. `https://store.kde.org/p/2134200`),
choose a downloads folder, click Fetch, review the selection list, and
click Download & Install Selected. Everything auto-installs to the
correct `~/.local/share/...` path per content type -- you don't need
to know where anything goes.

## Folder layout

```
src/        Real, working source modules (the actual installer logic)
scripts/    One-off diagnostic/investigation scripts used during
            development to figure out how the KDE Store's API works
tests/      Live smoke tests against the real OCS API
```

## Status

Complete and working end-to-end. All modules built and tested against
real KDE Store data:

- `src/ocs_client.py` -- OCS API client (provider discovery, content
  lookup, typed errors with retry, preview image URLs captured).
- `src/companion_finder.py` -- parses a theme's description HTML for
  companion content links, resolves each via the OCS API, buckets by
  type (auto-install vs. download-only/v2). Known typeids confirmed
  empirically: Global Themes (722), Splashscreens (716), Plasma
  Themes (104), Color Schemes (112), Icon Themes (132), Window
  Decorations/Aurorae (717), Konsole Color Schemes (462), Wallpapers
  (299), SDDM (101), GTK (135), Kvantum (123).
- `src/fetch_and_extract.py` -- downloads archives, verifies MD5
  checksums, extracts safely (path traversal guard, absolute symlink
  handling, bare non-archive files). Retries on HTTP 429/5xx.
  Downloads preview thumbnails alongside each archive.
- `src/installer.py` -- copies extracted content into the correct
  `~/.local/share/...` path, respecting `$XDG_DATA_HOME`. Two install
  paths: folder-based (Global Themes, Plasma styles, icon themes,
  Aurorae, splashscreens) and flat-file (Plasma color schemes, Konsole
  color schemes -- these must land as bare files directly in their XDG
  directory, not in a subfolder, to be found by their respective
  pickers). Reads real display names from `metadata.json`,
  `metadata.desktop`, `.colors`, or `.colorscheme` files.
- `src/pipeline.py` -- orchestration layer: fetch → discover → resolve
  → download everything → install auto-install bucket → summarize.
  Pluggable progress callback for the GUI. Paces downloads to avoid
  CDN rate limiting.
- `src/gui.py` -- four-screen Tkinter GUI: URL entry → selection list
  with async thumbnails → progress log → summary with copy-to-
  clipboard. All downloads and installs run on background threads so
  the UI stays responsive during 20+ API calls and several MB of
  downloads.

A full real-world run against Magna-Dark-Global-6 (1 primary + 21
companions) installs all 19 auto-install-bucket items correctly across
every content type, holds back the 3 download-only items (Kvantum,
SDDM, GTK), and downloads all 22 preview thumbnails. Everything
confirmed showing up correctly in System Settings, Konsole, and
KWin (Aurorae decorations).

## Not yet built (v2)

- SDDM, GTK, Kvantum install support (files are already downloaded,
  just not placed into their live locations).
- Click-to-preview popup before confirming download -- selecting
  between multiple similar-named items (color schemes, icon packs)
  currently requires visiting the store to compare visually.
- Cursor theme and font support (typeids not yet confirmed).

## Running the smoke tests

From the project root:

```bash
python3 tests/smoke_test_ocs_client.py
python3 tests/smoke_test_companion_finder.py
python3 tests/smoke_test_fetch_and_extract.py
python3 tests/smoke_test_thumbnails.py
python3 tests/smoke_test_installer.py
python3 tests/smoke_test_pipeline.py
```

`smoke_test_installer.py` explicitly tests both flat-file (Konsole/
color scheme) and folder-based (Global Theme, Plasma style, Aurorae)
install paths with pass/fail checks. `smoke_test_pipeline.py` is the
full integration test -- runs against the live KDE Store API, takes a
couple of minutes (~22 API calls + several MB of downloads). Both
install into a local `./test_fake_xdg_data_home/` rather than your
real `~/.local/share`, and both are safe to run at any time.

## Diagnostic scripts

`scripts/` holds investigation tools used during development:
`kde_store_diagnostic.py` and `lookup_content_types.py` for figuring
out how the OCS API works, and `diagnose_missing_thumbnail.py` for
debugging thumbnail fetch failures. Not needed for normal use.
