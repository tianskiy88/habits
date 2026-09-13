#!/usr/bin/env python3
"""Minimal long-polling bot behind the habit tracker mini app.

Answers /start (and any other message) with a button that opens the mini
app, because Telegram cannot auto-open a web app on /start by itself.

Deliberately stdlib-only: no venv, no pip, nothing to break on upgrade.
Single instance is enforced by an exclusive lock on the pid file, not by
matching process names (a pkill -f pattern once killed the agent itself).
"""

import datetime
import fcntl
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.expanduser("~/.claude-lab/shared/state/klava-coder")
OFFSET_FILE = os.path.join(STATE_DIR, "habits-bot.offset")
PID_FILE = os.path.join(STATE_DIR, "habits-bot.pid")
SECRET_FILE = os.path.expanduser("~/.claude-lab/shared/secrets/habits-bot.env")

APP_URL = "https://tianskiy88.github.io/habits/"

# Who may use the bot. Absent file or empty list = open to anyone, which is
# the state while the tracker keeps all data on the device. Fill it in before
# the bot starts holding anything server-side.
ALLOW_FILE = os.path.join(BASE, "allowed.json")

DENIED = (
    "Это личный трекер привычек. Доступ выдаёт владелец бота."
)

WELCOME = (
    "Трекер привычек.\n\n"
    "Открывай кнопкой «🔥 Привычки» внизу — тогда время напоминания "
    "ставится прямо в приложении, во вкладке «Привычки».\n\n"
    "Можно и отсюда: пришли время, например 21:00. Отключить — /off."
)

REMIND_TEXT = "Пора отметить привычки за сегодня."

# Reminder times are kept server-side; the marks themselves live in Telegram
# CloudStorage, which only the mini app can read — so the bot reminds by the
# clock and never claims to know what is already ticked.
REMIND_FILE = os.path.join(STATE_DIR, "habits-reminders.json")
MSK = datetime.timezone(datetime.timedelta(hours=3))
TIME_RE = re.compile(r"^\s*(?:/(?:время|time)\s+)?([01]?\d|2[0-3])[:.\s]([0-5]\d)\s*$")

KEYBOARD = {
    "inline_keyboard": [[
        {"text": "Открыть привычки", "web_app": {"url": APP_URL}}
    ]]
}

# A web app opened from a *keyboard* button may call sendData() and talk back
# to the bot; one opened from the menu button or an inline button may not.
# That is the only way the time picker inside the app can reach us without a
# server of our own, so the persistent keyboard is what we push people to.
REPLY_KEYBOARD = {
    "keyboard": [[
        {"text": "🔥 Привычки", "web_app": {"url": APP_URL}}
    ]],
    "resize_keyboard": True,
    "is_persistent": True,
}


