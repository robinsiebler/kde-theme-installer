"""
installer.py

Takes a FetchResult (already downloaded and extracted into the cache
folder by fetch_and_extract.py) plus its resolved ContentEntry, and:

  1. Determines the correct ~/.local/share/... install path for its
     content type (per design doc section 4).
  2. Copies the extracted content there.
  3. Reads back the real display name from metadata.json or
     metadata.desktop, if present -- this is the literal string that
     will appear in System Settings, which is what the design doc's
     final summary screen is supposed to show the user.

This module only ever handles items in the auto-install bucket
(BUCKET_AUTO_INSTALL from companion_finder.py). Download-only/v2-bucket
items should never reach install_content() -- callers are expected to
filter by bucket before calling this.
"""

from __future__ import annotations

import configparser
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import ocs_client
from . import fetch_and_extract
from . import companion_finder


class InstallError(Exception):
    """Raised when installing a content entry fails."""


@dataclass
class InstallResult:
    content_id: str
    typeid: str
    typename: str
    install_path: Path
    display_name: str
    # True if the display name came from real metadata (metadata.json
    # or metadata.desktop) rather than falling back to the store's own
    # "name" field. Useful for the summary screen to flag entries where
    # we couldn't confirm the exact name System Settings will show.
    display_name_confirmed: bool


def xdg_data_home() -> Path:
    """Respect $XDG_DATA_HOME if set, otherwise the standard default."""
    env_value = os.environ.get("XDG_DATA_HOME")
    if env_value:
        return Path(env_value)
    return Path.home() / ".local" / "share"


def xdg_config_home() -> Path:
    """Respect $XDG_CONFIG_HOME if set, otherwise the standard default."""
    env_value = os.environ.get("XDG_CONFIG_HOME")
    if env_value:
        return Path(env_value)
    return Path.home() / ".config"


# typeid -> path relative to XDG_DATA_HOME. Matches the table in design
# doc section 4. Only typeids present in companion_finder.TYPEID_BUCKETS
# as BUCKET_AUTO_INSTALL should ever be looked up here -- anything else
# is a programming error in the caller (see install_content's check).
INSTALL_SUBPATH_BY_TYPEID: dict[str, str] = {
    "722": "plasma/look-and-feel",      # Global Themes (Plasma 6)
    "716": "plasma/look-and-feel",      # Plasma 6 Splashscreens (same
                                         # packaging/path as Global
                                         # Themes -- both use the
                                         # Plasma/LookAndFeel KPackage
                                         # structure)
    "104": "plasma/desktoptheme",       # Plasma Themes
    "112": "color-schemes",             # Plasma Color Schemes
    "132": "icons",                     # Full Icon Themes
    "107": "icons",                     # Cursors (same path as icon themes;
                                         # structure is <name>/cursors/ inside)
    "717": "aurorae/themes",            # Plasma 6 Window Decorations
    "462": "konsole",                   # Konsole Color Schemes
    "299": "wallpapers",                # Wallpapers KDE Plasma
    "184": "yakuake/kns_skins",         # Yakuake Skins
    # Fonts: typeid not yet confirmed (see design doc open question).
    # Left out deliberately so an attempt to install one raises a
    # clear error rather than silently guessing a wrong path.
}

# typeids that install under XDG_CONFIG_HOME rather than XDG_DATA_HOME.
# Kvantum is the only known case -- its themes go in ~/.config/Kvantum/
# rather than ~/.local/share/... like everything else. These typeids
# must NOT appear in INSTALL_SUBPATH_BY_TYPEID (which is XDG_DATA_HOME-
# relative) -- install_content branches on this set first.
CONFIG_HOME_INSTALL_SUBPATH_BY_TYPEID: dict[str, str] = {
    "123": "Kvantum",  # Kvantum themes -- ~/.config/Kvantum/<theme-name>/
                       # Folder-based install (same pattern as Global Themes/
                       # Aurorae). A valid Kvantum theme folder must contain
                       # $THEME_NAME.kvconfig and/or $THEME_NAME.svg.
                       # Kvantum Manager will auto-detect it there and show
                       # it in the "Change/Delete Theme" dropdown.
}

