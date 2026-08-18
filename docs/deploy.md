# Hosting the demo — tested runbook

§6.1 wants the demo and repo **live, working and free to access through Sept 15**
— a month past submission, not just on the day.

Everything below was run end-to-end on this machine before being written down.

## What you are deploying

Three routes off one process:

| route | what it serves | needs |
|---|---|---|
| `/` | the digital twin | WebGL 2 |
| `/2d` | the Canvas 2D renderer — the floor the twin's capability notice links to | nothing |
| `/sim3d` | alias for the twin, kept because our own docs name it | WebGL 2 |

`/2d` is not optional dead weight: it is the only page that renders on a
machine with hardware acceleration disabled, and without it the twin's WebGL
notice is a dead end.

One Python process: the 4 Hz tick loop, the websocket broadcast, the commander
console, and the static client (served from the same origin — it is ~200 KB, and
a second origin would buy a CORS problem and nothing else).

**It needs no AWS credentials.** `adapter_from_env` defaults to replay and the
committed cassette carries Claude's recorded decisions, so the deployed demo
shows real Bedrock output with **no secret on the box**. That matters for a URL
sitting up for a month: there is nothing on it to leak.

**Measured footprint** (`docker stats`, idle mission running):

| container | memory | CPU |
|---|---|---|
| colony (app) | **45 MiB** | 4% |
| cockroach (single node) | **509 MiB** | 14% |

That table decides your hosting tier. The app alone fits anywhere. CockroachDB
is what needs real RAM.

---

**What that costs.** The commander console's free-form tier is off in this
configuration. It runs a live Bedrock loop, which a cassette cannot stand in
for, and it reads through the Managed MCP Server, which needs an OAuth refresh
token. The seven canned questions still answer, and `/api/console/agent` reports
which piece is missing rather than the tier silently not being there.

That is a real trade, and it is worth deciding rather than inheriting: a judge
visiting the public URL sees the canned tier only. To turn the agent on, the
container needs AWS credentials, `CRDB_CLUSTER_ID`, and
`~/.colony/mcp-token.json` mounted in — which puts a long-lived credential on a
box that sits on the public internet for a month. Our judgement is that the
free-form tier belongs in the demo video and in a judge's local run, and the
public URL stays credential-free.

## Decide one thing first

| | **A — Cloud DB + tiny VM** ⭐ | **B — everything on one VM** |
|---|---|---|
| database | CockroachDB Cloud free tier | single node beside the app |
| VM needed | anything ≥512 MB (app is 45 MiB) | **≥2 GB** |
| EC2 free tier (`t3.micro`, 1 GB) | ✅ comfortable | ⚠️ tight — 554 MiB idle, CRDB grows under load |
| cost | $0 | $0 if the VM is free-tier |
| judging | uses **CockroachDB Cloud**, closer to the sponsor's story | self-hosted single node |
| setup | two systems | one command |

**Recommended: A.** You already have a Cloud cluster with the least-privilege
grants applied, the app is 45 MiB so a free-tier `t3.micro` is not tight, and
"runs on CockroachDB Cloud" is the better sentence in front of a judge.

Take **B** if you want one command and do not want to manage a DSN.

---

## Path A — CockroachDB Cloud + free-tier EC2

```bash
# 1. Launch an instance: Amazon Linux 2023, t3.micro, 8 GB gp3.
#    Security group inbound: 22 from your IP, 80 from 0.0.0.0/0.

# 2. On the instance
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user && exec sudo su - ec2-user

git clone https://github.com/psamin/project_flock.git
cd project_flock/colony

# 3. Apply the schema to the Cloud cluster (idempotent — safe to re-run)
docker build -t colony .
docker run --rm colony python -m schema.apply \
  'postgresql://<user>:<pass>@<host>:26257/colony?sslmode=verify-full'

# 4. Seed tactics, so SEMANTIC memory is not an empty table on camera
docker run --rm -e COLONY_DSN='postgresql://...' colony python -m sim.seed_memory

# 5. Run it. Port 80 so the URL needs no port suffix.
docker run -d --name colony --restart unless-stopped \
  -p 80:8000 \
  -e COLONY_DSN='postgresql://<user>:<pass>@<host>:26257/colony?sslmode=verify-full' \
  colony

# 6. Verify from your laptop, not from the box
curl -s http://<public-ip>/health
```

Step 4 is not optional. With no lessons, `mission_memories` is empty and
criterion #1 — Agentic Memory Design — shows one of four memory systems blank.
`seed_memory` exits non-zero if it learns nothing, so a silent failure here is
now a loud one.

## Path B — one command, everything on one host

Tested end-to-end. Needs a VM with **≥2 GB** RAM.

```bash
git clone https://github.com/psamin/project_flock.git
cd project_flock/colony
docker compose -f docker-compose.deploy.yml up -d --build
curl -s http://localhost/health
```

That brings up CockroachDB with a persistent volume, applies the schema, seeds
tactics **only if semantic memory is empty**, then starts the server on port 80.

Verified on a restart: the second boot logs `semantic memory already has 3
tactics; skipping seed` and the data survives, so rebooting the VM does not pile
up a fresh set of lessons or wipe the old ones.

Note the single node here is the *demo*, not the resilience story. The node-kill
runs on the 3-node rig in `infra/`, which is where killing a node proves
something.

---

## After it is up — do not skip these

1. **Put the URL in the repo's About:**
   ```bash
   gh repo edit --homepage https://<your-url>
   ```
   `homepageUrl` is currently empty, so a judge who lands on the repo has
   nothing to click. This is a submission requirement, not a nicety.

2. **`--restart unless-stopped` is doing the month of uptime.** Without it the
   demo dies the first time the instance reboots and nobody notices until a
   judge looks.

3. **Check it again on Sept 1.** The requirement is a month of uptime and the
   most likely way this fails is silently.

---

## Known limits, stated rather than discovered

- **One container, one mission.** Mission state is in-process — the world, the
  agents, the viewer set — so `--workers 1` is deliberate. Horizontal scaling
  would serve *different* missions to different browsers. Fine for a demo; it is
  not a multi-tenant service and should not be described as one.
- **Every visitor shares one mission.** Two judges on the URL at once see the
  same world, and either can drop fire on the other's view. Acceptable for
  judging traffic, worth knowing before it surprises someone mid-demo.
- **No TLS on either path.** Plain HTTP on port 80. CloudFront or a certificate
  is a judging-window nicety, not a submission requirement.
