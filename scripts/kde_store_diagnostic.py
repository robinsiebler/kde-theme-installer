#!/usr/bin/env python3
"""
KDE Store / Pling diagnostic script.

Purpose: figure out what data we can actually get for a given theme page,
via the OCS API first, falling back to raw HTML scraping.

Usage:
    python3 kde_store_diagnostic.py <store-page-url>

Example:
    python3 kde_store_diagnostic.py https://store.kde.org/p/123456

Run this on a machine with normal internet access (not in a sandboxed
environment) and paste the full output back.
"""

import sys
import re
import json
import requests
from urllib.parse import urlparse, parse_qs

OCS_API_BASE = "https://api.opendesktop.org/v1"  # Pling/OCS network base
# store.kde.org content urls look like: https://store.kde.org/p/123456
# or https://www.pling.com/p/123456 etc.

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) KDEStoreDiagnostic/0.1"
}


def extract_content_id(url: str):
    """
    Try to pull a numeric content ID out of common KDE/Pling URL shapes.
    Known shapes:
        https://store.kde.org/p/123456
        https://store.kde.org/p/123456/
        https://www.pling.com/p/123456/
        https://store.kde.org/p/123456?something=1
    """
    parsed = urlparse(url)
    path_parts = [p for p in parsed.path.split("/") if p]

    content_id = None
    for part in path_parts:
        if part.isdigit():
            content_id = part
            break

    return content_id, parsed


def try_ocs_api(content_id: str):
    print(f"\n--- Trying OCS API for content id: {content_id} ---")
    endpoint = f"{OCS_API_BASE}/content/data/{content_id}"
    print(f"GET {endpoint}")
    try:
        resp = requests.get(endpoint, headers=HEADERS, timeout=15)
        print(f"Status: {resp.status_code}")
        print(f"Content-Type: {resp.headers.get('Content-Type')}")
        print("First 2000 chars of response body:")
        print(resp.text[:2000])
        return resp
    except requests.RequestException as e:
        print(f"Request failed: {e}")
        return None


def try_raw_html(url: str):
    print(f"\n--- Fetching raw HTML for: {url} ---")
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        print(f"Status: {resp.status_code}")
        html = resp.text
        print(f"HTML length: {len(html)} chars")

        # Look for things that smell like download links or embedded JSON
        print("\n--- Searching for download-link patterns ---")
        download_patterns = re.findall(
            r'href="([^"]*(?:download|files)[^"]*)"', html, re.IGNORECASE
        )
        seen = set()
        for d in download_patterns[:30]:
            if d not in seen:
                seen.add(d)
                print(d)

        print("\n--- Searching for embedded JSON (script tags / data attrs) ---")
        json_like = re.findall(r'(\{[^{}]{0,300}"files?"[^{}]{0,300}\})', html)
        for j in json_like[:5]:
            print(j[:300])
            print("---")

        # Save full html to disk for manual inspection
        out_path = "kde_store_page_dump.html"
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"\nFull HTML saved to: {out_path}")

        return resp
    except requests.RequestException as e:
        print(f"Request failed: {e}")
        return None


def main():
    if len(sys.argv) != 2:
        print("Usage: python3 kde_store_diagnostic.py <store-page-url>")
        sys.exit(1)

    url = sys.argv[1]
    print(f"Target URL: {url}")

    content_id, parsed = extract_content_id(url)
    print(f"Parsed netloc: {parsed.netloc}")
    print(f"Extracted content ID: {content_id}")

    if content_id:
        try_ocs_api(content_id)
    else:
        print("Could not extract a numeric content ID from the URL; skipping API attempt.")

    try_raw_html(url)

    print("\n--- Done. Please paste the full terminal output back. ---")


if __name__ == "__main__":
    main()
