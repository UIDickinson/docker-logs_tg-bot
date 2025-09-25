# Telegram Docker Monitor — Full Implementation
#!/usr/bin/env python3

import os
from dotenv import load_dotenv
import asyncio
import logging
import textwrap
import time
from datetime import datetime, timezone
from typing import Optional, Dict

import docker
from docker.errors import NotFound

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

load_dotenv()
# ---------------------- Configuration ----------------------
LOG_POLL_INTERVAL = float(os.getenv("LOG_POLL_INTERVAL", "1"))
STREAM_RATE_LIMIT = float(os.getenv("STREAM_RATE_LIMIT", "2"))
MAX_LINES_PER_MSG = int(os.getenv("MAX_LINES_PER_MSG", "10"))
MAX_MESSAGE_CHUNK = 3900  # safety under Telegram 4096 chars

from dotenv import dotenv_values
env = dotenv_values()

TELEGRAM_TOKEN = env.get("TELEGRAM_TOKEN")
ALLOWED_USERS = set()
if env.get("ALLOWED_USERS"):
    ALLOWED_USERS = set(int(x.strip()) for x in env.get("ALLOWED_USERS").split(",") if x.strip())

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN environment variable is required")

# ---------------------- Logging ----------------------
logging.basicConfig(
    format="[%(asctime)s] %(levelname)s: %(message)s", level=logging.INFO
)
logger = logging.getLogger("telegram-docker-monitor")

# ---------------------- Docker client ----------------------
try:
    docker_client = docker.from_env()
except Exception as e:
    logger.exception("Failed to create Docker client: %s", e)
    raise

# Active streams: chat_id -> {"task": asyncio.Task, "container": container_name_or_id}
active_streams: Dict[int, Dict] = {}

# ---------------------- Helpers ----------------------

def authorized(user_id: Optional[int]) -> bool:
    if not ALLOWED_USERS:
        # if ALLOWED_USERS is empty, be conservative and deny access
        return False
    return user_id in ALLOWED_USERS


async def require_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None or not authorized(user.id):
        await update.effective_message.reply_text("❌ Access denied. You are not authorized to use this bot.")
        return False
    return True


async def find_container(identifier: str):
    """Try to find a container by id, name or partial match. Returns container object or raises NotFound."""
    # Try exact get by id/name
    try:
        container = await asyncio.to_thread(docker_client.containers.get, identifier)
        return container
    except NotFound:
        pass

    # Try partial match against names and ids
    containers = await asyncio.to_thread(docker_client.containers.list, True)
    identifier_l = identifier.lower()
    for c in containers:
        # c.name is primary name without leading '/'
        try:
            if identifier_l == c.name.lower() or identifier_l in c.name.lower():
                return c
        except Exception:
            pass
        if identifier_l in c.short_id.lower() or identifier_l in c.id.lower():
            return c
    # not found
    raise NotFound(f"No container matching '{identifier}'")


def split_long_message(text: str, max_chunk: int = MAX_MESSAGE_CHUNK):
    """Split long text into chunks under max_chunk while preserving lines."""
    if len(text) <= max_chunk:
        return [text]
    lines = text.splitlines()
    chunks = []
    cur = []
    cur_len = 0
    for ln in lines:
        lnlen = len(ln) + 1
        if cur_len + lnlen > max_chunk and cur:
            chunks.append("\n".join(cur))
            cur = []
            cur_len = 0
        cur.append(ln)
        cur_len += lnlen
    if cur:
        chunks.append("\n".join(cur))
    return chunks


# ---------------------- Shared Helpers ----------------------

async def show_logs(chat_id: int, container, bot):
    """Send last 50 lines of logs from a container."""
    raw = await asyncio.to_thread(container.logs, tail=50, stdout=True, stderr=True)
    if not raw:
        await bot.send_message(chat_id, "(No logs yet)")
        return
    text = raw.decode(errors="replace")
    for chunk in split_long_message(text):
        await bot.send_message(chat_id, f"<pre>{chunk}</pre>", parse_mode=ParseMode.HTML)


async def show_status(chat_id: int, container, bot):
    """Send detailed status of a container."""
    await asyncio.to_thread(container.reload)
    info = (
        f"<b>{container.name}</b>\n"
        f"ID: {container.id[:12]}\n"
        f"Image: {container.image.tags[0] if container.image.tags else 'untagged'}\n"
        f"Created: {container.attrs['Created']}\n"
        f"Status: {container.status}"
    )
    await bot.send_message(chat_id, info, parse_mode=ParseMode.HTML)


async def start_stream(chat_id: int, container, bot):
    """Stream container logs live until /stop or task cancelled."""
    if chat_id in active_streams:
        await bot.send_message(chat_id, "⚠️ Stream already running. Use /stop first.")
        return

    async def stream_task():
        try:
            for line in container.logs(stream=True, follow=True, timestamps=True):
                if chat_id not in active_streams:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    await bot.send_message(chat_id, f"<pre>{text}</pre>", parse_mode=ParseMode.HTML)
                await asyncio.sleep(1)  # rate limit
        except Exception as e:
            logger.error(f"Stream error: {e}")
            await bot.send_message(chat_id, f"❌ Stream error: {e}")

    task = asyncio.create_task(stream_task())
    active_streams[chat_id] = {"task": task, "container": container.name}
    await bot.send_message(chat_id, f"▶️ Started streaming logs for <b>{container.name}</b>", parse_mode=ParseMode.HTML)

# ---------------------- Command Handlers ----------------------

from telegram import InlineKeyboardMarkup, InlineKeyboardButton

