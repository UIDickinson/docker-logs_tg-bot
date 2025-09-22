.PHONY: build up logs shell test run install

install:
	python -m pip install --upgrade pip
	python -m pip install -r requirements.txt

build:
	docker build -t telegram-docker-monitor:latest .

up:
	docker-compose up -d --build

logs:
	docker-compose logs -f

shell:
	docker-compose exec tbot /bin/sh

run:
	python bot.py

test:
	bash test_create_logger.sh
