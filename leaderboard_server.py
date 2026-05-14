#!/usr/bin/env python3
"""Local HTTP UI for the leaderboard snapshot.

Serves the same `docs/index.html` that GitHub Pages serves. The static page
loads `./data.json`; locally we compute that JSON on the fly per request,
in production (Pages) it's a snapshot written by `build_static.py` via the
GitHub Actions workflow in `.github/workflows/deploy.yml`.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import masters_poll
import pool_scoring

_SCRIPT_DIR = Path(__file__).resolve().parent
_DOCS_DIR = _SCRIPT_DIR / "docs"
_INDEX_HTML_PATH = _DOCS_DIR / "index.html"


def _json_bytes(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def build_dashboard(url: str, pool_config_path: Path) -> dict[str, Any]:
    """Fetch the leaderboard + compute pool standings; the same payload Pages serves."""
    html = masters_poll.fetch_html(url)
    next_data = masters_poll.parse_next_data(html)
    lb = masters_poll.leaderboard_snapshot_from_next(url, next_data)

    pool_payload: dict[str, Any] = {
        "configPath": str(pool_config_path),
        "fileExists": pool_config_path.is_file(),
        "parseError": None,
        "standings": None,
    }
    if pool_config_path.is_file():
        try:
            cfg = pool_scoring.load_pool_config(pool_config_path)
            pool_payload["standings"] = pool_scoring.compute_pool_standings(next_data, url, cfg)
        except Exception as e:
            pool_payload["parseError"] = str(e)
    return {"leaderboard": lb, "pool": pool_payload}


def _send_no_cache_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")


def make_handler(default_url: str, pool_config_path: Path):
    class LeaderboardHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, fmt: str, *args: object) -> None:
            sys.stderr.write("%s - - [%s] %s\n" % (self.client_address[0], self.log_date_time_string(), fmt % args))

        def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            _send_no_cache_headers(self)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_dashboard_json(self, url: str) -> None:
            try:
                bundle = build_dashboard(url, pool_config_path)
                self._send_bytes(200, "application/json; charset=utf-8", _json_bytes(bundle))
            except Exception as e:
                self._send_bytes(500, "application/json; charset=utf-8", _json_bytes({"error": str(e)}))

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path or "/"

            if path in ("/", "/app", "/index.html"):
                try:
                    body = _INDEX_HTML_PATH.read_bytes()
                except FileNotFoundError:
                    self._send_bytes(
                        500,
                        "text/plain; charset=utf-8",
                        b"docs/index.html not found. Run from the project root.",
                    )
                    return
                self._send_bytes(200, "text/html; charset=utf-8", body)
                return

            qs = urllib.parse.parse_qs(parsed.query or "")
            raw = (qs.get("url") or [default_url])[0]
            url = urllib.parse.unquote(raw).strip() or default_url

            if path in ("/data.json", "/api/dashboard"):
                self._serve_dashboard_json(url)
                return

            if path == "/api/leaderboard":
                try:
                    snap = masters_poll.leaderboard_snapshot(url)
                    self._send_bytes(200, "application/json; charset=utf-8", _json_bytes(snap))
                except Exception as e:
                    self._send_bytes(500, "application/json; charset=utf-8", _json_bytes({"error": str(e)}))
                return

            self.send_error(404, "Not found")

    return LeaderboardHandler


def main() -> int:
    p = argparse.ArgumentParser(description="Local leaderboard viewer")
    p.add_argument("--host", default="127.0.0.1", help="Bind address")
    p.add_argument("--port", type=int, default=8765, help="Port")
    p.add_argument("--url", default=masters_poll.DEFAULT_URL, help="Default source URL in the form")
    p.add_argument(
        "--pool",
        default="pool.json",
        help="Path to pool.json (created from pool.example.json); shown in UI even if missing",
    )
    args = p.parse_args()

    pool_path = Path(args.pool)
    if not pool_path.is_absolute():
        pool_path = (_SCRIPT_DIR / pool_path).resolve()

    handler = make_handler(args.url, pool_path)
    httpd = ThreadingHTTPServer((args.host, args.port), handler)
    base = f"http://{args.host}:{args.port}"
    print(f"Serving {base}/", file=sys.stderr)
    print(f"  -> If you only see PGA players: open {base}/app (fresh URL) or hard-refresh (Cmd+Shift+R).", file=sys.stderr)
    print("  -> Chosen 7 is the first tab; full PGA field is on PGA leaderboard.", file=sys.stderr)
    print(f"  → Data URL: {args.url}", file=sys.stderr)
    print(f"  → Pool file: {pool_path}  (exists: {pool_path.is_file()})", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
