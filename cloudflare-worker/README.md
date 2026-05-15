# pga-refresh-worker

Cloudflare Worker that **serves the live PGA Championship dashboard JSON**.

```
ESPN public golf API  →  this worker  →  docs/index.html in the browser
                        (edge cache, 60s)
```

It owns the entire data path: every minute the worker fetches ESPN's
leaderboard, fetches `pool.json` from this repo, scores the pool, and
caches the combined JSON at the Cloudflare edge for 60 s. Browsers
poll `/data.json` and always get something at most ~90 s stale.

Why a worker instead of just GitHub Pages: Pages publishes are soft-
capped at ~10/hour. Anything more aggressive than every-6-minute Pages
deploys started silently failing — the deployments API kept reporting
`success` while the live CDN froze on a stale artifact for hours.
Pulling data into the worker decouples refresh-rate from publish-rate
entirely; Pages only has to re-publish when the **HTML** changes
(rare).

The GitHub Actions cron at `*/5` still runs as a static fallback: if
the worker is unreachable, the front-end falls back to fetching
`./data.json` from Pages, refreshed every 5 minutes by the workflow.

---

## Endpoints

- `GET /data.json`        Cached for 60 s, the hot path for visitors.
- `GET /data.json?force=1` Bypass the cache, refetch ESPN now. Wired
                          to the **Update now** button in the UI.
- `GET /health`           Returns `ok\n`; cheap liveness probe.

Every endpoint emits CORS `Access-Control-Allow-Origin: *` so it can
be called from `marianne-ott.github.io`.

---

## One-time setup (~5 minutes)

You need:
- A free Cloudflare account.
- `node` + `npm` installed locally.

No GitHub PAT is required — the worker no longer dispatches GH Actions.

### 1. Install wrangler and log in to Cloudflare

```bash
cd cloudflare-worker
npm install
npx wrangler login   # opens a browser to authorize
```

### 2. Deploy the worker

```bash
npx wrangler deploy
```

This uploads `src/index.js` + its imports, registers the `* * * * *`
cron trigger, and gives you a public URL like

```
https://pga-refresh-worker.<your-subdomain>.workers.dev
```

Note that URL down — you'll paste it into the front-end in step 4.

### 3. (Optional) Override the pool config URL

The worker defaults to fetching `pool.json` from the public
`raw.githubusercontent.com` URL of this repo's `main` branch. If you
move the pool config to a private gist later, set:

```bash
npx wrangler secret put POOL_CONFIG_URL
# Paste the URL when prompted (must return JSON in the same shape as pool.json).
```

### 4. Tell the front-end about the worker

Open `docs/index.html` and find:

```html
<meta name="pga-worker-url" content="" />
```

Paste your worker URL into the `content` attribute, e.g.

```html
<meta name="pga-worker-url" content="https://pga-refresh-worker.your-subdomain.workers.dev" />
```

Commit and push — GitHub Pages redeploys, and the page starts serving
fresh data every minute via your worker.

If you leave `content` empty, the page falls back to the static
`./data.json` built by GitHub Actions every 5 minutes (still works,
just slower).

### 5. Verify

Health check from anywhere:

```bash
curl https://pga-refresh-worker.<your-subdomain>.workers.dev/health
# → ok
```

Fetch the live dashboard:

```bash
curl -s https://pga-refresh-worker.<your-subdomain>.workers.dev/data.json | jq '.leaderboard.fetchedAt'
```

Tail the worker logs (cron + fetch invocations) live:

```bash
npx wrangler tail
```

---

## Local validation

Before deploying changes to scoring or the ESPN adapter, you can run
the JS pipeline against live ESPN data on your laptop:

```bash
node cloudflare-worker/test-local.mjs
```

This pulls ESPN + `pool.json` and computes JS standings, then compares
them line-by-line with the Python-built `docs/data.json`. Zero
mismatches across the friend list = the worker will produce identical
standings to `build_static.py`.

You can also run `wrangler dev` for a real local edge runtime, though
the test script above is usually faster:

```bash
cd cloudflare-worker
npx wrangler dev --port 8787
curl -s http://localhost:8787/data.json | jq '.pool.standings.friends[0]'
```

---

## How the data flow works end-to-end

```
   Cloudflare cron (* * * * *)
           │
           │  buildDashboard()
           ▼
   ┌──────────────────────────────┐
   │  fetch ESPN leaderboard       │ ◀── site.api.espn.com
   │  fetch pool.json from GitHub  │ ◀── raw.githubusercontent.com
   │  score the pool (scoring.js)  │
   │  cache result for 60 s        │ ──▶ caches.default
   └──────────────────────────────┘
           │
           │  Browser polls /data.json every 30 s
           ▼
   docs/index.html renders fresh standings
```

End-to-end latency: scores in ESPN's API → visible on the page in
~30–90 s, depending on where in the polling cycle you arrive.

---

## Stopping or pausing

- **Pause cron**: edit `wrangler.toml`, comment out the `[triggers]`
  block, run `npx wrangler deploy`. The worker still serves on-demand
  fetches; visitors then pay the ESPN round-trip themselves on the
  first uncached hit of each minute.
- **Take the worker offline entirely**: empty the
  `<meta name="pga-worker-url" content="">` in `docs/index.html` and
  push. The front-end falls back to the static `./data.json` built
  every 5 min by GitHub Actions.
- **Delete the worker**: `npx wrangler delete`.

---

## Cost

$0 / month. Free tier is 100,000 requests/day; this worker uses
roughly:

- ~1,440 cron invocations/day (every minute).
- ~1 ESPN fetch per cache miss + ~1 pool fetch per 5 min.
- Visitor fetches scale linearly but each hits the edge cache, so a
  reasonable pool-watching audience adds at most a few thousand
  requests/day.

Total well under 5% of the daily quota.
