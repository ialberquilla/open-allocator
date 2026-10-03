.PHONY: install web types dev serve test lint

PNPM := cd apps/web && corepack pnpm

install:
	uv sync
	$(PNPM) install --frozen-lockfile

# Build the web app into apps/server/src/oa_server/web_dist, where the server serves it.
web:
	$(PNPM) build

# Regenerate the web app's API types from the server's OpenAPI document.
types:
	$(PNPM) gen:types

# Postgres, then the server with the built web app.
serve: web
	docker compose up -d --wait postgres
	uv run open-allocator-ui

# The web dev server on :5173, proxying to a server already running on :8787.
dev:
	$(PNPM) dev

test:
	uv run pytest -q

lint:
	uv run ruff check
	uv run ruff format --check
	$(PNPM) typecheck
	$(PNPM) lint
