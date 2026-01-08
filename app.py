import os
import time
import requests
from datetime import datetime, timezone, timedelta
from dateutil import parser as dateparser
import apprise
import sqlite3
from typing import Optional
import argparse


API_BASE = os.environ.get("HOMEBOX_API_BASE", "https://demo.homebox.software/api")
API_TOKEN = os.environ.get("HOMEBOX_API_TOKEN", "")
HOMEBOX_USERNAME = os.environ.get("HOMEBOX_USERNAME", "")
HOMEBOX_PASSWORD = os.environ.get("HOMEBOX_PASSWORD", "")
HOMEBOX_OIDC_TOKEN = os.environ.get("HOMEBOX_OIDC_TOKEN", "")
HOMEBOX_TOKEN_FILE = os.environ.get("HOMEBOX_TOKEN_FILE", "")
REMIND_START_DAYS_BEFORE = int(os.environ.get("REMIND_START_DAYS_BEFORE", "0"))
REMIND_END_DAYS_AFTER = int(os.environ.get("REMIND_END_DAYS_AFTER", "0"))
REMIND_REPEAT_DAYS = int(os.environ.get("REMIND_REPEAT_DAYS", "0"))
DB_PATH = os.environ.get("REMINDERS_DB", "/data/reminders.db")
# retention: remove reminder records older than this many days (0 = disabled)
REMIND_RETENTION_DAYS = int(os.environ.get("REMIND_RETENTION_DAYS", "0"))
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "3600"))
NOTIFIER_WEBHOOK = os.environ.get("NOTIFIER_WEBHOOK", "")
NOTIFIER_URLS = os.environ.get("NOTIFIER_URLS", "")
RUN_ONCE = os.environ.get("RUN_ONCE", "false").lower() in ("1", "true", "yes")


def get_headers():
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    token = get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def login_with_credentials():
    global API_TOKEN
    if not HOMEBOX_USERNAME or not HOMEBOX_PASSWORD:
        return None

    url = f"{API_BASE}/v1/users/login"
    payload = {"username": HOMEBOX_USERNAME, "password": HOMEBOX_PASSWORD, "stayLoggedIn": False}
    try:
        r = requests.post(url, json=payload, timeout=15)
    except Exception as e:
        print(f"ERROR: login request failed: {e}")
        return None

    if r.status_code != 200:
        print(f"WARNING: login failed ({r.status_code}): {r.text}")
        return None

    try:
        data = r.json()
        token = data.get("token") or data.get("Token") or ""
        if token.startswith("Bearer "):
            token = token.split(" ", 1)[1]
        API_TOKEN = token
        return token
    except Exception as e:
        print(f"ERROR: invalid login response: {e}")
        return None


def get_token():
    # Priority: explicit API_TOKEN, OIDC token env, token file, login creds
    if API_TOKEN:
        return API_TOKEN
    if HOMEBOX_OIDC_TOKEN:
        return HOMEBOX_OIDC_TOKEN
    if HOMEBOX_TOKEN_FILE:
        try:
            with open(HOMEBOX_TOKEN_FILE, "r") as fh:
                tok = fh.read().strip()
                if tok:
                    return tok
        except Exception as e:
            print(f"Failed to read token file {HOMEBOX_TOKEN_FILE}: {e}")

    # try login with username/password if provided
    return login_with_credentials()


def fetch_scheduled_maintenance():
    url = f"{API_BASE}/v1/maintenance"
    print(f"Fetching scheduled maintenance from {url}""")
    params = {"status": "scheduled"}
    try:
        r = requests.get(url, headers=get_headers(), params=params, timeout=15)
    except Exception as e:
        print(f"ERROR: failed to reach Homebox API: {e}")
        return []

    if r.status_code == 401:
        print("WARNING: unauthorized (401). Provide HOMEBOX_API_TOKEN to authenticate.")
        return []

    if r.status_code != 200:
        print(f"WARNING: unexpected status {r.status_code} from API: {r.text}")
        return []

    try:
        data = r.json()
    except Exception as e:
        print(f"ERROR: invalid JSON response: {e}")
        return []

    return data


