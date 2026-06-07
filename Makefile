# Personal Ops Bot — Docker workflow.
#
#   make build   build the bot image
#   make test    run the test suite inside Docker
#   make push    rebuild and replace the running container with the new image + code
#
# `make test` mounts the repo into the built image because the Dockerfile only copies
# `ops/` (not `tests/` or the dev-only deps); pytest + pytest-asyncio are installed at
# run time into the throwaway container, so the image stays runtime-only.

COMPOSE := docker compose
SERVICE := bot

.DEFAULT_GOAL := help
.PHONY: help build test push

help:
	@echo "Targets:"
	@echo "  build   build the bot Docker image"
	@echo "  test    run the test suite inside Docker"
	@echo "  push    rebuild and replace the running container with the new image + code"

build:
	$(COMPOSE) build

# One-off container (does not disturb the running bot — the command is overridden to
# pytest, so bot.py never starts and never contends for the Telegram poll). The repo is
# bind-mounted so the current code and tests/ are present; dev deps install on top of the
# image's runtime deps.
test: build
	$(COMPOSE) run --rm --no-deps -v "$(CURDIR):/app" $(SERVICE) \
		sh -c "pip install --no-cache-dir -r requirements-dev.txt && python -m pytest --pspec"

# Rebuild and recreate the running container from the new image + code in one step.
# Depends on `test`, so a failing suite aborts the deploy before the live bot is touched.
push: test
	$(COMPOSE) up -d --build
