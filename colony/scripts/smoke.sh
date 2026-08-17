#!/usr/bin/env bash
# Pre-recording smoke check for both renderers.
#
# WHY THIS EXISTS: 16 of the ~20 codepaths added by the 3D view are JavaScript,
# and this repo has no JS test framework — 701 pytest tests say nothing about
# whether a page renders. A blank canvas or a thrown exception at import time
# would pass `make test` and fail on camera. This catches that class.
#
# WHAT IT IS NOT: a substitute for looking at the thing. It asserts the pages
# load, throw nothing, and draw something other than one flat colour. It cannot
# tell you the diorama looks good.
#
# HEADLESS AND WEBGL: the headless browser used here has no WebGL2, so /sim3d
# takes its capability-gate path rather than rendering. That is deliberately
# treated as a PASS, because it is exactly the degradation a judge on a machine
# without hardware acceleration gets, and it is worth regression-testing. The 3D
# scene itself has to be checked in a real browser.
#
# Depends on the gstack browse binary, which lives outside this repo, so this is
# a local gate and not a CI step. Run it before recording.

set -uo pipefail

BASE="${COLONY_SMOKE_URL:-http://localhost:8000}"
OUT="${COLONY_SMOKE_OUT:-/tmp/colony-smoke}"
FAILED=0

B=""
for candidate in \
  "$(git rev-parse --show-toplevel 2>/dev/null)/.claude/skills/gstack/browse/dist/browse" \
  "$HOME/.claude/skills/gstack/browse/dist/browse"; do
  [ -x "$candidate" ] && B="$candidate" && break
done

if [ -z "$B" ]; then
  echo "SKIP: gstack browse binary not found."
  echo "      This check needs it; the pages themselves are unaffected."
  exit 0
fi

mkdir -p "$OUT"

fail() { echo "  FAIL: $*"; FAILED=1; }
pass() { echo "  ok:   $*"; }

echo "smoke: $BASE  (browse: $B)"

# --- 1. the routes answer at all --------------------------------------------
for path in / /sim3d /static/app.js /static/scene3d.js /static/ui-shared.js \
            /static/vendor/three/three.module.min.js; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE$path")
  [ "$code" = "200" ] && pass "GET $path -> 200" || fail "GET $path -> $code"
done

# --- 2. the 2D view renders --------------------------------------------------
echo "checking / ..."
"$B" console --clear >/dev/null 2>&1
"$B" goto "$BASE/" >/dev/null 2>&1
sleep 5

errors=$("$B" console --errors 2>/dev/null | grep -v "BEGIN UNTRUSTED\|END UNTRUSTED\|no console errors" | tr -d '[:space:]')
[ -z "$errors" ] && pass "/ threw nothing" || fail "/ console errors: $errors"

colours=$("$B" js "(()=>{const c=document.querySelector('#stage canvas');if(!c)return 0;const g=c.getContext('2d');const d=g.getImageData(0,0,c.width,c.height).data;const s=new Set();for(let i=0;i<d.length;i+=4000)s.add(d[i]+','+d[i+1]+','+d[i+2]);return s.size;})()" 2>/dev/null | tr -dc '0-9')
if [ "${colours:-0}" -gt 2 ]; then
  pass "/ canvas is drawing ($colours distinct colours)"
else
  fail "/ canvas is blank or flat (${colours:-0} distinct colours)"
fi

hud=$("$B" js "document.getElementById('m-tick').textContent" 2>/dev/null | tr -dc '0-9')
if [ "${hud:-0}" -gt 0 ]; then
  pass "/ HUD is populated (tick $hud)"
else
  fail "/ HUD never populated — the shared UI module may not be wired"
fi

"$B" screenshot "$OUT/2d.png" >/dev/null 2>&1 && pass "/ screenshot -> $OUT/2d.png"

# --- 3. the 3D view: renders, or degrades honestly ---------------------------
echo "checking /sim3d ..."
"$B" console --clear >/dev/null 2>&1
"$B" goto "$BASE/sim3d" >/dev/null 2>&1
sleep 6

errors=$("$B" console --errors 2>/dev/null | grep -v "BEGIN UNTRUSTED\|END UNTRUSTED\|no console errors" | tr -d '[:space:]')
[ -z "$errors" ] && pass "/sim3d threw nothing" || fail "/sim3d console errors: $errors"

verdict=$("$B" js "(()=>{
  if (document.getElementById('nogl').classList.contains('show')) return 'FALLBACK';
  const s = window.__colony && window.__colony.stats();
  if (!s) return 'NO_SCENE';
  if (!s.ready) return 'NOT_READY';
  if (!s.rigs) return 'NO_RIGS';
  return 'SCENE ' + s.tiles + ' tiles, ' + s.rigs + ' rigs, ' + s.drawCalls + ' draws';
})()" 2>/dev/null | tr -d '\r')

case "$verdict" in
  *FALLBACK*)
    pass "/sim3d degraded to the WebGL notice (expected headless)"
    link=$("$B" js "!!document.querySelector('#nogl a[href=\"/\"]')" 2>/dev/null | tr -d '[:space:]')
    [ "$link" = "true" ] && pass "  fallback links to the 2D view" \
                         || fail "  fallback has no link to /"
    ;;
  *SCENE*)  pass "/sim3d rendered: $verdict" ;;
  *NO_SCENE*)  fail "/sim3d: scene module never initialised" ;;
  *NOT_READY*) fail "/sim3d: no world frame arrived" ;;
  *NO_RIGS*)   fail "/sim3d: world loaded but no robot rigs were built" ;;
  *)           fail "/sim3d: unrecognised state ($verdict)" ;;
esac

"$B" screenshot "$OUT/3d.png" >/dev/null 2>&1 && pass "/sim3d screenshot -> $OUT/3d.png"

echo
if [ "$FAILED" = "0" ]; then
  echo "SMOKE PASSED — screenshots in $OUT"
  echo "NOTE: /sim3d's 3D scene needs a real browser with WebGL 2. Open $BASE/sim3d"
  echo "      and look at it before you record."
else
  echo "SMOKE FAILED"
fi
exit "$FAILED"