# typeids that must be installed as loose file(s) DIRECTLY inside
# their XDG subpath, never wrapped in a subfolder -- even if the
# downloaded archive itself contained one. This matters because the
# consumers of these specific paths (Konsole's scheme picker, Plasma's
# color scheme picker) only scan the immediate directory for matching
# files; they don't recurse into subfolders the way KPackage-based
# content (Global Themes, icon themes, Aurorae, etc.) is designed to
# be organized.
#
# Discovered as a real bug: Amy-Light-Global-6's Konsole color scheme
# and Plasma color scheme both extracted with a wrapping folder (e.g.
# "Amy-Light-Konsole/Amy-Light-Konsole.colorscheme"), and the original
# "always copy content_root as-is" install logic preserved that
# wrapper -- so the actual .colorscheme/.colors file ended up one
# level too deep for Konsole (and likely System Settings) to find.
FLAT_FILE_TYPEIDS: set[str] = {
    "112",  # Plasma Color Schemes
    "462",  # Konsole Color Schemes
}


def install_subpath_for_typeid(typeid: str) -> Optional[str]:
    return INSTALL_SUBPATH_BY_TYPEID.get(typeid)


def install_base_for_typeid(typeid: str) -> Optional[Path]:
    """
    Return the full base install directory for a given typeid, handling
    both XDG_DATA_HOME types (the majority) and XDG_CONFIG_HOME types
    (Kvantum). Returns None if the typeid isn't in either table --
    callers should treat that as an InstallError rather than guessing.
    """
    if typeid in CONFIG_HOME_INSTALL_SUBPATH_BY_TYPEID:
        return xdg_config_home() / CONFIG_HOME_INSTALL_SUBPATH_BY_TYPEID[typeid]
    subpath = INSTALL_SUBPATH_BY_TYPEID.get(typeid)
    if subpath:
        return xdg_data_home() / subpath
    return None


def _find_content_root(extracted_dir: Path) -> Path:
    """
    Most KDE Store archives extract to a single top-level folder named
    after the theme (e.g. extracted_dir/Magna-Dark-Global-6/...). Some
    -- especially simpler ones like a single color scheme file -- may
    extract directly into extracted_dir with no wrapping folder at all.

    Returns the directory that should actually be copied into the XDG
    install path: if there's exactly one top-level entry and it's a
    directory, use that; otherwise use extracted_dir itself.
    """
    entries = [p for p in extracted_dir.iterdir() if not p.name.startswith(".")]
    if len(entries) == 1 and entries[0].is_dir():
        return entries[0]
    return extracted_dir


# File extensions recognized as "the actual config file" for each
# flat-file typeid -- used by _find_flat_files to locate the real
# payload regardless of how deeply the archive nested it.
FLAT_FILE_EXTENSIONS_BY_TYPEID: dict[str, tuple[str, ...]] = {
    "112": (".colors",),
    "462": (".colorscheme", ".profile"),
}


def _find_flat_files(extracted_dir: Path, typeid: str) -> list[Path]:
    """
    For flat-file typeids (see FLAT_FILE_TYPEIDS), locate every
    matching config file anywhere under extracted_dir, regardless of
    nesting -- archives for these types are sometimes a loose file
    directly in extracted_dir (see fetch_and_extract's "unknown
    format" handling for bare .colors downloads), and sometimes a
    single file nested inside a wrapper folder (the Amy-Light-Konsole
    case that prompted this function to exist). Either way, we want
    the actual file(s), not the wrapper.

    Recursing with rglob rather than just checking extracted_dir and
    one level of wrapper also covers archives with multiple scheme
    files bundled together, which install_content then copies as a
    flat set with no subfolder, exactly like a single one would be.
    """
    extensions = FLAT_FILE_EXTENSIONS_BY_TYPEID.get(typeid, ())
    if not extensions:
        return []

    matches: list[Path] = []
    for ext in extensions:
        matches.extend(sorted(extracted_dir.rglob(f"*{ext}")))
    return matches


