import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from telethon import TelegramClient
from telethon.sessions import StringSession

# ========================= CONFIG =========================
API_ID = 29831434‎                # Your bot API ID
API_HASH = "ba7986a2b219e935f4b81e621f71b51d"     # Your bot API HASH
BOT_TOKEN = "8458729608:AAFi2m2nJUKeVPwjzoQUJz9t-mB68CaNSIw"   # Your bot token from BotFather
OWNER_ID =  8198692931       # Your Telegram user ID (owner)
LOG_GROUP = -1003089868386    # A private group ID for logs (make bot admin there)

bot = Client("session_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# State
maintenance = False
users = set()
Client.storage = {}

# ========================= UTILITIES =========================
async def log(text: str):
    try:
        await bot.send_message(LOG_GROUP, text)
    except:
        pass

def owner_only(func):
    async def wrapper(client, message):
        if message.from_user.id != OWNER_ID:
            return await message.reply("🚫 You are not authorized to use this command.")
        return await func(client, message)
    return wrapper

# ========================= COMMANDS =========================
@bot.on_message(filters.command("start"))
async def start(client, message):
    if maintenance and message.from_user.id != OWNER_ID:
        return await message.reply("⚠️ Bot is currently under maintenance. Please try again later.")

    users.add(message.from_user.id)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🤖 Pyrogram V2", callback_data="pyro_v2")],
        [InlineKeyboardButton("📜 Pyrogram V1", callback_data="pyro_v1")],
        [InlineKeyboardButton("⚡ Telethon", callback_data="telethon")]
    ])

    await message.reply(
        "👋 Welcome! Choose which session string you want to generate:",
        reply_markup=keyboard
    )
    await log(f"🚀 User {message.from_user.mention} started the bot.")

# Owner-only global broadcast
@bot.on_message(filters.command("gcast"))
@owner_only
async def gcast(client, message):
    if len(message.text.split()) < 2:
        return await message.reply("⚠️ Usage: /gcast <message>")
    text = message.text.split(" ", 1)[1]

    sent, failed = 0, 0
    for uid in users:
        try:
            await bot.send_message(uid, f"📢 Broadcast:\n\n{text}")
            sent += 1
        except:
            failed += 1

    await message.reply(f"✅ Broadcast completed.\nSent: {sent}, Failed: {failed}")
    await log(f"📢 Gcast by owner. Sent: {sent}, Failed: {failed}")

# Owner-only maintenance toggle
@bot.on_message(filters.command("maintenance"))
@owner_only
async def toggle_maintenance(client, message):
    global maintenance
    maintenance = not maintenance
    state = "ON 🛠️" if maintenance else "OFF ✅"
    await message.reply(f"🔧 Maintenance mode is now: {state}")
    await log(f"⚡ Maintenance toggled: {state}")

# Owner-only: list users
@bot.on_message(filters.command("users"))
@owner_only
async def list_users(client, message):
    if not users:
        return await message.reply("📂 No users have started the bot yet.")

    total = len(users)
    preview = list(users)[:10]  # show only first 10 in chat
    msg = "👥 **Users Preview**:\n\n"
    for uid in preview:
        try:
            user = await bot.get_users(uid)
            msg += f"- {user.mention} (`{uid}`)\n"
        except:
            msg += f"- `{uid}`\n"

    msg += f"\n📊 Total users: {total}"
    await message.reply(msg)

    # full log
    full_list = "📂 **Full User List**:\n\n"
    for uid in users:
        try:
            user = await bot.get_users(uid)
            full_list += f"- {user.mention} (`{uid}`)\n"
        except:
            full_list += f"- `{uid}`\n"

    await log(full_list)

# ========================= CALLBACK HANDLER =========================
@bot.on_callback_query()
async def callback_handler(client, callback_query):
    if maintenance and callback_query.from_user.id != OWNER_ID:
        return await callback_query.answer("⚠️ Bot under maintenance.", show_alert=True)

    await callback_query.answer()
    choice = callback_query.data
    user = callback_query.from_user

    try:
        await bot.send_message(
            user.id,
            "🔑 Please send your credentials in this format:\n\n"
            "`API_ID API_HASH PHONE`\n\nExample:\n`12345 abcd12345 +919876543210`",
            parse_mode=enums.ParseMode.MARKDOWN
        )
        client.storage[f"choice_{user.id}"] = choice
    except Exception as e:
        await log(f"❌ Error sending credentials request: {e}")

