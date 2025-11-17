"""
bot.py
Aiogram bot that:
 - provides /add_account flow (collect api_id, api_hash, phone, OTP)
 - stores sessions in sessions/<name>.session using Telethon
 - seeds links DB and classifies links (group/channel/bot/folder)
 - shows accounts and provides join-assist export
 - learning: accept /report to update per-account EMA cooldown

USAGE:
 export BOT_TOKEN="123:..."
 python bot.py
"""
import asyncio
import os
import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, Optional

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

# ---- Config ----
BOT_TOKEN = os.environ.get("8027957940:AAGhcwmiHk6B2XK6EMf5TAj9ahyHOPkJ2vU", "")
DATA_DIR = Path(".")
SESSIONS_DIR = DATA_DIR / "sessions"
DB_FILE = DATA_DIR / "links.db"
LINKS_FILE = DATA_DIR / "links.txt"
SESSIONS_DIR.mkdir(exist_ok=True)
# EMA smoothing for cooldown learning
EMA_ALPHA = 0.3
DEFAULT_COOLDOWN = 120  # seconds initial recommendation

# ---- Setup bot ----
if not BOT_TOKEN:
    print("Set BOT_TOKEN env var.")
    exit(1)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# In-memory temp store for sign-in flows:
# keyed by telegram user id (the admin who is adding the account)
pending_login: Dict[int, Dict] = {}  # {telegram_user_id: {"telethon_client": client, "phone":..., "name":...}}

