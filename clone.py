import asyncio
import base64
import re

import aiohttp
from pyrogram import filters
from pyrogram.enums import ChatMemberStatus
from pyrogram.errors import FloodWait, InputUserDeactivated, UserIsBlocked, UserNotParticipant
from pyrogram.handlers import CallbackQueryHandler, MessageHandler
from pyrogram.types import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

from config import ADMIN_USERNAME, SEP, FORCE_SUB_MESSAGE, LOGGER, MONGO_LIMIT_MB, START_MESSAGE
from database import (add_user, all_users, bot_storage, cluster_storage, del_user, put, user_count,
                      uses_default_cluster)

run = asyncio.to_thread

TASKS = set()       # background jobs (deliveries, broadcasts); referenced so they are not garbage collected
DELIVERING = set()  # (bot_id, user_id) pairs that currently have files being sent
BROADCASTS = {}     # bot_id -> {"cancel": bool} while a broadcast is running
STATES = {}         # (bot_id, user_id) -> unfinished /genlink or /batch conversation
CANCEL_BUTTON = Markup([[Button("❌ Cancel", callback_data="cancel_broadcast")]])


# ═══════════════════════════ helpers ═══════════════════════════
def encode(value):
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def decode(value):
    value = value.strip()
    return base64.urlsafe_b64decode((value + "=" * (-len(value) % 4)).encode()).decode()


def is_admin(client, user_id):
    return user_id in client.cfg["admins"]


def spawn(coro):
    """Run long work in the background so the bot keeps answering other users."""
    task = asyncio.create_task(coro)
    TASKS.add(task)
    task.add_done_callback(TASKS.discard)


def link_id(raw, channel):
    """Message ID from one number of a link. Rejects links made for another files channel."""
    number = int(raw)
    if number <= 0 or number % channel:
        raise ValueError("link was not generated for this files channel")
    return number // channel


async def copy_to(message, chat_id, **kwargs):
    """Copy a message, waiting out one FloodWait."""
    try:
        return await message.copy(chat_id, **kwargs)
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await message.copy(chat_id, **kwargs)


async def get_messages(client, ids):
    try:
        return await client.get_messages(client.db_channel.id, list(ids))
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await client.get_messages(client.db_channel.id, list(ids))


def get_message_id(client, message):
    """Message ID in the files channel, from a forwarded message or a t.me link. 0 if it is neither."""
    chat = message.forward_from_chat
    if chat:
        return message.forward_from_message_id if chat.id == client.db_channel.id else 0

    text = (message.text or "").strip()
    if message.forward_sender_name:
        return 0
    match = re.fullmatch(r"https?://(?:t\.me|telegram\.me)/(?:c/)?([^/]+)/(\d+)", text)
    if not match:
        return 0

    channel, message_id = match.groups()
    if channel.isdigit():  # private channel link: t.me/c/<id>/<message>
        return int(message_id) if int(f"-100{channel}") == client.db_channel.id else 0
    return int(message_id) if channel.lower() == (client.db_channel.username or "").lower() else 0


async def is_subscribed(client, user_id):
    forcesub = client.cfg["forcesub"]
    if not forcesub or is_admin(client, user_id):
        return True
    try:
        member = await client.get_chat_member(forcesub, user_id)
        return member.status in (ChatMemberStatus.OWNER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.MEMBER)
    except UserNotParticipant:
        return False
    except Exception:
        LOGGER.exception("ForceSub check failed for user %s on bot %s", user_id, client.bot_id)
        return False


