# bot.py
# Full Hosting Manager (raw Telegram API via requests)
# Python 3.13+ (Windows / Linux)
# Dependency: requests
#
# Paste this file as-is (no leading spaces). Edit BOT_TOKEN if you need to change it.

import os
import sys
import time
import json
import shutil
import subprocess
import traceback
from pathlib import Path

import requests

# ---------------- CONFIG ----------------
BOT_TOKEN = "8460725856:AAFOz3lBzx6tYeH36kKo4i0lME-nw1CzO6o"  # <--- your token (already filled)
OWNER_ID = 6123174299  # owner id (already filled)
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}/"
FILE_BASE = f"https://api.telegram.org/file/bot{BOT_TOKEN}/"

BASE_DIR = Path(__file__).parent.resolve()
USER_BOTS_DIR = BASE_DIR / "user_bots"
LOGS_DIR = BASE_DIR / "logs"
PLANS_FILE = BASE_DIR / "plans.json"
OFFSET_FILE = BASE_DIR / "offset.txt"

USER_BOTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# runtime state
offset = 0
user_states = {}   # user_id -> dict (awaiting_zip / awaiting_text_action)
running_bots = {}  # bot_id -> {"proc": Popen, "log": str}
plans = {}         # str(user_id) -> plan

# ---------------- Helpers - Telegram API ----------------
def api_post(method, payload=None, files=None, params=None):
    url = API_BASE + method
    try:
        if files:
            r = requests.post(url, data=payload or {}, files=files, timeout=120, params=params)
        else:
            r = requests.post(url, json=payload or {}, timeout=120, params=params)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print("API error", method, e)
        return None

def send_message(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return api_post("sendMessage", payload)

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    return api_post("editMessageText", payload)

def answer_callback(callback_query_id, text=""):
    return api_post("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

def send_document(chat_id, file_path, filename=None):
    try:
        with open(file_path, "rb") as f:
            files = {"document": (filename or os.path.basename(file_path), f)}
            return api_post("sendDocument", None, files=files, params={"chat_id": chat_id})
    except Exception as e:
        print("send_document error", e)
        return None

def get_file_path(file_id):
    res = api_post("getFile", {"file_id": file_id})
    if not res or not res.get("ok"):
        return None
    return res["result"]["file_path"]

def download_file(file_path, dest):
    url = FILE_BASE + file_path
    try:
        r = requests.get(url, stream=True, timeout=120)
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(64*1024):
                if not chunk:
                    break
                f.write(chunk)
        return True
    except Exception as e:
        print("download_file error", e)
        return False

# ---------------- Plans ----------------
def load_plans():
    global plans
    if PLANS_FILE.exists():
        try:
            plans = json.loads(PLANS_FILE.read_text(encoding="utf-8"))
        except Exception:
            plans = {}
    else:
        plans = {}

def save_plans():
    try:
        PLANS_FILE.write_text(json.dumps(plans, indent=2), encoding="utf-8")
    except Exception as e:
        print("save_plans error", e)

def get_plan(user_id):
    return plans.get(str(user_id), "free")

def max_bots_for_plan(plan):
    if plan == "free":
        return 1
    if plan == "premium":
        return 3
    if plan == "vip":
        return 999
    return 1

# ---------------- Run detection ----------------
def find_main_file(bot_dir: Path):
    # prefer bot.py main.py run.py
    for name in ("bot.py", "main.py", "run.py"):
        p = bot_dir / name
        if p.exists():
            return p
    # package.json -> npm start
    if (bot_dir / "package.json").exists():
        return "package.json"
    # fallback: any .py
    for p in bot_dir.glob("*.py"):
        return p
    # fallback: any executable start scripts
    if (bot_dir / "start.sh").exists():
        return "start.sh"
    return None

def build_run_command(bot_dir: Path, main):
    """
    Return (cmd_list, use_shell_bool). If use_shell True, cmd is a single string.
    """
    # main can be Path or special strings
    if isinstance(main, Path):
        name = main.name.lower()
        if name.endswith(".py"):
            return [sys.executable, str(main)], False
        if name.endswith(".js"):
            return ["node", str(main)], False
        # unknown python fallback
        return [sys.executable, str(main)], False
    else:
        # main is special token
        if main == "package.json":
            # npm start (works on Windows if npm in PATH)
            return ["npm", "start"], False
        if main == "start.sh":
            # shell script
            if os.name == "nt":
                # on Windows, try bash if available
                return ["bash", "start.sh"], False
            else:
                return ["sh", "start.sh"], False
    # fallback
    return [sys.executable, "bot.py"], False

# ---------------- Subprocess management ----------------
def start_user_bot(user_id, bot_id, bot_dir: Path):
    main_file = find_main_file(bot_dir)
    if not main_file:
        print("no entry file for", bot_id)
        return False
    cmd, use_shell = build_run_command(bot_dir, main_file)
    log_file = LOGS_DIR / f"{bot_id}.log"
    try:
        lf = open(log_file, "ab")
    except Exception as e:
        print("open log error", e)
        return False
    try:
        if use_shell:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, cwd=str(bot_dir), shell=True)
        else:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, cwd=str(bot_dir))
    except Exception as e:
        print("start subprocess error", e)
        lf.close()
        return False
    running_bots[bot_id] = {"proc": proc, "log": str(log_file)}
    print("started", bot_id, "pid", proc.pid)
    return True

