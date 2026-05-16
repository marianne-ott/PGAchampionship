#!/usr/bin/env python3
"""Pull live scoring from pgachampionship.com's Brightspot Delivery GraphQL.

The PGA Championship's own leaderboard page renders a React component that
talks to a persisted-query GraphQL endpoint behind the same origin. We bypass
the page render entirely and call that endpoint directly — the same data the
official site itself displays, typically a few minutes ahead of the snapshot
inlined in pgatour.com's `__NEXT_DATA__` blob.

Module shape mirrors `masters_poll`: the call sites that today consume the
PGA Tour `__NEXT_DATA__` (build_static, leaderboard_server, pool_scoring) can
swap to this module without otherwise restructuring.
"""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from typing import Any

# What users (and the workflow) point at. We accept either the public
# leaderboard URL (for display / hyperlinks) or the GraphQL URL itself.
DEFAULT_URL = "https://www.pgachampionship.com/leaderboard"

GRAPHQL_ENDPOINT = "https://www.pgachampionship.com/graphql/delivery/pga/v4/scoring"

# Brightspot Delivery exposes each GraphQL query under a stable named handle
# (`operationName`) plus an Apollo Automatic-Persisted-Queries SHA256 hash.
# Both must match a query the server already knows about — we never send the
# query body. The hash below was captured from the live web app's network
# traffic; if pga.com ever rotates it the call returns
# `PersistedQueryNotFound` and `fetch_leaderboard_data` will raise.
LEADERBOARD_OPERATION = "Leaderboard"
LEADERBOARD_QUERY_SHA256 = (
    "bfeee62887c71cd2341b2954f7fc98884b84192dd3d3d01a41f54b1f252ba6f6"
)

# Event id for the 2026 PGA Championship at Aronimink. The site bakes this
# into the page as `data-event-id` on `<leaderboard-react>`; we hard-code it
# because the surrounding pool config is also 2026-specific.
EVENT_ID = "00000173-3422-d5ad-a7fb-3c7ead7b0000"

# PGA Championship is always 4 rounds. Hard-coded because the response has
# no explicit `finalRound` field — round count is implicit in the four
# `round{N}GolferRoundScore` slots.
FINAL_ROUND_NUMBER = 4

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def is_pgac_url(url: str) -> bool:
    host = urllib.parse.urlparse(url).hostname or ""
    return host.endswith("pgachampionship.com")


def fetch_leaderboard_data(timeout: int = 30) -> dict[str, Any]:
    """Hit the GraphQL endpoint and return the parsed JSON response."""
    variables = {
        "id": f"event-{EVENT_ID}-leaderboard",
        # The server only uses `timestamp` as a cache-buster; freshness is
        # reported back in `data.leaderboard.lastUpdated` (epoch ms).
        "timestamp": int(time.time() * 1000),
    }
    extensions = {
        "persistedQuery": {
            "version": 1,
            "sha256Hash": LEADERBOARD_QUERY_SHA256,
        },
    }
    qs = urllib.parse.urlencode(
        {
            "operationName": LEADERBOARD_OPERATION,
            "variables": json.dumps(variables, separators=(",", ":")),
            "extensions": json.dumps(extensions, separators=(",", ":")),
        }
    )
    url = f"{GRAPHQL_ENDPOINT}?{qs}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Non-JSON GraphQL response: {body[:200]!r}") from e
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected GraphQL response: {payload!r}")
    if payload.get("errors"):
        # The most likely real-world cause: pgachampionship.com rotated the
        # persisted-query hash. Surface it with enough context to recapture
        # a new hash from the live site.
        raise RuntimeError(
            f"GraphQL errors from {GRAPHQL_ENDPOINT}: {payload['errors']!r}. "
            f"If this includes PersistedQueryNotFound, the {LEADERBOARD_OPERATION} "
            "operation's sha256Hash needs to be re-captured from the live site."
        )
    return payload


def _leaderboard_obj(payload: dict[str, Any]) -> dict[str, Any]:
    lb = (payload.get("data") or {}).get("leaderboard")
    if not isinstance(lb, dict):
        raise RuntimeError("No `data.leaderboard` in GraphQL response")
    return lb


def _display_name(row: dict[str, Any]) -> str:
    first = (row.get("golferFirstName") or "").strip()
    last = (row.get("golferLastName") or "").strip()
    return (first + " " + last).strip()


