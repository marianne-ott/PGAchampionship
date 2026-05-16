#!/usr/bin/env python3
"""Fetch the live leaderboard from ESPN, compute pool standings, write `docs/data.json`.

Used by the GitHub Actions workflow (`.github/workflows/deploy.yml`) every
5 minutes so the static site on GitHub Pages has a recent snapshot to fall
back on if the Cloudflare Worker is unreachable. Locally you can run this to
refresh `docs/data.json` for offline previewing of the static page.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from leaderboard_server import build_dashboard

_SCRIPT_DIR = Path(__file__).resolve().parent
_DOCS_DIR = _SCRIPT_DIR / "docs"
_DATA_JSON = _DOCS_DIR / "data.json"
_POOL_JSON = _SCRIPT_DIR / "pool.json"


def main() -> int:
    _DOCS_DIR.mkdir(parents=True, exist_ok=True)
    print("Fetching ESPN leaderboard", file=sys.stderr)
    bundle = build_dashboard(_POOL_JSON)
    _DATA_JSON.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rows = len((bundle.get("leaderboard") or {}).get("rows") or [])
    fetched_at = (bundle.get("leaderboard") or {}).get("fetchedAt")
    print(f"Wrote {_DATA_JSON} ({rows} rows, fetchedAt={fetched_at})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
