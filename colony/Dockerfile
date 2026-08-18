# Colony's sim server, for the public demo URL (§6.1, §6.4).
#
# One process: the 4 Hz tick loop, the websocket broadcast, the commander
# console and the static client. The frontend is served from here rather than
# from S3 because it is 200 KB of static files and a second origin would buy a
# CORS problem and nothing else at this size.
#
# Runs with no AWS credentials: `adapter_from_env` defaults to replay and the
# committed cassette carries Claude's recorded decisions, so the deployed demo
# shows real Bedrock output without a key on the box. Give it credentials and
# COLONY_BEDROCK_MODE=live if you want it calling AWS for real.
#
# The commander console's **free-form tier is off** in that credential-less
# configuration, and deliberately: it runs a live Bedrock loop, which a cassette
# cannot stand in for, and it reads through the Managed MCP Server, which needs
# an OAuth refresh token. The six canned questions still answer, and the console
# says which tier it is missing and why. To turn it on, the container needs
# AWS credentials, CRDB_CLUSTER_ID, and ~/.colony/mcp-token.json mounted in.
#
# Fleet memory comes from COLONY_DSN. Set it — with no DSN the server falls back
# to in-memory memory, and a deployed demo on the fake would be the exact
# failure `_make_memory` refuses for named clusters.
#
#   docker build -t colony colony/
#   docker run -p 8000:8000 -e COLONY_DSN='postgresql://...' colony

FROM python:3.13-slim AS base

# psycopg[binary] ships its own libpq, so there is no build toolchain here and
# nothing to compile. curl is for the healthcheck below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.115" "uvicorn[standard]>=0.32" "psycopg[binary]>=3.2"

# `colony/skills/` is gitignored, so this copies it only when the builder ran
# `make skills` first. Absent, the agent still starts and answers with an empty
# catalogue — `console/skills.py` degrades rather than raising — so a forgotten
# fetch costs the skills tool, not the build.
COPY . .

# Not root. The container runs a websocket server exposed to the internet for a
# month of judging; there is no reason for it to be able to write its own code.
RUN useradd --create-home --uid 10001 colony \
    && chown -R colony:colony /app
USER colony

EXPOSE 8000

# The mission keeps running whether or not anyone is watching, so liveness is
# "does it answer", not "is a viewer attached". /health reports the tick, the
# memory backend and Bedrock's effective mode, which is what a restarted
# container should be judged on.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# One worker, deliberately. The mission is in-process state — the world, the
# agents, the viewer set — so a second worker would serve a second, different
# mission to half the browsers.
CMD ["python", "-m", "uvicorn", "sim.server:app", \
     "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
