"""
fetch_and_extract.py

Downloads a content entry's primary archive into the user-chosen cache
folder, verifies its checksum if one was provided, and extracts it.

Per the design doc: every discovered item gets downloaded and
extracted regardless of which install bucket it ends up in -- the
bucket only controls what happens *after* this module runs (whether
something gets copied into a live XDG path or just left in the cache).
This module doesn't know or care about buckets at all; it just turns
a ContentEntry into a populated cache folder.

Cache folder layout (see design doc section 7):
    <cache_base>/<safe-name>/
        raw/
            <original-filename>
        extracted/
            ...unpacked tree...
        manifest.json
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import time
import zipfile
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

import ocs_client


DOWNLOAD_CHUNK_SIZE = 1024 * 256  # 256 KB
DOWNLOAD_TIMEOUT = 60  # seconds; archives can be several MB, longer than a metadata GET


class FetchError(Exception):
    """Raised when downloading or extracting a content entry fails."""


@dataclass
class FetchResult:
    content_id: str
    name: str
    cache_dir: Path
    raw_file: Path
    extracted_dir: Path
    md5_verified: Optional[bool]  # None if no md5 was provided to check against
    archive_format: str  # "tar.gz", "zip", "tar", "unknown", etc.
    skipped_entries: list[str] = field(default_factory=list)
    # Archive members that were intentionally NOT extracted because
    # they were unsafe/unresolvable absolute symlinks we couldn't map
    # to anything inside the archive (see _safe_extract_tar). This is
    # informational, not a failure -- a missing aliased icon doesn't
    # break the rest of a theme.
    thumbnail_file: Optional[Path] = None
    # Path to a downloaded preview thumbnail (entry.primary_preview_url),
    # if one was available and successfully fetched. None if the
    # content entry had no preview image, or the thumbnail download
    # failed -- a missing/broken preview image should never block the
    # rest of the install, so failures here are silent (logged via the
    # progress callback at the pipeline layer, not raised).

    def to_manifest_dict(self) -> dict:
        d = asdict(self)
        # Path objects aren't JSON-serializable -- stringify them.
        d["cache_dir"] = str(self.cache_dir)
        d["raw_file"] = str(self.raw_file)
        d["extracted_dir"] = str(self.extracted_dir)
        d["thumbnail_file"] = str(self.thumbnail_file) if self.thumbnail_file else None
        return d


def safe_dirname(name: str) -> str:
    """Turn a content entry's display name into a filesystem-safe
    directory name. Keeps it human-readable rather than hashing it,
    since the whole point of the cache folder is to be browsable."""
    name = name.strip()
    # Replace anything that's not alphanumeric, dash, underscore, or
    # dot with a dash. Collapse repeated dashes.
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-")
    return name or "unnamed-content"


def _compute_md5(file_path: Path) -> str:
    hasher = hashlib.md5()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(DOWNLOAD_CHUNK_SIZE), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


DOWNLOAD_RETRY_COUNT = 4
DOWNLOAD_RETRY_BASE_DELAY_SECONDS = 3.0
# Status codes worth retrying: 429 (rate limited) and the common
# transient 5xx server errors. NOT retried: 4xx errors other than 429
# (e.g. 404 means the link is just dead, retrying won't help).
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _download_file(url: str, dest_path: Path) -> None:
    """
    Download a file, retrying on rate-limit (HTTP 429) and transient
    server errors with exponential backoff. This matters in practice:
    a theme with many companion downloads (icon packs, wallpapers,
    etc.) can easily fire 15-20+ requests at the same CDN host in
    quick succession, and pling.com's file CDN (files0N.pling.com)
    will start returning 429 partway through a burst like that. A
    single retry-once wasn't enough in testing -- backing off for a
    few seconds and retrying a handful of times resolved it reliably.

    If a Retry-After header is present on a 429/503 response, that's
    honored over our own backoff schedule, since it's an explicit
    instruction from the server about how long to wait.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    last_status: Optional[int] = None
    for attempt in range(DOWNLOAD_RETRY_COUNT + 1):
        try:
            with requests.get(url, stream=True, timeout=DOWNLOAD_TIMEOUT) as resp:
                if resp.status_code == 200:
                    with open(dest_path, "wb") as f:
                        for chunk in resp.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                            if chunk:
                                f.write(chunk)
                    return

                last_status = resp.status_code

                if resp.status_code not in RETRYABLE_STATUS_CODES:
                    raise FetchError(
                        f"Download failed: HTTP {resp.status_code} for {url}"
                    )

                if attempt >= DOWNLOAD_RETRY_COUNT:
                    break

                retry_after = resp.headers.get("Retry-After")
                if retry_after is not None and retry_after.isdigit():
                    delay = float(retry_after)
                else:
                    delay = DOWNLOAD_RETRY_BASE_DELAY_SECONDS * (2 ** attempt)
                time.sleep(delay)

        except requests.RequestException as exc:
            if attempt >= DOWNLOAD_RETRY_COUNT:
                raise FetchError(f"Download request failed for {url}: {exc}") from exc
            time.sleep(DOWNLOAD_RETRY_BASE_DELAY_SECONDS * (2 ** attempt))

    raise FetchError(
        f"Download failed after {DOWNLOAD_RETRY_COUNT + 1} attempts: "
        f"HTTP {last_status} for {url}"
    )