def stop_user_bot(bot_id):
    info = running_bots.get(bot_id)
    if not info:
        return False
    proc = info["proc"]
    try:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    running_bots.pop(bot_id, None)
    return True

def restart_user_bot(bot_id):
    info = running_bots.get(bot_id)
    if info:
        stop_user_bot(bot_id)
    bot_dir = USER_BOTS_DIR / bot_id
    if bot_dir.exists():
        return start_user_bot(None, bot_id, bot_dir)
    return False

# ---------------- Utility: run shell and return output ----------------
def run_shell_local(cmd, cwd=None, timeout=60):
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd, timeout=timeout)
        out = (res.stdout or "") + (res.stderr or "")
        if not out.strip():
            out = "✅ (no output)"
        return out
    except Exception as e:
        return f"❌ Exception: {e}"

def send_long_text_or_file(chat_id, title, text):
    # Telegram message limit ~4096; keep safe margin
    MAX = 3500
    if len(text) <= MAX:
        send_message(chat_id, f"<b>{title}</b>\n<pre>{escape_html(text)}</pre>")
    else:
        # write to temp file and send as document
        tmp = BASE_DIR / "tmp_cmd_output.txt"
        tmp.write_text(text, encoding="utf-8")
        send_document(chat_id, str(tmp), filename=f"{title}.txt")
        try:
            tmp.unlink()
        except Exception:
            pass