def init_db(path: str):
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sent_reminders (
                id TEXT PRIMARY KEY,
                last_notified DATE
            )
            """
        )
        # index on last_notified for pruning
        cur.execute("CREATE INDEX IF NOT EXISTS idx_last_notified ON sent_reminders(last_notified)")
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Failed to initialize DB {path}: {e}")


def get_last_notified(path: str, entry_id: str) -> Optional[str]:
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("SELECT last_notified FROM sent_reminders WHERE id = ?", (entry_id,))
        row = cur.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception:
        return None


def mark_notified(path: str, entry_id: str, when: str):
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sent_reminders(id, last_notified) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET last_notified=excluded.last_notified",
            (entry_id, when),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Failed to mark notified {entry_id}: {e}")


def list_reminders(path: str, limit: int = 100):
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("SELECT id, last_notified FROM sent_reminders ORDER BY last_notified DESC LIMIT ?", (limit,))
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        print(f"Failed to list reminders: {e}")
        return []


def get_maintenance_name(entry_id: str) -> str:
    # If id appears to be a name-based fallback, extract the name
    if "::" in entry_id:
        return entry_id.split("::", 1)[0]

    # Try to fetch the maintenance entry by id from the API
    try:
        url = f"{API_BASE}/v1/maintenance/{entry_id}"
        r = requests.get(url, headers=get_headers(), timeout=10)
        if r.status_code == 200:
            data = r.json()
            # data may be object with 'name' field
            name = data.get("name") or data.get("Name")
            if name:
                return name
    except Exception:
        pass

    # fallback to id
    return entry_id


def render_boxed_table(headers, rows):
    # Compute column widths
    cols = len(headers)
    widths = [len(str(h)) for h in headers]
    for r in rows:
        for i in range(cols):
            widths[i] = max(widths[i], len(str(r[i])))

    # box drawing characters
    H = '─'
    V = '│'
    TL = '┌'
    TR = '┐'
    BL = '└'
    BR = '┘'
    TJ = '┬'
    MJ = '┼'
    BJ = '┴'
    LJ = '├'
    RJ = '┤'

    def hor_line(left, mid, right):
        parts = [H * (w + 2) for w in widths]
        return left + mid.join(parts) + right

    # top border
    print(hor_line(TL, TJ, TR))

    # header
    hdr_cells = [f" {str(headers[i]).ljust(widths[i])} " for i in range(cols)]
    print(V + V.join(hdr_cells) + V)

    # header separator
    print(hor_line(LJ, MJ, RJ))

    # rows (with separators between every row to produce boxed grid)
    for idx, r in enumerate(rows):
        cells = [f" {str(r[i]).ljust(widths[i])} " for i in range(cols)]
        print(V + V.join(cells) + V)
        # row separator (use LJ...RJ for middle, or bottom border for last)
        if idx < len(rows) - 1:
            print(hor_line(LJ, MJ, RJ))
        else:
            print(hor_line(BL, BJ, BR))


def reset_reminder(path: str, entry_id: str):
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        if entry_id == "all":
            cur.execute("DELETE FROM sent_reminders")
        else:
            cur.execute("DELETE FROM sent_reminders WHERE id = ?", (entry_id,))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Failed to reset reminder {entry_id}: {e}")
        return False


def prune_old_entries(path: str, days: int):
    if days <= 0:
        return 0
    try:
        # compute cutoff date in YYYY-MM-DD
        cutoff = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("DELETE FROM sent_reminders WHERE last_notified <= ?", (cutoff,))
        deleted = cur.rowcount
        conn.commit()
        conn.close()
        return deleted
    except Exception as e:
        print(f"Failed to prune entries: {e}")
        return 0


def is_due(entry):
    # Determine if an entry should trigger a reminder today.
    # entry fields: scheduledDate, completedDate
    sched = entry.get("scheduledDate") or entry.get("scheduled_date")
    comp = entry.get("completedDate") or entry.get("date") or entry.get("completed_date")
    if not sched:
        return False
    try:
        sd = dateparser.parse(sched).date()
    except Exception:
        return False

    today = datetime.now(timezone.utc).date()

    # If already completed, don't remind
    if comp:
        try:
            _ = dateparser.parse(comp)
            return False
        except Exception:
            pass

    # Compute reminder window
    start_date = sd - timedelta(days=REMIND_START_DAYS_BEFORE)
    end_date = sd + timedelta(days=REMIND_END_DAYS_AFTER)

    if today < start_date or today > end_date:
        return False

    if REMIND_REPEAT_DAYS <= 0:
        # If repeat is 0, remind for every day inside the start..end window
        return start_date <= today <= end_date

    # Repeat every REMIND_REPEAT_DAYS days starting at start_date
    delta_days = (today - start_date).days
    return (delta_days % REMIND_REPEAT_DAYS) == 0


def notify(entries):
    if not entries:
        print("No due maintenance found.")
        return

    lines = [f"Homebox maintenance reminders ({len(entries)}):"]
    for e in entries:
        name = e.get("name") or "<unnamed>"
        item = e.get("itemName") or e.get("item_name") or ""
        sd = e.get("scheduledDate") or e.get("scheduled_date")
        lines.append(f" - {name} {f'for {item}' if item else ''} scheduled: {sd}")

    message = "\n".join(lines)
    print(message)
    # Preferred: Apprise URLs (can be comma separated). Falls back to simple webhook.
    if NOTIFIER_URLS:
        a = apprise.Apprise()
        # Accept comma or whitespace separated lists
        urls = [u.strip() for u in NOTIFIER_URLS.replace(";", ",").split(",") if u.strip()]
        for u in urls:
            try:
                a.add(u)
            except Exception as e:
                print(f"Failed to add notifier {u}: {e}")

        if a:
            try:
                a.notify(title="Homebox maintenance reminders", body=message)
            except Exception as e:
                print(f"Apprise notify failed: {e}")
            return

    # Backwards-compatible single webhook URL
    if NOTIFIER_WEBHOOK:
        try:
            requests.post(NOTIFIER_WEBHOOK, json={"text": message}, timeout=10)
        except Exception as e:
            print(f"Failed to send webhook notification: {e}")


def filter_and_notify(entries):
    # Initialize DB
    init_db(DB_PATH)
    today = datetime.now(timezone.utc).date().isoformat()
    to_notify = []
    for e in entries:
        eid = e.get("id") or e.get("ID") or e.get("Id")
        if not eid:
            # If no id, fallback to name+scheduledDate key
            eid = (e.get("name", "") + "::" + str(e.get("scheduledDate") or e.get("scheduled_date") or "")).strip()

        if not is_due(e):
            continue

        last = get_last_notified(DB_PATH, eid)
        if last == today:
            # already notified today
            continue

        to_notify.append((eid, e))

    if not to_notify:
        print("No new due maintenance to notify.")
        return

    # Build message from entries
    message_entries = [entry for (_, entry) in to_notify]
    notify(message_entries)

    # Mark as notified
    for eid, _ in to_notify:
        mark_notified(DB_PATH, eid, today)


def main_loop():
    while True:
        print(f"Checking Homebox for scheduled maintenance at {datetime.now().isoformat()}...")
        entries = fetch_scheduled_maintenance()
        filter_and_notify(entries)

        if RUN_ONCE:
            break

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Homebox maintenance reminder service")
    parser.add_argument("--list", action="store_true", help="List persisted reminders")
    parser.add_argument("--reset", type=str, help="Reset reminder by id or 'all'")
    parser.add_argument("--prune", type=int, help="Prune reminders older than N days")
    parser.add_argument("--limit", type=int, default=100, help="Limit for list output")

    args = parser.parse_args()

    # ensure DB exists for admin actions
    init_db(DB_PATH)

    if args.list:
        rows = list_reminders(DB_PATH, limit=args.limit)
        if not rows:
            print("No persisted reminders found.")
        else:
            # Build table rows: Name, ID, Last Notified
            table_rows = []
            for r in rows:
                stored_id = r[0]
                last = r[1]
                try:
                    name = get_maintenance_name(stored_id)
                except Exception:
                    name = stored_id
                table_rows.append((name, stored_id, last))

            headers = ("Name", "ID", "Last Notified")
            render_boxed_table(headers, table_rows)
        raise SystemExit(0)

    if args.reset:
        ok = reset_reminder(DB_PATH, args.reset)
        print("Reset OK" if ok else "Reset failed")
        raise SystemExit(0)

    if args.prune is not None:
        deleted = prune_old_entries(DB_PATH, args.prune)
        print(f"Pruned {deleted} entries older than {args.prune} days")
        raise SystemExit(0)

    # run service loop
    # if retention configured, prune at startup
    if REMIND_RETENTION_DAYS > 0:
        deleted = prune_old_entries(DB_PATH, REMIND_RETENTION_DAYS)
        if deleted:
            print(f"Startup pruning removed {deleted} old reminder entries")

    main_loop()
