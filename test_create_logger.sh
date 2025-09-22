#!/usr/bin/env bash
# Creates a small background container that spits a timestamped line every second.
# Useful for testing the bot's streaming functionality.
set -e

CONTAINER_NAME=test-logger-$(date +%s)

docker run -d --name "$CONTAINER_NAME" alpine sh -c "while true; do date -u '+%Y-%m-%dT%H:%M:%SZ - test-logger' ; sleep 1; done"

echo "Started test container: $CONTAINER_NAME"

echo "To stop it: docker rm -f $CONTAINER_NAME"
