"""The FR-11 verdict logic (PRD §6.5, §5.4).

The rehearsal itself kills a container and runs a full mission, so it lives in
infra/chaos.py and is driven by hand. What belongs in the suite is the part that
decides whether a run passed — because a chaos rig that reports success no
matter what is worse than no rig at all, and that failure mode is invisible
until the one time something really does break on camera.
"""

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHAOS = ROOT / "infra" / "chaos.py"


def _load_chaos():
    spec = importlib.util.spec_from_file_location("chaos", CHAOS)
    module = importlib.util.module_from_spec(spec)
    sys.modules["chaos"] = module
    spec.loader.exec_module(module)
    return module


chaos = _load_chaos()


def _result(**kwargs):
    defaults = dict(
        claimed_before_kill=4,
        lost_tasks=[],
        completions_before=3,
        completions_after=5,
        stabilized=7,
        ticks=470,
    )
    return chaos.ChaosResult(**{**defaults, **kwargs})


# --- what FR-11 actually claims ---------------------------------------------


def test_a_clean_run_survives():
    assert _result().survived


def test_a_lost_task_fails_the_run():
    """ "Zero task loss" is the literal wording. One vanished row is a failure,
    however well the rest of the mission went."""
    result = _result(lost_tasks=["a2f1"])
    assert not result.zero_task_loss
    assert not result.survived


def test_a_stalled_fleet_fails_the_run():
    """The other half: the memory surviving is not enough if the fleet stops
    working. No completion after the kill means it froze."""
    result = _result(completions_after=0)
    assert not result.no_fleet_stall
    assert not result.survived


def test_a_kill_that_did_not_happen_fails_the_run():
    """A rig that cannot kill the node must not report a pass — that is the
    failure mode that would sail through five green rehearsals and then not
    work on camera."""
    assert not _result(kill_error="no such container").survived


def test_completions_after_the_kill_are_counted_as_a_delta():
    """Counting cumulative completions would call any mission that did work
    *before* the kill a survivor."""
    result = _result(completions_before=10, completions_after=0)
    assert not result.survived


def test_the_verdict_reads_clearly():
    """This line is what a human sees five times in a row before recording; it
    has to say what happened, not just pass or fail."""
    text = _result().describe()
    assert text.startswith("SURVIVED")
    assert "0 lost" in text
    assert "victims stabilized" in text

    failed = _result(lost_tasks=["x"]).describe()
    assert failed.startswith("FAILED")
    assert "1 lost" in failed


# --- the rig is wired to the real cluster ------------------------------------


def test_the_rehearsal_kills_mid_mission_not_at_the_edges():
    """Killing at tick 0 proves nothing (no work in flight) and killing near the
    end proves little (nothing left to do)."""
    assert 0 < chaos.KILL_AT_TICK < 300, chaos.KILL_AT_TICK


def test_the_node_comes_back_within_the_mission():
    """The demo restores the node on camera; a rehearsal that never revives it
    is not rehearsing the same thing."""
    assert chaos.REVIVE_AFTER_TICKS > 0


def test_it_refuses_to_run_without_a_healthy_cluster(monkeypatch, capsys):
    """Running against 2 nodes would measure the wrong thing and report a
    meaningless pass."""

    class NotThree:
        stdout = "2"
        returncode = 0

    monkeypatch.setattr(chaos, "_cluster", lambda action: NotThree())
    monkeypatch.setattr(sys, "argv", ["chaos.py"])  # not pytest's own argv
    assert chaos.main() == 2
    assert "3-node cluster" in capsys.readouterr().err


def test_rehearsals_default_to_one_but_five_is_the_bar(monkeypatch):
    """§5.4: at least five before recording. The default stays 1 so a quick
    check is cheap; the flag is how the bar gets met."""
    runs = []
    monkeypatch.setattr(
        chaos,
        "_cluster",
        lambda action: type("R", (), {"stdout": "3", "returncode": 0})(),
    )
    monkeypatch.setattr(
        chaos, "run_rehearsal", lambda **kw: runs.append(1) or _result()
    )
    monkeypatch.setattr(sys, "argv", ["chaos.py", "--rehearsals", "5"])

    assert chaos.main() == 0
    assert len(runs) == 5


def test_any_failed_rehearsal_fails_the_whole_run(monkeypatch):
    """Five runs where one lost a task is not a pass."""
    outcomes = iter([_result(), _result(lost_tasks=["x"]), _result()])
    monkeypatch.setattr(
        chaos,
        "_cluster",
        lambda action: type("R", (), {"stdout": "3", "returncode": 0})(),
    )
    monkeypatch.setattr(chaos, "run_rehearsal", lambda **kw: next(outcomes))
    monkeypatch.setattr(sys, "argv", ["chaos.py", "--rehearsals", "3"])

    assert chaos.main() == 1
