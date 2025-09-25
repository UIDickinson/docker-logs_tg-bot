# This bot features:
1. Interactive inline buttons for quick container selection

2. Restricted access to authorized users (by Telegram user ID set via env)

3. Commands: list containers, recent logs, stream logs, stop streaming, status

4. Robust error handling and user feedback

5. Per-user rate limiting / message batching (token-bucket + buffer flush)

6. Safe truncation of long outputs

7. Graceful shutdown

## Requirements
- Python 3.10+
- Docker daemon (if running locally)
- A Telegram bot token
- Your Telegram numeric user ID(s) to authorize

**Important:** The container mounts `/var/run/docker.sock` read-only so the bot can talk to the host Docker engine. This is powerful — secure your deployment!

## Configuration via environment variables:
- TELEGRAM_TOKEN : your bot token
- ALLOWED_USERS  : comma-separated Telegram numeric user IDs (e.g. 12345678,87654321)
- LOG_POLL_INTERVAL (optional) : how often to poll logs in seconds (default 1)
- STREAM_RATE_LIMIT (optional) : minimum seconds between sending batched messages (default 2)

since this already has a Makefile, use its these commands to setup docker and run the bot

# Cmd lines:
> make help

this would show you cmds to enter in order to setup after you've cloned this repo in a new dir (cd telegram-docker-bot)


## Usage (Telegram commands)
- `/container` — list containers with inline action buttons for logs/stream/status
- `/logs <container>` — fetch last 50 lines
- `/stream <container>` — start real-time stream (polling-based)
- `/stop` — stop active stream
- `/status <container>` — show container details

## Testing
- Run `bash test_create_logger.sh` to create a test container that prints a timestamp every second. Use `/container` or `/stream <container-name>` to test.

## Security notes
- Only users in `ALLOWED_USERS` can use the bot.
- Running this bot with `/var/run/docker.sock` mounted gives it powerful control. Prefer to run on a dedicated VM or manage access carefully.

## You can expand yours to
- Add per-user rate-limits, multiple simultaneous subscriptions, log filtering (grep), or saving logs to persistent storage.