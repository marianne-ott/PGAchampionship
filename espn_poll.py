#!/usr/bin/env python3
"""Pull live scoring from ESPN's public golf-leaderboard endpoint.

This is the same feed the Cloudflare Worker (`cloudflare-worker/src/espn.js`)
hits every minute; this Python port is used by the GitHub Pages fallback path
(see `build_static.py`) so both data paths see identical numbers.

Endpoint: GET https://site.api.espn.com/apis/site/v2/sports/golf/leaderboard
- Undocumented but stable; powers ESPN's own mobile app and many third-party
  golf trackers. Returns the *current* PGA Tour event automatically.
- Refreshes upstream every ~30 seconds.

Module shape mirrors the cloudflare-worker adapter 1:1: the same row shape
flows into `pool_scoring.compute_pool_standings`, and the same leaderboard
snapshot shape is consumed by `docs/index.html`. Keep them in lock-step.
"""

from __future__ import annotations

import calendar
import json
import re
import time
import urllib.request
from typing import Any

from pool_scoring import position_points

ESPN_LEADERBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/golf/leaderboard"

# Major championships are 4 rounds; ESPN's response has no `finalRound` field.
# Only matters if we ever point this at a 3-round alt-format event.
FINAL_ROUND_NUMBER = 4

# Round-N column count in the leaderboard-table headers.
ROUND_COLS = 4

# ESPN status.type.name values used by the adapter.
STATUS_IN_PROGRESS = "STATUS_IN_PROGRESS"
STATUS_PLAY_COMPLETE = "STATUS_PLAY_COMPLETE"
STATUS_SCHEDULED = "STATUS_SCHEDULED"
# Cut/WD/DQ are folded into one "non-playing" bucket; pool scoring treats them
# identically (cut_points + missed_cut=True). `STATUS_CUT` is the actual value
# ESPN emits — *not* `STATUS_CUT_OFF`, which is used in other ESPN endpoints
# and was previously a source-of-bug here.
STATUS_CUT = "STATUS_CUT"
STATUS_WITHDRAWN = "STATUS_WITHDRAWN"
STATUS_DISQUALIFIED = "STATUS_DISQUALIFIED"

NON_PLAYING_STATUSES = frozenset(
    {STATUS_CUT, STATUS_WITHDRAWN, STATUS_DISQUALIFIED}
)

# Tee-time marker on the THRU column for scheduled players. The front-end
# parses "@<ISO>" and renders it in the visitor's local timezone.
TEE_TIME_PREFIX = "@"

