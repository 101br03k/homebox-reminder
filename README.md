# Homebox Maintenance Reminder

Small Python service that periodically queries a Homebox instance for scheduled maintenance entries and notifies when maintenance is due.

Environment variables (see `config.example.env`):

- `HOMEBOX_API_BASE` (default: `https://demo.homebox.software/api`)
- `HOMEBOX_API_TOKEN` (optional Bearer token for API access)
- `CHECK_INTERVAL_SECONDS` (default: `3600`)
- `NOTIFIER_WEBHOOK` (optional webhook URL receiving `{"text": "..."}`)
- `NOTIFIER_URLS` (optional comma-separated Apprise URLs; preferred)
- `NOTIFIER_WEBHOOK` (optional webhook URL receiving `{"text": "..."}`)
- `RUN_ONCE` (set `true` to run a single check and exit)

Build and run with Docker:

```bash
# from this folder
docker build -t homebox-reminder:latest .

# run (example)
docker run --rm \
  -e HOMEBOX_API_BASE=https://demo.homebox.software/api \
  -e CHECK_INTERVAL_SECONDS=3600 \
  -e HOMEBOX_API_TOKEN="yourtoken" \
  -e NOTIFIER_URLS="discord://id/token,mailto://user:pass@smtp.example.com" \
  # or use the backwards-compatible single webhook option:
  -e NOTIFIER_WEBHOOK="https://maker.ifttt.com/trigger/xyz/with/key/abc" \
  homebox-reminder:latest
```

Or test locally in one-shot mode:

```bash
export RUN_ONCE=true
pip install -r requirements.txt
python app.py
```

Docker Compose example:

```bash
docker-compose up -d --build
```

`docker-compose.yml` will use `config.example.env` by default; customize the env file or set additional variables:

- `REMIND_START_DAYS_BEFORE`: days before scheduled date to start reminders (default 0)
- `REMIND_END_DAYS_AFTER`: days after scheduled date to continue reminders (default 0)
- `REMIND_REPEAT_DAYS`: repeat interval in days (0 = only once on start date)

Persistence
- The container stores sent reminders in a SQLite DB at `/data/reminders.db` by default.
- Mount a host folder to `/data` to persist state across restarts (the included `docker-compose.yml` mounts `./data`).

CLI commands
 - List persisted reminders:

```bash
docker compose run --rm homebox-reminder --list
```

 - Reset a specific reminder or all reminders:

```bash
# reset one
docker compose run --rm homebox-reminder --reset "<id>"
# reset all
docker compose run --rm homebox-reminder --reset all
```

 - Prune entries older than N days:

```bash
docker compose run --rm homebox-reminder --prune 90
```

Automatic retention
 - Set `REMIND_RETENTION_DAYS` in your `.env` to enable automatic pruning on startup. Set to `0` to disable.


OIDC notes
 - If your Homebox instance uses OIDC and the container cannot perform the browser-based
   flow, obtain a session/token from the Homebox web UI (open DevTools → Application → Cookies
   and copy the `hb.auth.session` value, or perform a login in the frontend and copy the
   `token` field from the API response). Then provide that token to the container:

```bash
# set directly
export HOMEBOX_API_TOKEN="<token>"

# or set HOMEBOX_OIDC_TOKEN
export HOMEBOX_OIDC_TOKEN="<token>"

# or save token to a file and mount it
echo "<token>" > /tmp/homebox_token.txt
docker run --rm -v /tmp/homebox_token.txt:/data/homebox_token.txt -e HOMEBOX_TOKEN_FILE=/data/homebox_token.txt \
  homebox-reminder:latest
```


