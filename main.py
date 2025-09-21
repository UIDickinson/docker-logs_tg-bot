#!/usr/bin/env python3
"""
main.py - Telegram Docker Logs Bot (async)

Features:
- /start, /containers, /logs <name>, /stream <name>, /stop, /status
- inline buttons to quickly request logs/stream
- whitelist-based auth
- per-user single active stream, token-bucket rate limiting + batching
- uses docker.APIClient for streaming and runs blocking IO in ThreadPoolExecutor
"""

import os
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional
from functools import partial
from concurrent.futures import ThreadPoolExecutor

import docker
from docker.errors import NotFound, APIError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from dotenv import load_dotenv

load_dotenv()

# Configuration from env
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ALLOWED_USERS = [int(x.strip()) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()]
LOG_TAIL = int(os.environ.get("LOG_TAIL", "50"))
MAX_MSG_CHARS = int(os.environ.get("MAX_MSG_CHARS", "2000"))
STREAM_FLUSH_SECONDS = float(os.environ.get("STREAM_FLUSH_SECONDS", "2.0"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required in environment")

# Logging
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("docker-logs-bot")

# Docker client (low-level APIClient for streaming)
DOCKER_CLIENT = docker.APIClient(base_url="unix://var/run/docker.sock")

# ThreadPool for blocking docker SDK operations (reads + streaming)
EXECUTOR = ThreadPoolExecutor(max_workers=6)

# Active streams: user_id -> StreamSession
active_streams: Dict[int, "StreamSession"] = {}

# Simple per-user token-bucket limiter
class TokenBucket:
    def __init__(self, rate: float, capacity: int):
        self.rate = rate  # tokens per second
        self.capacity = capacity
        self.tokens = capacity
        self.last = asyncio.get_event_loop().time()

    def consume(self, amount=1) -> bool:
        now = asyncio.get_event_loop().time()
        elapsed = now - self.last
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False

class StreamSession:
    def __init__(self, user_id: int, chat_id: int, container_name: str, app: Application):
        self.user_id = user_id
        self.chat_id = chat_id
        self.container_name = container_name
        self.app = app
        self.cancel_event = asyncio.Event()
        self.buffer = []
        self.buffer_lock = asyncio.Lock()
        self.last_flush = asyncio.get_event_loop().time()
        self.token_bucket = TokenBucket(rate=1.0, capacity=5)  # example: 1 token/sec, burst 5
        self.task: Optional[asyncio.Task] = None

    async def add_line(self, line: str):
        async with self.buffer_lock:
            self.buffer.append(line)
        # Possibly flush
        now = asyncio.get_event_loop().time()
        if (now - self.last_flush) >= STREAM_FLUSH_SECONDS or len("\n".join(self.buffer)) > 1000:
            await self.flush()

    async def flush(self):
        async with self.buffer_lock:
            if not self.buffer:
                return
            text = "\n".join(self.buffer)
            self.buffer = []
        self.last_flush = asyncio.get_event_loop().time()
        # Rate-limit sending: try to consume a token, otherwise drop older messages and send minimal notice
        if not self.token_bucket.consume():
            # If exhausted, combine into a short message
            truncated = text[-MAX_MSG_CHARS:]
            if len(truncated) < len(text):
                truncated = "...(truncated)\n" + truncated
            try:
                await self.app.bot.send_message(chat_id=self.chat_id, text=f"```\n{truncated}\n```", parse_mode="Markdown")
            except Exception as e:
                logger.exception("Error sending rate-limited flush: %s", e)
            return

        # Trim to safe length
        if len(text) > MAX_MSG_CHARS:
            text = "...(truncated)\n" + text[-MAX_MSG_CHARS:]
        try:
            await self.app.bot.send_message(chat_id=self.chat_id, text=f"```\n{text}\n```", parse_mode="Markdown")
        except Exception as e:
            logger.exception("Error sending log flush: %s", e)

    async def stop(self):
        self.cancel_event.set()
        if self.task:
            self.task.cancel()
        # flush remaining buffer
        await self.flush()

# Helper: auth
def is_allowed(user_id: Optional[int]) -> bool:
    if not user_id:
        return False
    return user_id in ALLOWED_USERS

# Utility: run blocking docker calls in executor
async def run_blocking(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(EXECUTOR, partial(func, *args, **kwargs))

# Command handlers
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_allowed(uid):
        await update.message.reply_text("❌ Unauthorized — you are not allowed to use this bot.")
        return
    help_text = (
        "🐳 *Docker Logs Bot*\n\n"
        "Commands:\n"
        "/containers - List containers\n"
        "/logs <name> - Get recent logs (tail)\n"
        "/stream <name> - Stream live logs\n"
        "/stop - Stop active stream\n"
        "/status <name> - Check container status\n\n"
        "You can also use inline buttons from /containers for quick actions."
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def containers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_allowed(uid):
        return
    try:
        containers = await run_blocking(DOCKER_CLIENT.containers, all=True)  # this returns - low-level: warning, not same as high-level
    except Exception:
        # fallback to listing via high-level client
        try:
            low = docker.from_env()
            containers = await run_blocking(low.containers.list, True)
        except Exception as e:
            logger.exception("Error listing containers: %s", e)
            await update.message.reply_text(f"❌ Error listing containers: {e}")
            return

    # If we got low-level list as list of dicts or high-level objects, normalize
    items = []
    for c in containers:
        try:
            # high-level container (has .name, .status)
            name = getattr(c, "name", None) or (c.attrs["Name"].lstrip("/") if isinstance(c.attrs, dict) and "Name" in c.attrs else None)
            status = getattr(c, "status", None) or (c.attrs.get("State", {}).get("Status") if isinstance(getattr(c, "attrs", None), dict) else "unknown")
            items.append((name, status))
        except Exception:
            continue

    if not items:
        await update.message.reply_text("📦 No containers found.")
        return

    msg_lines = []
    keyboard = []
    added = 0
    for name, status in items:
        emoji = "🟢" if status == "running" else "🔴"
        msg_lines.append(f"{emoji} `{name}` — {status}")
        # create inline buttons for running containers only to avoid starting streams on stopped ones
        if name and status == "running" and added < 10:
            keyboard.append([
                InlineKeyboardButton(f"📋 Logs: {name}", callback_data=f"logs|{name}"),
                InlineKeyboardButton(f"🔴 Stream: {name}", callback_data=f"stream|{name}")
            ])
            added += 1

    text = "*Available containers:*\n\n" + "\n".join(msg_lines)
    reply_markup = InlineKeyboardMarkup(keyboard) if keyboard else None
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_allowed(uid):
        return
    if not context.args:
        await update.message.reply_text("Usage: /logs <container_name>")
        return
    container_name = context.args[0]
    try:
        raw = await run_blocking(DOCKER_CLIENT.logs, container=container_name, tail=LOG_TAIL)
        if isinstance(raw, bytes):
            logs = raw.decode("utf-8", errors="replace")
        else:
            logs = str(raw)
        if not logs.strip():
            await update.message.reply_text(f"📝 No recent logs for `{container_name}`", parse_mode="Markdown")
            return
        if len(logs) > MAX_MSG_CHARS:
            logs = "...(truncated)\n" + logs[-MAX_MSG_CHARS:]
        await update.message.reply_text(f"```\n{logs}\n```", parse_mode="Markdown")
    except APIError as e:
        await update.message.reply_text(f"❌ Docker API error: {e.explanation if hasattr(e,'explanation') else str(e)}")
    except NotFound:
        await update.message.reply_text(f"❌ Container `{container_name}` not found.")
    except Exception as e:
        logger.exception("Error fetching logs: %s", e)
        await update.message.reply_text(f"❌ Error getting logs: {e}")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_allowed(uid):
        return
    if not context.args:
        await update.message.reply_text("Usage: /status <container_name>")
        return
    container_name = context.args[0]
    try:
        info = await run_blocking(DOCKER_CLIENT.inspect_container, container_name)
        status = info.get("State", {}).get("Status", "unknown")
        created = info.get("Created", "")
        image = info.get("Config", {}).get("Image", "N/A")
        msg = (
            f"*Container status*\n\n"
            f"*Name:* `{container_name}`\n"
            f"*Status:* `{status}`\n"
            f"*Image:* `{image}`\n"
            f"*Created:* `{created}`"
        )
        await update.message.reply_text(msg, parse_mode="Markdown")
    except NotFound:
        await update.message.reply_text(f"❌ Container `{container_name}` not found.")
    except Exception as e:
        logger.exception("Error inspecting container: %s", e)
        await update.message.reply_text(f"❌ Error: {e}")

# Streaming: creates StreamSession and starts background job that consumes docker API logs stream using executor
async def stream_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_allowed(uid):
        return
    if not context.args:
        await update.message.reply_text("Usage: /stream <container_name>")
        return
    container_name = context.args[0]
    # stop existing stream for user
    if uid in active_streams:
        await update.message.reply_text("🔁 Stopping existing stream first...")
        sess = active_streams[uid]
        await sess.stop()
        del active_streams[uid]

    # check container exists and running
    try:
        info = await run_blocking(DOCKER_CLIENT.inspect_container, container_name)
        status = info.get("State", {}).get("Status")
        if status != "running":
            await update.message.reply_text(f"❌ Container `{container_name}` is not running (status: {status}).")
            return
    except NotFound:
        await update.message.reply_text(f"❌ Container `{container_name}` not found.")
        return
    except Exception as e:
        logger.exception("Error inspecting container for stream: %s", e)
        await update.message.reply_text(f"❌ Error: {e}")
        return

    await update.message.reply_text(f"🔴 Starting stream for `{container_name}`. Send /stop to end.", parse_mode="Markdown")
    session = StreamSession(user_id=uid, chat_id=update.effective_chat.id, container_name=container_name, app=context.application)
    active_streams[uid] = session
    session.task = asyncio.create_task(_stream_worker(session))

async def _stream_worker(session: StreamSession):
    """
    Runs blocking docker.logs(stream=True, follow=True) in executor; pushes lines to session buffer.
    Cancels when session.cancel_event is set.
    """
    container = session.container_name
    logger.info("Stream worker starting for user %s container %s", session.user_id, container)
    loop = asyncio.get_event_loop()

    # blocking call that yields log lines; run in threadpool
    def blocking_stream():
        try:
            # follow logs as bytes; since we mounted socket ro, this is read-only
            for chunk in DOCKER_CLIENT.logs(container=container, stream=True, follow=True, tail=0):
                # chunk is bytes
                yield chunk
        except Exception as e:
            # propagate a marker exception
            raise

    try:
        # create generator in executor
        gen = await loop.run_in_executor(EXECUTOR, lambda: blocking_stream())
        # iterate generator but still process asynchronously
        # unlike calling next(gen) in executor, we'll pull chunks by scheduling next in executor to avoid blocking loop
        it = iter(gen)
        while not session.cancel_event.is_set():
            try:
                chunk = await loop.run_in_executor(EXECUTOR, lambda: next(it))
            except StopIteration:
                break
            except Exception as e:
                logger.exception("Error while streaming: %s", e)
                await session.app.bot.send_message(chat_id=session.chat_id, text=f"❌ Stream error: {e}")
                break

            if not chunk:
                continue
            # decode
            try:
                text = chunk.decode("utf-8", errors="replace").strip()
            except Exception:
                text = str(chunk)
            if not text:
                continue
            ts = datetime.utcnow().strftime("%H:%M:%S")
            await session.add_line(f"{ts} | {text}")

        # final flush
        await session.flush()
    except asyncio.CancelledError:
        logger.info("Stream worker cancelled for user %s", session.user_id)
    except Exception as e:
        logger.exception("Unhandled stream worker error: %s", e)
        try:
            await session.app.bot.send_message(chat_id=session.chat_id, text=f"❌ Stream terminated due to error: {e}")
        except Exception:
            pass
    finally:
        # cleanup
        logger.info("Stream worker ended for user %s", session.user_id)
        if session.user_id in active_streams:
            del active_streams[session.user_id]

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update.effective_user else None
    if not is_allowed(uid):
        return
    if uid not in active_streams:
        await update.message.reply_text("⏹️ No active stream to stop.")
        return
    session = active_streams[uid]
    await session.stop()
    del active_streams[uid]
    await update.message.reply_text(f"⏹️ Stopped streaming `{session.container_name}`.")

# Callback handler for inline buttons
async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id if query.from_user else None
    if not is_allowed(uid):
        await query.edit_message_text("❌ Unauthorized")
        return
    data = query.data or ""
    try:
        action, name = data.split("|", 1)
    except ValueError:
        await query.edit_message_text("❌ Invalid action")
        return

    if action == "logs":
        # emulate /logs
        context.args = [name]
        await logs_cmd(update, context)
    elif action == "stream":
        context.args = [name]
        await stream_cmd(update, context)
    else:
        await query.edit_message_text("❌ Unknown action")

# Global error handler
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Exception while handling update: %s", context.error)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("containers", containers_cmd))
    app.add_handler(CommandHandler("logs", logs_cmd))
    app.add_handler(CommandHandler("stream", stream_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_error_handler(error_handler)

    logger.info("Starting bot")
    app.run_polling()

if __name__ == "__main__":
    main()
