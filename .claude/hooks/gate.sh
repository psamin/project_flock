#!/bin/bash
cat > /dev/null
cd "$CLAUDE_PROJECT_DIR" || exit 0
tries=$(cat .claude/.gate_tries 2>/dev/null || echo 0)
if [ "$tries" -ge 5 ]; then rm -f .claude/.gate_tries; exit 0; fi
if ! python -m pytest -q tests/; then
  echo $((tries+1)) > .claude/.gate_tries
  echo "pytest failing — fix and rerun before stopping." >&2
  exit 2
fi
if grep '^- \[ \]' TODO.md 2>/dev/null | grep -v '(P1)' | grep -q .; then
  echo $((tries+1)) > .claude/.gate_tries
  echo "TODO.md has unchecked P0 items — pick the next one." >&2
  exit 2
fi
rm -f .claude/.gate_tries
exit 0
