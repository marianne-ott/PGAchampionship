/* ESPN public-facing leaderboard adapter.
 *
 * Endpoint: GET https://site.api.espn.com/apis/site/v2/sports/golf/leaderboard
 *
 * The endpoint is undocumented but has been used by thousands of third-party
 * golf apps for years (it's the same feed that powers ESPN's own mobile app).
 * Returns the *current* PGA Tour event automatically — during the PGA
 * Championship week that's the Major, the rest of the year it's whatever
 * is on tour. Refreshes upstream every ~30 s.
 *
 * Two outputs:
 *   - extractPlayerRows(raw, cutPoints) → rows used by scoring.js (pool match)
 *   - leaderboardSnapshot(raw)         → the leaderboard-table payload used
 *                                        by docs/index.html (columns/rows)
 *
 * Both shapes match the existing pgac_poll.py / masters_poll.py outputs
 * so the front-end consumes them without changes.
 */

export const ESPN_LEADERBOARD_URL =
  "https://site.api.espn.com/apis/site/v2/sports/golf/leaderboard";

// PGA majors are 4 rounds; ESPN's response doesn't expose a `finalRound`
// field. Hard-coded; only matters if we ever point the worker at a 3-round
// alt-format event.
const FINAL_ROUND_NUMBER = 4;

// Round-N column count in the leaderboard-table headers.
const ROUND_COLS = 4;

/* Status flags from ESPN's `status.type.name`. */
const STATUS_IN_PROGRESS = "STATUS_IN_PROGRESS";
const STATUS_PLAY_COMPLETE = "STATUS_PLAY_COMPLETE";
const STATUS_SCHEDULED = "STATUS_SCHEDULED";
const STATUS_CUT_OFF = "STATUS_CUT_OFF";
const STATUS_WITHDRAWN = "STATUS_WITHDRAWN";
const STATUS_DISQUALIFIED = "STATUS_DISQUALIFIED";

const NON_PLAYING_STATUSES = new Set([
  STATUS_CUT_OFF,
  STATUS_WITHDRAWN,
  STATUS_DISQUALIFIED,
]);

export async function fetchEspnLeaderboard({ fetchImpl = fetch, signal } = {}) {
  const res = await fetchImpl(ESPN_LEADERBOARD_URL, {
    headers: {
      // Same UA the pgac_poll.py module uses — ESPN serves a slightly
      // smaller payload to bot-y user-agents, harmless for our purposes
      // but worth keeping consistent.
      "User-Agent":
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " +
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
      Accept: "application/json",
    },
    signal,
  });
  if (!res.ok) {
    throw new Error(`ESPN leaderboard HTTP ${res.status}`);
  }
  return res.json();
}

function getEvent(raw) {
  const ev = (raw && raw.events && raw.events[0]) || null;
  if (!ev || !ev.competitions || !ev.competitions[0]) {
    throw new Error("ESPN response missing events[0].competitions[0]");
  }
  return ev;
}

function getCompetition(raw) {
  return getEvent(raw).competitions[0];
}

function countryCodeFromFlag(flagHref) {
  if (!flagHref) return "";
  // e.g. https://a.espncdn.com/i/teamlogos/countries/500/usa.png
  const m = /\/countries\/\d+\/([a-z]{2,4})\.[a-z]+$/i.exec(flagHref);
  return m ? m[1].toUpperCase() : "";
}

function displayPositionFor(competitor) {
  /* Use the position string ESPN renders when it's set; otherwise fall back
   * to the status type so pool scoring works for CUT/WD/DQ players. We map
   * WD/DQ to "CUT" so they consistently count as `cut_points` (and missed_cut)
   * in the tier pool — the alternative is to throw on the unrecognized label,
   * which would freeze the entire pool view if any single player WDs. */
  const status = competitor.status || {};
  const pos = status.position;
  if (pos && typeof pos === "object" && pos.displayName) {
    return String(pos.displayName);
  }
  const t = (status.type && status.type.name) || "";
  if (NON_PLAYING_STATUSES.has(t)) return "CUT";
  return "";
}

function linescoresByPeriod(competitor) {
  const out = new Map();
  for (const ls of competitor.linescores || []) {
    if (ls && typeof ls.period === "number") out.set(ls.period, ls);
  }
  return out;
}