async def get_shortlink(client, link):
    """Shorten a link with the clone's shortener. Returns the plain link if it is off or fails."""
    site = re.sub(r"^https?://", "", str(client.cfg["shortsite"] or "").strip()).rstrip("/")
    api = str(client.cfg["shortapi"] or "").strip()
    link = link.replace("http://", "https://", 1)
    if not site or not api:
        return link

    shareus = site == "api.shareus.in"
    if shareus:
        url = f"https://{site}/shortLink"
        params = {"token": api, "format": "json", "link": link}
    else:
        url = f"https://{site}/api"
        params = {"api": api, "url": link}

    try:
        session = getattr(client, "http_session", None)
        if session is None or session.closed:
            session = client.http_session = aiohttp.ClientSession()
        async with session.get(url, params=params, raise_for_status=True) as response:
            data = await response.json(content_type=None)
        if data.get("status") == "success":
            return data.get("shortlink" if shareus else "shortenedUrl", link)
    except Exception:
        LOGGER.exception("Shortener request failed for bot %s", client.bot_id)
    return link


# ═══════════════════════════ users: /start and file delivery ═══════════════════════════
async def start(client, message):
    user = message.from_user
    if not user:
        return
    first_name = user.first_name or "User"

    if not await is_subscribed(client, user.id):
        buttons = [[Button("Join Channel", url=client.invitelink)]]
        if len(message.command) > 1:
            buttons.append([Button("Try Again", url=f"https://t.me/{client.username}?start={message.command[1]}")])
        return await message.reply(FORCE_SUB_MESSAGE.format(first=first_name), reply_markup=Markup(buttons))

    try:  # a database problem must never stop people from getting their files
        await run(add_user, client.bot_id, user.id)
    except Exception:
        LOGGER.exception("Could not save user %s on bot %s", user.id, client.bot_id)

    if len(message.command) < 2:
        admin_button = Button("👨‍💻 Admin", url=f"https://t.me/{ADMIN_USERNAME.lstrip('@')}")
        return await message.reply_text(START_MESSAGE.format(first=first_name), reply_markup=Markup([[admin_button]]))

    # The link payload is "get-<id*channel>" for one file or "get-<first*channel>-<last*channel>" for a batch.
    try:
        parts = decode(message.command[1]).split("-")
        if len(parts) not in (2, 3):
            raise ValueError("unexpected link format")
        channel = abs(client.db_channel.id)
        first, last = link_id(parts[1], channel), link_id(parts[-1], channel)
        ids = range(first, last + 1) if first <= last else range(first, last - 1, -1)
        if len(ids) > 100:
            return await message.reply("❌ This link contains too many files.")
    except Exception as e:
        LOGGER.warning("Invalid start link for bot %s: %s", client.bot_id, e)
        return await message.reply("❌ Invalid link.")

    key = (client.bot_id, user.id)
    if key in DELIVERING:
        return await message.reply("⏳ Your previous files are still being sent. Please wait a moment.")

    try:
        messages = await get_messages(client, ids)
    except Exception:
        LOGGER.exception("Could not fetch files for bot %s", client.bot_id)
        return await message.reply("⚠️ Could not fetch the files right now. Please try again later.")

    DELIVERING.add(key)
    spawn(send_files(client.bot_id, user.id, messages))


async def send_files(bot_id, user_id, messages):
    try:
        for message in messages:
            if not message or message.empty:
                continue
            try:
                await copy_to(message, user_id, caption=message.caption.html if message.caption else None)
            except (UserIsBlocked, InputUserDeactivated):
                LOGGER.info("User %s is unreachable on bot %s; stopping delivery", user_id, bot_id)
                return
            except Exception:
                LOGGER.exception("File send error for bot %s", bot_id)
            await asyncio.sleep(0.5)
    finally:
        DELIVERING.discard((bot_id, user_id))


# ═══════════════════════════ admins: /genlink, /batch and link shortening ═══════════════════════════
async def link_command(client, message):
    """/genlink asks for one DB message, /batch for the first and the last one."""
    if not message.from_user or not is_admin(client, message.from_user.id):
        return
    batch = "batch" in message.command[0].lower()
    STATES[(client.bot_id, message.from_user.id)] = {"batch": batch, "first": None}
    await message.reply("📨 Forward the <b>first</b> DB message or send its link." if batch else "📨 Forward the <b>DB message</b> or send its link.")


