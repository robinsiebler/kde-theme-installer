"""
ocs_client.py

Minimal client for the OCS (Open Collaboration Services) API used by
the KDE Store / Pling network (store.kde.org, pling.com, kde-look.org,
opendesktop.org, gnome-look.org, etc.).

This deliberately does NOT scrape the store website. The website is a
JS-rendered SPA behind Cloudflare and isn't reliably reachable with
plain HTTP requests. Instead we talk to the underlying OCS REST API
directly -- the same one Plasma's own "Get New ..." dialogs use via
KNewStuff/Attica.

Key design points (see design doc for full rationale):
  - The provider's API base URL is discovered at runtime via
    download.kde.org/ocs/providers.xml, not hardcoded, since that's
    exactly the kind of thing that changes over time.
  - Content IDs are extracted from /p/<id> style URLs, which is a
    shape shared across the whole OCS network -- the same code handles
    store.kde.org, pling.com, opendesktop.org, etc.
  - Responses are OCS-spec XML; we parse with ElementTree, not regex.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import requests


PROVIDERS_XML_URL = "https://download.kde.org/ocs/providers.xml"

# Fallback if providers.xml is ever unreachable. Confirmed working as
# of mid-2026 testing; providers.xml is still the source of truth and
# should be preferred whenever it's reachable.
FALLBACK_PROVIDER_BASE = "https://api.kde-look.org/ocs/v1/"

USER_AGENT = "kde-theme-installer/0.1 (+https://robinsiebler.com)"

DEFAULT_TIMEOUT = 15  # seconds
RETRY_COUNT = 2
RETRY_BACKOFF_SECONDS = 1.5


class OcsError(Exception):
    """Base class for all OCS client errors."""


class OcsRequestError(OcsError):
    """Raised when an HTTP request fails outright (network, timeout, etc.)."""


class OcsApiError(OcsError):
    """Raised when the OCS API itself reports a non-ok status, or the
    response can't be parsed as expected OCS XML."""


@dataclass
class DownloadFile:
    """One downloadable file attached to a content entry. A single
    entry can have more than one (numbered downloadlink1, downloadlink2,
    ...) -- e.g. separate archives for different variants."""
    url: str
    filename: str
    size_kb: Optional[int] = None
    md5sum: Optional[str] = None
    mimetype: Optional[str] = None


@dataclass
class ContentEntry:
    """A single piece of content from the OCS store (a theme, icon
    pack, color scheme, etc.), as returned by content/data/<id>."""
    content_id: str
    name: str
    typeid: str
    typename: str
    description_html: str
    downloads: list[DownloadFile] = field(default_factory=list)
    homepage: str = ""
    preview_image_urls: list[str] = field(default_factory=list)
    # From previewpic1..N in the OCS response. Order matters: index 0
    # is the store's "main" preview image, used as the thumbnail in
    # the GUI's selection list. The rest are available for a fuller
    # gallery view if ever needed, but aren't fetched automatically.

    @property
    def primary_preview_url(self) -> Optional[str]:
        return self.preview_image_urls[0] if self.preview_image_urls else None

    @property
    def primary_download(self) -> Optional[DownloadFile]:
        return self.downloads[0] if self.downloads else None


def extract_content_id(url: str) -> Optional[str]:
    """
    Pull the numeric content ID out of a KDE Store / Pling-network URL.

    Handles shapes like:
        https://store.kde.org/p/123456
        https://store.kde.org/p/123456/
        https://www.pling.com/p/123456/
        https://www.pling.com/p/123456?something=1

    Returns None if no /p/<digits> segment is found.
    """
    match = re.search(r"/p/(\d+)", url)
    if match:
        return match.group(1)

    # Fallback: any URL whose path is purely digits in its last segment
    # (covers unusual or malformed but still numeric-id URLs).
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    for part in parts:
        if part.isdigit():
            return part

    return None


def _request_with_retry(url: str, **kwargs) -> requests.Response:
    """GET with a couple of retries on transient network/SSL errors.
    Deliberately does not retry on non-200 HTTP status -- that's a
    real response from the server and retrying won't change it."""
    last_exc: Optional[Exception] = None
    headers = kwargs.pop("headers", {})
    headers.setdefault("User-Agent", USER_AGENT)

    for attempt in range(RETRY_COUNT + 1):
        try:
            return requests.get(
                url, headers=headers, timeout=DEFAULT_TIMEOUT, **kwargs
            )
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < RETRY_COUNT:
                time.sleep(RETRY_BACKOFF_SECONDS * (attempt + 1))
                continue
            raise OcsRequestError(
                f"Request to {url} failed after {RETRY_COUNT + 1} attempts: {exc}"
            ) from last_exc

    # Unreachable, but keeps type checkers happy.
    raise OcsRequestError(f"Request to {url} failed: {last_exc}")


