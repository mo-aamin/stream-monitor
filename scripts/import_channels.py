#!/usr/bin/env python3
"""Import channels from the old channels.json format into Stream Monitor.

Usage:
    python3 scripts/import_channels.py channels.json [base_url]

The old file looks like:
    {"youtube": ["@a", "@b"], "twitch": ["x"], "kick": ["y"]}

Each platform list is posted to /api/channels/bulk. Existing channels are
skipped by the server. Rumble entries are supported if present.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

PLATFORMS = ("youtube", "twitch", "kick", "rumble")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2

    source = sys.argv[1]
    base_url = (sys.argv[2] if len(sys.argv) > 2 else "http://localhost:8000").rstrip("/")

    try:
        with open(source, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read {source}: {exc}", file=sys.stderr)
        return 1

    if not isinstance(data, dict):
        print("Expected a JSON object keyed by platform.", file=sys.stderr)
        return 1

    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    total_added = 0
    total_skipped = 0
    total_errors = 0
    for platform in PLATFORMS:
        entries = data.get(platform) or []
        if not isinstance(entries, list) or not entries:
            continue
        lines = "\n".join(str(entry).strip() for entry in entries if str(entry).strip())
        if not lines:
            continue
        payload = json.dumps({"platform": platform, "lines": lines}).encode()
        request = urllib.request.Request(
            f"{base_url}/api/channels/bulk", data=payload, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                result = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            print(f"{platform}: HTTP {exc.code} — {exc.read().decode(errors='replace')}", file=sys.stderr)
            return 1
        except urllib.error.URLError as exc:
            print(f"{platform}: could not reach {base_url}: {exc.reason}", file=sys.stderr)
            return 1
        total_added += int(result.get("added", 0))
        total_skipped += int(result.get("skipped", 0))
        total_errors += len(result.get("errors", []))
        print(f"{platform}: {result.get('message', 'imported')}")
        for error in result.get("errors", []):
            print(f"  ! {error.get('line')}: {error.get('error')}")

    print(f"\nDone. added={total_added} skipped={total_skipped} invalid={total_errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