async def cmd_container(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update, context):
        return

    chat_id = update.effective_chat.id
    containers = await asyncio.to_thread(docker_client.containers.list, all=True)

    if not containers:
        await context.bot.send_message(chat_id, "No containers found.")
        return

    for container in containers:
        await asyncio.to_thread(container.reload)

        # Add emoji: 🟢 running, 🔴 stopped/other
        status = container.status
        if status == "running":
            emoji = "🟢"
        else:
            emoji = "🔴"

        text = f"{emoji} <b>{container.name}</b> - {status}"

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Logs", callback_data=f"logs:{container.name}"),
                InlineKeyboardButton("Stream", callback_data=f"stream:{container.name}"),
                InlineKeyboardButton("Status", callback_data=f"status:{container.name}"),
            ]
        ])

        await context.bot.send_message(chat_id, text, reply_markup=keyboard, parse_mode=ParseMode.HTML)


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE, identifier: str = None):
    if not await require_auth(update, context):
        return
    chat_id = update.effective_chat.id
    identifier = identifier or " ".join(context.args)
    if not identifier:
        await context.bot.send_message(chat_id, "Usage: /logs <container>")
        return
    container = await find_container(identifier)
    if not container:
        await context.bot.send_message(chat_id, f"❌ No such container: {identifier}")
        return
    await show_logs(chat_id, container, context.bot)


async def _start_stream_for_chat(chat_id: int, container, context: ContextTypes.DEFAULT_TYPE):
    """Start a polling-based stream for that chat and container. Safe to run inside container."""
    bot = context.bot
    await bot.send_message(chat_id, f"📡 Starting stream for <b>{getattr(container, 'name', container.id[:12])}</b>", parse_mode=ParseMode.HTML)

    last_ts = int(time.time())
    buffer = []
    last_sent = 0
    try:
        while True:
            try:
                # fetch logs since last_ts (adds timestamps if available)
                raw = await asyncio.to_thread(container.logs, since=last_ts, stdout=True, stderr=True, timestamps=True)
            except Exception as e:
                await bot.send_message(chat_id, f"Error reading logs: {e}")
                break

            last_ts = int(time.time())
            if raw:
                decoded = raw.decode(errors="replace").strip()
                if decoded:
                    # each line already may contain a timestamp from docker
                    for ln in decoded.splitlines():
                        buffer.append(ln)

            if buffer and (len(buffer) >= MAX_LINES_PER_MSG or (time.time() - last_sent) >= STREAM_RATE_LIMIT):
                payload = "\n".join(buffer)
                chunks = split_long_message(payload)
                for chunk in chunks:
                    await bot.send_message(chat_id, f"<pre>{chunk}</pre>", parse_mode=ParseMode.HTML)
                buffer = []
                last_sent = time.time()

            await asyncio.sleep(LOG_POLL_INTERVAL)
    except asyncio.CancelledError:
        # streaming was stopped by user
        await bot.send_message(chat_id, "🛑 Stream stopped.")
        raise
    except Exception as e:
        logger.exception("Stream error: %s", e)
        await bot.send_message(chat_id, f"Stream terminated due to error: {e}")


async def cmd_stream(update: Update, context: ContextTypes.DEFAULT_TYPE, identifier: str = None):
    if not await require_auth(update, context):
        return
    chat_id = update.effective_chat.id
    identifier = identifier or " ".join(context.args)
    if not identifier:
        await context.bot.send_message(chat_id, "Usage: /stream <container>")
        return
    container = await find_container(identifier)
    if not container:
        await context.bot.send_message(chat_id, f"❌ No such container: {identifier}")
        return
    await start_stream(chat_id, container, context.bot)


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update, context):
        return
    chat = update.effective_chat
    existing = active_streams.get(chat.id)
    if not existing:
        await context.bot.send_message(chat.id, "No active stream for this chat.")
        return
    task = existing.get("task")
    if task and not task.done():
        task.cancel()
        await context.bot.send_message(chat.id, "Requested to stop the active stream...")
        # optionally wait a moment
        await asyncio.sleep(0.2)
    active_streams.pop(chat.id, None)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE, identifier: str = None):
    if not await require_auth(update, context):
        return
    chat_id = update.effective_chat.id
    identifier = identifier or " ".join(context.args)
    if not identifier:
        await context.bot.send_message(chat_id, "Usage: /status <container>")
        return
    container = await find_container(identifier)
    if not container:
        await context.bot.send_message(chat_id, f"❌ No such container: {identifier}")
        return
    await show_status(chat_id, container, context.bot)


# ---------------------- Callback Query Handler ----------------------

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles button presses from inline keyboards."""
    query = update.callback_query
    await query.answer()

    if not await require_auth(update, context):
        return

    try:
        action, identifier = query.data.split(":", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid action.")
        return

    if action == "logs":
        await show_logs(query.message.chat_id, await find_container(identifier), context.bot)
    elif action == "stream":
        await start_stream(query.message.chat_id, await find_container(identifier), context.bot)
    elif action == "status":
        await show_status(query.message.chat_id, await find_container(identifier), context.bot)
    else:
        await query.edit_message_text("❌ Unknown action.")


# ---------------------- Startup ----------------------

async def on_startup(app):
    logger.info("Bot started; authorized users: %s", ",".join(str(x) for x in ALLOWED_USERS))


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("container", cmd_container))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("stream", cmd_stream))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(callback_query_handler))

    app.post_init = on_startup

    logger.info("Starting Telegram Docker Monitor bot...")
    app.run_polling(allowed_updates=None)


if __name__ == "__main__":
    main()
