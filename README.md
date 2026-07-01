# KDE Store Theme Installer

A tool to take a KDE Store / Pling-network theme URL, discover and
download every companion piece referenced in its description (icon
packs, color schemes, Aurorae window decorations, wallpapers, etc.),
and install the safe/well-understood ones automatically into the
correct Plasma 6 user-data locations.

Full design rationale, scope decisions, and the OCS API investigation
that got us here live in `design_doc.md` -- read that first if you're
picking this project back up after a break.

## Installation

```bash
pip install git+https://github.com/robinsiebler/kde-theme-installer --user
```

Or clone and install locally:

```bash
git clone https://github.com/robinsiebler/kde-theme-installer
cd kde-theme-installer
pip install -e . --user
```

Tkinter is required and is usually available as a system package:

```bash
sudo dnf install python3-tkinter   # Nobara/Fedora/RHEL
sudo apt install python3-tk        # Debian/Ubuntu
```

## Running

After installing:

```bash
kde-theme-installer
```

Or without installing (from the project root):

```bash
python3 -m kde_theme_installer.gui
```

## Requirements

```bash
pip install -r requirements.txt --user
```

`Pillow` is optional (thumbnails won't display in preview popups without
it, but everything else works fine).

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

## Not yet built / manual install

- **SDDM login themes** -- files are downloaded to the cache folder
  automatically. Use the helper script to install:
  ```bash
  python3 scripts/install_sddm_theme.py \
      ~/kde-theme-downloads/<Theme>/<SDDM-item>/extracted/<theme-name>
  ```
  The script validates the theme, backs up your config, copies files
  with sudo, writes a drop-in at `/etc/sddm.conf.d/`, and prints the
  exact revert command. Does NOT restart SDDM automatically.

  **Important:** SDDM themes only work if your system uses SDDM as its
  display manager. If you use **Plasma Login Manager** (`plasmalogin`)
  -- which Nobara KDE ships by default -- SDDM themes have no effect.
  Plasma Login Manager does not support arbitrary QML themes and is
  fixed to its own Breeze-based login screen. The script detects your
  display manager and warns you before doing anything if SDDM is not
  active. Check which DM you're running with:
  ```bash
  systemctl status display-manager
  ```
- **GTK themes** -- files are downloaded. Install manually by copying
  the extracted folder to `~/.local/share/themes/` or `~/.themes/`.
  The summary screen shows the extracted path after a run.
- **Fonts** -- typeid not yet confirmed; no themes found that link to
  fonts as companions. Will be added once a real example is found.
- **Click-to-preview before downloading** -- the preview popup works
  after fetch but before the download/install confirmation. A true
  "preview before even fetching metadata" flow is a future addition.

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
