# Chosen 7 — Personal PGA Championship pool tracker

Near-live leaderboard for our friends' "Chosen 7" pool, served from
ESPN's public golf-leaderboard feed.

The live page is a static `docs/index.html` on GitHub Pages that talks to a
Cloudflare Worker (`cloudflare-worker/`) on every-minute cadence. If the
worker is unreachable, it falls back to a `docs/data.json` snapshot that a
5-minute GitHub Actions cron keeps fresh via `build_static.py`. Both paths
hit the same ESPN endpoint so totals never drift.

## Important

- This is **not** an official API. Use **only for personal, low-volume** experimentation.
- The endpoint can change without notice; the adapters may break.

## Run locally

```bash
python3 leaderboard_server.py
```

Open **http://127.0.0.1:8765/** in a browser. Same UI as on Pages, but each
refresh triggers a live ESPN fetch.

`pool.json` is read from the **same folder as `leaderboard_server.py`**, not
from your shell's cwd, so you can start the server from anywhere as long as
`pool.json` sits beside that file.

### Re-import picks from Excel

```bash
python3 import_pool_xlsx.py "/path/to/your-sheet.xlsx"
```

Or copy `pool.example.json` to `pool.json` and edit by hand.

## Deploy to GitHub Pages (free)

The site is a single static page (`docs/index.html`) that loads `docs/data.json`.
A GitHub Actions cron job (`.github/workflows/deploy.yml`) runs `build_static.py`
every 5 minutes, regenerates `data.json`, and deploys to Pages.

One-time setup:

1. Push this repo to a personal GitHub account (private is fine).
2. In **Settings → Pages**, set "Build and deployment" → **Source: GitHub Actions**.
3. The first scheduled run (or `workflow_dispatch`) publishes the site at
   `https://<your-user>.github.io/<repo-name>/`.

The `pool.json` file is committed to the repo and is therefore visible to anyone
who can read the deployed site URL. Don't put anything in there you wouldn't
share with the friends you give the URL to.
