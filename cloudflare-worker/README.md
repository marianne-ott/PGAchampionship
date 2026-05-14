# pga-refresh-worker

A tiny Cloudflare Worker that POSTs `workflow_dispatch` to this repo
**every minute**. It exists because GitHub Actions' own cron scheduler
is unreliable on the free tier (during busy hours it consolidates 2-min
schedules into bursts that fire roughly once an hour). Cloudflare's cron
is much more reliable, so we let it pull the trigger.

After end-of-day Tuesday 19 May UTC (`2026-05-20 00:00 UTC`) the worker
stops dispatching automatically — GitHub's 5-minute baseline cron is
plenty for any post-tournament wind-down.

---

## One-time setup (~10 minutes)

You need:
- A free Cloudflare account.
- A GitHub fine-grained Personal Access Token (PAT).
- `node` + `npm` installed locally.

### 1. Create the GitHub PAT

1. Open <https://github.com/settings/personal-access-tokens/new>.
2. **Token name**: `pga-refresh-worker`.
3. **Expiration**: pick something past the tournament, e.g. 30 days.
4. **Resource owner**: your personal account.
5. **Repository access** → "Only select repositories" → pick
   `marianne-ott/PGAchampionship`.
6. **Permissions** → Repository permissions:
   - **Actions**: `Read and write` (the only one you need).
   - Everything else: leave as `No access`.
7. Click "Generate token" and copy the value. It starts with
   `github_pat_…`. You'll paste it in step 4 below.

### 2. Install wrangler and log in to Cloudflare

```bash
cd cloudflare-worker
npm install
npx wrangler login   # opens a browser to authorize
```

If you don't already have a Cloudflare account, the login flow walks
you through creating one. No credit card required.

### 3. Deploy the worker

```bash
npx wrangler deploy
```

This uploads `src/index.js`, registers the `*/2 * * * *` cron trigger,
and gives you a public URL like
`https://pga-refresh-worker.<your-subdomain>.workers.dev`.

### 4. Set the GitHub PAT as a secret

```bash
npx wrangler secret put GITHUB_TOKEN
# Paste the github_pat_… value when prompted, then press Enter.
```

(Optional, only if you want to manually trigger via HTTP later — see
"Manual trigger" below.)

```bash
npx wrangler secret put TRIGGER_SECRET
# Type any random string (used as a query-string key).
```

### 5. Verify

The cron starts firing on the **next** minute boundary. Within a
minute or two you should see new `workflow_dispatch` runs:

```bash
gh run list --workflow=deploy.yml --event=workflow_dispatch --limit=10
```

You can also tail the worker logs in real time:

```bash
npx wrangler tail
```

You'll see lines like `[2026-05-14T22:14:01.123Z] dispatch ok`
every minute.

Health check from anywhere:

```bash
curl https://pga-refresh-worker.<your-subdomain>.workers.dev/health
# → ok
```

---

## How the dispatch flow works end-to-end

```
Cloudflare cron (* * * * *)
        │
        │  POST /repos/marianne-ott/PGAchampionship/
        │       actions/workflows/deploy.yml/dispatches
        ▼
GitHub Actions queues a workflow_dispatch run (~5-15 s)
        │
        ▼
deploy.yml runs build_static.py → docs/data.json (~30-60 s)
        │
        ▼
Pages deploys (~10-20 s) → CDN serves new data.json
        │
        ▼
Browser polls data.json every 30 s → renders fresh standings
```

End-to-end latency: **~60-90 seconds** from cron tick to fresh data on
the page. The page's "Last updated" timestamp is set inside
`build_static.py` at the moment the scrape happens, so it correlates
with the actual scores at that time — exactly what you want.

---

## Manual trigger (optional)

If you set `TRIGGER_SECRET` above, you can fire a one-off dispatch:

```bash
curl "https://pga-refresh-worker.<your-subdomain>.workers.dev/trigger?key=<your-secret>"
```

Response is a small JSON blob saying whether the GitHub API accepted
the dispatch.

---

## Stopping or pausing

- **Pause cron**: edit `wrangler.toml`, comment out the `[triggers]`
  block, run `npx wrangler deploy`.
- **Delete worker entirely**: `npx wrangler delete`.
- **Pause GitHub side**: rotate the PAT (delete it on GitHub) — the
  worker will keep firing but every dispatch will fail with 401 until
  you remove or update the secret.

---

## Cost

$0 / month. Cloudflare Workers free tier is **100,000 requests/day**;
this worker uses about **1,440/day** (one cron tick every minute).