def position_points(position_display: str, *, cut_points: int = 75) -> tuple[int, bool]:
    """Listed place → min(place, cut_points); MC/CUT → cut_points; unstarted → cut_points.

    Anyone outside the cut tier (e.g. T117) is capped at `cut_points` — a
    pick that finished the tournament shouldn't score worse than a missed-cut
    pick. MC/CUT and unstarted picks also score `cut_points` so a card full
    of not-yet-started picks doesn't trivially "win" the pool. The
    `missed_cut` flag is True only for an actual MC/CUT line, so the
    tier-drop rule only triggers in that case.

    Mirrors `cloudflare-worker/src/scoring.js#positionPoints` and
    `masters_poll.pga_position_points` — keep them in lock-step.
    Returns (points, missed_cut).
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


def _round_completed(round_score: Any) -> bool:
    return isinstance(round_score, dict) and bool(round_score.get("completed"))


def _normalize_position(raw_position: Any) -> str:
    """Translate pgachampionship.com's `position` field to our display form.

    Cut players come back as "CUT" (or with the C upper/lowercased depending on
    feed version); we surface them as "MC" to match the worker's ESPN path and
    keep the POS column and pool chip readable. Everything else passes through.
    """
    pos = str(raw_position or "").strip()
    if pos.upper() == "CUT":
        return "MC"
    return pos


def _is_done_final_round(row: dict[str, Any]) -> bool:
    return _round_completed(row.get(f"round{FINAL_ROUND_NUMBER}GolferRoundScore"))


def pga_player_rows(payload: dict[str, Any], *, cut_points: int = 75) -> list[dict[str, Any]]:
    """One pool-scoring row per golfer — same shape as `masters_poll.pga_player_rows`."""
    lb = _leaderboard_obj(payload)
    out: list[dict[str, Any]] = []
    for r in lb.get("rows") or []:
        if r.get("__typename") != "GolferScore":
            continue
        pos = _normalize_position(r.get("position"))
        pts, mc = position_points(pos, cut_points=cut_points)
        out.append(
            {
                "displayName": _display_name(r),
                # The PGA Championship feed has no country code on the row
                # (those live in a separate StaticLeaderboardAssets query
                # that doesn't expose nationality, only sponsor logos).
                # The pool matches by name anyway, so empty is fine; the UI's
                # Nordic-flag pill in the player search just won't fire.
                "country": "",
                "position": pos,
                "points": pts,
                "missedCut": mc,
                "doneFinalRound": _is_done_final_round(r),
            }
        )
    return out


def _round_total_cell(round_score: Any) -> str:
    """Final strokes for a completed round, or "--" if the round hasn't been
    finished yet. pgatour.com leaves `roundTotal` empty/None until the player
    signs their card, which is exactly when ESPN's display switches from
    "--" to the strokes total."""
    if not isinstance(round_score, dict):
        return "--"
    total = round_score.get("roundTotal")
    return str(total) if total else "--"


def _today_cells(row: dict[str, Any]) -> tuple[str, str]:
    """Return (today-score-vs-par, thru) for the player's most recent round.

    Picks the round the golfer is currently in (per `currentRound` on the
    row). If they haven't teed off yet — for the late wave during R1 or
    anyone before their R2/R3/R4 tee time — `roundN…RoundScore` is null and
    both cells are blank, matching how pgatour.com renders unstarted picks.
    """
    cur_rn = int(row.get("currentRound") or 0)
    if not cur_rn:
        return "", ""
    score_obj = row.get(f"round{cur_rn}GolferRoundScore")
    if not isinstance(score_obj, dict):
        return "", ""
    today = str(score_obj.get("roundScore") or "")
    thru = score_obj.get("thru")
    if thru:
        thru_cell = str(thru)
    else:
        hole = score_obj.get("currentHole")
        thru_cell = str(hole) if hole is not None else ""
    return today, thru_cell


def extract_leaderboard(payload: dict[str, Any]) -> tuple[list[str], list[list[str]], list[bool]]:
    """Headers + rows for the display table.

    STROKES (cumulative total strokes) is intentionally omitted: until all
    four rounds are complete it's just R1 (or R1+R2, etc.) which reads as a
    confusing partial sum. The per-round R1..R4 columns carry the same
    information without ambiguity. Kept in lock-step with the Cloudflare
    worker's espn.js `leaderboardSnapshot` so both data paths emit the same
    column set."""
    lb = _leaderboard_obj(payload)
    headers = ["POS", "PLAYER", "TOT", "TODAY", "THRU", "R1", "R2", "R3", "R4"]

    rows_in = [r for r in (lb.get("rows") or []) if r.get("__typename") == "GolferScore"]
    rows_in.sort(key=lambda r: int(r.get("sortOrder") or 0))

    rows_out: list[list[str]] = []
    done_final: list[bool] = []
    for r in rows_in:
        today_cell, thru_cell = _today_cells(r)
        rnd_cells = [_round_total_cell(r.get(f"round{n}GolferRoundScore")) for n in (1, 2, 3, 4)]
        rows_out.append(
            [
                _normalize_position(r.get("position")),
                _display_name(r),
                str(r.get("overallPar") or ""),
                today_cell,
                thru_cell,
                *rnd_cells,
            ]
        )
        done_final.append(_is_done_final_round(r))
    return headers, rows_out, done_final


def leaderboard_snapshot_from_payload(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    headers, rows, done_final = extract_leaderboard(payload)
    lb = _leaderboard_obj(payload)
    last_updated_ms = lb.get("lastUpdated") or 0
    snap: dict[str, Any] = {
        "source": url,
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "columns": headers,
        "rows": rows,
        "rowDoneFinalRound": done_final,
    }
    if last_updated_ms:
        # The official site's own scoring clock. Exposed alongside fetchedAt
        # so the UI can later distinguish "scoring last changed at …" from
        # "we last fetched the page at …" if we want to.
        snap["scoringUpdatedAt"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_updated_ms / 1000)
        )
    return snap


def leaderboard_snapshot(url: str = DEFAULT_URL) -> dict[str, Any]:
    payload = fetch_leaderboard_data()
    return leaderboard_snapshot_from_payload(url, payload)


def main() -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="PGA Championship live leaderboard (GraphQL)")
    p.add_argument("--json", action="store_true", help="Print raw snapshot as JSON")
    args = p.parse_args()
    try:
        snap = leaderboard_snapshot()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0
    print(f"Source: {snap['source']}   fetchedAt: {snap['fetchedAt']}")
    if snap.get("scoringUpdatedAt"):
        print(f"Scoring last updated (official): {snap['scoringUpdatedAt']}")
    headers = snap["columns"]
    rows = snap["rows"]
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            if i < len(widths):
                widths[i] = max(widths[i], len(c))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * w for w in widths))
    for r in rows[:20]:
        print("  ".join(c.ljust(widths[i]) if i < len(widths) else c for i, c in enumerate(r)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
