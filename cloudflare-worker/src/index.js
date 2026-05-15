/*
 * pga-refresh-worker
 *
 * Cloudflare Worker that owns the live data path for the Chosen 7 pool:
 *
 *   ESPN public golf API → this worker → docs/index.html in the browser
 *
 * Responsibilities:
 *   - GET /data.json
 *       Returns the same JSON shape build_static.py produces, but built
 *       on demand from ESPN's leaderboard + pool.json fetched live from
 *       this repo's main branch. Cached at the edge for 60 s so concurrent
 *       viewers don't each hit ESPN.
 *   - GET /data.json?force=1
 *       Bypass the cache and rebuild. Wire for the front-end's
 *       "Update now" button.
 *   - GET /health
 *       Cheap liveness probe — returns "ok\n".
 *   - scheduled() every minute
 *       Pre-warms the edge cache by hitting the same buildDashboard path
 *       used by /data.json. The very-first viewer of each minute then sees
 *       sub-100 ms latency instead of paying the ESPN round-trip themselves.
 *
 * Why we left the GitHub-workflow-dispatch path behind:
 *   Pages publishes were rate-limited at ~10/hour, so even with a perfect
 *   trigger we could only refresh every 6 min before content stopped
 *   reaching the CDN. Pulling data into the worker takes that cap off
 *   the hot path entirely — Pages only has to redeploy when the HTML
 *   itself changes (rare). The Python build_static.py + GH Actions cron
 *   still runs every 5 min and produces docs/data.json as a *fallback*
 *   that the front-end falls back to if the worker is unreachable.
 *
 * Secrets (set with `wrangler secret put …`):
 *   POOL_CONFIG_URL  Optional override. Default points to the public raw
 *                    pool.json on this repo's main branch — change it to a
 *                    private gist URL if you ever move the pool config off
 *                    GitHub.
 */

import { extractPlayerRows, fetchEspnLeaderboard, leaderboardSnapshot } from "./espn.js";
import { computePoolStandings, positionPoints } from "./scoring.js";

const DEFAULT_POOL_CONFIG_URL =
  "https://raw.githubusercontent.com/marianne-ott/PGAchampionship/main/pool.json";

const DASHBOARD_CACHE_TTL_SECONDS = 60;
const POOL_CONFIG_CACHE_TTL_SECONDS = 300; // pool.json rarely changes mid-event

// Stable cache keys for Cache API. Must be absolute URLs even though the
// cache is local to the worker — caches.default keys on Request URL.
const DASHBOARD_CACHE_KEY = "https://pga-refresh-worker.internal/cache/dashboard";
const POOL_CONFIG_CACHE_KEY = "https://pga-refresh-worker.internal/cache/pool-config";

const CORS_HEADERS = {
  // The front-end is hosted on a different origin (github.io) than the
  // worker (workers.dev), so we need to opt the worker's responses into
  // cross-origin reads.
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "content-type",
};

function jsonResponse(obj, { status = 200, maxAge = 0 } = {}) {
  return new Response(JSON.stringify(obj) + "\n", {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": maxAge > 0 ? `public, max-age=${maxAge}` : "no-store",
      ...CORS_HEADERS,
    },
  });
}

function corsPreflightResponse() {
  return new Response(null, { status: 204, headers: CORS_HEADERS });
}

async function fetchPoolConfig(env) {
  const url = (env && env.POOL_CONFIG_URL) || DEFAULT_POOL_CONFIG_URL;
  const cache = caches.default;
  const cacheReq = new Request(POOL_CONFIG_CACHE_KEY, { method: "GET" });
  const cached = await cache.match(cacheReq);
  if (cached) return cached.json();

  const res = await fetch(url, {
    headers: { Accept: "application/json", "User-Agent": "pga-refresh-worker" },
  });
  if (!res.ok) {
    throw new Error(`pool config fetch failed: HTTP ${res.status} from ${url}`);
  }
  const cfg = await res.json();
  // Write a cacheable copy so concurrent dashboard builds reuse the same
  // pool config without re-hitting raw.githubusercontent.com every time.
  const cacheable = new Response(JSON.stringify(cfg), {
    headers: {
      "content-type": "application/json",
      "cache-control": `public, max-age=${POOL_CONFIG_CACHE_TTL_SECONDS}`,
    },
  });
  await cache.put(cacheReq, cacheable);
  return cfg;
}

