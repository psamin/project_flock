"""Is this demo recordable right now? One command, run it before hitting record.

Every check here exists because something in it has actually gone wrong during
this build, silently, in a way that looked fine on screen:

  memory backend   a server with no cluster falls back to in-memory memory and
                   keeps working. The mission looks identical and writes
                   nothing.
  semantic memory  tactics are seeded by a separate step, and the demo runs
                   perfectly without them — `mission_memories` simply reads 0.
                   Agentic Memory Design is judging criterion #1, so the one
                   blank table is the one a judge is told to look at.

                   The usual cause is not a failed seed. **`make test` empties
                   it.** `tests/test_recall.py` clears the table in its
                   fixtures, correctly — recall assertions need a known one —
                   and the suite shares the dev cluster with the demo. So
                   seeding and then running tests leaves a demo that looks
                   fine and has forgotten everything. Re-seed after testing,
                   or run `make demo`, which resets and seeds together.
  bedrock          replay with an unloaded cassette produced 34 plans all
                   reading `source: rules`, making "Claude decides" false on
                   the one path anybody watches.
  console          seven questions that each have to actually answer. A
                   question that raises is only discovered by clicking it.

    uv run python -m preflight                 # against localhost:8000
    uv run python -m preflight --url :3000
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

MEMORY_SYSTEMS = ("working", "episodic", "provenance", "semantic")


def get(url: str, path: str) -> dict:
    with urllib.request.urlopen(url + path, timeout=10) as r:
        return json.loads(r.read())


def post(url: str, path: str, body: dict) -> dict:
    req = urllib.request.Request(
        url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8000")
    args = ap.parse_args()
    url = args.url if args.url.startswith("http") else f"http://localhost{args.url}"

    fails: list[str] = []
    warns: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}{(' — ' + detail) if detail else ''}")
        if not ok:
            fails.append(label)

    print(f"preflight against {url}\n")

    try:
        health = get(url, "/health")
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"  FAIL  server not answering at {url} — {e}")
        print("\nNOT RECORDABLE. Start it with `make demo`.")
        return 1

    check(bool(health.get("ok")), "server healthy", f"tick {health.get('tick')}")
    check(
        health.get("memory") == "cockroach",
        "fleet memory is a real cluster",
        f"memory={health.get('memory')!r}"
        + (" — the fake writes nothing" if health.get("memory") != "cockroach" else ""),
    )

    bedrock = health.get("bedrock") or {}
    check(
        (bedrock.get("cassette_entries") or 0) > 0,
        "bedrock cassette loaded",
        f"{bedrock.get('cassette_entries')} entries, mode={bedrock.get('mode')!r}",
    )

    rail = get(url, "/api/memory")
    counts = rail.get("counts") or {}
    for system in MEMORY_SYSTEMS:
        total = sum((counts.get(system) or {}).values())
        hint = ""
        if system == "semantic" and total == 0:
            hint = "run `uv run python -m sim.seed_memory` — criterion #1 reads this"
        check(total > 0, f"{system} memory populated", hint or f"{total} rows")

    questions = get(url, "/api/console/questions").get("questions") or []
    check(len(questions) >= 7, "console questions listed", f"{len(questions)} questions")

    for q in questions:
        try:
            answered = post(url, "/api/console/ask", {"question": q["id"]})
            summary = (answered.get("summary") or "").strip()
            check(bool(summary), f"console answers {q['id']}", summary[:60])
        except Exception as e:  # noqa: BLE001 - any failure here is a failure
            check(False, f"console answers {q['id']}", str(e)[:70])

    for path in ("/", "/sim3d"):
        try:
            with urllib.request.urlopen(url + path, timeout=10) as r:
                check(r.status == 200, f"page {path}", f"HTTP {r.status}")
        except Exception as e:  # noqa: BLE001
            check(False, f"page {path}", str(e)[:70])

    # Warnings: true but not blocking. The feed is empty early in every mission
    # because a handoff needs a scout to find somebody first, so an empty one at
    # tick 40 means "too early", not "broken".
    feed = get(url, "/api/coordination")
    entries = feed.get("events") or feed.get("items") or []
    if not entries:
        warns.append(
            f"coordination feed is empty at tick {health.get('tick')} — normal "
            "before the first handoff; let it run to ~tick 150 before recording"
        )

    print()
    for w in warns:
        print(f"  note: {w}")
    if fails:
        print(f"\nNOT RECORDABLE — {len(fails)} failed: {', '.join(fails)}")
        return 1
    print("\nRECORDABLE.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