def escape_html(s):
    # minimal escaping for <, >
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# ---------------- Message & Callback handling ----------------
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    text = (msg.get("text") or "").strip()
    state = user_states.setdefault(user_id, {})

    # document handling (if awaiting upload)
    if state.get("awaiting_zip") and "document" in msg:
        doc = msg["document"]
        fname = doc.get("file_name", "")
        if not fname.lower().endswith(".zip"):
            send_message(chat_id, "❌ Please upload a ZIP file.")
            state.pop("awaiting_zip", None)
            return
        file_id = doc["file_id"]
        file_path = get_file_path(file_id)
        if not file_path:
            send_message(chat_id, "❌ Could not get file info.")
            state.pop("awaiting_zip", None)
            return
        bot_id = f"{user_id}_{doc.get('file_unique_id')}"
        bot_dir = USER_BOTS_DIR / bot_id
        bot_dir.mkdir(parents=True, exist_ok=True)
        local_zip = bot_dir / fname
        ok = download_file(file_path, str(local_zip))
        if not ok:
            send_message(chat_id, "❌ Failed to download file.")
            state.pop("awaiting_zip", None)
            return
        try:
            shutil.unpack_archive(str(local_zip), str(bot_dir))
        except Exception as e:
            send_message(chat_id, f"❌ Failed to extract ZIP: {e}")
            state.pop("awaiting_zip", None)
            return

        # check plan limits
        plan = get_plan(user_id)
        bots = [d.name for d in USER_BOTS_DIR.iterdir() if d.is_dir() and d.name.startswith(str(user_id))]
        if len(bots) > max_bots_for_plan(plan):
            send_message(chat_id, f"❌ Your plan ({plan}) allows max {max_bots_for_plan(plan)} bots. Remove some or upgrade.")
            state.pop("awaiting_zip", None)
            return

        send_message(chat_id, f"✅ Uploaded as <code>{bot_id}</code>, starting...")
        started = start_user_bot(user_id, bot_id, bot_dir)
        if not started:
            send_message(chat_id, "❌ Failed to start user bot. Check logs.")
        state.pop("awaiting_zip", None)
        plans.setdefault(str(user_id), "free")
        save_plans()
        return

    # owner-only commands /cmd and /allcmd
    if text.startswith("/cmd ") and user_id == OWNER_ID:
        cmd = text[len("/cmd "):].strip()
        if not cmd:
            send_message(chat_id, "❌ Usage: /cmd <command>")
            return
        out = run_shell_local(cmd, cwd=None)
        send_long_text_or_file(chat_id, "CMD Output", out)
        return

    if text.startswith("/allcmd ") and user_id == OWNER_ID:
        cmd = text[len("/allcmd "):].strip()
        if not cmd:
            send_message(chat_id, "❌ Usage: /allcmd <command>")
            return
        parts = []
        for bot_id in list(running_bots.keys()) + [d.name for d in USER_BOTS_DIR.iterdir() if d.is_dir() and d.name not in running_bots]:
            bot_dir = USER_BOTS_DIR / bot_id
            try:
                out = run_shell_local(cmd, cwd=str(bot_dir))
                parts.append(f"==== {bot_id} ====\n{out}")
            except Exception as e:
                parts.append(f"==== {bot_id} ====\nError: {e}")
        full = "\n\n".join(parts)
        send_long_text_or_file(chat_id, "ALLCMD Output", full[:200000])  # big cap
        return

    # /start or /panel
    if text in ("/start", "/panel"):
        if user_id == OWNER_ID:
            keyboard = [
                [{"text": "📋 All Users", "callback_data": "all_users"}],
                [{"text": "🛠 All Bots", "callback_data": "all_bots"}],
                [{"text": "💀 Kill All", "callback_data": "kill_all"}],
                [{"text": "📢 Broadcast", "callback_data": "broadcast"}],
                [{"text": "👑 Manage Plans", "callback_data": "plans"}],
            ]
            send_message(chat_id, "👑 Owner Panel\n\n👨‍💻 Dev: EC-NISHITH", reply_markup={"inline_keyboard": keyboard})
        else:
            plan = get_plan(user_id)
            keyboard = [
                [{"text": "📂 Upload Bot", "callback_data": "upload_bot"}],
                [{"text": "📋 My Bots", "callback_data": "my_bots"}],
                [{"text": f"⭐ Plan: {plan.upper()}", "callback_data": "noop"}],
            ]
            send_message(chat_id, "🛠 User Panel\n\n👨‍💻 Dev: EC-NISHITH", reply_markup={"inline_keyboard": keyboard})
        return

    # owner text actions if awaiting
    if str(user_id) == str(OWNER_ID) and state.get("awaiting_text_action"):
        action = state.pop("awaiting_text_action")
        if action == "broadcast":
            msg = text
            for uid in list(plans.keys()):
                try:
                    send_message(int(uid), f"📢 Broadcast:\n\n{escape_html(msg)}")
                except Exception as e:
                    print("broadcast failed to", uid, e)
            send_message(chat_id, "✅ Broadcast sent.")
        elif action.startswith("plan:"):
            _, act, plan = action.split(":")
            try:
                target = int(text.strip())
                if act == "grant":
                    plans[str(target)] = plan
                    save_plans()
                    send_message(chat_id, f"✅ Granted {plan} to {target}")
                elif act == "revoke":
                    plans[str(target)] = "free"
                    save_plans()
                    send_message(chat_id, f"✅ Revoked plans for {target}")
            except Exception as e:
                send_message(chat_id, "❌ Invalid user id.")
        return

