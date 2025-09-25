# ============================
# Docker Monitor Telegram Bot
# ============================

PYTHON = python3
APP = main.py
PIDFILE = .bot.pid
VENV = venv
REQ = requirements.txt

.PHONY: help install run run-bg stop clean lint mock up down logs

# ----------------------------------
# Show available commands
# ----------------------------------
help:
	@echo "Available targets:"
	@echo "  make install   - Install dependencies in venv"
	@echo "  make run       - Run bot in foreground"
	@echo "  make run-bg    - Run bot in background (like ctrl+A+D)"
	@echo "  make stop      - Stop bot if running in background"
	@echo "  make mock      - Run in mock mode (no Docker needed)| Not available"
	@echo "  make lint      - Run flake8 lint checks"
	@echo "  make clean     - Remove cache, venv, pidfile"
	@echo "  make up        - Start services with docker-compose"
	@echo "  make down      - Stop services with docker-compose"
	@echo "  make logs      - View bot logs via docker-compose"

# ----------------------------------
# Local Python (venv)
# ----------------------------------
install:
	@test -d $(VENV) || $(PYTHON) -m venv $(VENV)
	@$(VENV)/bin/pip install --upgrade pip
	@$(VENV)/bin/pip install -r $(REQ)

run:
	@$(VENV)/bin/$(PYTHON) $(APP)

run-bg:
	@echo "Starting bot in background..."
	@nohup $(VENV)/bin/$(PYTHON) $(APP) > bot.log 2>&1 & echo $$! > $(PIDFILE)
	@echo "Bot running with PID `cat $(PIDFILE)`"

stop:
	@if [ -f $(PIDFILE) ]; then \
		kill `cat $(PIDFILE)` && rm -f $(PIDFILE); \
		echo "Bot stopped."; \
	else \
		echo "No running bot found."; \
	fi

mock:
	@USE_MOCK_DOCKER=1 $(VENV)/bin/$(PYTHON) $(APP)

lint:
	@$(VENV)/bin/flake8 .

clean:
	@rm -rf __pycache__ *.pyc .pytest_cache .mypy_cache
	@rm -rf $(VENV)
	@rm -f $(PIDFILE) bot.log

# ----------------------------------
# Docker Compose
# ----------------------------------
up:
	@docker-compose up -d --build

down:
	@docker-compose down

logs:
	@docker-compose logs -f tbot