/* True if the player has actually played their R(FINAL_ROUND_NUMBER) — i.e.
 * the linescore for that period has a numeric `value`. Missed-cut / WD / DQ
 * players never reach this state, which is exactly what the pool's "all picks
 * finished R4" rule wants (`doneFinalRound` stays false). */
function isDoneFinalRound(competitor) {
  const ls = linescoresByPeriod(competitor).get(FINAL_ROUND_NUMBER);
  return !!(ls && typeof ls.value === "number");
}

/* Row shape consumed by computePoolStandings (and matches pgac_poll's
 * `pga_player_rows` output 1:1). */
export function extractPlayerRows(raw, cutPoints, positionPoints) {
  const competitors = getCompetition(raw).competitors || [];
  const rows = [];
  for (const c of competitors) {
    const athlete = c.athlete || {};
    const displayName = (athlete.displayName || "").trim();
    if (!displayName) continue;
    const country = countryCodeFromFlag((athlete.flag || {}).href);
    const position = displayPositionFor(c);
    const { points, missedCut } = positionPoints(position, cutPoints);
    rows.push({
      displayName,
      country,
      position,
      points,
      missedCut,
      doneFinalRound: isDoneFinalRound(c),
    });
  }
  return rows;
}

/* Pick the (today-score, thru) cells for the display table.
 *
 *   - Round in progress     → today's to-par, thru-hole number
 *   - Round complete (signed)→ today's to-par, "F"
 *   - Scheduled (between rounds or pre-tournament) → "-" plus the tee time,
 *                              emitted as "@<ISO>" so the client formats it
 *                              in the visitor's local timezone (matches
 *                              ESPN's UI which shows e.g. "7:38 PM*").
 *   - Cut/WD/DQ             → both blank
 */
const TEE_TIME_PREFIX = "@";

function todayCells(competitor) {
  const lsByPeriod = linescoresByPeriod(competitor);
  const status = competitor.status || {};
  const statusType = (status.type && status.type.name) || "";
  const period = typeof status.period === "number" ? status.period : 0;
  const thru = typeof status.thru === "number" ? status.thru : null;

  const currentLs = period ? lsByPeriod.get(period) : null;
  const currentHasValue = currentLs && typeof currentLs.value === "number";

  if (statusType === STATUS_IN_PROGRESS && currentHasValue) {
    return [String(currentLs.displayValue || ""), thru != null ? String(thru) : ""];
  }
  if (statusType === STATUS_PLAY_COMPLETE && currentHasValue) {
    return [String(currentLs.displayValue || ""), "F"];
  }
  if (statusType === STATUS_SCHEDULED) {
    return ["-", status.teeTime ? TEE_TIME_PREFIX + status.teeTime : ""];
  }
  if (NON_PLAYING_STATUSES.has(statusType)) {
    return ["", ""];
  }
  if (currentHasValue) {
    return [String(currentLs.displayValue || ""), thru != null ? String(thru) : ""];
  }
  return ["", ""];
}

/* Strokes for a completed round, or "--" if not yet finished.
 *
 * Two ESPN quirks to defend against:
 *
 *   1. Future / not-started rounds are pre-populated with `value: 0`, which
 *      would (incorrectly) display as "0" in the R{N} column.
 *   2. The CURRENT in-progress round's linescore carries a *partial cumulative*
 *      stroke total (e.g. 7 thru 2 holes). That's a running score, not a
 *      round total — running scores already live in the TODAY column.
 *
 * Any other case where `value > 0` we treat as a completed round, which
 * also correctly surfaces R1/R2 for cut/WD/DQ players (their card was
 * signed before stoppage).
 */
/* Parse an ESPN to-par display value into a signed integer.
 * Returns null for blanks or placeholders ("", "-") so missing/scheduled
 * rounds don't contribute to the cumulative total. */
function parseToParDisplay(displayValue) {
  if (displayValue === undefined || displayValue === null) return null;
  const s = String(displayValue).trim();
  if (s === "" || s === "-") return null;
  if (/^E$/i.test(s)) return 0;
  const n = Number.parseInt(s, 10);
  return Number.isFinite(n) ? n : null;
}

/* Running tournament-total to par.
 *
 * ESPN's `competitor.score.displayValue` only updates when a player signs
 * their card for the round, so it lags behind whenever someone is mid-round.
 * (Sungjae Im: c.score="+3" from R1, but ESPN's UI shows "+2" because R2
 * is currently -1 thru 2.) We compute the running total by summing each
 * linescore's `displayValue` — which includes the in-progress round's
 * partial to-par — so the TOT column matches what ESPN displays. */
