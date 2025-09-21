APP_NAME = telegram-bot
DOCKER_IMAGE = $(APP_NAME):latest
DOCKER_CONTAINER = $(APP_NAME)-container

.PHONY: build run stop logs restart clean

build:
	docker build -t $(DOCKER_IMAGE) .

run:
	docker run -d \
		--name $(DOCKER_CONTAINER) \
		--restart unless-stopped \
		-v /var/run/docker.sock:/var/run/docker.sock \
		$(DOCKER_IMAGE)


stop:
	docker stop $(DOCKER_CONTAINER) || true
	docker rm $(DOCKER_CONTAINER) || true

logs:
	docker logs -f $(DOCKER_CONTAINER)

restart: stop run

clean: stop
	docker rmi $(DOCKER_IMAGE) || true