import asyncio
import os
import time
import platform
from datetime import datetime, timedelta

from pyrogram import Client, filters, enums, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import psutil

# ========================= CONFIG =========================
API_ID = 29831434                # Your bot API ID
API_HASH = "ba7986a2b219e935f4b81e621f71b51d"     # Your bot API HASH
BOT_TOKEN = "8458729608:AAFi2m2nJUKeVPwjzoQUJz9t-mB68CaNSIw"   # Your bot token from BotFather
OWNER_ID =  8198692931       # Your Telegram user ID (owner)
LOG_GROUP = -1003089868386    # A private group ID for logs (make bot admin there)

bot = Client("session_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ========================= STATE =========================
START_TIME = time.time()
maintenance = False
users = set()
blocked_users = set()

# Per-user flow state:
# user_state[user_id] = {
#   "choice": "pyro_v2"|"pyro_v1"|"telethon",
#   "api_id": int, "api_hash": str, "phone": str,
#   "step": "awaiting_credentials"|"awaiting_code"|"awaiting_2fa",
#   "phone_code_hash": str (pyrogram only),
#   "pyro": Client (temporary) | None,
#   "tele": TelegramClient (temporary) | None
# }
user_state = {}

# ========================= UTILITIES =========================
async def log(text: str):
    try:
        await bot.send_message(LOG_GROUP, text)
    except Exception:
        pass

def owner_only(func):
    async def wrapper(client, message):
        if message.from_user.id != OWNER_ID:
            return await message.reply("🚫 You are not authorized to use this command.")
        return await func(client, message)
    return wrapper

def is_blocked(uid: int) -> bool:
    return uid in blocked_users

def fmt_uptime() -> str:
    delta = timedelta(seconds=int(time.time() - START_TIME))
    return str(delta)

# ========================= AUTO BACKUP =========================
async def backup_users():
    total = len(users)
    title = f"📂 **Users Backup** — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    if total == 0:
        return await log(title + "\nNo users yet.")
    lines = []
    for uid in users:
        try:
            u = await bot.get_users(uid)
            lines.append(f"- {u.mention} (`{uid}`)")
        except Exception:
            lines.append(f"- `{uid}`")
    chunk = "\n".join(lines)
    await log(title + "\n" + chunk + f"\n\n📊 Total users: {total}")

# ========================= HELP =========================
HELP_PUBLIC = (
    "ℹ️ **Available Commands:**\n\n"
    "/start - Start the bot\n"
    "/help - Show this help menu\n\n"
    "👉 Use the buttons to generate session strings."
)

HELP_OWNER = (
    "👑 **Owner Commands**\n\n"
    "/gcast <text> — Broadcast to all users\n"
    "/maintenance — Toggle maintenance mode\n"
    "/users — Show user count & preview (full list goes to logs)\n"
    "/backup — Send full user list to logs now\n"
    "/block <user_id> — Block a user\n"
    "/unblock <user_id> — Unblock a user\n"
    "/status — System status (CPU, RAM, Disk, Uptime)\n"
)

@bot.on_message(filters.command("help"))
async def help_menu(client, message):
    if message.from_user.id == OWNER_ID:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Gcast", callback_data="help_gcast")],
            [InlineKeyboardButton("🛠 Maintenance", callback_data="help_maintenance")],
            [InlineKeyboardButton("👥 Users", callback_data="help_users")],
            [InlineKeyboardButton("🧱 Block/Unblock", callback_data="help_block")],
            [InlineKeyboardButton("🖥 Status", callback_data="help_status")],
            [InlineKeyboardButton("💾 Backup", callback_data="help_backup")],
        ])
        await message.reply("👑 **Owner Help Menu**\n\nChoose a command:", reply_markup=keyboard)
    else:
        await message.reply(HELP_PUBLIC)

