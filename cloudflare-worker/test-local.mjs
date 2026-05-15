/* Quick local sanity test for the worker's data path.
 *
 * Run with: node cloudflare-worker/test-local.mjs
 *
 * Confirms the JS pipeline (ESPN fetch → row extraction → pool scoring)
 * produces the same friend totals + ranks the Python build does. Compares
 * against ./docs/data.json (which build_static.py writes locally).
 *
 * Not shipped to the worker bundle — `test-local.mjs` is not imported by
 * src/index.js. Safe to delete or rename if you want it out of the way.
 */

import { readFile } from "node:fs/promises";

import { extractPlayerRows, fetchEspnLeaderboard, leaderboardSnapshot } from "./src/espn.js";
import { computePoolStandings, positionPoints } from "./src/scoring.js";

const POOL_PATH = new URL("../pool.json", import.meta.url);
const PY_DATA_PATH = new URL("../docs/data.json", import.meta.url);

async function main() {
  const poolCfg = JSON.parse(await readFile(POOL_PATH, "utf8"));
  const raw = await fetchEspnLeaderboard();

  const lb = leaderboardSnapshot(raw);
  const cut = Number.parseInt(poolCfg.cut_points ?? 75, 10);
  const rows = extractPlayerRows(raw, cut, positionPoints);
  const standings = computePoolStandings(rows, poolCfg);

  console.log("ESPN event:", lb.eventName, "—", lb.roundDetail);
  console.log("rows in leaderboard table:", lb.rows.length);
  console.log("scoringUpdatedAt:", lb.scoringUpdatedAt);
  console.log("fetchedAt:", lb.fetchedAt);
  console.log("\n=== JS-computed standings (top 8) ===");
  for (const f of standings.friends.slice(0, 8)) {
    console.log(`  ${String(f.rank ?? "?").padStart(2)}  ${f.name.padEnd(12)}  total=${f.total}`);
  }

  // Cross-check vs the Python build that built_static.py just wrote.
  try {
    const py = JSON.parse(await readFile(PY_DATA_PATH, "utf8"));
    const pyStandings = py?.pool?.standings;
    if (pyStandings?.ok) {
      console.log("\n=== Python-computed standings (top 8) ===");
      for (const f of pyStandings.friends.slice(0, 8)) {
        console.log(`  ${String(f.rank ?? "?").padStart(2)}  ${f.name.padEnd(12)}  total=${f.total}`);
      }
      const byName = new Map(pyStandings.friends.map((f) => [f.name, f]));
      let mismatches = 0;
      for (const f of standings.friends) {
        const py = byName.get(f.name);
        if (!py) { mismatches++; console.log("  ! missing in Python:", f.name); continue; }
        if (py.total !== f.total) {
          mismatches++;
          console.log(`  ! ${f.name}: JS total=${f.total}, Python total=${py.total}`);
        }
      }
      console.log(`\nTotal mismatches: ${mismatches} of ${standings.friends.length} friends`);
    } else {
      console.log("\n(no Python standings in docs/data.json to compare against)");
    }
  } catch (e) {
    console.log("\n(could not load docs/data.json for comparison:", e.message + ")");
  }
}

main().catch((e) => {
  console.error("FATAL:", e);
  process.exit(1);
});
