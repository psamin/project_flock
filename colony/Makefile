.PHONY: up down schema schema-cloud test map dev sim seed-memory reset demo demo-cold \
	cluster-3 cluster-3-health cluster-3-kill cluster-3-down

# One command to a working dev cluster with the schema applied.
dev: up schema
	@echo "cluster ready: postgresql://root@localhost:26257/colony?sslmode=disable"
	@echo "admin ui:      http://localhost:8080"

up:
	docker compose up -d --wait

down:
	docker compose down -v

schema:
	docker compose exec -T cockroach ./cockroach sql --insecure \
		-e "CREATE DATABASE IF NOT EXISTS colony"
	docker compose exec -T cockroach ./cockroach sql --insecure -d colony \
		--set errexit=true < schema/v1_1.sql
	# Off by default, and a core changefeed cannot start without it (§4.4 P1,
	# fleetmem/changefeed.py). Set here so the changefeed tests are reproducible
	# rather than passing only on whichever laptop enabled it by hand.
	docker compose exec -T cockroach ./cockroach sql --insecure \
		-e "SET CLUSTER SETTING kv.rangefeed.enabled = true"

# The same schema against CockroachDB Cloud, which has no container to exec
# into. Needs COLONY_DSN — see .env.
schema-cloud:
	uv run python -m schema.apply "$$COLONY_DSN"

test:
	uv run --extra dev pytest -q

map:
	uv run python -m world.build_aftershock
sim:
	uv run --extra dev uvicorn sim.server:app --reload --port 8000

# Run missions headless so the fleet has tactics before anyone watches (§4.0
# semantic memory). Needs COLONY_DSN: a lesson written to an in-memory store
# dies with the process that wrote it.
seed-memory:
	uv run python -m sim.seed_memory

# Empty the mission tables. Asks first — the Cloud cluster is shared.
reset:
	uv run python -m schema.reset

# The demo, from a known state every time: wipe everything, run one mission
# headless to learn from (~30s), then serve a mission that draws on it. The
# scoreboard reads `tactics 3`, and clicking a robot shows which of them were in
# front of it when it decided.
#
# Deterministic because the seed is fixed: the two missions are byte-identical
# worlds with byte-identical fleets, and the only difference is the experience.
demo:
	uv run python -m schema.reset --yes
	uv run python -m sim.seed_memory
	@echo ""
	@echo "seeded. starting a mission that draws on it -> http://localhost:8000"
	@echo ""
	uv run --extra dev uvicorn sim.server:app --port 8000

# The same starting state with nothing learned, for showing the contrast.
demo-cold:
	uv run python -m schema.reset --yes
	@echo ""
	@echo "cold start, no tactics -> http://localhost:8000"
	@echo ""
	COLONY_RECALL=0 uv run --extra dev uvicorn sim.server:app --port 8000

# --- chaos rig: 3-node cluster (§6.5, FR-11) ---------------------------------
cluster-3:
	../infra/cluster3.sh up

cluster-3-health:
	../infra/cluster3.sh health

cluster-3-kill:
	../infra/cluster3.sh kill

cluster-3-down:
	../infra/cluster3.sh down
