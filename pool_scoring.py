"""Tier pool: 7 picks, sum best 5 listed positions; CUT → cut_points (default 75)."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

# Fold common Scandinavian / Latin letters so Excel (ASCII) picks match PGA display names (e.g. Højgaard).
_ASCII_FOLD = str.maketrans(
    {
        "\u00f8": "o",
        "\u00d8": "o",  # ø Ø
        "\u00e5": "a",
        "\u00c5": "a",  # å Å
        "\u00e6": "ae",
        "\u00c6": "ae",
        "\u00f6": "o",
        "\u00d6": "o",
        "\u00e4": "a",
        "\u00c4": "a",
        "\u00fc": "u",
        "\u00dc": "u",
        "\u00ff": "y",
        "\u0178": "y",
    }
)


def normalize_player_name(name: str) -> str:
    s = (name or "").strip().translate(_ASCII_FOLD)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def position_points(position_display: str, *, cut_points: int = 75) -> tuple[int, bool]:
    """Listed place → min(place, cut_points); MC/CUT → cut_points; unstarted → cut_points.

    Anyone outside the cut tier (e.g. T117) is capped at `cut_points` — a
    pick that finished the tournament shouldn't score worse than a missed-cut
    pick. MC/CUT and unstarted picks also score `cut_points` so a card full
    of not-yet-started picks doesn't trivially "win" the pool. The
    `missed_cut` flag is True only for an actual MC/CUT line, so the
    tier-drop rule only triggers in that case.

    Mirrors `cloudflare-worker/src/scoring.js#positionPoints` — keep them in
    lock-step. Returns (points, missed_cut).
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


def load_pool_config(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8"))


def _build_player_index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = normalize_player_name(r["displayName"])
        if key:
            index[key] = r
    return index


def compute_pool_standings(
    player_rows: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Score the pool against a pre-extracted list of player rows.

    Source-agnostic — the caller (see `leaderboard_server.build_dashboard`)
    fetches from the upstream feed (ESPN today; see `espn_poll.py`) and
    passes in rows already shaped like
    `{displayName, country, position, points, missedCut, doneFinalRound}`.
    """
    # `cut_points` is no longer used to *compute* points here — the caller
    # already applied it when extracting `player_rows` — but we still echo
    # it back in the response so the frontend can render the rule text.
    cut_points = int(config.get("cut_points", 75))
    counting_picks = int(config.get("counting_picks", 5))
    picks_per = int(config.get("picks_per_friend", 7))

    index = _build_player_index(player_rows)

    friends_out: list[dict[str, Any]] = []
    for fr in config.get("friends", []):
        display = str(fr.get("name") or "").strip() or "?"
        raw_picks = fr.get("picks") or []
        if len(raw_picks) != picks_per:
            friends_out.append(
                {
                    "name": display,
                    "ok": False,
                    "error": f"Expected {picks_per} picks, got {len(raw_picks)}",
                    "total": None,
                    "picks": [],
                }
            )
            continue

        pick_rows: list[dict[str, Any]] = []
        pts_list: list[int | None] = []
        for p in raw_picks:
            label = str(p).strip()
            key = normalize_player_name(label)
            row = index.get(key) if key else None
            if not row:
                pick_rows.append(
                    {
                        "pick": label,
                        "matched": None,
                        "position": None,
                        "points": None,
                        "missedCut": None,
                        "counts": False,
                        "missing": True,
                        "doneFinalRound": False,
                    }
                )
                pts_list.append(None)
            else:
                pick_rows.append(
                    {
                        "pick": label,
                        "matched": row["displayName"],
                        "position": row["position"],
                        "points": row["points"],
                        "missedCut": row["missedCut"],
                        "counts": False,
                        "missing": False,
                        "doneFinalRound": bool(row.get("doneFinalRound")),
                    }
                )
                pts_list.append(int(row["points"]))

        if any(x is None for x in pts_list):
            friends_out.append(
                {
                    "name": display,
                    "ok": False,
                    "error": "One or more picks did not match the leaderboard (check spelling vs PGA display names).",
                    "total": None,
                    "picks": pick_rows,
                }
            )
            continue

        assert all(isinstance(x, int) for x in pts_list)
        pts_only = [int(x) for x in pts_list]
        n_drop = max(0, len(pts_only) - counting_picks)
        # Always drop the picks with the highest point totals (worst results).
        # Tie-break priority for which equal-points pick to drop:
        #   1. missed_cut=True first — those picks are locked at cut_points
        #      forever, so dropping them frees a tied non-MC pick (e.g. a player
        #      capped at T78 = cut_points) to still benefit from any future
        #      climb up the leaderboard.
        #   2. then highest pot index (rightmost) — protects the marquee
        #      early-tier picks like the Pot 1 favourite when two same-points
        #      actives are competing for the drop.
        indexed = [(i, pts_only[i], bool(pick_rows[i].get("missedCut"))) for i in range(len(pts_only))]
        worst = sorted(indexed, key=lambda t: (t[1], 1 if t[2] else 0, t[0]), reverse=True)[:n_drop]
        drop_idx = {i for i, _, _ in worst}
        total = sum(p for i, p in enumerate(pts_only) if i not in drop_idx)
        for i, pr in enumerate(pick_rows):
            pr["counts"] = i not in drop_idx

        friends_out.append(
            {
                "name": display,
                "ok": True,
                "error": None,
                "total": total,
                "picks": pick_rows,
            }
        )

    ok_rows = [f for f in friends_out if f["ok"]]
    bad_rows = [f for f in friends_out if not f["ok"]]
    ok_rows.sort(key=lambda f: int(f["total"] or 0))

    rank_next = 1
    i = 0
    while i < len(ok_rows):
        tval = ok_rows[i]["total"]
        j = i
        while j < len(ok_rows) and ok_rows[j]["total"] == tval:
            j += 1
        for k in range(i, j):
            ok_rows[k]["rank"] = rank_next
        rank_next += j - i
        i = j

    for f in bad_rows:
        f["rank"] = None

    return {
        "ok": True,
        "cut_points": cut_points,
        "counting_picks": counting_picks,
        "picks_per_friend": picks_per,
        "friends": ok_rows + bad_rows,
    }
