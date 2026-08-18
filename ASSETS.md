# Assets and third-party code

Hackathon rule §6.1: the submission must contain no third-party copyrighted
material, standard frameworks are explicitly allowed, and any pre-existing code
must be disclosed. This file is that disclosure.

## Artwork: none

There is no third-party artwork in this project, and no asset pack is
downloaded at any point.

| What | Where | Provenance |
|---|---|---|
| 2D sprites (tiles, robots, victims, fire) | [`colony/client/atlas.js`](colony/client/atlas.js) | Hand-drawn in code on a 16×16 grid, painted once into an offscreen canvas at boot. No image files. |
| 3D geometry (buildings, slab, robot rigs, sensor volumes) | [`colony/client/scene3d.js`](colony/client/scene3d.js), [`colony/client/rigs.js`](colony/client/rigs.js) | Generated procedurally from the map's own tile and zone data. No models, no textures. |
| Fonts | both views | System monospace stack only (`ui-monospace`, `SF Mono`, `Menlo`). Nothing downloaded. |
| Audio | — | None. |

The reason is the same in both cases and predates the rule: a repo that needs an
asset pack fetched before it renders is a repo that breaks on somebody else's
laptop the day before the deadline.

## Vendored library: Three.js

The 3D view at `/` uses [Three.js](https://threejs.org) **r180**, vendored
into the repository rather than loaded from a CDN.

| File | Purpose |
|---|---|
| `colony/client/vendor/three/three.module.min.js` | Core renderer |
| `colony/client/vendor/three/three.core.min.js` | Core internals (r180 splits the build) |
| `colony/client/vendor/three/OrbitControls.js` | Orbit / pan / zoom camera |
| `colony/client/vendor/three/CSS2DRenderer.js` | DOM telemetry labels positioned in 3D space |

- **Licence:** MIT. Copyright 2010-2025 Three.js Authors. The licence header is
  retained verbatim at the top of each vendored file.
- **Source:** `https://cdn.jsdelivr.net/npm/three@0.180.0/`
- **Why vendored:** no CDN and no bundler anywhere in this project, so the demo
  cannot break because a third-party host is unreachable during judging. The
  addons resolve their bare `three` specifier through an
  [import map](colony/client/sim3d.html), which is the platform's own answer to
  this and needs no build step.
- **Modifications:** none. The files are byte-for-byte as published.

## CockroachDB Agent Skills

| Path | What it is |
|---|---|
| `colony/skills/` | 34 skills, fetched not vendored |

- **Licence:** Apache 2.0, the same licence as this repo. `LICENSE` is copied in
  beside the skills by the fetch script.
- **Source:** [`cockroachlabs/cockroachdb-skills`](https://github.com/cockroachlabs/cockroachdb-skills),
  pinned at `e14e86d` in [`colony/scripts/fetch_skills.sh`](colony/scripts/fetch_skills.sh).
- **Why fetched rather than vendored:** the commander agent routes on these
  descriptions, so the pin is what stops an upstream edit changing which skill it
  picks mid-demo — and 34 files of third-party markdown in the diff would hide
  our own changes rather than document a dependency. `colony/skills/` is
  gitignored; the script plus the pin is the record.
- **Modifications:** none. Only the `skills/` subtree and `LICENSE` are taken;
  upstream's `docs/` and `scripts/` are for people contributing to that repo.
- **How it is used:** `console/skills.py` reads each skill's YAML frontmatter
  into a routing catalogue in the agent's system prompt, and loads a full body
  only when the model calls `load_skill`. That two-tier shape is the
  agentskills.io contract, not something we invented for it.

## Runtime dependencies

Declared in [`colony/pyproject.toml`](colony/pyproject.toml) and installed by
`uv`: FastAPI, Uvicorn, psycopg (CockroachDB driver), boto3 (AWS Bedrock), and
pytest + httpx for the test suite. All standard frameworks under permissive
licences.

The Managed MCP Server client (`console/mcp_client.py`) adds nothing to that
list: it speaks JSON-RPC and OAuth 2.1 over `urllib` from the standard library,
so the console's dependency on CockroachDB Cloud costs no third-party package.

## AI-assisted development

This project was built with AI coding assistants, which §6.1 explicitly allows.
