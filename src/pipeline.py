"""
pipeline.py

The orchestration layer: takes a theme URL and a cache folder, and
runs the entire flow described in the design doc's section 6 diagram:

    fetch primary theme metadata
        -> discover companion links in its description
        -> resolve each companion via the OCS API, bucket by type
        -> download + extract EVERY discovered item (primary + all
           companions, regardless of bucket)
        -> install only the auto-install-bucket items into their
           correct XDG paths
        -> write a manifest.json per item
        -> produce a single PipelineResult summarizing everything,
           ready for a GUI (or a CLI printout) to display

This module doesn't know anything about Tkinter or any other UI --
it's pure orchestration logic, callable from a GUI, a CLI, or a test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import ocs_client
import companion_finder
import fetch_and_extract as fe
import installer


# Progress callback shape: (stage: str, detail: str) -> None
# Stages: "fetching_primary", "finding_companions", "resolving_companion",
#         "downloading", "installing", "done"
# A GUI can use this to update a progress bar / log pane; a CLI can
# just print it. Pipeline logic doesn't care who's listening.
ProgressCallback = Callable[[str, str], None]

# Small pause between companion downloads to avoid triggering rate
# limiting on the pling.com file CDN -- discovered during testing that
# a burst of 15-20+ downloads fired back-to-back at the same host
# starts getting HTTP 429 partway through. _download_file in
# fetch_and_extract.py also retries with backoff if a 429 still slips
# through, but spacing requests out up front avoids triggering the
# limit in the first place, which is both faster overall (fewer
# retries needed) and more polite to the CDN.
INTER_DOWNLOAD_DELAY_SECONDS = 1.5


def _noop_progress(stage: str, detail: str) -> None:
    pass


@dataclass
class ItemOutcome:
    """The final outcome for a single content item (primary or
    companion) after a full pipeline run -- one of these per item,
    success or failure, so the summary screen has a complete picture
    rather than only seeing the items that worked."""
    content_id: str
    name: str
    typeid: str
    typename: str
    label: str  # the description label for companions; same as name for primary
    bucket: str  # companion_finder.BUCKET_* constant

    fetch_succeeded: bool
    fetch_error: Optional[str] = None

    install_attempted: bool = False
    install_succeeded: bool = False
    install_error: Optional[str] = None
    install_path: Optional[Path] = None
    display_name: Optional[str] = None
    display_name_confirmed: bool = False

    cache_dir: Optional[Path] = None
    thumbnail_file: Optional[Path] = None
    # Preview image downloaded alongside the content, for GUI display.
    # None if the content had no preview image, or it failed to
    # download -- see fetch_and_extract._fetch_thumbnail. A missing
    # thumbnail is never a fetch failure on its own.


@dataclass
class PipelineResult:
    primary: ItemOutcome
    companions: list[ItemOutcome] = field(default_factory=list)
    companion_lookup_failures: list[companion_finder.FailedCompanion] = field(default_factory=list)

    @property
    def all_items(self) -> list[ItemOutcome]:
        return [self.primary] + self.companions

    @property
    def installed_items(self) -> list[ItemOutcome]:
        return [i for i in self.all_items if i.install_succeeded]

    @property
    def download_only_items(self) -> list[ItemOutcome]:
        return [
            i for i in self.all_items
            if i.fetch_succeeded and i.bucket == companion_finder.BUCKET_DOWNLOAD_ONLY
        ]

    @property
    def unknown_type_items(self) -> list[ItemOutcome]:
        return [
            i for i in self.all_items
            if i.fetch_succeeded and i.bucket == companion_finder.BUCKET_UNKNOWN
        ]

    @property
    def failed_items(self) -> list[ItemOutcome]:
        return [i for i in self.all_items if not i.fetch_succeeded]


def _process_item(
    entry: ocs_client.ContentEntry,
    label: str,
    bucket: str,
    cache_base: Path,
    install: bool,
    progress: ProgressCallback,
) -> ItemOutcome:
    """Download+extract one item, and install it if requested and its
    bucket allows it. Never raises -- all failures are captured into
    the returned ItemOutcome so one bad item can't abort the whole
    pipeline run."""
    outcome = ItemOutcome(
        content_id=entry.content_id,
        name=entry.name,
        typeid=entry.typeid,
        typename=entry.typename,
        label=label,
        bucket=bucket,
        fetch_succeeded=False,
    )

    progress("downloading", f"{entry.name} ({entry.typename})")
    try:
        fetch_result = fe.fetch_and_extract(entry, cache_base)
    except fe.FetchError as exc:
        outcome.fetch_error = str(exc)
        progress("downloading", f"FAILED: {entry.name}: {exc}")
        return outcome

    outcome.fetch_succeeded = True
    outcome.cache_dir = fetch_result.cache_dir
    outcome.thumbnail_file = fetch_result.thumbnail_file

    if install and bucket == companion_finder.BUCKET_AUTO_INSTALL:
        progress("installing", f"{entry.name}")
        outcome.install_attempted = True
        try:
            install_result = installer.install_content(entry, fetch_result)
        except installer.InstallError as exc:
            outcome.install_error = str(exc)
            progress("installing", f"FAILED: {entry.name}: {exc}")
        else:
            outcome.install_succeeded = True
            outcome.install_path = install_result.install_path
            outcome.display_name = install_result.display_name
            outcome.display_name_confirmed = install_result.display_name_confirmed

        installed_to = str(outcome.install_path) if outcome.install_path else None
        fe.write_manifest(
            fetch_result.cache_dir, entry, fetch_result, bucket,
            installed_to=installed_to,
            installed_display_name=outcome.display_name,
        )
    else:
        # Download-only / unknown-bucket item, or install explicitly
        # not requested for this run -- still write a manifest so the
        # cache folder's audit trail is complete, just with no install
        # info attached.
        fe.write_manifest(fetch_result.cache_dir, entry, fetch_result, bucket)

    return outcome


def run_pipeline(
    theme_url: str,
    cache_base: Path,
    install: bool = True,
    progress: Optional[ProgressCallback] = None,
) -> PipelineResult:
    """
    Run the full pipeline for a single KDE Store theme URL.

    Args:
        theme_url: a store.kde.org / pling.com / etc. /p/<id> URL.
        cache_base: where to download/extract everything (see
            fetch_and_extract.py's cache folder layout).
        install: if True (default), auto-install-bucket items get
            copied into their real XDG paths. If False, everything is
            still downloaded and extracted, just not installed --
            useful for a "preview first" mode in the GUI.
        progress: optional callback for status updates; see
            ProgressCallback above. Defaults to a no-op.

    Raises ocs_client.OcsError if the PRIMARY theme itself can't be
    fetched (without a working primary entry there's nothing to
    orchestrate). Companion failures, by contrast, are captured in the
    result rather than raised -- see PipelineResult.failed_items and
    companion_lookup_failures.
    """
    if progress is None:
        progress = _noop_progress

    progress("fetching_primary", theme_url)
    provider_base = ocs_client.get_provider_base_url()
    primary_entry = ocs_client.get_content_from_url(theme_url, provider_base=provider_base)

    primary_bucket = companion_finder.bucket_for_typeid(primary_entry.typeid)
    primary_outcome = _process_item(
        primary_entry, primary_entry.name, primary_bucket, cache_base, install, progress
    )

    progress("finding_companions", "parsing description for companion links")
    companion_links = companion_finder.extract_companion_links(primary_entry.description_html)
    progress("finding_companions", f"found {len(companion_links)} links")

    companion_outcomes: list[ItemOutcome] = []
    lookup_failures: list[companion_finder.FailedCompanion] = []

    for link in companion_links:
        time.sleep(INTER_DOWNLOAD_DELAY_SECONDS)

        progress("resolving_companion", f"{link.label} ({link.content_id})")
        try:
            entry = ocs_client.get_content(link.content_id, provider_base=provider_base)
        except ocs_client.OcsError as exc:
            lookup_failures.append(companion_finder.FailedCompanion(link=link, error=str(exc)))
            progress("resolving_companion", f"FAILED: {link.label}: {exc}")
            continue

        bucket = companion_finder.bucket_for_typeid(entry.typeid)
        outcome = _process_item(entry, link.label, bucket, cache_base, install, progress)
        companion_outcomes.append(outcome)

    progress("done", f"{len(companion_outcomes) + 1} items processed")

    return PipelineResult(
        primary=primary_outcome,
        companions=companion_outcomes,
        companion_lookup_failures=lookup_failures,
    )


def format_summary(result: PipelineResult) -> str:
    """
    Build the plain-text summary described in the design doc's flow
    diagram (section 6) -- a CLI can print this directly; a GUI can
    use it as a starting point or build its own richer view from the
    same PipelineResult data.
    """
    lines: list[str] = []

    lines.append("=== Installed ===")
    if result.installed_items:
        for item in result.installed_items:
            lines.append(
                f"  \"{item.display_name}\" ({item.typename}) "
                f"-> {item.install_path}"
            )
    else:
        lines.append("  (nothing installed)")

    download_only = result.download_only_items
    if download_only:
        lines.append("")
        lines.append("=== Downloaded but not installed (v2 feature) ===")
        for item in download_only:
            lines.append(
                f"  \"{item.name}\" ({item.typename}) "
                f"-> files at {item.cache_dir}/extracted/"
            )

    unknown = result.unknown_type_items
    if unknown:
        lines.append("")
        lines.append("=== Unknown content type (needs review) ===")
        for item in unknown:
            lines.append(
                f"  \"{item.name}\" (typeid={item.typeid}, typename={item.typename!r}) "
                f"-- downloaded to {item.cache_dir}, not installed; "
                f"this typeid isn't in our known table yet"
            )

    failed = result.failed_items
    if failed:
        lines.append("")
        lines.append("=== Failed to download ===")
        for item in failed:
            lines.append(f"  \"{item.name}\": {item.fetch_error}")

    if result.companion_lookup_failures:
        lines.append("")
        lines.append("=== Companion links that couldn't be looked up ===")
        for failure in result.companion_lookup_failures:
            lines.append(f"  \"{failure.link.label}\" (id={failure.link.content_id}): {failure.error}")

    return "\n".join(lines)
