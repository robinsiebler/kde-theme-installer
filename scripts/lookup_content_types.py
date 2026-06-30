#!/usr/bin/env python3
"""
One-shot OCS content-type lookup.

Fetches content/data for a fixed list of known content IDs (pulled from
the Magna-Dark-Global-6 description, which conveniently links to one
example of nearly every content type we care about) and prints out
their id, name, typeid, and typename.

Run on a machine with normal internet access:
    python3 lookup_content_types.py

No arguments needed -- the IDs are hardcoded below. Edit CONTENT_IDS
to add more if you want to test additional content.
"""

import re
import sys
import requests
import xml.etree.ElementTree as ET

PROVIDER_BASE = "https://api.kde-look.org/ocs/v1/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) KDEStoreTypeLookup/0.1"
}

# Content IDs pulled from links in the Magna-Dark-Global-6 description.
# Label is just for our own readability in the output; the real type
# comes from the API response itself.
CONTENT_IDS = {
    "2102240": "Magna-Dark-Icons (expect: Icon Theme)",
    "2102246": "Magna-Dark-Plasma (expect: Plasma Style / Desktop Theme)",
    "2102245": "Magna-Dark-Kvantum (expect: Kvantum / App Style -- not in our v1 table yet)",
    "2139968": "Magna-SDDM-6 (expect: SDDM / Login Theme)",
    "2102231": "Magna-Violet-Dark-ColorScheme (expect: Color Scheme)",
    "2102230": "Magna-Blue-Dark-ColorScheme (expect: Color Scheme)",
    "2134193": "Magna-Blur-Dark-Aurorae-6 (expect: Window Decoration / Aurorae)",
    "2134194": "Magna-Dark-Aurorae-6 (expect: Window Decoration / Aurorae)",
    "2102220": "Magna-Dark-Konsole (expect: Konsole Color Scheme)",
    "2102221": "Magna-Blur-Dark-Konsole (expect: Konsole Color Scheme)",
    "2102228": "Magna-Dark-GTK (expect: GTK Theme)",
    "2102216": "Magna Wallpaper (expect: Wallpaper)",
}


def extract_id_from_url(url: str):
    match = re.search(r"/p/(\d+)", url)
    return match.group(1) if match else None


def fetch_content(content_id: str):
    url = f"{PROVIDER_BASE}content/data/{content_id}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
    except requests.RequestException as e:
        return {"error": f"request failed: {e}"}

    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return {"error": f"XML parse failed: {e}"}

    status_el = root.find("./meta/status")
    if status_el is None or status_el.text != "ok":
        msg_el = root.find("./meta/message")
        return {"error": f"OCS status not ok: {msg_el.text if msg_el is not None else 'unknown'}"}

    content_el = root.find("./data/content")
    if content_el is None:
        return {"error": "no <content> element in response"}

    def field(tag):
        el = content_el.find(tag)
        return el.text if el is not None and el.text else ""

    return {
        "id": field("id"),
        "name": field("name"),
        "typeid": field("typeid"),
        "typename": field("typename"),
    }


def main():
    print(f"Provider base: {PROVIDER_BASE}\n")
    results = []

    for content_id, label in CONTENT_IDS.items():
        print(f"--- {content_id} :: {label} ---")
        result = fetch_content(content_id)
        if "error" in result:
            print(f"  ERROR: {result['error']}")
        else:
            print(f"  name:     {result['name']}")
            print(f"  typeid:   {result['typeid']}")
            print(f"  typename: {result['typename']}")
            results.append(result)
        print()

    # Summary table, deduplicated by typeid, for easy pasting back
    print("=" * 60)
    print("SUMMARY (unique typeid -> typename)")
    print("=" * 60)
    seen = {}
    for r in results:
        seen[r["typeid"]] = r["typename"]
    for typeid, typename in sorted(seen.items(), key=lambda x: (x[0] or "")):
        print(f"  {typeid}\t{typename}")


if __name__ == "__main__":
    main()
