#!/usr/bin/env python3
"""
Pull golf leaderboard rows from embedded Next.js __NEXT_DATA__.

Default source is pgatour.com/leaderboard (full field in dehydrated React Query).
Optional: NYT Athletic live blog URL (--url) uses the tournament widget in the blog payload.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_URL = "https://www.pgatour.com/leaderboard"

ATHLETIC_FALLBACK_URL = (
    "https://www.nytimes.com/athletic/live-blogs/"
    "masters-2026-live-updates-final-round-leaderboard-result/68ScBNVsJRMC/"
)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_html(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_next_data(html: str) -> dict[str, Any]:
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">([^<]+)</script>',
        html,
    )
    if not m:
        raise RuntimeError("Could not find __NEXT_DATA__ in page (layout changed?)")
    return json.loads(m.group(1))


def _is_pga_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host.endswith("pgatour.com")


def cell_display(cell: dict[str, Any]) -> str:
    val = cell.get("value")
    if not val:
        return ""
    typ = val.get("__typename")
    if typ == "TextCellValue":
        return str(val.get("text") or "")
    if typ == "PlayerCellValue":
        name = val.get("name") or ""
        cc = val.get("country_code") or ""
        return f"{name} ({cc})" if cc else name
    return str(val)


def iter_tournament_modules(node: Any) -> Any:
    if isinstance(node, dict):
        if node.get("__typename") == "TournamentTableModule":
            yield node
        for v in node.values():
            yield from iter_tournament_modules(v)
    elif isinstance(node, list):
        for item in node:
            yield from iter_tournament_modules(item)


def pick_best_table(module: dict[str, Any]) -> dict[str, Any] | None:
    groups = module.get("groups") or []
    expanded: list[dict[str, Any]] = []
    preview: list[dict[str, Any]] = []
    for g in groups:
        gid = str(g.get("id") or "")
        tbl = g.get("table")
        if not isinstance(tbl, dict):
            continue
        if "expanded" in gid:
            expanded.append(tbl)
        elif "preview" in gid:
            preview.append(tbl)
    if expanded:
        return max(expanded, key=lambda t: len(t.get("rows") or []))
    if preview:
        return max(preview, key=lambda t: len(t.get("rows") or []))
    tables = [g.get("table") for g in groups if isinstance(g.get("table"), dict)]
    if not tables:
        return None
    return max(tables, key=lambda t: len(t.get("rows") or []))


def _pick_pga_leaderboard_payload(page_props: dict[str, Any]) -> dict[str, Any]:
    target_id = page_props.get("leaderboardId")
    queries = (page_props.get("dehydratedState") or {}).get("queries") or []
    candidates: list[tuple[dict[str, Any], str | None]] = []
    for q in queries:
        key = q.get("queryKey")
        if not isinstance(key, list) or not key or key[0] != "leaderboard":
            continue
        st = q.get("state") or {}
        if st.get("status") != "success":
            continue
        data = st.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("players"), list):
            continue
        q_lid: str | None = None
        if len(key) > 1 and isinstance(key[1], dict):
            raw = key[1].get("leaderboardId")
            q_lid = str(raw).strip() if raw is not None else None
        candidates.append((data, q_lid))

    if not candidates:
        raise RuntimeError("No leaderboard query in PGA __NEXT_DATA__ (layout changed?)")

    if target_id:
        ts = str(target_id).strip()
        for data, q_lid in candidates:
            if q_lid and q_lid == ts:
                return data
        for data, _ in candidates:
            tid = str(data.get("tournamentId") or data.get("id") or "").strip()
            if tid == ts:
                return data

    return max((d for d, _ in candidates), key=lambda d: len(d.get("players") or []))


def _pga_final_round_number(lb: dict[str, Any]) -> int:
    """Highest scheduled round (e.g. 4 at the Masters)."""
    rounds_meta = lb.get("rounds") or []
    nums: list[int] = []
    for r in rounds_meta:
        if isinstance(r, dict) and r.get("roundNumber") is not None:
            try:
                nums.append(int(r["roundNumber"]))
            except (TypeError, ValueError):
                pass
    return max(nums) if nums else 4


def pga_done_final_round(sd: dict[str, Any], final_round: int) -> bool:
    """True if the player finished all 18 holes of the tournament's last scheduled round (PGA Tour payload)."""
    rs = str(sd.get("roundStatus") or "")
    if "Completed" in rs and f"R{final_round}" in rs:
        return True
    thru = str(sd.get("thru") or "").strip().upper()
    try:
        cr = int(sd.get("currentRound") or 0)
    except (TypeError, ValueError):
        cr = 0
    return cr == final_round and thru in ("F", "FIN", "18")


