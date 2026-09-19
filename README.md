# Pika Rank Finder

Auto-crawls the Pika Network friend graph and surfaces accounts that match a target rank, have an available Minecraft username, no custom skin, and are confirmed **Premium** (Microsoft account) on Pika.

## Features

- **BFS friend-graph crawler** — starts from any seed username and expands through friends of friends
- **Multi-rank targeting** — pick any combination of Pika ranks (Titan, Champion, Elite, etc.)
- **Name availability** — checks Mojang API for each candidate username
- **NameMC countdown** — live countdown timer showing when a name becomes claimable
- **Premium verification** — bot-connects to Pika via mineflayer to confirm Microsoft account
- **Persistent results** — found accounts survive across sessions and new crawls; never auto-cleared
- **Rank filter bar** — filter results by which rank they were found for when crawling multiple ranks
- **Watchlist** — tracks accounts with the right rank but whose name is currently taken; re-checks on demand
- **Rate limiting** — token bucket limiter on all external APIs to avoid 429s
- **Player cache** — 7-day persistent cache of profiles to skip re-fetching known accounts

## Stack

- **Backend**: Python 3.11 + Flask + Gunicorn
- **Bot check**: Node.js 20 + mineflayer
- **Frontend**: Vanilla JS + CSS (no framework)

## Local Setup

```bash
# Python deps
pip install -r requirements.txt

# Node deps
npm install

# Run
python app.py
```

Opens at `http://127.0.0.1:5000` automatically.

## Deploy on Render

1. Push this repo to GitHub
2. Go to [render.com](https://render.com) → New → Web Service → connect your repo
3. Set **Runtime** to **Docker**
4. Add a **Persistent Disk**: mount path `/data`, size 1 GB
5. Add environment variables:
   - `DATA_DIR` = `/data`
   - `RENDER` = `true`
6. Deploy

The `Dockerfile` installs both Python and Node so the bot check works.

> **Note**: The free Render tier has an ephemeral filesystem — you **must** use a paid plan ($7/mo) with a persistent disk attached, otherwise all your scan data is lost on every restart.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `5000` | Port to bind to (Render sets this automatically) |
| `DATA_DIR` | app directory | Where JSON data files are stored — set to your disk mount path |
| `RENDER` | unset | Set to `true` to disable auto browser-open on startup |

## Project Structure

```
app.py              — Flask app, crawl engine, all API routes
pika_check.js       — mineflayer bot that verifies premium/cracked status
static/
  script.js         — frontend JS (SSE, card rendering, countdowns)
  style.css         — all styles
templates/
  index.html        — single-page UI
requirements.txt    — Python dependencies
package.json        — Node dependencies
Dockerfile          — Python + Node combined image for Render
render.yaml         — Render service config reference
```