# ========================= SESSION GENERATION =========================
@bot.on_message(filters.text & ~filters.command(["start", "gcast", "maintenance", "users"]))
async def handle_credentials(client, message):
    user = message.from_user
    choice = client.storage.get(f"choice_{user.id}")
    if not choice:
        return

    try:
        data = message.text.split()
        if len(data) < 3:
            return await message.reply("⚠️ Invalid format. Use:\n`API_ID API_HASH PHONE`")

        api_id = int(data[0])
        api_hash = data[1]
        phone = data[2]

        if choice == "pyro_v2":
            try:
                async with Client("gen_v2", api_id=api_id, api_hash=api_hash, phone_number=phone) as app:
                    session = await app.export_session_string()
                    me = await app.get_me()
                    await message.reply(
                        f"✅ Pyrogram V2 session for {me.first_name}\n\n`{session}`",
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                    await log(f"🎯 {user.mention} generated Pyrogram V2 session.")
            except Exception as e:
                if "PASSWORD" in str(e).upper():
                    await message.reply("🔐 This account has 2FA enabled. Please send your password now:")
                    client.storage[f"2fa_{user.id}"] = ("pyro_v2", api_id, api_hash, phone)
                else:
                    await message.reply(f"❌ Error: `{e}`")
                    await log(f"❌ Pyrogram V2 error for {user.mention}: {e}")

        elif choice == "pyro_v1":
            try:
                async with Client("gen_v1", api_id=api_id, api_hash=api_hash, phone_number=phone, parse_mode=enums.ParseMode.HTML) as app:
                    session = await app.export_session_string()
                    me = await app.get_me()
                    await message.reply(
                        f"✅ Pyrogram V1 session for {me.first_name}\n\n`{session}`",
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                    await log(f"🎯 {user.mention} generated Pyrogram V1 session.")
            except Exception as e:
                if "PASSWORD" in str(e).upper():
                    await message.reply("🔐 This account has 2FA enabled. Please send your password now:")
                    client.storage[f"2fa_{user.id}"] = ("pyro_v1", api_id, api_hash, phone)
                else:
                    await message.reply(f"❌ Error: `{e}`")
                    await log(f"❌ Pyrogram V1 error for {user.mention}: {e}")

        elif choice == "telethon":
            try:
                with TelegramClient(StringSession(), api_id, api_hash) as tclient:
                    tclient.start(phone=phone)
                    session = tclient.session.save()
                    me = tclient.get_me()
                    await message.reply(
                        f"✅ Telethon session for {me.first_name}\n\n`{session}`",
                        parse_mode=enums.ParseMode.MARKDOWN
                    )
                    await log(f"🎯 {user.mention} generated Telethon session.")
            except Exception as e:
                if "PASSWORD" in str(e).upper():
                    await message.reply("🔐 This account has 2FA enabled. Please send your password now:")
                    client.storage[f"2fa_{user.id}"] = ("telethon", api_id, api_hash, phone)
                else:
                    await message.reply(f"❌ Error: `{e}`")
                    await log(f"❌ Telethon error for {user.mention}: {e}")

    except Exception as e:
        await message.reply(f"❌ Error: `{e}`")
        await log(f"❌ Error for {user.mention}: {e}")

    finally:
        client.storage.pop(f"choice_{user.id}", None)

# ========================= 2FA HANDLER =========================
@bot.on_message(filters.text & ~filters.command(["start", "gcast", "maintenance", "users"]))
async def handle_2fa(client, message):
    user = message.from_user
    twofa_data = client.storage.get(f"2fa_{user.id}")
    if not twofa_data:
        return

    try:
        choice, api_id, api_hash, phone = twofa_data
        password = message.text.strip()

        if choice == "pyro_v2":
            async with Client("gen_v2", api_id=api_id, api_hash=api_hash, phone_number=phone, password=password) as app:
                session = await app.export_session_string()
                me = await app.get_me()
                await message.reply(
                    f"✅ Pyrogram V2 session (2FA) for {me.first_name}\n\n`{session}`",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                await log(f"🔑 {user.mention} generated Pyrogram V2 session with 2FA.")

        elif choice == "pyro_v1":
            async with Client("gen_v1", api_id=api_id, api_hash=api_hash, phone_number=phone, password=password, parse_mode=enums.ParseMode.HTML) as app:
                session = await app.export_session_string()
                me = await app.get_me()
                await message.reply(
                    f"✅ Pyrogram V1 session (2FA) for {me.first_name}\n\n`{session}`",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                await log(f"🔑 {user.mention} generated Pyrogram V1 session with 2FA.")

        elif choice == "telethon":
            with TelegramClient(StringSession(), api_id, api_hash) as tclient:
                tclient.start(phone=phone, password=password)
                session = tclient.session.save()
                me = tclient.get_me()
                await message.reply(
                    f"✅ Telethon session (2FA) for {me.first_name}\n\n`{session}`",
                    parse_mode=enums.ParseMode.MARKDOWN
                )
                await log(f"🔑 {user.mention} generated Telethon session with 2FA.")

    except Exception as e:
        await message.reply(f"❌ 2FA Error: `{e}`")
        await log(f"❌ 2FA error for {user.mention}: {e}")

    finally:
        client.storage.pop(f"2fa_{user.id}", None)

# ========================= RUN =========================
bot.run()