async def link_input(client, message, state):
    """Next step of a /genlink or /batch conversation."""
    key = (client.bot_id, message.from_user.id)
    message_id = get_message_id(client, message)
    if not message_id:
        return await message.reply("❌ Invalid DB message. Forward a message from the files channel or send its link.")

    if state["batch"] and state["first"] is None:
        state["first"] = message_id
        return await message.reply("📩 Forward the <b>last</b> DB message or send its link.")

    STATES.pop(key, None)
    ids = sorted((state["first"], message_id)) if state["batch"] else [message_id]
    channel = abs(client.db_channel.id)
    payload = "get-" + "-".join(str(i * channel) for i in ids)
    link = f"https://telegram.me/{client.username}?start={encode(payload)}"
    short = await get_shortlink(client, link)

    await message.reply(
        f"🔗 <b>Link Generated</b>\n{SEP}\n\n📎 <b>Link:</b> {link}\n\n⚡ <b>Short Link:</b> <code>{short}</code>",
        disable_web_page_preview=True)


async def admin_text(client, message):
    """Any other text from an admin: continue /genlink or /batch, otherwise shorten the first link in it."""
    if not message.from_user or not is_admin(client, message.from_user.id):
        return

    state = STATES.get((client.bot_id, message.from_user.id))
    if state:
        return await link_input(client, message, state)

    original = message.text or message.caption or ""
    match = re.search(r"https?://\S+", original)
    if not match:
        return

    link = match.group(0).rstrip(".,!?)]}>\"'")
    link = re.sub(r"^https?://(?:t|telegram)\.me/", "https://telegram.me/", link)
    link = link.replace("telegram.me/+", "telegram.me/%2B")
    short = await get_shortlink(client, link)

    await message.reply_text(
        f"⚡ <b>Short Link Ready</b>\n{SEP}\n\n📎 <b>Original:</b> {original}\n\n🔗 <b>Short:</b> <code>{short}</code>",
        quote=True, disable_web_page_preview=True)


# ═══════════════════════════ admins: shortener ═══════════════════════════
async def shortener_command(client, message):
    """/shortener shows the current shortener."""
    if not message.from_user or not is_admin(client, message.from_user.id):
        return
    cfg = client.cfg
    await message.reply(
        f"🔗 <b>Shortener</b>\n{SEP}\n\n"
        f"🌍 Site: <code>{cfg['shortsite'] or 'Not set'}</code>\n"
        f"🔑 API: <code>{cfg['shortapi'] or 'Not set'}</code>\n\n"
        f"💡 Change: <code>/shortlink site.com API</code>")


async def shortlink_command(client, message):
    """/shortlink site.com API changes the shortener."""
    if not message.from_user or not is_admin(client, message.from_user.id):
        return
    args = message.text.split()
    if len(args) != 3:
        return await message.reply("🔗 <b>Usage</b>\n<code>/shortlink site.com API</code>")
    site, api = re.sub(r"^https?://", "", args[1]).rstrip("/"), args[2]

    await run(put, client.bot_id, shortsite=site, shortapi=api)
    client.cfg.update(shortsite=site, shortapi=api)
    await message.reply(f"✅ <b>Shortener Updated</b>\n{SEP}\n\n🌍 Site: <code>{site or 'Off'}</code>\n🔑 API: <code>{api or 'Off'}</code>")


# ═══════════════════════════ admins: broadcast and stats ═══════════════════════════
async def broadcast_command(client, message):
    if not message.from_user or not is_admin(client, message.from_user.id):
        return
    source = message.reply_to_message
    if not source:
        return await message.reply("↩️ Reply to the message you want to broadcast.")
    if client.bot_id in BROADCASTS:
        return await message.reply("⏳ A broadcast is already running.")

    state = BROADCASTS[client.bot_id] = {"cancel": False}  # claim the lock first
    try:
        users = await run(all_users, client.bot_id)
        status = await message.reply(
            f"📢 <b>Broadcasting…</b>\n{SEP}\n\n👥 Total: <code>{len(users)}</code>\n✅ Sent: <code>0</code>",
            reply_markup=CANCEL_BUTTON)
    except Exception:
        BROADCASTS.pop(client.bot_id, None)
        raise
    spawn(run_broadcast(client.bot_id, source, status, users, state))