# ESPN serves a slightly smaller payload to bot-y user-agents — harmless for
# our purposes, but worth mimicking a real browser to stay consistent with the
# worker.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def fetch_leaderboard_data(timeout: int = 30) -> dict[str, Any]:
    """Hit the ESPN endpoint and return the parsed JSON response."""
    req = urllib.request.Request(
        ESPN_LEADERBOARD_URL,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Non-JSON ESPN response: {body[:200]!r}") from e
    if not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected ESPN response: {payload!r}")
    return payload


def _event(payload: dict[str, Any]) -> dict[str, Any]:
    events = payload.get("events") or []
    if not events or not isinstance(events[0], dict):
        raise RuntimeError("ESPN response missing events[0]")
    return events[0]


def _competition(payload: dict[str, Any]) -> dict[str, Any]:
    ev = _event(payload)
    comps = ev.get("competitions") or []
    if not comps or not isinstance(comps[0], dict):
        raise RuntimeError("ESPN response missing events[0].competitions[0]")
    return comps[0]


_FLAG_CC_RE = re.compile(r"/countries/\d+/([a-z]{2,4})\.[a-z]+$", re.IGNORECASE)


def _country_code_from_flag(flag_href: str | None) -> str:
    if not flag_href:
        return ""
    m = _FLAG_CC_RE.search(flag_href)
    return m.group(1).upper() if m else ""


def _display_position(competitor: dict[str, Any]) -> str:
    """Cut/WD/DQ first (ESPN sends "-" in position.displayName for those),
    otherwise the position string ESPN renders. WD/DQ are surfaced as "MC"
    too — they score `cut_points` in the pool just like a missed cut."""
    status = competitor.get("status") or {}
    t = ((status.get("type") or {}).get("name")) or ""
    if t in NON_PLAYING_STATUSES:
        return "MC"
    pos = status.get("position")
    if isinstance(pos, dict) and pos.get("displayName"):
        return str(pos["displayName"])
    return ""


def _linescores_by_period(competitor: dict[str, Any]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = {}
    for ls in competitor.get("linescores") or []:
        if isinstance(ls, dict) and isinstance(ls.get("period"), int):
            out[ls["period"]] = ls
    return out


def _is_done_final_round(competitor: dict[str, Any]) -> bool:
    """True only if the player has actually played their R(FINAL_ROUND_NUMBER).
    Missed-cut / WD / DQ never reach this state, which is exactly what the
    pool's "all picks finished R4" rule needs."""
    ls = _linescores_by_period(competitor).get(FINAL_ROUND_NUMBER)
    return bool(ls and isinstance(ls.get("value"), (int, float)))


def pga_player_rows(payload: dict[str, Any], *, cut_points: int = 75) -> list[dict[str, Any]]:
    """One pool-scoring row per golfer.

    Row shape matches `cloudflare-worker/src/espn.js#extractPlayerRows` so
    both data paths feed `pool_scoring.compute_pool_standings` identically.
    """
    competitors = _competition(payload).get("competitors") or []
    out: list[dict[str, Any]] = []
    for c in competitors:
        athlete = c.get("athlete") or {}
        display_name = str(athlete.get("displayName") or "").strip()
        if not display_name:
            continue
        country = _country_code_from_flag(((athlete.get("flag") or {}).get("href")))
        position = _display_position(c)
        pts, mc = position_points(position, cut_points=cut_points)
        out.append(
            {
                "displayName": display_name,
                "country": country,
                "position": position,
                "points": pts,
                "missedCut": mc,
                "doneFinalRound": _is_done_final_round(c),
            }
        )
    return out


def _today_cells(competitor: dict[str, Any]) -> tuple[str, str]:
    """Pick the (today-score, thru) cells for the display table.

    - Round in progress       → today's to-par, thru-hole number
    - Round complete (signed) → today's to-par, "F"
    - Scheduled (between rounds or pre-tournament) → "-" plus "@<ISO tee time>"
      so the client formats it in the visitor's local timezone
    - Cut/WD/DQ               → both blank (matches ESPN's UI)
    """
    ls_by_period = _linescores_by_period(competitor)
    status = competitor.get("status") or {}
    t = ((status.get("type") or {}).get("name")) or ""
    period_raw = status.get("period")
    period = period_raw if isinstance(period_raw, int) else 0
    thru_raw = status.get("thru")
    thru = thru_raw if isinstance(thru_raw, int) else None

    current_ls = ls_by_period.get(period) if period else None
    current_has_value = bool(
        current_ls and isinstance(current_ls.get("value"), (int, float))
    )

    if t == STATUS_IN_PROGRESS and current_has_value:
        return (
            str(current_ls.get("displayValue") or ""),
            str(thru) if thru is not None else "",
        )
    if t == STATUS_PLAY_COMPLETE and current_has_value:
        return str(current_ls.get("displayValue") or ""), "F"
    if t == STATUS_SCHEDULED:
        tee = status.get("teeTime")
        return "-", (TEE_TIME_PREFIX + tee) if tee else ""
    if t in NON_PLAYING_STATUSES:
        return "", ""
    if current_has_value:
        return (
            str(current_ls.get("displayValue") or ""),
            str(thru) if thru is not None else "",
        )
    return "", ""


def _parse_to_par_display(display_value: Any) -> int | None:
    """Parse an ESPN to-par display value into a signed integer.
    Returns None for blanks / "-" / "E" placeholders so they don't contribute
    to the cumulative total. ("E" returns 0; only truly missing rounds → None.)"""
    if display_value is None:
        return None
    s = str(display_value).strip()
    if s == "" or s == "-":
        return None
    if s.upper() == "E":
        return 0
    try:
        return int(s)
    except ValueError:
        return None


def _running_total_to_par(competitor: dict[str, Any]) -> str:
    """Sum of each linescore's displayValue to give a running tournament-total.

    ESPN's `competitor.score.displayValue` only updates when a player signs
    their card for the round, so it lags behind whenever someone is mid-round.
    Summing the linescores (which include the in-progress round's partial to-par)
    matches ESPN's own UI. Returns "", "E", or signed string like "+3" / "-5".
    """
    total = 0
    any_scored = False
    for ls in competitor.get("linescores") or []:
        n = _parse_to_par_display(isinstance(ls, dict) and ls.get("displayValue"))
        if n is None:
            continue
        total += n
        any_scored = True
    if not any_scored:
        return ""
    if total == 0:
        return "E"
    return f"+{total}" if total > 0 else str(total)


def _round_total_cell(competitor: dict[str, Any], period: int) -> str:
    """Strokes for a completed round, or "--" if not yet finished.

    ESPN pre-populates future / not-started rounds with `value: 0`, which we
    suppress. The current in-progress round's linescore carries a partial
    cumulative stroke total — running scores live in the TODAY column, so we
    return "--" for the current round when it's mid-play too.
    """
    ls = _linescores_by_period(competitor).get(period)
    if not ls or not isinstance(ls.get("value"), (int, float)) or ls["value"] <= 0:
        return "--"
    status = competitor.get("status") or {}
    t = ((status.get("type") or {}).get("name")) or ""
    cur_period_raw = status.get("period")
    cur_period = cur_period_raw if isinstance(cur_period_raw, int) else 0
    if period == cur_period and t == STATUS_IN_PROGRESS:
        return "--"
    return str(round(ls["value"]))


def _player_cell(athlete: dict[str, Any], country: str) -> str:
    name = str(athlete.get("displayName") or "").strip()
    return f"{name} ({country})" if country else name


def _latest_scoring_timestamp(payload: dict[str, Any]) -> str | None:
    """Most recent linescore.teeTime across completed rounds — a rough
    "scoring last seen at" marker the UI can show next to fetchedAt."""
    best_ms = 0
    for c in _competition(payload).get("competitors") or []:
        for ls in c.get("linescores") or []:
            if (
                isinstance(ls, dict)
                and isinstance(ls.get("value"), (int, float))
                and ls.get("teeTime")
            ):
                t = ls["teeTime"]
                # ESPN timestamps are UTC ("…Z"); use calendar.timegm so the
                # struct_time is interpreted as UTC, not local time.
                try:
                    ms = calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%MZ")) * 1000
                except (TypeError, ValueError):
                    continue
                if ms > best_ms:
                    best_ms = ms
    if not best_ms:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(best_ms / 1000))


def leaderboard_snapshot_from_payload(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Display-table snapshot — same shape as the worker's `leaderboardSnapshot`."""
    ev = _event(payload)
    comp = _competition(payload)
    competitors = list(comp.get("competitors") or [])
    competitors.sort(
        key=lambda c: c.get("sortOrder") if isinstance(c.get("sortOrder"), int) else 9999
    )

    headers = ["POS", "PLAYER", "TOT", "TODAY", "THRU"] + [f"R{r}" for r in range(1, ROUND_COLS + 1)]

    rows: list[list[str]] = []
    row_done_final: list[bool] = []
    for c in competitors:
        athlete = c.get("athlete") or {}
        country = _country_code_from_flag(((athlete.get("flag") or {}).get("href")))
        today, thru = _today_cells(c)
        rnd_cells = [_round_total_cell(c, r) for r in range(1, ROUND_COLS + 1)]
        rows.append(
            [
                _display_position(c),
                _player_cell(athlete, country),
                _running_total_to_par(c),
                today,
                thru,
                *rnd_cells,
            ]
        )
        row_done_final.append(_is_done_final_round(c))

    snap: dict[str, Any] = {
        "source": url or ESPN_LEADERBOARD_URL,
        "fetchedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "columns": headers,
        "rows": rows,
        "rowDoneFinalRound": row_done_final,
        "eventName": ev.get("name"),
        "eventStatus": ((ev.get("status") or {}).get("type") or {}).get("description"),
        "roundDetail": ((comp.get("status") or {}).get("type") or {}).get("detail"),
    }
    scoring_ts = _latest_scoring_timestamp(payload)
    if scoring_ts:
        snap["scoringUpdatedAt"] = scoring_ts
    return snap


def leaderboard_snapshot(url: str | None = None) -> dict[str, Any]:
    """Convenience: fetch + snapshot in one call."""
    payload = fetch_leaderboard_data()
    return leaderboard_snapshot_from_payload(url or ESPN_LEADERBOARD_URL, payload)


def main() -> int:
    import argparse
    import sys

    p = argparse.ArgumentParser(description="ESPN live leaderboard")
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
        print(f"Scoring last updated: {snap['scoringUpdatedAt']}")
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
