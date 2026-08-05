"""Lane 4 — orchestration.

§4.1 draws the orchestrator with three jobs: allocation, dependency unblocking,
and lost-marking. Two of them moved in v3.1 and are not here:

    allocation            decentralized. Agents rank open work by §4.4's
                          allocation score and claim it themselves; the claim
                          transaction makes that exactly as safe as an assigned
                          claim, so the orchestrator is "an optimizer, not a
                          dependency" (§4.4).
    dependency unblocking  inside `complete_task`'s transaction, where a
                          dependent flips `blocked -> open` in the same commit
                          that finishes its last dependency (FR-3).

Lost-marking is what has no other owner, so it is what lives here.
"""

from orchestrator.lost import LOST_AFTER_SECONDS, LostScan, LostWatch

__all__ = ["LOST_AFTER_SECONDS", "LostScan", "LostWatch"]