# ---- DB helpers (links + accounts table for metadata) ----
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        url TEXT UNIQUE,
        kind TEXT DEFAULT 'unknown', -- group/channel/bot/folder
        status TEXT DEFAULT 'pending',
        last_checked INTEGER DEFAULT 0,
        tries INTEGER DEFAULT 0
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_name TEXT UNIQUE,
        owner_telegram_id INTEGER,
        ema_cooldown REAL DEFAULT ?
    )""", (DEFAULT_COOLDOWN,))
    conn.commit()
    conn.close()

def seed_links_from_file():
    if not LINKS_FILE.exists():
        return 0
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    added = 0
    with open(LINKS_FILE, "r", encoding="utf-8") as f:
        for l in f:
            u = l.strip()
            if not u:
                continue
            try:
                cur.execute("INSERT OR IGNORE INTO links (url) VALUES (?)", (u,))
                added += cur.rowcount
            except Exception:
                pass
    conn.commit()
    conn.close()
    return added

def list_accounts():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT session_name, owner_telegram_id, ema_cooldown FROM accounts")
    rows = cur.fetchall()
    conn.close()
    return rows

def add_account_meta(session_name: str, owner_telegram_id: int):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO accounts (session_name, owner_telegram_id, ema_cooldown) VALUES (?, ?, ?)",
                (session_name, owner_telegram_id, DEFAULT_COOLDOWN))
    conn.commit()
    conn.close()

def update_ema(session_name: str, observed_seconds: float):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT ema_cooldown FROM accounts WHERE session_name=?", (session_name,))
    row = cur.fetchone()
    if row:
        prev = row[0]
        ema = EMA_ALPHA * observed_seconds + (1 - EMA_ALPHA) * prev
        cur.execute("UPDATE accounts SET ema_cooldown=? WHERE session_name=?", (ema, session_name))
    else:
        ema = DEFAULT_COOLDOWN
        cur.execute("INSERT OR IGNORE INTO accounts (session_name, owner_telegram_id, ema_cooldown) VALUES (?, ?, ?)",
                    (session_name, 0, ema))
    conn.commit()
    conn.close()
    return ema

# ---- Link classifier helper (very conservative heuristics) ----
def classify_link(url: str) -> str:
    u = url.strip().lower()
    if u.startswith("@"):
        # could be group/channel/bot; keep unknown
        return "unknown"
    if "t.me/joinchat" in u or "/+/" in u or "t.me/+" in u or "joinchat" in u:
        return "group_invite"
    if "t.me/" in u and ("bot" in u or u.endswith("bot")):
        return "bot"
    # heuristics for channel vs group are hard; mark unknown
    return "unknown"

# ---- States for FSM (add account flow) ----
class AddAccount(StatesGroup):
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()
    waiting_session_name = State()

# ---- Handlers ----
@dp.message(Command("start"))
async def start_cmd(msg: types.Message):
    await msg.reply(
        "Welcome — I help you manage multiple user accounts.\n\n"
        "Commands:\n"
        "/add_account - add a new user account (API_ID, API_HASH, phone, OTP)\n"
        "/sessions - list saved user sessions\n"
        "/seed_links - seed links from links.txt into DB\n"
        "/export_assist <session_name> - prepare join-assist file for that account\n"
        "/report <session_name> <flood_seconds|ok|fail> - report a manual join outcome (updates cooldown)\n"
    )

@dp.message(Command("add_account"))
async def add_account_start(msg: types.Message, state: FSMContext):
    await state.set_state(AddAccount.waiting_session_name)
    await msg.reply("Choose a short session name (e.g. acc1). This will save sessions/<name>.session")

@dp.message(state=AddAccount.waiting_session_name)
async def got_session_name(msg: types.Message, state: FSMContext):
    name = msg.text.strip()
    if not name:
        await msg.reply("Session name cannot be empty.")
        return
    # store name then ask api id
    await state.update_data(session_name=name)
    await state.set_state(AddAccount.waiting_api_id)
    await msg.reply("Enter API_ID (numeric) for this user (you get this from https://my.telegram.org).")

@dp.message(state=AddAccount.waiting_api_id)
async def got_api_id(msg: types.Message, state: FSMContext):
    txt = msg.text.strip()
    if not txt.isdigit():
        await msg.reply("API_ID must be numeric.")
        return
    await state.update_data(api_id=int(txt))
    await state.set_state(AddAccount.waiting_api_hash)
    await msg.reply("Enter API_HASH (string).")

@dp.message(state=AddAccount.waiting_api_hash)
async def got_api_hash(msg: types.Message, state: FSMContext):
    txt = msg.text.strip()
    await state.update_data(api_hash=txt)
    await state.set_state(AddAccount.waiting_phone)
    await msg.reply("Enter phone number with country code (e.g. +91XXXXXXXXXX).")

@dp.message(state=AddAccount.waiting_phone)
async def got_phone(msg: types.Message, state: FSMContext):
    phone = msg.text.strip()
    data = await state.get_data()
    name = data["session_name"]
    api_id = data["api_id"]
    api_hash = data["api_hash"]
    # start telethon client to send code
    tmp_session = StringSession()  # in-memory session to perform auth; we'll save final session to file
    client = TelegramClient(tmp_session, api_id, api_hash)
    await msg.reply("Sending code... please wait.")
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
    except Exception as e:
        await msg.reply(f"Failed to send code: {e}")
        await client.disconnect()
        await state.clear()
        return
    # store client and context
    pending_login[msg.from_user.id] = {
        "telethon_client": client,
        "phone": phone,
        "session_name": name,
        "api_id": api_id,
        "api_hash": api_hash
    }
    await state.set_state(AddAccount.waiting_code)
    await msg.reply("Code sent to your number. Please send me the code you received (just digits).")

@dp.message(state=AddAccount.waiting_code)
async def got_code(msg: types.Message, state: FSMContext):
    code = msg.text.strip()
    obj = pending_login.get(msg.from_user.id)
    if not obj:
        await msg.reply("No pending login found. Please restart with /add_account.")
        await state.clear()
        return
    client: TelegramClient = obj["telethon_client"]
    phone = obj["phone"]
    session_name = obj["session_name"]
    try:
        # attempt sign in
        await msg.reply("Trying to sign in...")
        try:
            res = await client.sign_in(phone=phone, code=code)
        except errors.SessionPasswordNeededError:
            # 2FA needed
            await state.set_state(AddAccount.waiting_password)
            await msg.reply("This account has two-step verification (password). Send the password now.")
            return
        except Exception as e:
            # some Telethon versions use sign_in with code else use sign_in(code=code)
            # try fallback:
            try:
                await client.sign_in(code=code)
            except Exception as e2:
                await msg.reply(f"Sign-in failed: {e} / {e2}")
                await client.disconnect()
                pending_login.pop(msg.from_user.id, None)
                await state.clear()
                return
    except Exception as e:
        await msg.reply(f"Sign in error: {e}")
        await client.disconnect()
        pending_login.pop(msg.from_user.id, None)
        await state.clear()
        return

    # if success, save client session to file
    final_path = SESSIONS_DIR / f"{session_name}.session"
    # Telethon session string approach: get StringSession string and save file
    string_sess = client.session.save()
    # write binary session by creating a TelegramClient with filename session_name and saving
    # easiest: create client with filename session_name and export session
    await client.disconnect()
    # create real file-backed client to persist session
    file_client = TelegramClient(str(SESSIONS_DIR / session_name), obj["api_id"], obj["api_hash"])
    # to persist, we can load string session into new client
    file_client.session = client.session
    # Telethon automatically writes .session file when connected; do connect->disconnect to flush
    try:
        await file_client.connect()
        await file_client.disconnect()
    except Exception:
        # even if connect fails, try to write session via StringSession -> file
        with open(SESSIONS_DIR / f"{session_name}.session", "wb") as f:
            f.write(client.session.save().encode() if isinstance(client.session.save(), str) else client.session.save())
    add_account_meta(session_name, msg.from_user.id)
    pending_login.pop(msg.from_user.id, None)
    await state.clear()
    await msg.reply(f"Account saved as `{session_name}.session`.\nUse /sessions to list accounts.", parse_mode="Markdown")

@dp.message(state=AddAccount.waiting_password)
async def got_password(msg: types.Message, state: FSMContext):
    pwd = msg.text.strip()
    obj = pending_login.get(msg.from_user.id)
    if not obj:
        await msg.reply("No pending login.")
        await state.clear()
        return
    client: TelegramClient = obj["telethon_client"]
    phone = obj["phone"]
    session_name = obj["session_name"]
    try:
        await client.sign_in(password=pwd)
    except Exception as e:
        await msg.reply(f"Password sign-in failed: {e}")
        await client.disconnect()
        pending_login.pop(msg.from_user.id, None)
        await state.clear()
        return
    # save session same as above
    await client.disconnect()
    try:
        file_client = TelegramClient(str(SESSIONS_DIR / session_name), obj["api_id"], obj["api_hash"])
        file_client.session = client.session
        await file_client.connect()
        await file_client.disconnect()
    except Exception:
        with open(SESSIONS_DIR / f"{session_name}.session", "wb") as f:
            f.write(client.session.save().encode() if isinstance(client.session.save(), str) else client.session.save())
    add_account_meta(session_name, msg.from_user.id)
    pending_login.pop(msg.from_user.id, None)
    await state.clear()
    await msg.reply(f"Account saved as `{session_name}.session`.")

@dp.message(Command("sessions"))
async def cmd_sessions(msg: types.Message):
    rows = list_accounts()
    if not rows:
        await msg.reply("No saved sessions found.")
        return
    out = "Saved sessions:\n"
    for r in rows:
        out += f"- {r[0]} (owner_telegram_id={r[1]}) cooldown={r[2]:.1f}s\n"
    await msg.reply(out)

@dp.message(Command("seed_links"))
async def cmd_seed_links(msg: types.Message):
    n = seed_links_from_file()
    await msg.reply(f"Seeded {n} links from {LINKS_FILE} (if exists).")

@dp.message(Command("export_assist"))
async def cmd_export_assist(msg: types.Message):
    parts = msg.text.split()
    if len(parts) < 2:
        await msg.reply("Usage: /export_assist <session_name>")
        return
    session_name = parts[1].strip()
    # fetch pending links classified as group_invite or unknown
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT url FROM links WHERE status='pending' AND (kind='group_invite' OR kind='unknown')")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        await msg.reply("No pending group links.")
        return
    urls = [r[0] for r in rows]
    # chunk into recommended batches by session's ema
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("SELECT ema_cooldown FROM accounts WHERE session_name=?", (session_name,))
    row = cur.fetchone()
    conn.close()
    ema = row[0] if row else DEFAULT_COOLDOWN
    # create a file with t.me links and a header recommending delay
    out_file = DATA_DIR / f"assist_{session_name}.txt"
    with open(out_file, "w", encoding="utf-8") as f:
        f.write(f"Recommended delay between joins for {session_name}: {ema:.0f} seconds\n\n")
        for u in urls:
            f.write(u + "\n")
    await msg.reply_document(types.InputFile(out_file), caption=f"Assist file for {session_name}. Recommended delay {ema:.0f}s")

@dp.message(Command("report"))
async def cmd_report(msg: types.Message):
    # /report <session_name> <ok|flood 120|fail>
    parts = msg.text.split()
    if len(parts) < 3:
        await msg.reply("Usage: /report <session_name> <ok|flood <seconds>|fail>")
        return
    session_name = parts[1].strip()
    res = parts[2].strip()
    if res == "ok":
        # slight decrease of ema
        new_ema = update_ema(session_name, DEFAULT_COOLDOWN * 0.9)
        await msg.reply(f"Recorded success. New EMA cooldown for {session_name}: {new_ema:.1f}s")
    elif res == "fail":
        new_ema = update_ema(session_name, DEFAULT_COOLDOWN * 1.2)
        await msg.reply(f"Recorded failure. New EMA cooldown for {session_name}: {new_ema:.1f}s")
    elif res == "flood" and len(parts) >= 4 and parts[3].isdigit():
        sec = int(parts[3])
        new_ema = update_ema(session_name, sec)
        await msg.reply(f"Recorded FloodWait {sec}s. New EMA cooldown for {session_name}: {new_ema:.1f}s")
    else:
        await msg.reply("Invalid report format. Examples:\n/report acc1 ok\n/report acc1 fail\n/report acc1 flood 120")

# fallback handler to classify link messages
@dp.message()
async def classify_msg(msg: types.Message):
    text = msg.text or ""
    if text.startswith("http") or text.startswith("@"):
        kind = classify_link(text)
        conn = sqlite3.connect(DB_FILE)
        cur = conn.cursor()
        cur.execute("INSERT OR IGNORE INTO links (url, kind) VALUES (?, ?)", (text.strip(), kind))
        conn.commit()
        conn.close()
        await msg.reply(f"Saved link as kind={kind}. Use /export_assist <session_name> to get assist file.")
    else:
        # ignore other messages
        pass

# run init
init_db()

if __name__ == "__main__":
    print("Starting bot...")
    try:
        import asyncio
        asyncio.run(dp.start_polling(bot))
    finally:
        print("Bot stopped.")
