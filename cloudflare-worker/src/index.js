/*
 * pga-refresh-worker
 *
 * A tiny Cloudflare Worker whose only job is to POST a workflow_dispatch
 * to GitHub every minute during the PGA Championship 2026, so the
 * leaderboard refreshes at a predictable cadence even when GitHub's own
 * cron scheduler decides to consolidate scheduled events into hour-long
 * bursts.
 *
 * Triggers:
 *   - scheduled(): fires on the cron defined in wrangler.toml.
 *   - fetch(/health): returns 'ok'. Useful as a sanity check.
 *   - fetch(/trigger?key=...): manually fires one dispatch (gated by
 *     a shared secret) so you can test without waiting for the cron.
 *
 * Secrets (set with `wrangler secret put …`):
 *   GITHUB_TOKEN      Fine-grained PAT scoped to this one repo with
 *                     "Actions: read and write" permission.
 *   TRIGGER_SECRET    Optional. Required to use /trigger over HTTP.
 */

const REPO = "marianne-ott/PGAchampionship";
const WORKFLOW_FILE = "deploy.yml";
const REF = "main";

// Stop firing dispatches after this UTC instant. Mirrors the Cron-gate
// cutoff in .github/workflows/deploy.yml so the worker also drops back
// to "let GitHub's 5-min baseline cron handle it" after the final round.
// Extended through end-of-day 19 May UTC so Monday + Tuesday remain on
// the 1-minute cadence for post-tournament discussion.
const CUTOFF_UTC = "2026-05-20T00:00:00Z";

async function dispatchWorkflow(env) {
  if (Date.now() >= Date.parse(CUTOFF_UTC)) {
    console.log(`[${new Date().toISOString()}] past cutoff ${CUTOFF_UTC} — not dispatching`);
    return { ok: true, skipped: true };
  }
  if (!env.GITHUB_TOKEN) {
    console.error("GITHUB_TOKEN secret is not configured");
    return { ok: false, error: "missing-token" };
  }
  const url = `https://api.github.com/repos/${REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;
  const res = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "pga-refresh-worker",
    },
    body: JSON.stringify({ ref: REF }),
  });
  if (!res.ok) {
    const body = await res.text();
    console.error(`[${new Date().toISOString()}] dispatch failed ${res.status}: ${body}`);
    return { ok: false, status: res.status, body };
  }
  console.log(`[${new Date().toISOString()}] dispatch ok`);
  return { ok: true };
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchWorkflow(env));
  },

  async fetch(request, env, _ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/health") {
      return new Response("ok\n", { status: 200 });
    }

    if (url.pathname === "/trigger") {
      const key = url.searchParams.get("key") || request.headers.get("x-trigger-key");
      if (!env.TRIGGER_SECRET || key !== env.TRIGGER_SECRET) {
        return new Response("forbidden\n", { status: 403 });
      }
      const result = await dispatchWorkflow(env);
      const status = result.ok ? 200 : 500;
      return new Response(JSON.stringify(result, null, 2) + "\n", {
        status,
        headers: { "content-type": "application/json" },
      });
    }

    return new Response("not found\n", { status: 404 });
  },
};
