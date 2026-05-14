"""Tier pool: 7 picks, sum best 5 listed positions; CUT → cut_points (default 75)."""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

import masters_poll

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
    next_data: dict[str, Any],
    data_url: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    if not masters_poll._is_pga_url(data_url):
        return {
            "ok": False,
            "error": "Pool scoring only works with a pgatour.com leaderboard URL.",
        }

    cut_points = int(config.get("cut_points", 75))
    counting_picks = int(config.get("counting_picks", 5))
    picks_per = int(config.get("picks_per_friend", 7))

    rows = masters_poll.pga_player_rows(next_data, cut_points=cut_points)
    index = _build_player_index(rows)

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
        # Ties are broken by *pot number* — when several picks have the same
        # points (e.g. multiple unstarted/missed-cut picks all at cut_points),
        # we drop the rightmost slots first (highest pot tier), protecting
        # the marquee early-tier picks like the Pot 1 favourite.
        indexed = list(enumerate(pts_only))
        worst = sorted(indexed, key=lambda t: (t[1], t[0]), reverse=True)[:n_drop]
        drop_idx = {i for i, _ in worst}
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
