# Telegram Docker Monitor — Full Implementation
#!/usr/bin/env python3
"""
Telegram Docker Monitor Bot
- Lists containers
- Fetches last N lines of logs
- Streams logs in (near) real-time via polling (safe inside containers)
- Shows container details
- Restricts access to authorized Telegram user IDs

Configuration via environment variables:
- TELEGRAM_TOKEN : your bot token
- ALLOWED_USERS  : comma-separated Telegram numeric user IDs (e.g. 12345678,87654321)
- LOG_POLL_INTERVAL (optional) : how often to poll logs in seconds (default 1)
- STREAM_RATE_LIMIT (optional) : minimum seconds between sending batched messages (default 2)

Run locally: python bot.py

"""

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
if os.getenv("USE_MOCK_DOCKER"):
    from unittest.mock import MagicMock
    import datetime
    import random

    class MockContainer:
        def __init__(self, name, image, status):
            self.name = name
            self.id = f"{name[:3]}_{random.randint(1000,9999)}"
            self.image = type("Image", (), {"tags": [image]})
            self.status = status
            self.attrs = {
                "Created": datetime.datetime.now().isoformat(),
                "Config": {"Image": image},
                "State": {"Status": status},
            }

        def logs(self, tail=50, stream=False, timestamps=True):
            if stream:
                for i in range(1, 100):
                    yield f"{datetime.datetime.now().isoformat()} mock log line {i} from {self.name}\n".encode()
            else:
                return "\n".join(
                    f"{datetime.datetime.now().isoformat()} mock log line {i} from {self.name}"
                    for i in range(1, tail + 1)
                ).encode()

    class MockDockerClient:
        def __init__(self):
            self.containers = self

            # Fake containers
            self._containers = [
                MockContainer("web_app", "nginx:latest", "running"),
                MockContainer("db", "postgres:14", "exited"),
                MockContainer("worker", "python:3.12", "running"),
            ]

        def list(self, all=True):
            return self._containers

        def get(self, name_or_id):
            for c in self._containers:
                if c.name == name_or_id or c.id.startswith(name_or_id):
                    return c
            raise Exception(f"No such container: {name_or_id}")

    docker_client = MockDockerClient()
    print("⚠️ Using MOCK Docker client with fake containers/logs.")

else:
    try:
        import docker
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


# ---------------------- Command Handlers ----------------------

async def cmd_container(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update, context):
        return
    chat = update.effective_chat
    containers = await asyncio.to_thread(docker_client.containers.list, True)
    if not containers:
        await context.bot.send_message(chat.id, "No Docker containers found.")
        return

    lines = []
    keyboard = []
    for c in containers:
        # get a friendly name; container.name can raise if object stale, so guard
        try:
            name = c.name
        except Exception:
            name = (c.attrs.get("Name") or "").lstrip("/")
        status = getattr(c, "status", c.attrs.get("State", {}).get("Status", "unknown"))
        image = getattr(c, "image", None)
        image_name = image.tags[0] if image and image.tags else c.image.id if hasattr(c, "image") else "<unknown>"
        lines.append(f"• <b>{name}</b> — <code>{status}</code> — {image_name}")
        # buttons: logs, stream, status
        keyboard.append(
            [
                InlineKeyboardButton(f"Logs", callback_data=f"logs:{c.id}"),
                InlineKeyboardButton(f"Stream", callback_data=f"stream:{c.id}"),
                InlineKeyboardButton(f"Status", callback_data=f"status:{c.id}"),
            ]
        )

    text = "<b>Docker containers</b>\n" + "\n".join(lines)

    reply_markup = InlineKeyboardMarkup(keyboard)
    await context.bot.send_message(chat.id, text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)


async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update, context):
        return
    chat = update.effective_chat
    if not context.args:
        await context.bot.send_message(chat.id, "Usage: /logs <container-name-or-id>")
        return
    identifier = context.args[0]
    try:
        container = await find_container(identifier)
    except NotFound:
        await context.bot.send_message(chat.id, f"Container '{identifier}' not found.")
        return

    # fetch last 50 lines
    try:
        raw = await asyncio.to_thread(container.logs, tail=50, stdout=True, stderr=True)
    except Exception as e:
        await context.bot.send_message(chat.id, f"Error fetching logs: {e}")
        return
    if not raw:
        await context.bot.send_message(chat.id, "(No logs yet)")
        return

    text = raw.decode(errors="replace")
    # split if too long
    for chunk in split_long_message(text):
        await context.bot.send_message(chat.id, f"<pre>{chunk}</pre>", parse_mode=ParseMode.HTML)


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


