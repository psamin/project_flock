.PHONY: up down seed test

up:
	docker compose up -d --wait

down:
	docker compose down -v

seed:
	uv run python -m harness.seed

test:
	uv run pytest -q
