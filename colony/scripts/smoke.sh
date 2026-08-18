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
# HEADLESS AND WEBGL: the headless browser used here has no WebGL2, so `/` --
# now the digital twin -- takes its capability-gate path rather than rendering.
# That is deliberately treated as a PASS: it is exactly the degradation a judge
# on a machine without hardware acceleration gets, and it is the path that has
# to send them to /2d rather than to itself. The 3D scene itself can only be
# checked in a real browser.
#
# So the routes swapped roles here too: `/` is the fallback check and `/2d` is
# the one whose canvas must actually be drawing.
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
for path in / /2d /sim3d /static/app.js /static/scene3d.js /static/ui-shared.js \
            /static/vendor/three/three.module.min.js; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "$BASE$path")
  [ "$code" = "200" ] && pass "GET $path -> 200" || fail "GET $path -> $code"
done

# --- 2. the 2D view renders --------------------------------------------------
echo "checking /2d ..."
"$B" console --clear >/dev/null 2>&1
"$B" goto "$BASE/2d" >/dev/null 2>&1
# 8s, not 5. The canvas is only created once the first snapshot lands, so a
# short settle intermittently measured a page that had not drawn yet and
# reported "blank canvas" for what was really "not finished loading".
sleep 8

errors=$("$B" console --errors 2>/dev/null | grep -v "BEGIN UNTRUSTED\|END UNTRUSTED\|no console errors" | tr -d '[:space:]')
[ -z "$errors" ] && pass "/2d threw nothing" || fail "/2d console errors: $errors"

colours=$("$B" js "(()=>{const c=document.querySelector('#stage canvas');if(!c)return 0;const g=c.getContext('2d');const d=g.getImageData(0,0,c.width,c.height).data;const s=new Set();for(let i=0;i<d.length;i+=4000)s.add(d[i]+','+d[i+1]+','+d[i+2]);return s.size;})()" 2>/dev/null | tr -dc '0-9')
if [ "${colours:-0}" -gt 2 ]; then
  pass "/2d canvas is drawing ($colours distinct colours)"
else
  fail "/2d canvas is blank or flat (${colours:-0} distinct colours)"
fi

hud=$("$B" js "document.getElementById('m-tick').textContent" 2>/dev/null | tr -dc '0-9')
if [ "${hud:-0}" -gt 0 ]; then
  pass "/2d HUD is populated (tick $hud)"
else
  fail "/2d HUD never populated — the shared UI module may not be wired"
fi

"$B" screenshot "$OUT/2d.png" >/dev/null 2>&1 && pass "/2d screenshot -> $OUT/2d.png"

# --- 3. the 3D view: renders, or degrades honestly ---------------------------
echo "checking / (the twin) ..."
"$B" console --clear >/dev/null 2>&1
"$B" goto "$BASE/" >/dev/null 2>&1
sleep 6

errors=$("$B" console --errors 2>/dev/null | grep -v "BEGIN UNTRUSTED\|END UNTRUSTED\|no console errors" | tr -d '[:space:]')
[ -z "$errors" ] && pass "/ threw nothing" || fail "/ console errors: $errors"

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
    pass "/ degraded to the WebGL notice (expected headless)"
    link=$("$B" js "!!document.querySelector('#nogl a[href=\"/2d\"]')" 2>/dev/null | tr -d '[:space:]')
    [ "$link" = "true" ] && pass "  fallback links to /2d" \
                         || fail "  fallback has no link to /2d"
    # The mistake this swap could have shipped: the notice used to link to `/`,
    # which is now the page showing it.
    self=$("$B" js "!!document.querySelector('#nogl a[href=\"/\"]')" 2>/dev/null | tr -d '[:space:]')
    [ "$self" = "true" ] && fail "  the WebGL notice links to itself" \
                         || pass "  fallback is not a self-link"
    ;;
  *SCENE*)  pass "/ rendered: $verdict" ;;
  *NO_SCENE*)  fail "/: scene module never initialised" ;;
  *NOT_READY*) fail "/: no world frame arrived" ;;
  *NO_RIGS*)   fail "/: world loaded but no robot rigs were built" ;;
  *)           fail "/: unrecognised state ($verdict)" ;;
esac

"$B" screenshot "$OUT/3d.png" >/dev/null 2>&1 && pass "/ screenshot -> $OUT/3d.png"

# --- 4. the console's two tiers are both wired -------------------------------
#
# The free-form tier is JavaScript talking to two external services, so it fails
# the same way the rest of this file exists to catch: silently, and only in a
# browser.
#
# Checked on /2d only, and that is not a gap. On `/` this browser has no WebGL,
# so scene3d.js is never imported and initConsole() never runs — the console
# markup is present and deliberately inert, which is the correct behaviour and
# not something to assert liveness against. Both pages share ui-shared.js and
# the same element ids (test_routes.py pins that), so a console that works on
# /2d works on the twin; what cannot be verified headlessly is the twin's
# renderer, which this file has never claimed to cover.
#
# What is asserted is that the row exists and that the page resolved its
# availability — NOT that the agent is on. A deployment with no AWS credentials
# is a valid deployment, and the console is supposed to say so rather than hide.
echo "checking the commander console on /2d ..."
"$B" goto "$BASE/2d" >/dev/null 2>&1
sleep 6

present=$("$B" js "!!document.getElementById('console-ask') && !!document.getElementById('console-steps')" 2>/dev/null | tr -d '[:space:]')
[ "$present" = "true" ] && pass "/2d has the ask row" \
                        || fail "/2d is missing the free-form tier's markup"

# The note is written by initAgent() once /api/console/agent answers, so a blank
# one means the fetch never resolved — a wiring failure even when the agent
# itself is unavailable.
note=$("$B" js "(document.getElementById('console-agent-note')||{}).textContent||''" 2>/dev/null | tr -d '\r')
if [ -n "$(echo "$note" | tr -d '[:space:]')" ]; then
  pass "/2d console reports its agent state: $note"
else
  fail "/2d never resolved /api/console/agent"
fi

buttons=$("$B" js "document.querySelectorAll('#console-questions button').length" 2>/dev/null | tr -dc '0-9')
[ "${buttons:-0}" -ge 6 ] && pass "/2d offers the ${buttons} canned questions" \
                          || fail "/2d canned tier has ${buttons:-0} questions, expected 6"

# The twin carries the same markup even when it cannot run it. Cheap, and it is
# the regression that started this work: panels added to one page and not the
# other, with nothing failing.
"$B" goto "$BASE/" >/dev/null 2>&1
sleep 3
parity=$("$B" js "['console-ask','memory-rail','fleet','coordination','intervene-kinds','kill-robot','compare'].every(i=>!!document.getElementById(i))" 2>/dev/null | tr -d '[:space:]')
[ "$parity" = "true" ] && pass "/ carries every shared panel's markup" \
                       || fail "/ is missing markup for a panel /2d has"

echo
if [ "$FAILED" = "0" ]; then
  echo "SMOKE PASSED — screenshots in $OUT"
  echo "NOTE: the twin's 3D scene needs a real browser with WebGL 2. Open $BASE/"
  echo "      and look at it before you record."
else
  echo "SMOKE FAILED"
fi
exit "$FAILED"
