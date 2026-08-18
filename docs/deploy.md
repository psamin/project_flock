# Deploying the demo URL

§6.1 requires the demo and the repo to be **live, working and free to access
through Sept 15** — a month past submission, not just on the day. This is the
runbook for that.

Everything here is prepared and tested except the parts that need Praneeth's AWS
account. `colony/Dockerfile` is built and smoke-tested against the live cluster:

```
ok: True | memory: cockroach | tick: 30
bedrock: {'requested': 'replay', 'mode': 'replay', 'calls': 0, 'cassette_entries': 76}
console available: True | questions: 6
memory rail rows: 149
GET /        -> 200   (the twin)
GET /2d      -> 200   (Canvas 2D, the no-WebGL floor)
GET /sim3d   -> 200   (alias, kept for the URLs in our own docs)
healthcheck  -> healthy
```

## What the container is

One process: the 4 Hz tick loop, the websocket broadcast, the commander console
and the static client. The frontend is served from the same origin rather than
S3 — it is ~200 KB of static files, and a second origin would buy a CORS problem
and nothing else at this size.

It runs **with no AWS credentials**: `adapter_from_env` defaults to replay and
the committed cassette carries Claude's recorded decisions, so the deployed demo
shows real Bedrock output without a key on the box. That matters for a public
URL sitting up for a month — there is no credential on it to leak.

**What that costs.** The commander console's free-form tier is off in this
configuration. It runs a live Bedrock loop, which a cassette cannot stand in
for, and it reads through the Managed MCP Server, which needs an OAuth refresh
token. The six canned questions still answer, and `/api/console/agent` reports
which piece is missing rather than the tier silently not being there.

That is a real trade, and it is worth deciding rather than inheriting: a judge
visiting the public URL sees the canned tier only. To turn the agent on, the
container needs AWS credentials, `CRDB_CLUSTER_ID`, and
`~/.colony/mcp-token.json` mounted in — which puts a long-lived credential on a
box that sits on the public internet for a month. Our judgement is that the
free-form tier belongs in the demo video and in a judge's local run, and the
public URL stays credential-free.

## Decide one thing first

| | free-tier EC2 | ECS Fargate |
|---|---|---|
| cost for the judging window | **$0** if the AWS account is <12 months old (750 h/month of `t3.micro`) | ~$15–20 for a month of a 0.25 vCPU task |
| setup | one instance, docker run, security group | ECR repo, task definition, service, ALB |
| §6.4 alignment | deviates — PRD names Fargate | as specified |
| failure mode the day before | you can SSH in | you read CloudWatch |

**Recommendation: EC2 free tier.** The PRD chose Fargate before "must stay up
for a month at zero budget" was the binding constraint. A single always-on
instance is the smaller thing that meets the actual requirement, and §6.4's
reasoning against Lambda (a 4 Hz tick loop with persistent websockets) argues
just as well for a plain instance as for Fargate.

## Path A — EC2 free tier

```bash
# 1. Launch: Amazon Linux 2023, t3.micro, 8 GB gp3.
#    Security group: inbound 22 from your IP, 80 from 0.0.0.0/0.

# 2. On the instance
sudo dnf install -y docker git
sudo systemctl enable --now docker
sudo usermod -aG docker ec2-user && exec sudo su - ec2-user

git clone https://github.com/psamin/project_flock.git
cd project_flock

# 3. Build and run. Port 80 so the URL needs no port suffix.
docker build -t colony colony/
docker run -d --name colony --restart unless-stopped \
  -p 80:8000 \
  -e COLONY_DSN='postgresql://<user>:<pass>@<cloud-host>:26257/colony?sslmode=verify-full' \
  colony

# 4. Verify from your laptop, not from the box
curl -s http://<public-ip>/health
```

`--restart unless-stopped` is doing the month of uptime. Without it the demo
dies the first time the instance reboots and nobody notices until a judge looks.

## Path B — ECS Fargate

Same image. `docker build`, tag, `docker push` to ECR, then a task definition
with 0.25 vCPU / 0.5 GB, `COLONY_DSN` from Secrets Manager, and one service
behind an ALB. The healthcheck in the Dockerfile is what the target group should
use (`/health`, 200).

## After it is up — do not skip these

1. **Put the URL in the repo's About.** `gh repo edit --homepage https://…`.
   §6.1 asks for the demo to be reachable, and right now `homepageUrl` is empty,
   so a judge who lands on the repo has nothing to click.
2. **Point it at the Cloud cluster, not a local one.** With no `COLONY_DSN` the
   server falls back to in-memory memory, and a deployed demo on the fake is
   exactly the failure `_make_memory` refuses for named clusters — it would look
   perfect and write nothing.
3. **Check it again on Sept 1.** The requirement is a month of uptime, and the
   most likely way this fails is silently.

## Known limits, stated rather than discovered

- **One container, one mission.** Mission state is in-process — the world, the
  agents, the viewer set — so `--workers 1` is deliberate and horizontal scaling
  would serve different missions to different browsers. Fine for a demo; it is
  not a multi-tenant service and should not be described as one.
- **Every visitor shares one mission.** Two judges on the URL at once see the
  same world, and either can toggle coordination or drop fire on the other's
  view. Acceptable for judging traffic, and worth knowing before it surprises
  someone mid-demo.
- **No TLS in Path A.** Plain HTTP on port 80. Adding CloudFront or a
  certificate is a judging-window nicety, not a submission requirement.