function runningTotalToPar(competitor) {
  let sum = 0;
  let anyScored = false;
  for (const ls of competitor.linescores || []) {
    const n = parseToParDisplay(ls?.displayValue);
    if (n === null) continue;
    sum += n;
    anyScored = true;
  }
  if (!anyScored) return "";
  if (sum === 0) return "E";
  return sum > 0 ? `+${sum}` : String(sum);
}

function roundTotalCell(competitor, period) {
  const ls = linescoresByPeriod(competitor).get(period);
  if (!ls || typeof ls.value !== "number" || ls.value <= 0) return "--";
  const status = competitor.status || {};
  const statusType = (status.type && status.type.name) || "";
  const currentPeriod = typeof status.period === "number" ? status.period : 0;
  if (period === currentPeriod && statusType === STATUS_IN_PROGRESS) return "--";
  return String(Math.round(ls.value));
}

function playerCell(athlete, country) {
  const name = (athlete.displayName || "").trim();
  return country ? `${name} (${country})` : name;
}

/* Display-table snapshot — same shape pgac_poll.leaderboard_snapshot returns. */
export function leaderboardSnapshot(raw, { sourceUrl, fetchedAt } = {}) {
  const ev = getEvent(raw);
  const comp = getCompetition(raw);
  const competitors = (comp.competitors || []).slice();
  competitors.sort((a, b) => {
    const sa = typeof a.sortOrder === "number" ? a.sortOrder : 9999;
    const sb = typeof b.sortOrder === "number" ? b.sortOrder : 9999;
    return sa - sb;
  });

  // STROKES (cumulative total strokes) is intentionally omitted: until all
  // four rounds are complete it's just R1 (or R1+R2, etc.) and reads as
  // confusing "0-padded" partial sums to most users. The per-round R1..R4
  // columns convey the same information without ambiguity.
  const headers = ["POS", "PLAYER", "TOT", "TODAY", "THRU"];
  for (let r = 1; r <= ROUND_COLS; r++) headers.push(`R${r}`);

  const rows = [];
  const rowDoneFinalRound = [];
  for (const c of competitors) {
    const athlete = c.athlete || {};
    const country = countryCodeFromFlag((athlete.flag || {}).href);
    const position = displayPositionFor(c);
    const tot = runningTotalToPar(c);
    const [today, thru] = todayCells(c);
    const rndCells = [];
    for (let r = 1; r <= ROUND_COLS; r++) rndCells.push(roundTotalCell(c, r));
    rows.push([
      position,
      playerCell(athlete, country),
      tot,
      today,
      thru,
      ...rndCells,
    ]);
    rowDoneFinalRound.push(isDoneFinalRound(c));
  }

  return {
    source: sourceUrl || ev.links?.[0]?.href || ESPN_LEADERBOARD_URL,
    fetchedAt: fetchedAt || new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    // Tournament-level clock — what the upstream feed thinks the latest
    // scoring moment is. ESPN doesn't have a single `lastUpdated` field
    // for the whole event so we report the most recent linescore timestamp
    // when available. Falls through to fetchedAt otherwise.
    scoringUpdatedAt: latestScoringTimestamp(raw),
    eventName: ev.name || null,
    eventStatus: ev.status?.type?.description || null,
    roundDetail: comp.status?.type?.detail || null,
    columns: headers,
    rows,
    rowDoneFinalRound,
  };
}

function latestScoringTimestamp(raw) {
  // Heuristic: the most recent `teeTime` across all completed linescores
  // gives a rough "latest scoring activity" marker. Good enough for the
  // UI to render a "scoring last seen at …" tag distinct from "fetched at".
  let bestMs = 0;
  const comp = getCompetition(raw);
  for (const c of comp.competitors || []) {
    for (const ls of c.linescores || []) {
      if (ls && typeof ls.value === "number" && ls.teeTime) {
        const ms = Date.parse(ls.teeTime);
        if (Number.isFinite(ms) && ms > bestMs) bestMs = ms;
      }
    }
  }
  return bestMs ? new Date(bestMs).toISOString().replace(/\.\d+Z$/, "Z") : null;
}
