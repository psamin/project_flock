#!/usr/bin/env bash
# Fetch the CockroachDB Agent Skills repo (§6.2 required tool #3).
#
# Pinned to a commit rather than tracking main: the commander agent routes on
# these descriptions, so an upstream edit would change which skill it picks
# mid-demo. Bump PIN deliberately, and re-run the console tests after.
#
# Lands in colony/skills/, which is gitignored — 34 skills of third-party
# markdown do not belong in this repo's diff, and this script plus the pin is a
# smaller, more honest record of the dependency than a vendored copy.
set -euo pipefail

REPO="https://github.com/cockroachlabs/cockroachdb-skills.git"
PIN="e14e86d23ce8ee2e7e40a34ce2944c2502b6eadd"
DEST="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/skills"

if [ -f "$DEST/.pin" ] && [ "$(cat "$DEST/.pin")" = "$PIN" ]; then
  echo "skills already at $PIN"
  exit 0
fi

rm -rf "$DEST"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

git clone --quiet --filter=blob:none "$REPO" "$TMP/skills"
git -C "$TMP/skills" checkout --quiet "$PIN"

# Only the skills themselves. The upstream docs/ and scripts/ are for people
# contributing to that repo, not for an agent reading it at runtime.
mv "$TMP/skills/skills" "$DEST"
cp "$TMP/skills/LICENSE" "$DEST/LICENSE"
echo "$PIN" > "$DEST/.pin"

echo "fetched $(find "$DEST" -name SKILL.md | wc -l | tr -d ' ') skills at $PIN -> $DEST"