def pga_position_points(position_display: str, *, cut_points: int = 75) -> tuple[int, bool]:
    """Listed place → min(place, cut_points); MC/CUT → cut_points. Returns (points, missed_cut).

    Anyone outside the cut tier (e.g. T117) is capped at `cut_points` so a
    pick that finished the tournament can't score worse than a missed-cut
    pick. A blank/dashed position (player has not teed off yet — common
    during R1 or for the late wave) is also treated as `cut_points`; otherwise
    a card stuffed with not-yet-started picks would trivially "win" the pool.
    The `missed_cut` flag stays False outside actual MC/CUT lines so the
    tier-drop rule only triggers when the player really missed the cut.
    """
    raw = (position_display or "").strip().upper()
    if not raw or raw in {"-", "\u2010", "\u2013", "\u2014", "--"}:
        return cut_points, False
    if raw in {"CUT", "MC"}:
        return cut_points, True
    num = raw[1:] if raw.startswith("T") else raw
    try:
        return min(int(num), cut_points), False
    except ValueError as e:
        raise ValueError(f"Unrecognized position {position_display!r}") from e


def pga_player_rows(next_data: dict[str, Any], *, cut_points: int = 75) -> list[dict[str, Any]]:
    """One dict per PGA leaderboard row (for pool matching). PlayerRowV3 only."""
    page_props = next_data.get("props", {}).get("pageProps", {})
    lb = _pick_pga_leaderboard_payload(page_props)
    final_rn = _pga_final_round_number(lb)
    out: list[dict[str, Any]] = []
    for pl in lb.get("players") or []:
        if pl.get("__typename") != "PlayerRowV3":
            continue
        info = pl.get("player") or {}
        sd = pl.get("scoringData") or {}
        pos = str(sd.get("position") or "").strip()
        pts, mc = pga_position_points(pos, cut_points=cut_points)
        out.append(
            {
                "displayName": str(info.get("displayName") or ""),
                "country": str(info.get("country") or ""),
                "position": pos,
                "points": pts,
                "missedCut": mc,
                "doneFinalRound": pga_done_final_round(sd, final_rn),
            }
        )
    return out


def extract_pga_leaderboard(next_data: dict[str, Any]) -> tuple[list[str], list[list[str]], list[bool]]:
    page_props = next_data.get("props", {}).get("pageProps", {})
    lb = _pick_pga_leaderboard_payload(page_props)
    final_rn = _pga_final_round_number(lb)
    rounds_meta = lb.get("rounds") or []
    round_labels: list[str] = []
    for r in rounds_meta:
        if isinstance(r, dict) and r.get("displayText"):
            round_labels.append(str(r["displayText"]))
    while len(round_labels) < 4:
        round_labels.append(f"R{len(round_labels) + 1}")

    headers = [
        "POS",
        "PLAYER",
        "TOT",
        "TODAY",
        "THRU",
        *round_labels[:4],
        "STROKES",
    ]
    players = [pl for pl in (lb.get("players") or []) if pl.get("__typename") == "PlayerRowV3"]
    players_sorted = sorted(
        players,
        key=lambda pl: int(pl.get("leaderboardSortOrder") or 0),
    )
    rows_out: list[list[str]] = []
    done_final: list[bool] = []
    for pl in players_sorted:
        info = pl.get("player") or {}
        sd = pl.get("scoringData") or {}
        name = str(info.get("displayName") or "")
        cc = str(info.get("country") or "")
        player_cell = f"{name} ({cc})" if cc else name
        rnds = sd.get("rounds") or []
        rnd_cells = [str(rnds[i]) if i < len(rnds) else "" for i in range(4)]
        done_final.append(pga_done_final_round(sd, final_rn))
        rows_out.append(
            [
                str(sd.get("position") or ""),
                player_cell,
                str(sd.get("total") or ""),
                str(sd.get("score") or ""),
                str(sd.get("thru") or ""),
                *rnd_cells,
                str(sd.get("totalStrokes") or ""),
            ]
        )
    return headers, rows_out, done_final


