# Chosen 7 — Personal PGA Championship pool tracker

Live (well, every 5 min) leaderboard for our friends' "Chosen 7" pool, scraped
from the public PGA TOUR leaderboard page.

## Important

- This is **not** an official API. Use **only for personal, low-volume** experimentation.
- Respect site terms (e.g. [PGA TOUR Terms of Use](https://www.pgatour.com/page/terms-of-use))
  and robots guidance; do not fetch aggressively or republish content commercially.
- The HTML/JSON shape can change anytime; the script may break without notice.

## Run locally

```bash
python3 leaderboard_server.py
```

Open **http://127.0.0.1:8765/** in a browser. Same UI as on Pages, but each click
of **Update** triggers a live fetch from `pgatour.com`.

`pool.json` is read from the **same folder as `leaderboard_server.py`**, not from
your shell's cwd, so you can start the server from anywhere as long as `pool.json`
sits beside that file.

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