async def cmd_stream(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update, context):
        return
    chat = update.effective_chat
    if not context.args:
        await context.bot.send_message(chat.id, "Usage: /stream <container-name-or-id>")
        return
    identifier = context.args[0]
    try:
        container = await find_container(identifier)
    except NotFound:
        await context.bot.send_message(chat.id, f"Container '{identifier}' not found.")
        return

    # If there's an active stream for this chat, cancel it first
    existing = active_streams.get(chat.id)
    if existing:
        existing_task = existing.get("task")
        if existing_task and not existing_task.done():
            existing_task.cancel()
            await context.bot.send_message(chat.id, "Stopping existing stream...")
            # allow a short time to cancel
            await asyncio.sleep(0.2)

    task = asyncio.create_task(_start_stream_for_chat(chat.id, container, context))
    active_streams[chat.id] = {"task": task, "container": getattr(container, "name", container.id[:12])}


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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_auth(update, context):
        return
    chat = update.effective_chat
    if not context.args:
        await context.bot.send_message(chat.id, "Usage: /status <container-name-or-id>")
        return
    identifier = context.args[0]
    try:
        container = await find_container(identifier)
    except NotFound:
        await context.bot.send_message(chat.id, f"Container '{identifier}' not found.")
        return

    try:
        # refresh attributes
        await asyncio.to_thread(container.reload)
        attrs = container.attrs
    except Exception as e:
        await context.bot.send_message(chat.id, f"Error retrieving container info: {e}")
        return

    state = attrs.get("State", {})
    created = attrs.get("Created")
    image = attrs.get("Config", {}).get("Image")
    name = attrs.get("Name", "").lstrip("/")
    status = state.get("Status")
    started_at = state.get("StartedAt")
    finished_at = state.get("FinishedAt")

    info_lines = [
        f"<b>{name}</b>",
        f"Image: <code>{image}</code>",
        f"Status: <code>{status}</code>",
        f"Created: <code>{created}</code>",
        f"StartedAt: <code>{started_at}</code>",
        f"FinishedAt: <code>{finished_at}</code>",
    ]

    # ports
    ports = attrs.get("NetworkSettings", {}).get("Ports")
    if ports:
        info_lines.append("Ports:")
        for k, v in ports.items():
            info_lines.append(f" - {k} -> {v}")

    # mounts
    mounts = attrs.get("Mounts", [])
    if mounts:
        info_lines.append("Mounts:")
        for m in mounts:
            info_lines.append(f" - {m.get('Source')} -> {m.get('Destination')}")

    text = "\n".join(info_lines)
    for chunk in split_long_message(text):
        await context.bot.send_message(chat.id, chunk, parse_mode=ParseMode.HTML)


# ---------------------- Callback Query Handler ----------------------

async def callback_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    user = update.effective_user
    if not authorized(user.id):
        await query.edit_message_text("❌ Access denied. You are not authorized to use this bot.")
        return

    if data.startswith("logs:"):
        identifier = data.split("logs:", 1)[1]
        # call logs implementation
        # reuse cmd_logs logic but pass identifier
        try:
            container = await find_container(identifier)
        except NotFound:
            await query.message.reply_text(f"Container '{identifier}' not found.")
            return
        raw = await asyncio.to_thread(container.logs, tail=50, stdout=True, stderr=True)
        if not raw:
            await query.message.reply_text("(No logs yet)")
            return
        text = raw.decode(errors="replace")
        for chunk in split_long_message(text):
            await query.message.reply_text(f"<pre>{chunk}</pre>", parse_mode=ParseMode.HTML)

    elif data.startswith("stream:"):
        identifier = data.split("stream:", 1)[1]
        try:
            container = await find_container(identifier)
        except NotFound:
            await query.message.reply_text(f"Container '{identifier}' not found.")
            return
        # start stream for this chat
        chat_id = query.message.chat_id
        # stop existing
        existing = active_streams.get(chat_id)
        if existing:
            existing_task = existing.get("task")
            if existing_task and not existing_task.done():
                existing_task.cancel()
                await context.bot.send_message(chat_id, "Stopping existing stream...")
                await asyncio.sleep(0.2)
        task = asyncio.create_task(_start_stream_for_chat(chat_id, container, context))
        active_streams[chat_id] = {"task": task, "container": getattr(container, "name", container.id[:12])}

    elif data.startswith("status:"):
        identifier = data.split("status:", 1)[1]
        try:
            container = await find_container(identifier)
        except NotFound:
            await query.message.reply_text(f"Container '{identifier}' not found.")
            return
        await query.message.reply_text("Fetching status...")
        # reuse cmd_status logic
        try:
            await asyncio.to_thread(container.reload)
            attrs = container.attrs
        except Exception as e:
            await query.message.reply_text(f"Error retrieving container info: {e}")
            return
        state = attrs.get("State", {})
        created = attrs.get("Created")
        image = attrs.get("Config", {}).get("Image")
        name = attrs.get("Name", "").lstrip("/")
        status = state.get("Status")
        started_at = state.get("StartedAt")
        finished_at = state.get("FinishedAt")
        info_lines = [
            f"<b>{name}</b>",
            f"Image: <code>{image}</code>",
            f"Status: <code>{status}</code>",
            f"Created: <code>{created}</code>",
            f"StartedAt: <code>{started_at}</code>",
            f"FinishedAt: <code>{finished_at}</code>",
        ]
        text = "\n".join(info_lines)
        for chunk in split_long_message(text):
            await query.message.reply_text(chunk, parse_mode=ParseMode.HTML)

    else:
        await query.message.reply_text("Unknown action")


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