def _detect_archive_format(file_path: Path, mimetype: Optional[str]) -> str:
    """Best-effort archive format detection, preferring the actual
    file content over the filename/mimetype (which can be wrong or
    missing) but using those as a fallback hint."""
    name_lower = file_path.name.lower()

    if zipfile.is_zipfile(file_path):
        return "zip"

    # tarfile.is_tarfile handles .tar, .tar.gz, .tar.bz2, .tar.xz
    # transparently via its own magic-byte sniffing.
    try:
        if tarfile.is_tarfile(file_path):
            if name_lower.endswith(".tar.gz") or name_lower.endswith(".tgz"):
                return "tar.gz"
            if name_lower.endswith(".tar.bz2"):
                return "tar.bz2"
            if name_lower.endswith(".tar.xz"):
                return "tar.xz"
            return "tar"
    except (OSError, tarfile.TarError):
        pass

    if mimetype:
        if "gzip" in mimetype:
            return "tar.gz"
        if "zip" in mimetype:
            return "zip"

    return "unknown"


def _extract_archive(archive_path: Path, archive_format: str, dest_dir: Path) -> list[str]:
    """
    "Extract" an archive into dest_dir. If the file isn't actually a
    recognized archive at all (archive_format == "unknown"), it's
    treated as a single standalone file and simply copied into
    dest_dir as-is -- this is the common case for small, simple
    content like a lone .colors color scheme file, which the OCS store
    serves directly with no wrapping archive at all. Downstream code
    (installer.py's _find_content_root) already handles both shapes:
    a wrapping folder, or loose file(s) directly in extracted_dir.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    try:
        if archive_format == "zip":
            with zipfile.ZipFile(archive_path) as zf:
                return _safe_extract_zip(zf, dest_dir)

        if archive_format.startswith("tar"):
            with tarfile.open(archive_path) as tf:
                return _safe_extract_tar(tf, dest_dir)

        if archive_format == "unknown":
            shutil.copy2(archive_path, dest_dir / archive_path.name)
            return []
    except (tarfile.TarError, zipfile.BadZipFile) as exc:
        raise FetchError(
            f"Extraction of {archive_path.name} was rejected for safety "
            f"or integrity reasons: {exc}"
        ) from exc

    raise FetchError(
        f"Unhandled archive format {archive_format!r} for "
        f"{archive_path.name}. This shouldn't be reachable -- "
        f"_detect_archive_format should only ever return zip/tar* "
        f"variants or 'unknown'."
    )


def _safe_extract_tar(tf: tarfile.TarFile, dest_dir: Path) -> list[str]:
    """Extract a tarfile safely against path traversal ("zip slip" /
    "tar slip") -- a malicious archive could otherwise contain entries
    like '../../etc/passwd' or symlinks pointing outside dest_dir.
    Theme archives are third-party user-submitted content per the
    design doc's own risk note, so this isn't paranoia, it's a
    baseline requirement.

    We use Python's built-in 'data' extraction filter (PEP 706,
    default as of Python 3.12+) for the core traversal/permission
    checks, since it's more thorough than a hand-rolled path check.
    However its default behavior rejects ALL absolute-path symlinks.
    In real-world theme archives (icon themes especially) these are
    common and benign: a symlink like
    'mimetypes/22/application-x-vdi-disk.svg' -> '/usr/share/icons/.../foo.svg'
    is leftover from however the original packager's filesystem was
    laid out, but the *intent* is just "alias this icon to another
    icon already present in this same archive." We handle this by:

      1. If the absolute target, once resolved, lands inside dest_dir
         (rare, but possible if it happens to already be relative-ish),
         keep it as a real symlink, rewritten to a relative path.
      2. Otherwise, try to find a member in THIS archive whose path
         ends with the same trailing path as the symlink target (e.g.
         match on 'mimetypes/22/foo.svg' against the archive's own
         entries) -- if exactly one match is found, treat the symlink
         as an alias to that archive-internal file and rewrite the
         link accordingly.
      3. If neither resolves cleanly, skip just that one symlink
         (don't extract it) rather than aborting the whole archive --
         it's reported back to the caller so it can be logged, but a
         missing icon alias isn't worth failing an entire theme
         install over.

    Real path-traversal attempts (entries that try to write outside
    dest_dir via '../' or unresolvable absolute targets that don't
    match anything in the archive) are still rejected by falling
    through to tarfile's own strict filter.
    """
    dest_dir = dest_dir.resolve()
    skipped: list[str] = []

    # Build a lookup of "trailing path -> full archive member name" for
    # every regular file in the archive, so absolute symlink targets
    # can be resolved against the archive's own contents (see case 2
    # in the docstring above).
    archive_files_by_suffix: dict[str, list[str]] = {}
    for member in tf.getmembers():
        if member.isfile():
            parts = Path(member.name).parts
            # Index by every trailing suffix (e.g. for a/b/c.svg, index
            # "c.svg", "b/c.svg", "a/b/c.svg") so we can match symlink
            # targets of varying specificity.
            for i in range(len(parts)):
                suffix = "/".join(parts[i:])
                archive_files_by_suffix.setdefault(suffix, []).append(member.name)

    def permissive_data_filter(member: tarfile.TarInfo, path: str) -> Optional[tarfile.TarInfo]:
        if (member.issym() or member.islnk()) and member.linkname.startswith("/"):
            link_target = member.linkname
            resolved_target = Path(link_target).resolve()

            if str(resolved_target).startswith(str(dest_dir)):
                try:
                    rel_target = relpath_for_symlink(member.name, link_target, dest_dir)
                    member = member.replace(deep=False)
                    member.linkname = rel_target
                    return tarfile.data_filter(member, path)
                except ValueError:
                    pass  # fall through to archive-internal matching

            target_suffix = Path(link_target).name  # just the filename
            # Try progressively longer suffixes of the target path
            # against the archive's own files, preferring the most
            # specific (longest) match to reduce false positives.
            target_parts = Path(link_target).parts
            match: Optional[str] = None
            for i in range(len(target_parts)):
                suffix = "/".join(target_parts[i:])
                candidates = archive_files_by_suffix.get(suffix)
                if candidates and len(candidates) == 1:
                    match = candidates[0]
                    break

            if match is not None:
                rel_target = relpath_for_symlink(member.name, str(dest_dir / match), dest_dir)
                member = member.replace(deep=False)
                member.linkname = rel_target
                return tarfile.data_filter(member, path)

            # Couldn't safely resolve this symlink to anything inside
            # the archive -- skip it rather than failing the whole
            # extraction. Record it so the caller can report it.
            skipped.append(f"{member.name} -> {link_target} (target not found in archive)")
            return None

        return tarfile.data_filter(member, path)

    tf.extractall(dest_dir, filter=permissive_data_filter)
    # Note: tf.extractall still raises tarfile.FilterError (a
    # tarfile.TarError subclass) for genuinely unsafe members that
    # don't go through our symlink special-case above (e.g. real path
    # traversal via '../', device files, setuid bits) -- those should
    # propagate as failures, not be silently skipped.
    return skipped


def relpath_for_symlink(member_name: str, abs_link_target: str, dest_dir: Path) -> str:
    """Compute a relative symlink target equivalent to abs_link_target,
    given that the symlink itself will live at dest_dir/member_name
    after extraction. Raises ValueError if the target isn't actually
    inside dest_dir (caller should treat that as unsafe)."""
    target_path = Path(abs_link_target).resolve()
    if not str(target_path).startswith(str(dest_dir)):
        raise ValueError("symlink target escapes destination directory")

    link_path = (dest_dir / member_name).parent
    return os.path.relpath(target_path, link_path)


def _safe_extract_zip(zf: zipfile.ZipFile, dest_dir: Path) -> list[str]:
    """Zip extraction doesn't support symlinks the way tar does (zip
    symlink support is an unofficial, inconsistently-implemented
    extension), so we don't need the same absolute-symlink handling
    here -- just the standard path-traversal guard."""
    dest_dir = dest_dir.resolve()
    for member in zf.namelist():
        member_path = (dest_dir / member).resolve()
        if not str(member_path).startswith(str(dest_dir)):
            raise FetchError(
                f"Archive contains an unsafe path outside the extraction "
                f"directory: {member!r}. Refusing to extract."
            )
    zf.extractall(dest_dir)
    return []


def _fetch_thumbnail(entry: ocs_client.ContentEntry, cache_dir: Path) -> Optional[Path]:
    """
    Download entry.primary_preview_url into cache_dir/thumbnail.<ext>,
    if a preview URL is available. Never raises -- a missing or failed
    thumbnail download should never block the actual content
    install, it's a nice-to-have for the GUI, not a requirement.
    Returns None if there's no preview URL or the download fails.
    """
    preview_url = entry.primary_preview_url
    if not preview_url:
        return None

    # Guess an extension from the URL itself (OCS preview URLs are
    # consistently .png/.jpg in practice); fall back to .img if we
    # can't tell, since the GUI can still attempt to load it either
    # way -- Pillow/Tk don't strictly need a correct extension to open
    # an image, but it's nice for browsing the cache folder manually.
    suffix = Path(preview_url.split("?")[0]).suffix or ".img"
    thumbnail_path = cache_dir / f"thumbnail{suffix}"

    if thumbnail_path.exists() and thumbnail_path.stat().st_size > 0:
        return thumbnail_path

    try:
        _download_file(preview_url, thumbnail_path)
    except FetchError:
        return None

    return thumbnail_path


def fetch_and_extract(
    entry: ocs_client.ContentEntry,
    cache_base: Path,
    download_file: Optional[ocs_client.DownloadFile] = None,
    overwrite: bool = False,
    fetch_thumbnail: bool = True,
) -> FetchResult:
    """
    Download and extract a single content entry into the cache folder.

    Args:
        entry: the resolved ContentEntry (from ocs_client.get_content).
        cache_base: the user-chosen base cache directory.
        download_file: which of entry.downloads to fetch. Defaults to
            entry.primary_download (downloadlink1) if not specified --
            most entries only have one, but this lets a caller pick a
            specific one for entries with multiple download options.
        overwrite: if False (default) and the cache directory for this
            entry already has a raw file present, skip re-downloading
            and re-extracting. Useful for re-runs without re-fetching
            everything.
        fetch_thumbnail: if True (default), also download
            entry.primary_preview_url into the cache folder for GUI
            display purposes. Failures here are always silent (see
            _fetch_thumbnail) -- a missing thumbnail never blocks the
            actual content install.

    Raises FetchError on any failure (network, extraction, etc).
    """
    if download_file is None:
        download_file = entry.primary_download
    if download_file is None:
        raise FetchError(f"Content entry {entry.name!r} has no downloads to fetch.")

    cache_dir = cache_base / safe_dirname(entry.name)
    raw_dir = cache_dir / "raw"
    extracted_dir = cache_dir / "extracted"
    raw_file = raw_dir / download_file.filename

    already_present = raw_file.exists() and raw_file.stat().st_size > 0
    if already_present and not overwrite:
        # Still re-verify md5 against the existing file rather than
        # blindly trusting it -- cheap, and catches a previous partial
        # or corrupted download.
        md5_verified = _verify_md5_if_present(raw_file, download_file.md5sum)
        skipped_entries: list[str] = []
        if not extracted_dir.exists() or not any(extracted_dir.iterdir()):
            archive_format = _detect_archive_format(raw_file, download_file.mimetype)
            skipped_entries = _extract_archive(raw_file, archive_format, extracted_dir)
        else:
            archive_format = _detect_archive_format(raw_file, download_file.mimetype)
        thumbnail_file = _fetch_thumbnail(entry, cache_dir) if fetch_thumbnail else None
        return FetchResult(
            content_id=entry.content_id,
            name=entry.name,
            cache_dir=cache_dir,
            raw_file=raw_file,
            extracted_dir=extracted_dir,
            md5_verified=md5_verified,
            archive_format=archive_format,
            skipped_entries=skipped_entries,
            thumbnail_file=thumbnail_file,
        )

    _download_file(download_file.url, raw_file)

    md5_verified = _verify_md5_if_present(raw_file, download_file.md5sum)
    if md5_verified is False:
        raise FetchError(
            f"MD5 checksum mismatch for {download_file.filename} "
            f"(content id {entry.content_id}, {entry.name!r}). "
            f"The download may be corrupted or tampered with. "
            f"Refusing to extract."
        )

    archive_format = _detect_archive_format(raw_file, download_file.mimetype)

    if extracted_dir.exists():
        shutil.rmtree(extracted_dir)
    skipped_entries = _extract_archive(raw_file, archive_format, extracted_dir)

    thumbnail_file = _fetch_thumbnail(entry, cache_dir) if fetch_thumbnail else None

    return FetchResult(
        content_id=entry.content_id,
        name=entry.name,
        cache_dir=cache_dir,
        raw_file=raw_file,
        extracted_dir=extracted_dir,
        md5_verified=md5_verified,
        archive_format=archive_format,
        skipped_entries=skipped_entries,
        thumbnail_file=thumbnail_file,
    )


def _verify_md5_if_present(file_path: Path, expected_md5: Optional[str]) -> Optional[bool]:
    """Returns True/False if expected_md5 was provided, None if there
    was nothing to check against (some entries simply don't supply
    one)."""
    if not expected_md5:
        return None
    actual = _compute_md5(file_path)
    return actual.lower() == expected_md5.lower()


def write_manifest(
    cache_dir: Path,
    entry: ocs_client.ContentEntry,
    fetch_result: FetchResult,
    bucket: str,
    installed_to: Optional[str] = None,
    installed_display_name: Optional[str] = None,
) -> Path:
    """Write/update manifest.json for a single content entry's cache
    folder, recording what was fetched, verified, and (if applicable)
    installed. This is what makes the cache folder useful as a
    standalone audit trail per the design doc."""
    manifest_path = cache_dir / "manifest.json"
    manifest = {
        "content_id": entry.content_id,
        "name": entry.name,
        "typeid": entry.typeid,
        "typename": entry.typename,
        "homepage": entry.homepage,
        "bucket": bucket,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "raw_file": str(fetch_result.raw_file),
        "extracted_dir": str(fetch_result.extracted_dir),
        "archive_format": fetch_result.archive_format,
        "md5_verified": fetch_result.md5_verified,
        "skipped_entries": fetch_result.skipped_entries,
        "thumbnail_file": str(fetch_result.thumbnail_file) if fetch_result.thumbnail_file else None,
        "installed_to": installed_to,
        "installed_display_name": installed_display_name,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest_path