def get_provider_base_url(use_fallback_on_failure: bool = True) -> str:
    """
    Fetch download.kde.org/ocs/providers.xml and return the current
    provider's API base URL (e.g. "https://api.kde-look.org/ocs/v1/").

    If the providers.xml fetch or parse fails, falls back to a known
    hardcoded value rather than hard-failing the whole client -- this
    keeps the tool usable even if download.kde.org has a transient
    issue, at the cost of possibly using a stale provider URL.
    """
    try:
        resp = _request_with_retry(PROVIDERS_XML_URL)
    except OcsRequestError:
        if use_fallback_on_failure:
            return FALLBACK_PROVIDER_BASE
        raise

    if resp.status_code != 200:
        if use_fallback_on_failure:
            return FALLBACK_PROVIDER_BASE
        raise OcsApiError(
            f"providers.xml returned HTTP {resp.status_code}"
        )

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as exc:
        if use_fallback_on_failure:
            return FALLBACK_PROVIDER_BASE
        raise OcsApiError(f"Could not parse providers.xml: {exc}") from exc

    location_el = root.find("./provider/location")
    if location_el is None or not location_el.text:
        if use_fallback_on_failure:
            return FALLBACK_PROVIDER_BASE
        raise OcsApiError("providers.xml had no usable <location> element")

    base = location_el.text.strip()
    if not base.endswith("/"):
        base += "/"
    return base


def _parse_content_xml(xml_text: str, content_id: str) -> ContentEntry:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise OcsApiError(
            f"Could not parse content/data response for id {content_id}: {exc}"
        ) from exc

    status_el = root.find("./meta/status")
    if status_el is None or status_el.text != "ok":
        message_el = root.find("./meta/message")
        message = message_el.text if message_el is not None and message_el.text else "unknown error"
        raise OcsApiError(
            f"OCS API returned non-ok status for content id {content_id}: {message}"
        )

    content_el = root.find("./data/content")
    if content_el is None:
        raise OcsApiError(
            f"No <content> element in response for content id {content_id}"
        )

    def field_text(tag: str) -> str:
        el = content_el.find(tag)
        return el.text if el is not None and el.text else ""

    downloads: list[DownloadFile] = []
    # downloadlink1, downloadlink2, ... -- keep going until one is
    # missing. Real entries rarely go past 2-3, but don't hardcode a
    # ceiling lower than what the store could plausibly use.
    i = 1
    while True:
        link = field_text(f"downloadlink{i}")
        if not link:
            break
        name = field_text(f"downloadname{i}") or link.rsplit("/", 1)[-1]
        size_text = field_text(f"downloadsize{i}")
        md5 = field_text(f"downloadmd5sum{i}") or None
        tags = field_text(f"downloadtags{i}")

        mimetype = None
        if "mimetype=" in tags:
            mimetype = tags.split("mimetype=", 1)[1].split(",")[0].strip() or None

        size_kb = None
        if size_text.isdigit():
            size_kb = int(size_text)

        downloads.append(
            DownloadFile(
                url=link,
                filename=name,
                size_kb=size_kb,
                md5sum=md5,
                mimetype=mimetype,
            )
        )
        i += 1

    description_el = content_el.find("description")
    description_html = description_el.text if description_el is not None and description_el.text else ""

    preview_urls: list[str] = []
    j = 1
    while True:
        preview_url = field_text(f"previewpic{j}")
        if not preview_url:
            break
        preview_urls.append(preview_url)
        j += 1

    return ContentEntry(
        content_id=field_text("id") or content_id,
        name=field_text("name"),
        typeid=field_text("typeid"),
        typename=field_text("typename"),
        description_html=description_html,
        downloads=downloads,
        homepage=field_text("homepage"),
        preview_image_urls=preview_urls,
    )


def get_content(content_id: str, provider_base: Optional[str] = None) -> ContentEntry:
    """
    Fetch and parse content/data/<content_id> from the OCS API.

    If provider_base isn't supplied, it's resolved fresh via
    get_provider_base_url(). Callers doing many lookups in a batch
    (e.g. resolving a theme's companion links) should resolve the
    provider base once and pass it in explicitly to avoid refetching
    providers.xml for every single item.
    """
    if provider_base is None:
        provider_base = get_provider_base_url()

    url = f"{provider_base}content/data/{content_id}"
    resp = _request_with_retry(url, headers={"Accept": "application/xml"})

    if resp.status_code != 200:
        raise OcsApiError(
            f"content/data/{content_id} returned HTTP {resp.status_code}"
        )

    return _parse_content_xml(resp.text, content_id)


def get_content_from_url(url: str, provider_base: Optional[str] = None) -> ContentEntry:
    """Convenience wrapper: extract the content ID from a store URL and
    fetch it in one call. Raises OcsError if the URL doesn't look like
    a valid content URL."""
    content_id = extract_content_id(url)
    if content_id is None:
        raise OcsError(f"Could not find a content ID in URL: {url}")
    return get_content(content_id, provider_base=provider_base)