def extract_athletic_leaderboard(next_data: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    page_props = next_data.get("props", {}).get("pageProps", {})
    blog = page_props.get("initialLiveBlogData") or {}
    posts_conn = blog.get("posts") or {}
    items = posts_conn.get("items") or []

    chosen: dict[str, Any] | None = None
    for post in items:
        for att in post.get("attachments") or []:
            if att.get("type") != "tournament":
                continue
            tournament = att.get("tournament")
            for mod in iter_tournament_modules(tournament):
                tbl = pick_best_table(mod)
                if tbl and (chosen is None or len(tbl.get("rows") or []) > len(chosen.get("rows") or [])):
                    chosen = tbl

    if not chosen:
        raise RuntimeError("No tournament table found in live blog payload")

    cols = [c.get("title") or c.get("id") or "" for c in (chosen.get("columns") or [])]
    rows_out: list[list[str]] = []
    for row in chosen.get("rows") or []:
        cells = [cell_display(c) for c in (row.get("cells") or [])]
        rows_out.append(cells)
    return list(cols), rows_out


def extract_leaderboard(url: str, next_data: dict[str, Any]) -> tuple[list[str], list[list[str]], list[bool]]:
    if _is_pga_url(url):
        return extract_pga_leaderboard(next_data)
    headers, rows = extract_athletic_leaderboard(next_data)
    return headers, rows, [False] * len(rows)


def print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(h) for h in headers]
    for r in rows:
        for i, cell in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))

    def fmt_row(cells: list[str]) -> str:
        parts = []
        for i, h in enumerate(headers):
            text = cells[i] if i < len(cells) else ""
            parts.append(text.ljust(widths[i]))
        return "  ".join(parts)

    print(fmt_row(headers))
    print(fmt_row(["-" * w for w in widths]))
    for r in rows:
        print(fmt_row(r))


def leaderboard_snapshot_from_next(url: str, next_data: dict[str, Any]) -> dict[str, Any]:
    headers, rows, done_final = extract_leaderboard(url, next_data)
    return {
        "source": url,
        # Emit ISO 8601 in UTC so the frontend can render it in the user's
        # local timezone unambiguously (the previous local-time string was
        # ambiguous between the GitHub Actions runner's UTC and a developer's
        # machine TZ).
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "columns": headers,
        "rows": rows,
        "rowDoneFinalRound": done_final,
    }


def leaderboard_snapshot(url: str) -> dict[str, Any]:
    """Fetch the page, parse __NEXT_DATA__, return columns/rows plus metadata."""
    html = fetch_html(url)
    data = parse_next_data(html)
    return leaderboard_snapshot_from_next(url, data)


def run_once(url: str, as_json: bool) -> None:
    snap = leaderboard_snapshot(url)
    if as_json:
        out = {"columns": snap["columns"], "rows": snap["rows"]}
        if "rowDoneFinalRound" in snap:
            out["rowDoneFinalRound"] = snap["rowDoneFinalRound"]
        print(json.dumps(out, indent=2))
        return
    print(f"Source: {snap['source']}\n")
    print_table(snap["columns"], snap["rows"])


def main() -> int:
    p = argparse.ArgumentParser(
        description="Golf leaderboard from pgatour.com or NYT Athletic (__NEXT_DATA__)"
    )
    p.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"Page URL (default: PGA Tour). Athletic example: {ATHLETIC_FALLBACK_URL}",
    )
    p.add_argument("--json", action="store_true", help="Print JSON instead of ASCII table")
    p.add_argument("--watch", action="store_true", help="Re-fetch every --interval seconds")
    p.add_argument("--interval", type=int, default=45, help="Seconds between polls (with --watch)")
    args = p.parse_args()

    try:
        if args.watch:
            while True:
                print(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
                run_once(args.url, args.json)
                time.sleep(max(15, args.interval))
        else:
            run_once(args.url, args.json)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