async def run_broadcast(bot_id, source, status, users, state):
    count = {"sent": 0, "blocked": 0, "deleted": 0, "failed": 0}

    def report(title):
        remaining = len(users) - sum(count.values())
        return (f"{title}\n{SEP}\n\n👥 Total: <code>{len(users)}</code>\n"
                f"✅ Sent: <code>{count['sent']}</code>\n🚫 Blocked: <code>{count['blocked']}</code>\n"
                f"👻 Deleted: <code>{count['deleted']}</code>\n❌ Failed: <code>{count['failed']}</code>\n"
                f"⏳ Remaining: <code>{remaining}</code>")

    try:
        for number, user_id in enumerate(users, 1):
            if state["cancel"]:
                break
            try:
                await copy_to(source, user_id)
                count["sent"] += 1
            except UserIsBlocked:
                await run(del_user, bot_id, user_id)
                count["blocked"] += 1
            except InputUserDeactivated:
                await run(del_user, bot_id, user_id)
                count["deleted"] += 1
            except Exception:
                LOGGER.exception("Broadcast failed for user %s on bot %s", user_id, bot_id)
                count["failed"] += 1

            if number % 25 == 0:
                try:
                    await status.edit(report("📢 <b>Broadcasting…</b>"), reply_markup=CANCEL_BUTTON)
                except Exception:
                    pass

        await status.edit(report("🛑 <b>Broadcast Cancelled</b>" if state["cancel"] else "✅ <b>Broadcast Completed</b>"))
    except Exception:
        LOGGER.exception("Broadcast crashed on bot %s", bot_id)
    finally:
        BROADCASTS.pop(bot_id, None)


async def cancel_broadcast(client, query):
    if not is_admin(client, query.from_user.id):
        return await query.answer("🚫 Not authorized.", show_alert=True)
    if client.bot_id in BROADCASTS:
        BROADCASTS[client.bot_id]["cancel"] = True
    await query.answer("🛑 Broadcast cancelled.")


async def stats_command(client, message):
    if not message.from_user or not is_admin(client, message.from_user.id):
        return
    limit = MONGO_LIMIT_MB * 1024 * 1024
    mine, total, users, shared = await asyncio.gather(
        run(bot_storage, client.bot_id), run(cluster_storage), run(user_count, client.bot_id),
        run(uses_default_cluster, client.bot_id))

    def size(n):
        return f"{n / 1024 / 1024:.2f} MB" if n >= 1024 * 1024 else f"{n / 1024:.2f} KB"

    text = f"📊 <b>Bot Stats</b>\n{SEP}\n\n👥 Users: <code>{users}</code>\n\n💾 This bot: <code>{size(mine)}</code>\n"
    if shared:
        text += (f"🤖 Other bots: <code>{size(max(0, total - mine))}</code>\n"
                 f"🗄 MongoDB: <code>{size(total)} / {MONGO_LIMIT_MB} MB</code>\n"
                 f"🟢 Left: <code>{size(max(0, limit - total))}</code>")
    else:
        text += "\n🗄 This bot uses its own MongoDB, so the shared limit does not apply."
    await message.reply(text)


# ═══════════════════════════ registration ═══════════════════════════
def register(client):
    """Attach every handler to a clone's client. Order matters: the catch-all text handler goes last."""
    private = filters.private
    commands = (
        (start, "start"),
        (broadcast_command, "broadcast"),
        (stats_command, "stats"),
        (shortener_command, "shortener"),
        (shortlink_command, "shortlink"),
        (link_command, ["batch", "genlink"]),
    )
    for handler, names in commands:
        client.add_handler(MessageHandler(handler, filters.command(names) & private))

    client.add_handler(MessageHandler(admin_text, (filters.text | filters.caption | filters.forwarded) & private))
    client.add_handler(CallbackQueryHandler(cancel_broadcast, filters.regex("^cancel_broadcast$")))
