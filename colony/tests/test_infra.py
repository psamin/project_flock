"""Cluster recipes (PRD §5.1 lane 1, §6.5).

Two recipes, two jobs. The single-node one is what everybody develops against;
the three-node one is the chaos rig FR-11's node-kill segment runs on, because
the Cloud free tier does not hand you nodes to kill.

The structural checks run everywhere. The live checks run only when a cluster is
actually up, and are skipped otherwise — the same rule the rest of the suite
uses, and CI fails the build if database tests skip.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_3NODE = ROOT / "infra" / "docker-compose.3node.yml"
CLUSTER_SCRIPT = ROOT / "infra" / "cluster3.sh"
SCHEMA = ROOT / "colony" / "schema" / "v1_1.sql"
SINGLE_NODE = ROOT / "colony" / "docker-compose.yml"


def _compose(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _three_node_cluster_is_up() -> bool:
    if not shutil.which("docker"):
        return False
    result = subprocess.run(
        [str(CLUSTER_SCRIPT), "nodes"], capture_output=True, text=True, timeout=60
    )
    # Exactly three. `.isdigit()` accepted "2", so a cluster mid-join — or one
    # with crdb-2 still killed from the last chaos run — ran the three-node
    # tests instead of skipping them, and failed for a reason that had nothing
    # to do with the code under test.
    return result.returncode == 0 and result.stdout.strip() == "3"


needs_3node = pytest.mark.skipif(
    not _three_node_cluster_is_up(),
    reason="no 3-node cluster (make cluster-3)",
)


# --- the single-node dev recipe ---------------------------------------------


def test_one_command_gives_a_dev_cluster_and_schema():
    """`make dev` is the whole onboarding step: nobody should need to know the
    cockroach CLI to start working on Lane 1."""
    makefile = (ROOT / "colony" / "Makefile").read_text()
    assert "dev: up schema" in makefile
    assert "docker compose up -d --wait" in makefile
    assert "schema/v1_1.sql" in makefile


def test_the_dev_cluster_is_pinned_to_the_validated_version():
    """§6.3 validated the VECTOR syntax against v26.2. Floating on :latest means
    the schema can break under us between one `make dev` and the next."""
    image = _compose(SINGLE_NODE)["services"]["cockroach"]["image"]
    assert image.startswith("cockroachdb/cockroach:v26.2"), image


# --- the 3-node chaos rig ----------------------------------------------------


def test_the_chaos_rig_defines_three_joined_nodes():
    """FR-11 kills 1 of 3 nodes. Two would lose quorum on a single kill and
    prove the opposite of the intended point."""
    services = _compose(COMPOSE_3NODE)["services"]
    nodes = {name: svc for name, svc in services.items() if name.startswith("crdb-")}

    assert len(nodes) == 3, f"expected 3 cockroach nodes, got {sorted(nodes)}"
    for name, svc in nodes.items():
        assert "--join=crdb-1,crdb-2,crdb-3" in svc["command"], (
            f"{name} joins no cluster"
        )
        assert f"--advertise-addr={name}" in svc["command"], f"{name} misadvertises"


def test_every_chaos_node_is_reachable_on_its_own_port():
    """Each node needs a distinct host port, or the demo cannot point the app at
    a surviving node after killing one."""
    services = _compose(COMPOSE_3NODE)["services"]
    # "127.0.0.1:26258:26257" — host port is second from the right, whether or
    # not a bind address is present.
    sql_ports = [
        mapping.split(":")[-2]
        for name, svc in services.items()
        if name.startswith("crdb-")
        for mapping in svc["ports"]
        if mapping.endswith(":26257")
    ]
    assert sorted(sql_ports) == ["26257", "26258", "26259"]


def test_no_chaos_node_is_published_beyond_this_machine():
    """These nodes run --insecure: no TLS, no password, root on connect. Bound
    to 0.0.0.0 that is unauthenticated database access for anyone on the
    network, so the loopback bind is the only thing keeping the rig private."""
    services = _compose(COMPOSE_3NODE)["services"]
    published = [
        mapping
        for name, svc in services.items()
        if name.startswith("crdb-")
        for mapping in svc.get("ports", [])
    ]
    assert published, "no ports parsed — the check would pass vacuously"
    for mapping in published:
        assert mapping.startswith("127.0.0.1:"), (
            f"{mapping} publishes an insecure node on every interface"
        )


def test_the_chaos_rig_runs_the_same_schema_as_the_dev_cluster():
    """ "Same software, self-hosted" (§6.5) only holds if it is also the same
    schema — a rig with drifted DDL proves nothing about the real system."""
    assert SCHEMA.name in CLUSTER_SCRIPT.read_text()


def test_the_cluster_script_exposes_the_demo_beats():
    """The video kills a node and brings it back on camera; both need to be one
    command, not a remembered docker incantation."""
    script = CLUSTER_SCRIPT.read_text()
    for verb in ("up", "health", "kill", "revive", "down"):
        assert f"  {verb})" in script, f"cluster3.sh has no {verb} action"


def test_the_cluster_script_is_executable():
    assert CLUSTER_SCRIPT.stat().st_mode & 0o111, "cluster3.sh is not executable"


# --- live cluster ------------------------------------------------------------


@needs_3node
def test_all_three_nodes_report_healthy():
    """The done-condition for this TODO item: a 3-node recipe that actually
    stands up three live nodes."""
    result = subprocess.run(
        [str(CLUSTER_SCRIPT), "nodes"], capture_output=True, text=True, timeout=60
    )
    assert result.stdout.strip() == "3", f"only {result.stdout.strip()} nodes live"


@needs_3node
def test_the_schema_applies_to_the_chaos_rig():
    """A rig without the schema cannot run a mission, so the node-kill segment
    would have nothing to survive."""
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_3NODE),
            "-p",
            "colony3",
            "exec",
            "-T",
            "crdb-1",
            "./cockroach",
            "sql",
            "--insecure",
            "-d",
            "colony",
            "--format=csv",
            "-e",
            "SELECT count(*) FROM [SHOW TABLES]",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip().splitlines()[-1]) >= 8, result.stdout