def _read_metadata_json_name(content_root: Path) -> Optional[str]:
    """KF6/Plasma 6 standard: metadata.json with a KPlugin.Name field."""
    metadata_path = content_root / "metadata.json"
    if not metadata_path.exists():
        return None
    try:
        with open(metadata_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    kplugin = data.get("KPlugin", {})
    name = kplugin.get("Name")
    if isinstance(name, str) and name.strip():
        return name.strip()

    # Some metadata.json variants put Name in a translated-strings
    # form, e.g. {"Name": {"en": "..."}} -- handle that shape too
    # rather than just giving up.
    if isinstance(name, dict):
        for key in ("en", "en_US", "C"):
            if key in name and isinstance(name[key], str):
                return name[key].strip()
        # Fall back to whatever the first available translation is.
        for value in name.values():
            if isinstance(value, str) and value.strip():
                return value.strip()

    return None


def _read_metadata_desktop_name(content_root: Path) -> Optional[str]:
    """Legacy (pre-KF6) format: metadata.desktop, an INI-style file
    with a [Desktop Entry] section and a Name= key."""
    metadata_path = content_root / "metadata.desktop"
    if not metadata_path.exists():
        return None

    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(metadata_path, encoding="utf-8")
    except configparser.Error:
        return None

    for section in ("Desktop Entry", "DEFAULT"):
        if parser.has_section(section) and parser.has_option(section, "Name"):
            value = parser.get(section, "Name").strip()
            if value:
                return value

    return None


def _read_colors_file_name(content_root: Path) -> Optional[str]:
    """Color scheme files (.colors) are INI-style with a [General]
    section containing a Name= key -- distinct from metadata.desktop,
    these ARE the actual content file, not a sidecar describing it."""
    colors_files = list(content_root.glob("*.colors"))
    if not colors_files:
        return None

    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(colors_files[0], encoding="utf-8")
    except configparser.Error:
        return None

    if parser.has_section("General") and parser.has_option("General", "Name"):
        value = parser.get("General", "Name").strip()
        if value:
            return value

    return None


def _read_konsole_colorscheme_name(content_root: Path) -> Optional[str]:
    """Konsole .colorscheme files are also INI-style, but the display
    name lives in [General] Description=, not Name= -- this is the
    actual string Konsole's own ColorSchemeManager reads and what
    appears in Settings > Edit Current Profile > Appearance. Confirmed
    against Konsole's real source (ColorSchemeManager.cpp /
    ColorScheme.cpp) rather than guessed."""
    scheme_files = list(content_root.glob("*.colorscheme"))
    if not scheme_files:
        return None

    parser = configparser.ConfigParser(strict=False)
    try:
        parser.read(scheme_files[0], encoding="utf-8")
    except configparser.Error:
        return None

    if parser.has_section("General") and parser.has_option("General", "Description"):
        value = parser.get("General", "Description").strip()
        if value:
            return value

    return None


def determine_display_name(
    content_root: Path, fallback_name: str
) -> tuple[str, bool]:
    """
    Try each known metadata source in order of preference, falling
    back to the OCS store's own 'name' field if none are found or
    parseable. Returns (name, was_confirmed_from_real_metadata).
    """
    for reader in (
        _read_metadata_json_name,
        _read_metadata_desktop_name,
        _read_colors_file_name,
        _read_konsole_colorscheme_name,
    ):
        name = reader(content_root)
        if name:
            return name, True

    return fallback_name, False


def install_content(
    entry: ocs_client.ContentEntry,
    fetch_result: fetch_and_extract.FetchResult,
    overwrite: bool = True,
) -> InstallResult:
    """
    Copy a single already-extracted content entry into its correct
    XDG install path, and determine its real display name.

    Only call this for entries whose typeid is in the auto-install
    bucket (companion_finder.bucket_for_typeid(entry.typeid) ==
    BUCKET_AUTO_INSTALL) -- anything else raises InstallError rather
    than guessing a path, since installing to the wrong location could
    silently do nothing or, worse, write somewhere unintended.
    """
    bucket = companion_finder.bucket_for_typeid(entry.typeid)
    if bucket != companion_finder.BUCKET_AUTO_INSTALL:
        raise InstallError(
            f"Refusing to install content id {entry.content_id} ({entry.name!r}): "
            f"typeid {entry.typeid} ({entry.typename!r}) is not in the "
            f"auto-install bucket (bucket={bucket}). This is a caller bug -- "
            f"filter by bucket before calling install_content()."
        )

    install_base = install_base_for_typeid(entry.typeid)
    if install_base is None:
        raise InstallError(
            f"No known install path for typeid {entry.typeid} "
            f"({entry.typename!r}). This typeid is marked auto-install "
            f"in companion_finder but has no entry in either "
            f"INSTALL_SUBPATH_BY_TYPEID or CONFIG_HOME_INSTALL_SUBPATH_BY_TYPEID "
            f"-- these tables have drifted out of sync and need reconciling."
        )

    if not fetch_result.extracted_dir.exists():
        raise InstallError(
            f"Extracted directory does not exist: {fetch_result.extracted_dir}. "
            f"Was fetch_and_extract.fetch_and_extract() called successfully "
            f"for this entry first?"
        )

    install_base.mkdir(parents=True, exist_ok=True)

    if entry.typeid in FLAT_FILE_TYPEIDS:
        return _install_flat_files(entry, fetch_result, install_base, overwrite)

    return _install_as_folder(entry, fetch_result, install_base, overwrite)


def _install_flat_files(
    entry: ocs_client.ContentEntry,
    fetch_result: fetch_and_extract.FetchResult,
    install_base: Path,
    overwrite: bool,
) -> InstallResult:
    """
    Install logic for FLAT_FILE_TYPEIDS (Plasma color schemes, Konsole
    color schemes): copy the actual config file(s) DIRECTLY into
    install_base, with no wrapping subfolder, regardless of how deeply
    the source archive nested them. This is what fixes the real bug
    where Amy-Light-Konsole's .colorscheme file was installed one
    level too deep for Konsole's picker to find it.
    """
    source_files = _find_flat_files(fetch_result.extracted_dir, entry.typeid)
    if not source_files:
        raise InstallError(
            f"No matching config file found for content id {entry.content_id} "
            f"({entry.name!r}, typeid {entry.typeid}) under "
            f"{fetch_result.extracted_dir}. Expected one of "
            f"{FLAT_FILE_EXTENSIONS_BY_TYPEID.get(entry.typeid)} somewhere in "
            f"the extracted archive."
        )

    # Display name is read from the first matching file found -- for
    # the overwhelming common case (one scheme per archive) this is
    # the only file anyway. If an archive bundles multiple schemes,
    # we still install all of them, but the display name shown to the
    # user reflects just the first one; the install_path below points
    # at install_base itself (a directory) rather than a single file
    # in that case, since there's no single destination to point to.
    display_name, confirmed = determine_display_name(
        source_files[0].parent, entry.name
    )
    if not confirmed:
        # _read_colors_file_name/_read_metadata_* all expect to be
        # pointed at the file's containing directory and glob for a
        # match -- if that didn't find a name (e.g. unusual internal
        # format), fall back to the source filename itself rather than
        # the generic store name, since it's more specific to this
        # particular file when there are multiple in one archive.
        display_name = source_files[0].stem

    installed_paths: list[Path] = []
    try:
        for source_file in source_files:
            dest = install_base / source_file.name
            if dest.exists() and not overwrite:
                raise InstallError(
                    f"Install destination already exists and overwrite=False: {dest}"
                )
            shutil.copy2(source_file, dest)
            installed_paths.append(dest)
    except OSError as exc:
        raise InstallError(
            f"Failed to copy {source_file} to {install_base}: {exc}"
        ) from exc

    # Single file: report its exact path. Multiple files: report the
    # shared install_base directory, since there's no single
    # meaningful "the" path to point at.
    install_path = installed_paths[0] if len(installed_paths) == 1 else install_base

    return InstallResult(
        content_id=entry.content_id,
        typeid=entry.typeid,
        typename=entry.typename,
        install_path=install_path,
        display_name=display_name,
        display_name_confirmed=confirmed,
    )


def _install_as_folder(
    entry: ocs_client.ContentEntry,
    fetch_result: fetch_and_extract.FetchResult,
    install_base: Path,
    overwrite: bool,
) -> InstallResult:
    """
    Install logic for KPackage-style content (Global Themes, Plasma
    styles, icon themes, Aurorae, splashscreens): copy content_root as
    a self-contained subfolder under install_base. This is the
    original install_content behavior, now isolated into its own
    function so flat-file types (see _install_flat_files) can use
    entirely different logic instead of both being forced through one
    "copy content_root verbatim" code path.
    """
    content_root = _find_content_root(fetch_result.extracted_dir)
    display_name, confirmed = determine_display_name(content_root, entry.name)

    # Install destination is named after the content root's own folder
    # name when there was a wrapping folder (preserves whatever the
    # theme author named it, which is usually already a sensible slug),
    # falling back to a sanitized version of the display name when the
    # archive had no wrapping folder at all.
    if content_root != fetch_result.extracted_dir:
        dest_name = content_root.name
    else:
        dest_name = fetch_and_extract.safe_dirname(display_name)

    install_dest = install_base / dest_name

    try:
        if install_dest.exists():
            if not overwrite:
                raise InstallError(
                    f"Install destination already exists and overwrite=False: "
                    f"{install_dest}"
                )
            if install_dest.is_dir():
                shutil.rmtree(install_dest)
            else:
                install_dest.unlink()

        if content_root.is_dir():
            shutil.copytree(content_root, install_dest, symlinks=True)
        else:
            # Single-file content (e.g. a lone .colors file with no
            # wrapping folder) -- copy directly.
            install_dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(content_root, install_dest / content_root.name)
    except OSError as exc:
        raise InstallError(
            f"Failed to copy {content_root} to {install_dest}: {exc}"
        ) from exc

    return InstallResult(
        content_id=entry.content_id,
        typeid=entry.typeid,
        typename=entry.typename,
        install_path=install_dest,
        display_name=display_name,
        display_name_confirmed=confirmed,
    )