@bot.on_callback_query()
async def help_callback(client, cq):
    data = cq.data
    if data.startswith("help_"):
        if cq.from_user.id != OWNER_ID:
            return await cq.answer("🚫 Not authorized.", show_alert=True)
        mapping = {
            "help_gcast": "📢 **/gcast <message>**\nSends a broadcast to all users.",
            "help_maintenance": "🛠 **/maintenance**\nToggle maintenance mode ON/OFF.",
            "help_users": "👥 **/users**\nShows preview; full list is sent to log group.",
            "help_block": "🧱 **/block <user_id>** / **/unblock <user_id>**\nBlock or unblock a user.",
            "help_status": "🖥 **/status**\nShows CPU, RAM, Disk, Uptime, Python & OS.",
            "help_backup": "💾 **/backup**\nImmediately sends full user list to log group.",
        }
        await cq.message.edit_text(mapping.get(data, HELP_OWNER))
        return

    # Other callback data = session generator choices
    if maintenance and cq.from_user.id != OWNER_ID:
        return await cq.answer("⚠️ Bot under maintenance.", show_alert=True)
    if is_blocked(cq.from_user.id):
        return await cq.answer("🚫 You are blocked from using this bot.", show_alert=True)

    await cq.answer()
    choice = cq.data
    uid = cq.from_user.id
    # init per-user state
    user_state[uid] = {"choice": choice, "step": "awaiting_credentials"}
    await bot.send_message(
        uid,
        "🔑 Send your credentials in **one line**:\n`API_ID API_HASH PHONE`\n\nExample:\n`12345 abcd12345 +919876543210`",
        parse_mode=enums.ParseMode.MARKDOWN
    )

# ========================= START =========================
@bot.on_message(filters.command("start"))
async def start_cmd(client, message):
    uid = message.from_user.id
    if is_blocked(uid):
        return await message.reply("🚫 You are blocked from using this bot.")
    if maintenance and uid != OWNER_ID:
        return await message.reply("⚠️ Bot is currently under maintenance. Please try again later.")
    users.add(uid)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Pyrogram V2", callback_data="pyro_v2")],
        [InlineKeyboardButton("📜 Pyrogram V1", callback_data="pyro_v1")],
        [InlineKeyboardButton("⚡ Telethon", callback_data="telethon")],
        [InlineKeyboardButton("❓ Help", callback_data="help_status" if uid == OWNER_ID else "noop")]
    ])
    await message.reply("👋 Welcome! Choose which session string you want to generate:", reply_markup=keyboard)
    await log(f"🚀 User {message.from_user.mention} started the bot.")

