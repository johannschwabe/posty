# posty

Syncs letters from the Swiss [ePost](https://www.epost.ch) digital letterbox to a [paperless-ngx](https://github.com/paperless-ngx/paperless-ngx) instance.

## How it works

ePost is a JavaScript-heavy web app that requires 2FA login, so direct HTTP scraping isn't viable. Posty uses a real Chromium browser (via Playwright) to handle authentication, then reuses the saved session for headless syncs.

**Two-phase design:**

1. **Login (interactive, once)** — opens a visible browser, you complete the SwissID + 2FA flow, the session is saved to `session.json`.
2. **Sync (headless, repeatable)** — loads the saved session, scrapes the letterbox for new letters, downloads each as PDF via ePost's REST API, and either uploads to paperless-ngx or saves locally.

Letter IDs that have already been synced are tracked in `synced_letters.json` so re-running is safe and idempotent.

## Requirements

- Python 3.10+
- A running paperless-ngx instance (optional — without it, PDFs are saved to `downloads/`)

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env with your paperless-ngx URL and API token
```

**Browser:** Playwright's bundled Chromium may not support your OS version. The script auto-detects system Chromium if available. On Ubuntu/Debian, install it with:

```bash
sudo apt install chromium-browser
```

If your OS is supported by Playwright, you can alternatively run `playwright install chromium` to use the bundled version instead.

## Usage

**Step 1 — log in once** (opens a real browser window):

```bash
python sync.py login
```

Complete the SwissID login and 2FA. The browser closes automatically when the dashboard loads and saves `session.json`.

**Step 2 — sync** (headless, can be run on a server or via cron):

```bash
python sync.py sync
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `PAPERLESS_URL` | `http://localhost:8000` | Base URL of your paperless-ngx instance |
| `PAPERLESS_TOKEN` | — | API token from paperless-ngx Settings → API |
| `TELEGRAM_BOT_TOKEN` | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_ID` | — | Your personal chat ID (get it from [@userinfobot](https://t.me/userinfobot)) |
| `SYNC_INTERVAL_HOURS` | `6` | How often the bot auto-syncs. Set to `0` to disable auto-sync. |

If `PAPERLESS_TOKEN` is not set, PDFs are saved to `downloads/` instead of being uploaded.

## Telegram bot

The bot provides notifications and a manual trigger, running as a long-lived process (e.g. under systemd or in a screen session).

```bash
python bot.py
```

**Commands:**

- `/sync` — trigger a sync immediately and reply with results

**Automatic behaviour:**

- Syncs every `SYNC_INTERVAL_HOURS` automatically
- Sends a message when new letters are found or errors occur
- Silent when there is no new mail (so it doesn't spam you)
- Alerts you when the ePost session has expired so you know to re-login

**Session expiry:** when the session expires, the bot sends a message like:

> ⚠️ ePost session expired. Run `python sync.py login` to re-authenticate.

Re-run `python sync.py login` on a machine with a display, copy the new `session.json` to the server, and restart the bot.

## Docker / Swarm

**Build the image** on your swarm manager (or push to a registry if multi-node):

```bash
docker build -t posty:latest .
```

**Seed the volume with your session** — login must happen on a machine with a display, outside the container:

```bash
python sync.py login   # generates session.json locally
docker run --rm \
  -v posty_data:/data \
  -v "$(pwd)/session.json:/tmp/session.json:ro" \
  alpine cp /tmp/session.json /data/
```

**Deploy the stack:**

```bash
docker stack deploy -c docker-compose.yml posty
```

The bot runs as a single replica. State (`session.json`, `synced_letters.json`) persists in the `posty_data` volume across restarts and redeployments.

**When the session expires**, re-run `python sync.py login` locally, then repeat the volume seed step and restart the service:

```bash
docker service update --force posty_posty
```

> `env_file` in the stack file requires Docker 23+. On older versions, pass environment variables directly via `docker service create --env-file .env ...`.

## Running on a schedule (without Telegram)

If you don't want the bot, you can use a plain cron job instead:

```cron
0 7 * * * /path/to/.venv/bin/python /path/to/posty/sync.py sync
```
