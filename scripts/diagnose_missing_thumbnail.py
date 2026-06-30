#!/usr/bin/env python3
"""
One-off diagnostic: why does Magna-Blur-Dark-Konsole have no thumbnail
in the GUI selection screen?

Run from the project root:
    python3 scripts/diagnose_missing_thumbnail.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import ocs_client
import requests

# Content ID for Magna-Blur-Dark-Konsole, pulled from the Magna
# description links we already know (https://www.pling.com/p/2102221/)
CONTENT_ID = "2102221"


def main():
    provider_base = ocs_client.get_provider_base_url()
    print(f"Provider base: {provider_base}")

    entry = ocs_client.get_content(CONTENT_ID, provider_base=provider_base)
    print(f"name: {entry.name}")
    print(f"typeid: {entry.typeid}")
    print(f"preview_image_urls: {entry.preview_image_urls}")
    print(f"primary_preview_url: {entry.primary_preview_url}")
    print()

    if not entry.preview_image_urls:
        print("DIAGNOSIS: the OCS API response for this content id has NO")
        print("previewpic fields at all -- this is a data gap on the API")
        print("side, not a bug in our fetch/render code. The website likely")
        print("pulls images from somewhere the OCS API doesn't expose for")
        print("this particular item.")
        return

    url = entry.primary_preview_url
    print(f"Attempting to fetch: {url}")
    try:
        resp = requests.get(url, timeout=10)
        print(f"HTTP status: {resp.status_code}")
        print(f"Content-Type: {resp.headers.get('Content-Type')}")
        print(f"Content length: {len(resp.content)} bytes")

        if resp.status_code == 200 and len(resp.content) > 0:
            print()
            print("DIAGNOSIS: the URL works fine and returns real image data.")
            print("The bug is likely in our PIL-based loading/rendering path")
            print("(gui.py's _load_thumbnail_async), not in fetching the URL.")
            # Try actually opening it with PIL to see if THAT's where it breaks
            try:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(resp.content))
                print(f"PIL opened it fine: format={img.format}, size={img.size}")
            except ImportError:
                print("(PIL not installed in this environment, can't test that step)")
            except Exception as e:
                print(f"PIL FAILED to open it: {e}")
                print("THIS is likely the actual bug.")
        else:
            print()
            print(f"DIAGNOSIS: the URL itself is failing (status {resp.status_code}).")
    except requests.RequestException as e:
        print(f"DIAGNOSIS: request itself failed: {e}")


if __name__ == "__main__":
    main()