# ---------------- Callback handling ----------------
def handle_callback(cb):
    query_id = cb.get("id")
    data = cb.get("data", "")
    msg = cb.get("message", {}) or {}
    chat_id = msg.get("chat", {}).get("id")
    user_id = cb.get("from", {}).get("id")
    message_id = msg.get("message_id")

    answer_callback(query_id)

    if data == "upload_bot":
        user_states.setdefault(user_id, {})["awaiting_zip"] = True
        edit_message(chat_id, message_id, "📂 Please send me a ZIP file (upload document now).")
        return

    if data == "my_bots":
        bots = [p.name for p in USER_BOTS_DIR.iterdir() if p.is_dir() and p.name.startswith(str(user_id))]
        if not bots:
            edit_message(chat_id, message_id, "❌ You have no hosted bots.")
            return
        lines = ["📋 Your bots:"]
        keyboard = []
        for b in bots:
            keyboard.append([
                {"text": f"▶️ Restart {b}", "callback_data": f"restart:{b}"},
                {"text": f"⛔ Stop {b}", "callback_data": f"stop:{b}"},
                {"text": "📜 Logs", "callback_data": f"logs:{b}"}
            ])
        edit_message(chat_id, message_id, "\n".join(lines), reply_markup={"inline_keyboard": keyboard})
        return

    if data.startswith("restart:"):
        bot_id = data.split(":", 1)[1]
        restart_user_bot(bot_id)
        edit_message(chat_id, message_id, f"🔄 Restarted {bot_id}")
        return

    if data.startswith("stop:"):
        bot_id = data.split(":", 1)[1]
        stop_user_bot(bot_id)
        edit_message(chat_id, message_id, f"⛔ Stopped {bot_id}")
        return

    if data.startswith("logs:"):
        bot_id = data.split(":", 1)[1]
        logf = LOGS_DIR / f"{bot_id}.log"
        if not logf.exists():
            edit_message(chat_id, message_id, "❌ No logs found.")
            return
        with open(logf, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()[-30:]
        text = "<b>Last 30 lines:</b>\n" + "".join(lines)
        keyboard = [[{"text": "📂 Full Log File", "callback_data": f"logfile:{bot_id}"}]]
        edit_message(chat_id, message_id, text, reply_markup={"inline_keyboard": keyboard})
        return

    if data.startswith("logfile:"):
        bot_id = data.split(":", 1)[1]
        logf = LOGS_DIR / f"{bot_id}.log"
        if not logf.exists():
            edit_message(chat_id, message_id, "❌ No logs found.")
            return
        send_document(chat_id, str(logf))
        return

    if data == "plans" and user_id == OWNER_ID:
        keyboard = [
            [{"text": "Grant Premium", "callback_data": "grant_premium"}],
            [{"text": "Revoke Premium", "callback_data": "revoke_premium"}],
            [{"text": "Grant VIP", "callback_data": "grant_vip"}],
            [{"text": "Revoke VIP", "callback_data": "revoke_vip"}],
        ]
        edit_message(chat_id, message_id, "Manage Plans:", reply_markup={"inline_keyboard": keyboard})
        return

    if data.startswith("grant_") or data.startswith("revoke_"):
        action = data.split("_", 1)[0]
        plan = data.split("_", 1)[1]
        user_states.setdefault(user_id, {})["awaiting_text_action"] = f"plan:{action}:{plan}"
        edit_message(chat_id, message_id, "📌 Reply with the user ID to apply this action.")
        return

    if data == "broadcast" and user_id == OWNER_ID:
        user_states.setdefault(user_id, {})["awaiting_text_action"] = "broadcast"
        edit_message(chat_id, message_id, "📢 Now send the broadcast message as text.")
        return

    if data == "all_users" and user_id == OWNER_ID:
        if plans:
            text = "All users:\n" + "\n".join([f"{uid} -> {plans[uid]}" for uid in plans.keys()])
        else:
            text = "No users with plans yet."
        edit_message(chat_id, message_id, text)
        return

    if data == "kill_all" and user_id == OWNER_ID:
        for b in list(running_bots.keys()):
            stop_user_bot(b)
        edit_message(chat_id, message_id, "💀 Killed all user bots.")
        return

    if data == "all_bots" and user_id == OWNER_ID:
        bots = [p.name for p in USER_BOTS_DIR.iterdir() if p.is_dir()]
        if not bots:
            edit_message(chat_id, message_id, "No bots uploaded yet.")
            return
        text = "All bots:\n" + "\n".join(bots)
        edit_message(chat_id, message_id, text)
        return

# ---------------- offset persistence ----------------
def save_offset():
    try:
        OFFSET_FILE.write_text(str(offset), encoding="utf-8")
    except Exception:
        pass

def load_offset():
    global offset
    try:
        if OFFSET_FILE.exists():
            offset = int(OFFSET_FILE.read_text(encoding="utf-8").strip() or "0")
    except Exception:
        offset = 0

# ---------------- Main loop ----------------
def main_loop():
    global offset
    print("Hosting bot starting (long-polling)...")
    load_plans()
    load_offset()
    while True:
        try:
            res = requests.post(API_BASE + "getUpdates", json={"timeout": 30, "offset": offset}, timeout=35)
            res.raise_for_status()
            data = res.json()
            if not data.get("ok"):
                time.sleep(1)
                continue
            updates = data.get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                save_offset()
                if "message" in upd:
                    try:
                        handle_message(upd["message"])
                    except Exception:
                        print("handle_message error", traceback.format_exc())
                elif "callback_query" in upd:
                    try:
                        handle_callback(upd["callback_query"])
                    except Exception:
                        print("handle_callback error", traceback.format_exc())
        except Exception as e:
            print("Main loop error:", e)
            time.sleep(2)

if __name__ == "__main__":
    main_loop()
