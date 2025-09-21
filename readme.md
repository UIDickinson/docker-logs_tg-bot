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
make build    # Build the Docker image
make run      # Run the container
make logs     # Tail the logs
make stop     # Stop & remove container
make restart  # Restart the bot
make clean    # Remove image + container

# Telegram cmds
- /start
- /containers
- /logs <name>
- /stream <name>
- /stop
- /status