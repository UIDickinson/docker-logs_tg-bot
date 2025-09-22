# This bot features includes:
1. Real-time log streaming (Docker streaming API, non-blocking via thread executor)

2. Interactive inline buttons for quick container selection

3. Whitelisted users (by Telegram user ID set via env)

4. Commands: list containers, recent logs, stream logs, stop streaming, status

5. Robust error handling and user feedback

6. Per-user rate limiting / message batching (token-bucket + buffer flush)

7. Safe truncation of long outputs

8. Graceful shutdown

# Since this already has a Makefile, use its these commands to setup docker

# Cmd lines:
* make build    # Build the Docker image
* make run      # Run the container
* make logs     # Tail the logs
* make stop     # Stop & remove container
* make restart  # Restart the bot
* make clean    # Remove image + container

# Telegram cmds
- /start
- /containers
- /logs <name>
- /stream <name>
- /stop
- /status

# Telegram Docker Monitor

A small Telegram bot (Python) to: list containers, fetch recent logs, stream logs in near-real-time, and show container details.

## Requirements
- Python 3.10+
- Docker daemon (if running locally)
- A Telegram bot token
- Your Telegram numeric user ID(s) to authorize

## Quick local run
1. Copy `.env.example` to `.env` and set TELEGRAM_TOKEN and ALLOWED_USERS.
2. Install deps: `pip install -r requirements.txt` (or `make install`).
3. Run: `python bot.py`.

## Run with Docker Compose (recommended for VPS)

1. Set environment variables in your shell or in a `.env` file (compose will read it).
2. `make up` or `docker-compose up -d --build`

**Important:** The container mounts `/var/run/docker.sock` read-only so the bot can talk to the host Docker engine. This is powerful — secure your deployment!

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

## Extending
- Add per-user rate-limits, multiple simultaneous subscriptions, log filtering (grep), or saving logs to persistent storage.
```

---

# End of document