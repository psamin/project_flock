Work TODO.md top to bottom, one unchecked non-(P1) item per cycle:
1. Pick the first unchecked non-(P1) item.
2. Implement it in Lane 1 code only (migrations, fleetmem/, tests/, infra/).
3. Write or update tests proving that item's done-condition. Run pytest.
4. Green: check the item off in TODO.md, commit with a conventional message.
5. Blocked after 2 attempts: leave it unchecked, add a note under a "## Blocked" section in TODO.md, move on.
Never change public fleetmem signatures after Aug 3 without asking. Never touch other lanes' directories.
