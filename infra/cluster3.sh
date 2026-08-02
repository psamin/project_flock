#!/bin/bash
# Three-node cluster lifecycle for the chaos rig (PRD §6.5).
#
#   ./infra/cluster3.sh up      start, initialise, apply the schema
#   ./infra/cluster3.sh health  report each node's health (exit 1 if any is down)
#   ./infra/cluster3.sh kill    kill node 2 — the demo beat
#   ./infra/cluster3.sh revive  bring node 2 back
#   ./infra/cluster3.sh down    tear it all down
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE="docker compose -f $ROOT/infra/docker-compose.3node.yml -p colony3"
NODES=(crdb-1 crdb-2 crdb-3)
SCHEMA="$ROOT/colony/schema/v1_1.sql"

case "${1:-}" in
  up)
    $COMPOSE up -d
    # Wait for all three nodes, not just for crdb-1 to answer. A node responds
    # before the cluster has formed, so a one-node check let the schema apply to
    # a partly-joined cluster and left the rig quietly running on fewer nodes
    # than the node-kill segment assumes.
    for _ in $(seq 1 90); do
      live=$($COMPOSE exec -T crdb-1 ./cockroach node status --insecure \
        --format=csv 2>/dev/null | awk -F, 'NR > 1 && $NF == "true" { n++ } END { print n + 0 }')
      [ "$live" = "3" ] && break
      sleep 2
    done
    if [ "${live:-0}" != "3" ]; then
      echo "only ${live:-0}/3 nodes joined — not applying the schema" >&2
      exit 1
    fi
    $COMPOSE exec -T crdb-1 ./cockroach sql --insecure \
      -e "CREATE DATABASE IF NOT EXISTS colony"
    $COMPOSE exec -T crdb-1 ./cockroach sql --insecure -d colony \
      --set errexit=true < "$SCHEMA"
    echo "3-node cluster ready: postgresql://root@localhost:26257/colony?sslmode=disable"
    ;;
  health)
    live=0
    for node in "${NODES[@]}"; do
      if $COMPOSE exec -T "$node" ./cockroach node status --insecure \
           >/dev/null 2>&1; then
        echo "$node: up"
        live=$((live + 1))
      else
        echo "$node: down"
      fi
    done
    echo "$live/3 nodes up"
    [ "$live" -eq 3 ]
    ;;
  nodes)
    # Machine-readable: how many nodes the cluster itself considers live.
    #
    # Via `node status` rather than crdb_internal.gossip_nodes: v26.2 restricts
    # crdb_internal and system ("unsupported in production"), so the query works
    # on an older build and fails on the one §6.3 pinned — the worst kind of
    # difference to discover while filming.
    $COMPOSE exec -T crdb-1 ./cockroach node status --insecure --format=csv \
      2>/dev/null | awk -F, 'NR > 1 && $NF == "true" { n++ } END { print n + 0 }'
    ;;
  kill)
    $COMPOSE kill crdb-2
    echo "crdb-2 killed — the fleet should keep claiming and completing"
    ;;
  revive)
    $COMPOSE start crdb-2
    echo "crdb-2 restarted"
    ;;
  down)
    $COMPOSE down -v
    ;;
  *)
    echo "usage: $0 {up|health|nodes|kill|revive|down}" >&2
    exit 1
    ;;
esac
