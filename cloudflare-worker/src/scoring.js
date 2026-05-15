/* Pool scoring — direct JS port of pool_scoring.py.
 *
 * Mirrors the Python implementation 1:1 so the Cloudflare-served and the
 * GitHub-Pages-served `data.json` produce identical standings. Any change
 * to the scoring rules needs to land here AND in pool_scoring.py.
 */

// Fold common Scandinavian / Latin letters so Excel-imported (ASCII) picks
// match the official feed's display names (e.g. Højgaard ⇄ Hojgaard).
const ASCII_FOLD = {
  "\u00f8": "o", "\u00d8": "o", // ø Ø
  "\u00e5": "a", "\u00c5": "a", // å Å
  "\u00e6": "ae", "\u00c6": "ae", // æ Æ
  "\u00f6": "o", "\u00d6": "o", // ö Ö
  "\u00e4": "a", "\u00c4": "a", // ä Ä
  "\u00fc": "u", "\u00dc": "u", // ü Ü
  "\u00ff": "y", "\u0178": "y", // ÿ Ÿ
};

function normalizePlayerName(name) {
  let s = (name == null ? "" : String(name)).trim();
  let out = "";
  for (const ch of s) out += ASCII_FOLD[ch] != null ? ASCII_FOLD[ch] : ch;
  // NFKD then strip combining marks — same as unicodedata.normalize+filter.
  out = out.normalize("NFKD").replace(/[\u0300-\u036f]/g, "");
  return out.toLowerCase().split(/\s+/).filter(Boolean).join(" ");
}

/* Parse a position display string into (points, missedCut).
 *
 *   "T49"   → 49
 *   "117"   → min(117, cutPoints)   ← anyone outside the cut-tier still
 *                                     scores no worse than a missed-cut
 *                                     pick (Hovland T117 would otherwise
 *                                     dwarf the legitimate 75-point CUT).
 *   "CUT"   → cutPoints (missedCut=true)
 *   blank   → cutPoints              ← so unstarted picks don't trivially
 *                                     "win" the pool.
 *
 * Mirrors pga_position_points in pgac_poll.py / masters_poll.py — keep
 * them in lock-step.
 */
export function positionPoints(positionDisplay, cutPoints) {
  const raw = (positionDisplay == null ? "" : String(positionDisplay)).trim().toUpperCase();
  const BLANK = new Set(["", "-", "\u2010", "\u2013", "\u2014", "--"]);
  if (BLANK.has(raw)) return { points: cutPoints, missedCut: false };
  if (raw === "CUT") return { points: cutPoints, missedCut: true };
  const numStr = raw.startsWith("T") ? raw.slice(1) : raw;
  const n = Number.parseInt(numStr, 10);
  if (!Number.isFinite(n)) {
    throw new Error(`Unrecognized position ${JSON.stringify(positionDisplay)}`);
  }
  return { points: Math.min(n, cutPoints), missedCut: false };
}

function buildPlayerIndex(rows) {
  const idx = new Map();
  for (const r of rows) {
    const key = normalizePlayerName(r.displayName);
    if (key) idx.set(key, r);
  }
  return idx;
}

/* Score the pool against a pre-extracted list of player rows.
 * `playerRows[i]` shape: { displayName, country, position, points, missedCut, doneFinalRound }.
 * Returns the same `standings` payload `pool_scoring.compute_pool_standings`
 * does so the existing front-end keeps working without changes. */
export function computePoolStandings(playerRows, config) {
  const cutPoints = Number.parseInt(config.cut_points ?? 75, 10);
  const countingPicks = Number.parseInt(config.counting_picks ?? 5, 10);
  const picksPer = Number.parseInt(config.picks_per_friend ?? 7, 10);

  const index = buildPlayerIndex(playerRows);
  const friendsOut = [];

  for (const fr of config.friends || []) {
    const name = (fr.name || "").toString().trim() || "?";
    const rawPicks = fr.picks || [];
    if (rawPicks.length !== picksPer) {
      friendsOut.push({
        name,
        ok: false,
        error: `Expected ${picksPer} picks, got ${rawPicks.length}`,
        total: null,
        picks: [],
      });
      continue;
    }

    const pickRows = [];
    const ptsList = [];
    for (const p of rawPicks) {
      const label = String(p ?? "").trim();
      const key = normalizePlayerName(label);
      const row = key ? index.get(key) : null;
      if (!row) {
        pickRows.push({
          pick: label,
          matched: null,
          position: null,
          points: null,
          missedCut: null,
          counts: false,
          missing: true,
          doneFinalRound: false,
        });
        ptsList.push(null);
      } else {
        pickRows.push({
          pick: label,
          matched: row.displayName,
          position: row.position,
          points: row.points,
          missedCut: row.missedCut,
          counts: false,
          missing: false,
          doneFinalRound: !!row.doneFinalRound,
        });
        ptsList.push(Number.parseInt(row.points, 10));
      }
    }

    if (ptsList.some((x) => x == null)) {
      friendsOut.push({
        name,
        ok: false,
        error:
          "One or more picks did not match the leaderboard (check spelling vs PGA display names).",
        total: null,
        picks: pickRows,
      });
      continue;
    }

    const ptsOnly = ptsList.map((x) => Number.parseInt(x, 10));
    const nDrop = Math.max(0, ptsOnly.length - countingPicks);
    // Drop the picks with the *highest* point totals (worst results). Ties
    // broken by pot index — when several picks share a value (e.g. several
    // unstarted/missed-cut picks all at cutPoints), drop the rightmost slots
    // first so the marquee early-tier picks (Pot 1 favourite, etc.) survive.
    const indexed = ptsOnly.map((v, i) => [i, v]);
    indexed.sort((a, b) => b[1] - a[1] || b[0] - a[0]); // descending by (pts, idx)
    const dropIdx = new Set(indexed.slice(0, nDrop).map((t) => t[0]));
    let total = 0;
    for (let i = 0; i < ptsOnly.length; i++) {
      if (!dropIdx.has(i)) total += ptsOnly[i];
      pickRows[i].counts = !dropIdx.has(i);
    }

    friendsOut.push({
      name,
      ok: true,
      error: null,
      total,
      picks: pickRows,
    });
  }

  const okRows = friendsOut.filter((f) => f.ok);
  const badRows = friendsOut.filter((f) => !f.ok);
  okRows.sort((a, b) => (a.total ?? 0) - (b.total ?? 0));

  let rankNext = 1;
  let i = 0;
  while (i < okRows.length) {
    const tval = okRows[i].total;
    let j = i;
    while (j < okRows.length && okRows[j].total === tval) j++;
    for (let k = i; k < j; k++) okRows[k].rank = rankNext;
    rankNext += j - i;
    i = j;
  }
  for (const f of badRows) f.rank = null;

  return {
    ok: true,
    cut_points: cutPoints,
    counting_picks: countingPicks,
    picks_per_friend: picksPer,
    friends: [...okRows, ...badRows],
  };
}