async function buildDashboard(env) {
  const fetchedAt = new Date().toISOString().replace(/\.\d+Z$/, "Z");

  // Run the two upstream fetches concurrently; ESPN is the slow one.
  const [raw, poolCfg] = await Promise.all([
    fetchEspnLeaderboard(),
    fetchPoolConfig(env).catch((e) => ({ __error: String(e) })),
  ]);

  const lb = leaderboardSnapshot(raw, { fetchedAt });

  // Pool payload mirrors `leaderboard_server.build_dashboard` exactly so the
  // front-end can't tell the worker-served data apart from the Pages one.
  const pool = {
    configPath: DEFAULT_POOL_CONFIG_URL,
    fileExists: !poolCfg.__error,
    parseError: poolCfg.__error || null,
    standings: null,
  };
  if (!poolCfg.__error) {
    try {
      const cutPoints = Number.parseInt(poolCfg.cut_points ?? 75, 10);
      const playerRows = extractPlayerRows(raw, cutPoints, positionPoints);
      pool.standings = computePoolStandings(playerRows, poolCfg);
    } catch (e) {
      pool.parseError = String(e && e.message ? e.message : e);
    }
  }

  return { leaderboard: lb, pool };
}

/* Dashboard with edge caching.
 *   - force=true  →  always rebuild and overwrite the cache
 *   - force=false →  return cached body if present, else rebuild
 *
 * Cache is per-edge (Cache API limitation). The scheduled() handler keeps
 * one specific edge warm; other edges populate lazily on first visitor. */
async function getDashboardJson(env, { force = false } = {}) {
  const cache = caches.default;
  const cacheReq = new Request(DASHBOARD_CACHE_KEY, { method: "GET" });
  if (!force) {
    const cached = await cache.match(cacheReq);
    if (cached) return cached.text();
  }
  const dashboard = await buildDashboard(env);
  const body = JSON.stringify(dashboard) + "\n";
  const cacheable = new Response(body, {
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": `public, max-age=${DASHBOARD_CACHE_TTL_SECONDS}`,
    },
  });
  await cache.put(cacheReq, cacheable);
  return body;
}

async function handleDataJson(request, env) {
  const url = new URL(request.url);
  const force = url.searchParams.get("force") === "1" || url.searchParams.has("nocache");
  let body;
  try {
    body = await getDashboardJson(env, { force });
  } catch (e) {
    return jsonResponse({ error: String(e && e.message ? e.message : e) }, { status: 502 });
  }
  return new Response(body, {
    status: 200,
    headers: {
      "content-type": "application/json; charset=utf-8",
      // Browsers + Cloudflare's own CDN respect this; the value matches the
      // worker's internal cache TTL so a downstream fetch within the same
      // minute is served from cache, but a forced reload always re-asks the
      // worker (which then itself may serve from edge cache or refresh).
      "cache-control": force ? "no-store" : `public, max-age=${DASHBOARD_CACHE_TTL_SECONDS}`,
      ...CORS_HEADERS,
    },
  });
}

export default {
  async scheduled(event, env, ctx) {
    // Cron tick — prewarm the edge cache. We just rebuild and overwrite;
    // the next /data.json hit at this edge then serves from cache. Other
    // edges that didn't run this tick still serve fine, just lazily.
    ctx.waitUntil(
      getDashboardJson(env, { force: true }).catch((e) => {
        console.error("scheduled refresh failed:", e && e.message ? e.message : e);
      }),
    );
  },

  async fetch(request, env, _ctx) {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return corsPreflightResponse();
    }

    if (url.pathname === "/health") {
      return new Response("ok\n", {
        status: 200,
        headers: { "content-type": "text/plain", ...CORS_HEADERS },
      });
    }

    if (url.pathname === "/data.json" || url.pathname === "/") {
      return handleDataJson(request, env);
    }

    return new Response("not found\n", {
      status: 404,
      headers: { "content-type": "text/plain", ...CORS_HEADERS },
    });
  },
};
