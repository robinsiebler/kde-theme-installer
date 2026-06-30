"""
companion_finder.py

Finds "companion content" referenced in a theme's OCS description HTML
-- icon packs, color schemes, Aurorae window decorations, fonts,
wallpapers, etc. -- and resolves each one into a real ContentEntry via
the OCS API.

Per the design doc: we don't try to classify companion links from
their label text alone (no regex-guessing "is this the word 'Icons'
or 'Cursor'?"). Instead we extract every /p/<id> link found in the
description, look each one up for real via the OCS API, and let the
API's own typeid/typename tell us definitively what it is. The label
text is kept around for display purposes only (so the user can see
"Icons Magna-Dark-Icons" in the UI), not for classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

import ocs_client


# --- Type bucket classification -------------------------------------------
#
# typeid -> bucket mapping, confirmed empirically (see design doc
# section 3). Anything not in this dict is treated as UNKNOWN and
# surfaced to the user rather than silently dropped or silently
# auto-installed -- new/unrecognized content types should never be
# assumed safe.

BUCKET_AUTO_INSTALL = "auto_install"
BUCKET_DOWNLOAD_ONLY = "download_only"  # v2 install support not built yet
BUCKET_UNKNOWN = "unknown"

TYPEID_BUCKETS: dict[str, str] = {
    "722": BUCKET_AUTO_INSTALL,  # Global Themes (Plasma 6)
    "716": BUCKET_AUTO_INSTALL,  # Plasma 6 Splashscreens (same install
                                 # path/packaging as Global Themes --
                                 # confirmed via web search, real-world
                                 # discovered while testing Amy-Light)
    "104": BUCKET_AUTO_INSTALL,  # Plasma Themes
    "112": BUCKET_AUTO_INSTALL,  # Plasma Color Schemes
    "132": BUCKET_AUTO_INSTALL,  # Full Icon Themes
    "717": BUCKET_AUTO_INSTALL,  # Plasma 6 Window Decorations (Aurorae)
    "462": BUCKET_AUTO_INSTALL,  # Konsole Color Schemes
    "299": BUCKET_AUTO_INSTALL,  # Wallpapers KDE Plasma
    "101": BUCKET_DOWNLOAD_ONLY,  # SDDM Login Themes
    "135": BUCKET_DOWNLOAD_ONLY,  # GTK3/4 Themes
    "123": BUCKET_DOWNLOAD_ONLY,  # Kvantum
}

# Cursor theme and font typeids are not yet confirmed (see design doc
# open question #1) -- left out of TYPEID_BUCKETS deliberately so they
# fall through to BUCKET_UNKNOWN and get surfaced for review rather
# than silently mishandled. Fill these in once confirmed.


def bucket_for_typeid(typeid: str) -> str:
    return TYPEID_BUCKETS.get(typeid, BUCKET_UNKNOWN)


@dataclass
class CompanionLink:
    """A single /p/<id> link found in a description, before resolution."""
    label: str
    url: str
    content_id: str


@dataclass
class ResolvedCompanion:
    """A companion link after a successful OCS lookup."""
    link: CompanionLink
    entry: ocs_client.ContentEntry
    bucket: str


@dataclass
class FailedCompanion:
    """A companion link whose OCS lookup failed. Kept separate from
    ResolvedCompanion rather than using Optional fields, so calling
    code can't accidentally treat a failure as a success."""
    link: CompanionLink
    error: str


def extract_companion_links(description_html: str) -> list[CompanionLink]:
    """
    Parse a description's HTML and return every link pointing at a
    /p/<id> content URL, with a best-effort label.

    The label is the text between this link and the previous "break"
    (a <br> tag, or the start of the description if there isn't one)
    -- e.g. the "Icons Magna-Dark-Icons:" part of
    "Icons <b>Magna-Dark-Icons</b>: <a href=...>Here</a><br/>". It's
    used for display only -- never for type classification.
    """
    if not description_html:
        return []

    soup = BeautifulSoup(description_html, "html.parser")
    links: list[CompanionLink] = []
    seen_ids: set[str] = set()

    # Walk the whole soup's descendants once, in document order,
    # tracking text seen since the last <br> (or the start). This
    # correctly scopes each link's label to "this entry" rather than
    # accumulating everything from the start of the description --
    # KDE Store authors commonly write many entries inside a single
    # <p>, separated only by <br> tags, not separate paragraphs.
    current_segment: list[str] = []

    for element in soup.descendants:
        name = getattr(element, "name", None)

        if name == "br":
            current_segment = []
            continue

        if name == "a" and element.has_attr("href"):
            href = element["href"]
            content_id = ocs_client.extract_content_id(href)

            if content_id is not None and content_id not in seen_ids:
                seen_ids.add(content_id)
                label = _clean_label("".join(current_segment))
                if not label:
                    label = element.get_text(strip=True) or "(unlabeled link)"
                links.append(
                    CompanionLink(label=label, url=href, content_id=content_id)
                )

            # Whether or not this was a usable link, don't let the
            # link's own visible text ("Here") bleed into the next
            # segment's label.
            current_segment = []
            continue

        if isinstance(element, str):
            # Skip text that's inside an <a> we already handled above
            # (NavigableStrings inside <a> are still separate
            # descendants we'd otherwise double-collect).
            if element.find_parent("a") is not None:
                continue
            current_segment.append(str(element))

    return links


def _clean_label(raw: str) -> str:
    """Strip whitespace, stray colons, underscores/dashes used as
    visual separators in some descriptions, and collapse internal
    whitespace down to single spaces."""
    text = raw.strip(" :\u00a0_-")
    text = " ".join(text.split())
    return text


def resolve_companions(
    links: list[CompanionLink],
    provider_base: Optional[str] = None,
) -> tuple[list[ResolvedCompanion], list[FailedCompanion]]:
    """
    Look up every companion link via the OCS API and bucket each
    successful result by its real typeid.

    Per the design doc: one failed lookup must not abort the whole
    batch. Failures are collected separately and returned alongside
    successes so the caller can report them without losing the items
    that did resolve.
    """
    if provider_base is None:
        provider_base = ocs_client.get_provider_base_url()

    resolved: list[ResolvedCompanion] = []
    failed: list[FailedCompanion] = []

    for link in links:
        try:
            entry = ocs_client.get_content(link.content_id, provider_base=provider_base)
        except ocs_client.OcsError as exc:
            failed.append(FailedCompanion(link=link, error=str(exc)))
            continue

        bucket = bucket_for_typeid(entry.typeid)
        resolved.append(ResolvedCompanion(link=link, entry=entry, bucket=bucket))

    return resolved, failed


def find_and_resolve_companions(
    description_html: str,
    provider_base: Optional[str] = None,
) -> tuple[list[ResolvedCompanion], list[FailedCompanion]]:
    """Convenience wrapper: extract links from description HTML and
    resolve them in one call."""
    links = extract_companion_links(description_html)
    return resolve_companions(links, provider_base=provider_base)