# ========================= OWNER COMMANDS =========================
@bot.on_message(filters.command("gcast"))
@owner_only
async def gcast(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return await message.reply("⚠️ Usage: /gcast <message>")
    text = parts[1]
    sent = failed = 0
    for uid in list(users):
        try:
            await bot.send_message(uid, f"📢 Broadcast:\n\n{text}")
            sent += 1
        except Exception:
            failed += 1
    await message.reply(f"✅ Broadcast completed.\nSent: {sent}, Failed: {failed}")
    await log(f"📢 Gcast by owner. Sent: {sent}, Failed: {failed}")

@bot.on_message(filters.command("maintenance"))
@owner_only
async def toggle_maintenance(client, message):
    global maintenance
    maintenance = not maintenance
    state = "ON 🛠️" if maintenance else "OFF ✅"
    await message.reply(f"🔧 Maintenance mode is now: {state}")
    await log(f"⚡ Maintenance toggled: {state}")

@bot.on_message(filters.command("users"))
@owner_only
async def list_users(client, message):
    if not users:
        return await message.reply("📂 No users have started the bot yet.")
    total = len(users)
    preview = list(users)[:10]
    msg = "👥 **Users Preview**:\n\n"
    for uid in preview:
        try:
            u = await bot.get_users(uid)
            msg += f"- {u.mention} (`{uid}`)\n"
        except Exception:
            msg += f"- `{uid}`\n"
    msg += f"\n📊 Total users: {total}"
    await message.reply(msg)
    await backup_users()

@bot.on_message(filters.command("backup"))
@owner_only
async def backup_now(client, message):
    await backup_users()
    await message.reply("💾 Backup sent to logs.")

@bot.on_message(filters.command("block"))
@owner_only
async def cmd_block(client, message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        return await message.reply("⚠️ Usage: /block <user_id>")
    uid = int(parts[1])
    blocked_users.add(uid)
    await message.reply(f"🧱 Blocked `{uid}`", parse_mode=enums.ParseMode.MARKDOWN)
    await log(f"🧱 User {uid} blocked by owner.")

@bot.on_message(filters.command("unblock"))
@owner_only
async def cmd_unblock(client, message):
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].lstrip("-").isdigit():
        return await message.reply("⚠️ Usage: /unblock <user_id>")
    uid = int(parts[1])
    blocked_users.discard(uid)
    await message.reply(f"✅ Unblocked `{uid}`", parse_mode=enums.ParseMode.MARKDOWN)
    await log(f"✅ User {uid} unblocked by owner.")

@bot.on_message(filters.command("status"))
@owner_only
async def status_cmd(client, message):
    try:
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        pyver = platform.python_version()
        osver = platform.platform()
        uptime = fmt_uptime()
        msg = (
            "🖥 **System Status**\n\n"
            f"• CPU: {cpu}%\n"
            f"• RAM: {mem.percent}% ({mem.used // (1024**2)}MB / {mem.total // (1024**2)}MB)\n"
            f"• Disk: {disk.percent}% ({disk.used // (1024**3)}GB / {disk.total // (1024**3)}GB)\n"
            f"• Uptime: {uptime}\n"
            f"• Python: {pyver}\n"
            f"• OS: {osver}\n"
            f"• Users: {len(users)}\n"
            f"• Blocked: {len(blocked_users)}\n"
            f"• Maintenance: {'ON' if maintenance else 'OFF'}"
        )
        await message.reply(msg)
    except Exception as e:
        await message.reply(f"❌ Status error: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)
        await log(f"❌ Status error: {e}")

# ========================= SESSION GENERATION FLOW =========================
@bot.on_message(filters.text & ~filters.command(["start", "help", "gcast", "maintenance", "users", "backup", "block", "unblock", "status"]))
async def flow_handler(client, message):
    uid = message.from_user.id
    if is_blocked(uid):
        return await message.reply("🚫 You are blocked from using this bot.")
    st = user_state.get(uid)
    if not st:
        return  # ignore random text

    choice = st.get("choice")

    # STEP A: credentials
    if st.get("step") == "awaiting_credentials":
        try:
            parts = message.text.strip().split()
            if len(parts) < 3:
                return await message.reply("⚠️ Invalid format. Use:\n`API_ID API_HASH PHONE`", parse_mode=enums.ParseMode.MARKDOWN)
            api_id = int(parts[0])
            api_hash = parts[1]
            phone = parts[2]
            st.update({"api_id": api_id, "api_hash": api_hash, "phone": phone})

            if choice in ("pyro_v2", "pyro_v1"):
                # Prepare Pyrogram temp client
                session_name = f"gen_{uid}"
                pyro = Client(session_name, api_id=api_id, api_hash=api_hash, in_memory=True)
                await pyro.connect()
                sent = await pyro.send_code(phone)
                st.update({
                    "pyro": pyro,
                    "phone_code_hash": sent.phone_code_hash,
                    "step": "awaiting_code"
                })
                await message.reply("📨 A login code was sent to your Telegram.\nPlease send the 5-digit **code** now (just the numbers).")
                await log(f"✉️ Code sent (Pyrogram) to {message.from_user.mention}")
                return

            elif choice == "telethon":
                # Telethon temp client
                tele = TelegramClient(StringSession(), api_id, api_hash)
                await tele.connect()
                sent = await tele.send_code_request(phone)
                st.update({
                    "tele": tele,
                    "step": "awaiting_code"
                })
                await message.reply("📨 A login code was sent to your Telegram.\nPlease send the 5-digit **code** now (just the numbers).")
                await log(f"✉️ Code sent (Telethon) to {message.from_user.mention}")
                return

            else:
                await message.reply("❌ Unknown choice. Please /start again.")
                user_state.pop(uid, None)
                return

        except Exception as e:
            await message.reply(f"❌ Error while sending code: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)
            await log(f"❌ Send code error for {message.from_user.mention}: {e}")
            user_state.pop(uid, None)
            return

    # STEP B: code
    if st.get("step") == "awaiting_code":
        code = message.text.strip().replace(" ", "")
        if not code.isdigit():
            return await message.reply("⚠️ Please send only the numeric **code** you received.")
        try:
            if choice in ("pyro_v2", "pyro_v1"):
                pyro: Client = st.get("pyro")
                phone = st.get("phone")
                phone_code_hash = st.get("phone_code_hash")
                await pyro.sign_in(phone_number=phone, phone_code_hash=phone_code_hash, phone_code=code)
                # If 2FA is enabled, Pyrogram raises an error string; but simpler to try exporting:
                me = await pyro.get_me()
                try:
                    session = await pyro.export_session_string()
                    await message.reply(f"✅ Pyrogram session for {me.first_name}\n\n`{session}`", parse_mode=enums.ParseMode.MARKDOWN)
                    await log(f"🎯 {message.from_user.mention} generated Pyrogram session.")
                    await pyro.disconnect()
                    user_state.pop(uid, None)
                    return
                except Exception as e:
                    # Probably needs 2FA
                    if "PASSWORD" in str(e).upper():
                        st["step"] = "awaiting_2fa"
                        await message.reply("🔐 2FA is enabled. Please send your **password** now.")
                    else:
                        await message.reply(f"❌ Error: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)
                        await log(f"❌ Pyrogram export error: {e}")
                        await pyro.disconnect()
                        user_state.pop(uid, None)
                    return

            elif choice == "telethon":
                tele: TelegramClient = st.get("tele")
                phone = st.get("phone")
                try:
                    await tele.sign_in(phone=phone, code=code)
                    # If no 2FA, done:
                    me = await tele.get_me()
                    session = tele.session.save()
                    await message.reply(f"✅ Telethon session for {me.first_name}\n\n`{session}`", parse_mode=enums.ParseMode.MARKDOWN)
                    await log(f"🎯 {message.from_user.mention} generated Telethon session.")
                    await tele.disconnect()
                    user_state.pop(uid, None)
                    return
                except SessionPasswordNeededError:
                    st["step"] = "awaiting_2fa"
                    await message.reply("🔐 2FA is enabled. Please send your **password** now.")
                    return

        except Exception as e:
            await message.reply(f"❌ Sign-in error: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)
            await log(f"❌ Sign-in error for {message.from_user.mention}: {e}")
            # cleanup
            try:
                if st.get("pyro"):
                    await st["pyro"].disconnect()
                if st.get("tele"):
                    await st["tele"].disconnect()
            except Exception:
                pass
            user_state.pop(uid, None)
            return

    # STEP C: 2FA
    if st.get("step") == "awaiting_2fa":
        password = message.text.strip()
        try:
            if choice in ("pyro_v2", "pyro_v1"):
                pyro: Client = st.get("pyro")
                await pyro.check_password(password=password)
                me = await pyro.get_me()
                session = await pyro.export_session_string()
                await message.reply(f"✅ Pyrogram session (2FA) for {me.first_name}\n\n`{session}`", parse_mode=enums.ParseMode.MARKDOWN)
                await log(f"🔑 {message.from_user.mention} generated Pyrogram session with 2FA.")
                await pyro.disconnect()
                user_state.pop(uid, None)
                return

            elif choice == "telethon":
                tele: TelegramClient = st.get("tele")
                await tele.sign_in(password=password)
                me = await tele.get_me()
                session = tele.session.save()
                await message.reply(f"✅ Telethon session (2FA) for {me.first_name}\n\n`{session}`", parse_mode=enums.ParseMode.MARKDOWN)
                await log(f"🔑 {message.from_user.mention} generated Telethon session with 2FA.")
                await tele.disconnect()
                user_state.pop(uid, None)
                return

        except Exception as e:
            await message.reply(f"❌ 2FA Error: `{e}`", parse_mode=enums.ParseMode.MARKDOWN)
            await log(f"❌ 2FA error for {message.from_user.mention}: {e}")
            # cleanup
            try:
                if st.get("pyro"):
                    await st["pyro"].disconnect()
                if st.get("tele"):
                    await st["tele"].disconnect()
            except Exception:
                pass
            user_state.pop(uid, None)
            return

# ========================= SCHEDULER & RUN =========================
async def main():
    scheduler = AsyncIOScheduler()
    scheduler.add_job(backup_users, "interval", hours=24)  # daily backup
    scheduler.start()
    await bot.start()
    print("✅ Bot is running...")
    await idle()

if __name__ == "__main__":
    bot.run()