def log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def read_token():
    with open(SECRET_FILE, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("HABITS_BOT_TOKEN="):
                return line.split("=", 1)[1]
    raise SystemExit("no HABITS_BOT_TOKEN in %s" % SECRET_FILE)


TOKEN = read_token()
API = "https://api.telegram.org/bot%s/" % TOKEN
CTX = ssl.create_default_context()


def call(method, payload=None, timeout=60):
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        API + method,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def load_offset():
    try:
        with open(OFFSET_FILE, "r", encoding="utf-8") as fh:
            return int(fh.read().strip() or 0)
    except (OSError, ValueError):
        return 0


def save_offset(value):
    tmp = OFFSET_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(str(value))
    os.replace(tmp, OFFSET_FILE)


def load_allowed():
    """Re-read on every message so the list can change without a restart."""
    try:
        with open(ALLOW_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return [int(x) for x in data.get("allow", [])]
    except (OSError, ValueError, TypeError):
        return []


def who(user):
    """Readable identity for the log — this is how a new person is recognised."""
    parts = [str(user.get("first_name") or ""), str(user.get("last_name") or "")]
    name = " ".join(p for p in parts if p).strip() or "без имени"
    handle = user.get("username")
    return "%s%s id=%s" % (name, (" @" + handle) if handle else "", user.get("id"))


def load_reminders():
    try:
        with open(REMIND_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_reminders(data):
    tmp = REMIND_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, REMIND_FILE)


def set_reminder(chat_id, hhmm):
    data = load_reminders()
    entry = data.get(str(chat_id), {})
    entry["at"] = hhmm
    entry.pop("sent_on", None)
    data[str(chat_id)] = entry
    save_reminders(data)


def clear_reminder(chat_id):
    data = load_reminders()
    if data.pop(str(chat_id), None) is None:
        return False
    save_reminders(data)
    return True


def due_reminders(now):
    """Chat ids whose time has come and who were not reminded today yet."""
    data = load_reminders()
    today = now.strftime("%Y-%m-%d")
    now_minutes = now.hour * 60 + now.minute
    out = []
    changed = False
    for chat_id, entry in data.items():
        at = entry.get("at")
        if not at or entry.get("sent_on") == today:
            continue
        try:
            hh, mm = [int(x) for x in at.split(":")]
        except ValueError:
            continue
        due = hh * 60 + mm
        # a 10-minute window covers a restart right at the appointed minute,
        # without firing yesterday's reminder after a long outage
        if 0 <= now_minutes - due <= 10:
            out.append(int(chat_id))
            entry["sent_on"] = today
            changed = True
    if changed:
        save_reminders(data)
    return out


def send_reminders():
    now = datetime.datetime.now(MSK)
    for chat_id in due_reminders(now):
        try:
            call("sendMessage", {
                "chat_id": chat_id,
                "text": REMIND_TEXT,
                "reply_markup": KEYBOARD,
            }, timeout=20)
            log("reminder sent to %s" % chat_id)
        except Exception as err:
            log("reminder failed for %s: %s" % (chat_id, err))


def reply(chat_id, text, markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if markup:
        payload["reply_markup"] = markup
    try:
        call("sendMessage", payload, timeout=20)
    except Exception as err:
        log("send failed for %s: %s" % (chat_id, err))


def handle_app_data(chat_id, raw, user):
    """Settings saved inside the mini app arrive here as web_app_data."""
    try:
        data = json.loads(raw)
    except ValueError:
        log("bad app data from %s: %r" % (chat_id, raw[:80]))
        return
    if data.get("off"):
        clear_reminder(chat_id)
        reply(chat_id, "Напоминания отключены.")
        log("reminder off (app) for %s" % who(user))
        return
    at = str(data.get("at") or "")
    match = TIME_RE.match(at)
    if not match:
        reply(chat_id, "Не разобрала время: %s" % at[:20])
        return
    hhmm = "%02d:%02d" % (int(match.group(1)), int(match.group(2)))
    set_reminder(chat_id, hhmm)
    reply(chat_id, "Напоминание каждый день в %s по Москве." % hhmm)
    log("reminder %s (app) for %s" % (hhmm, who(user)))


def handle(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return
    # Groups would need an @mention convention; the tracker is personal,
    # so only private chats get an answer.
    if chat.get("type") != "private":
        return

    user = msg.get("from") or {}
    allowed = load_allowed()
    if allowed and int(chat_id) not in allowed:
        log("REFUSED %s" % who(user))
        try:
            call("sendMessage", {"chat_id": chat_id, "text": DENIED}, timeout=20)
        except Exception as err:
            log("refusal notice failed for %s: %s" % (chat_id, err))
        return

    app_data = msg.get("web_app_data")
    if app_data:
        handle_app_data(chat_id, app_data.get("data") or "", user)
        return

    text = (msg.get("text") or "").strip()
    low = text.lower()

    if low in ("/выкл", "/off", "выкл", "/stop"):
        if clear_reminder(chat_id):
            reply(chat_id, "Напоминания отключены.")
        else:
            reply(chat_id, "Напоминаний и так не было.")
        log("reminder off for %s" % who(user))
        return

    match = TIME_RE.match(text)
    if match:
        hhmm = "%02d:%02d" % (int(match.group(1)), int(match.group(2)))
        set_reminder(chat_id, hhmm)
        reply(chat_id, "Буду напоминать каждый день в %s по Москве.\n"
                       "Другое время — пришли его же цифрами. Отключить — /выкл." % hhmm)
        log("reminder %s for %s" % (hhmm, who(user)))
        return

    current = load_reminders().get(str(chat_id), {}).get("at")
    tail = ("\n\nНапоминание стоит на %s по Москве." % current) if current else ""
    reply(chat_id, WELCOME + tail, markup=REPLY_KEYBOARD)
    log("answered %s" % who(user))


def main():
    os.makedirs(STATE_DIR, exist_ok=True)
    pid_fh = open(PID_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(pid_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("another instance holds the lock, exiting")
        return 0
    pid_fh.seek(0)
    pid_fh.truncate()
    pid_fh.write(str(os.getpid()))
    pid_fh.flush()

    offset = load_offset()
    log("started, offset=%d" % offset)

    while True:
        # checked on every pass; long polling returns at least once a minute
        try:
            send_reminders()
        except Exception as err:
            log("reminder pass failed: %s" % err)

        try:
            resp = call("getUpdates", {
                "offset": offset,
                "timeout": 50,
                "allowed_updates": ["message"],
            }, timeout=70)
        except urllib.error.HTTPError as err:
            log("http %s, backing off" % err.code)
            time.sleep(5)
            continue
        except Exception as err:
            log("poll failed: %s" % err)
            time.sleep(5)
            continue

        if not resp.get("ok"):
            log("api said not ok: %s" % resp.get("description"))
            time.sleep(5)
            continue

        for update in resp.get("result", []):
            offset = update["update_id"] + 1
            try:
                handle(update)
            except Exception as err:
                log("handler error: %s" % err)
        if resp.get("result"):
            save_offset(offset)


if __name__ == "__main__":
    sys.exit(main())
