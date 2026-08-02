.PHONY: up down schema test map dev sim cluster-3 cluster-3-health cluster-3-kill cluster-3-down

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

test:
	uv run --extra dev pytest -q

map:
	uv run python -m world.build_aftershock
sim:
	uv run --extra dev uvicorn sim.server:app --reload --port 8000

# --- chaos rig: 3-node cluster (§6.5, FR-11) ---------------------------------
cluster-3:
	../infra/cluster3.sh up

cluster-3-health:
	../infra/cluster3.sh health

cluster-3-kill:
	../infra/cluster3.sh kill

cluster-3-down:
	../infra/cluster3.sh down
