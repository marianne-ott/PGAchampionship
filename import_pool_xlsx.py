#!/usr/bin/env python3
"""
Read the Masters 'Chosen 7' Excel export and write pool.json.

Expects row 1 headers with 'Name' in column B and Pot 1–7 *names* in
columns D,F,H,J,L,N,P (even columns after Name / Best 5).
Ignores all point columns.

Duplicate friend names get suffixes (2), (3), … in display order.
"""

from __future__ import annotations

import argparse
import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path


def _col_row(cell_ref: str) -> tuple[str, int]:
    col_s, row_s = "", ""
    for ch in cell_ref:
        if ch.isalpha():
            col_s += ch
        else:
            row_s += ch
    return col_s, int(row_s)


def _col_to_idx(col: str) -> int:
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
    return n - 1


def _load_cells(path: Path) -> dict[tuple[int, int], str]:
    z = zipfile.ZipFile(path, "r")
    ss: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        for si in root.findall(".//m:si", ns):
            ss.append("".join(t.text or "" for t in si.findall(".//m:t", ns)))

    sheet = "xl/worksheets/sheet1.xml"
    if sheet not in z.namelist():
        for n in z.namelist():
            if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"):
                sheet = n
                break

    root = ET.fromstring(z.read(sheet))
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    cells: dict[tuple[int, int], str] = {}
    for c in root.findall(".//m:sheetData//m:c", ns):
        ref = c.get("r")
        if not ref:
            continue
        col_s, row = _col_row(ref)
        t = c.get("t")
        v_el = c.find("m:v", ns)
        is_el = c.find("m:is", ns)
        val: str | None = None
        if t == "s" and v_el is not None and v_el.text is not None:
            val = ss[int(v_el.text)]
        elif is_el is not None:
            val = "".join(t.text or "" for t in is_el.findall(".//m:t", ns))
        elif v_el is not None and v_el.text is not None:
            val = v_el.text
        if val is not None:
            cells[(row, _col_to_idx(col_s))] = val
    return cells


def _to_display_name(cell: str) -> str:
    s = (cell or "").strip()
    if not s or s.lower() == "n/a":
        return s
    if "," in s:
        last, first = s.split(",", 1)
        return f"{first.strip()} {last.strip()}"
    return s


def _mask_friend_names(raw_names: list[str]) -> list[str]:
    """Privacy-safe display names: first name only, with numeric suffix on collisions.

    The repo and the deployed pool.json / docs/data.json snapshot are public,
    so we deliberately leak nothing about a person's last name. If two friends
    share the same first name they become e.g. "Mathias" and "Mathias (2)".
    The original spreadsheet (kept off-repo) is the only place full names live.
    """
    firsts: list[str] = []
    for raw in raw_names:
        parts = (raw or "").strip().split()
        firsts.append(parts[0] if parts else "")

    seen: dict[str, int] = {}
    out: list[str] = []
    for first in firsts:
        seen[first] = seen.get(first, 0) + 1
        n = seen[first]
        out.append(f"{first} ({n})" if n > 1 else first)
    return out


def import_friends(path: Path) -> list[dict[str, list[str]]]:
    cells = _load_cells(path)
    name_cols = (3, 5, 7, 9, 11, 13, 15)
    raw_rows: list[tuple[str, list[str]]] = []
    for row in range(2, 500):
        raw_name = (cells.get((row, 1)) or "").strip()
        if not raw_name:
            break
        picks_raw = [(cells.get((row, c)) or "").strip() for c in name_cols]
        picks = [_to_display_name(p) for p in picks_raw]
        raw_rows.append((raw_name, picks))

    labels = _mask_friend_names([n for n, _ in raw_rows])
    return [{"name": label, "picks": picks} for label, (_, picks) in zip(labels, raw_rows)]


def main() -> int:
    p = argparse.ArgumentParser(description="Import pool picks from Excel to pool.json")
    p.add_argument("xlsx", type=Path, help="Path to .xlsx")
    p.add_argument("-o", "--output", type=Path, default=Path("pool.json"), help="Output JSON path")
    args = p.parse_args()

    friends = import_friends(args.xlsx)
    if not friends:
        print("No friend rows found (expected data from row 2).", flush=True)
        return 1

    cfg = {
        "cut_points": 75,
        "counting_picks": 5,
        "picks_per_friend": 7,
        "friends": friends,
    }
    args.output.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(friends)} friends to {args.output.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
