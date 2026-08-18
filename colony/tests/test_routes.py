"""The pages actually load (§4.8, docs/designs/3d-simulation-view.md).

`/` is the digital twin and `/2d` is the Canvas 2D renderer. That is the
inverse of how they shipped, and the swap is only safe because `/2d` exists and
the WebGL notice points at it — a judge without hardware acceleration now lands
on the one page that cannot render for them, so the escape hatch is no longer a
nicety. `test_the_webgl_escape_hatch_is_not_a_self_link` is the test that would
have caught the obvious version of this mistake.

Nothing else in the suite makes an HTTP request for a page. `test_server.py`
exercises viewer fan-out and the API by calling into `Mission` directly, which
means a `FileResponse` pointing at a filename that does not exist would pass
every test in this repo and fail as a 500 on the demo URL — the one place it
cannot be allowed to fail, since judging runs for four weeks after submission
on a deployment nobody is watching.

These are deliberately shallow. They assert the routes are wired and serving the
real files, not what the files contain; the renderers themselves are checked by
the smoke target in the Makefile, which needs a browser.
"""

import pathlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    # The fake keeps this off the database: route wiring is what is under test,
    # and a missing cluster must not be able to fail it.
    monkeypatch.setenv("COLONY_MEMORY", "fake")
    from sim.server import app

    with TestClient(app) as running:
        yield running


def test_the_root_is_the_digital_twin(client):
    """`/` carries the submission video and is what a judge who types the bare
    URL gets. A typo in the filename is a 500 on the front door."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "<title>Project-Flock" in body
    # The import map is what lets the vendored Three.js addons resolve their
    # bare 'three' specifier with no bundler. Without it the page is blank.
    assert '"three":' in body
    assert "/static/vendor/three/three.module.min.js" in body


def test_the_2d_view_is_served_at_its_own_route(client):
    """The no-WebGL floor. It is what the twin's capability notice links to, so
    it has to work on a machine that could not render `/`."""
    response = client.get("/2d")
    assert response.status_code == 200
    body = response.text
    assert "<title>Project-Flock" in body
    assert "/static/app.js" in body


def test_the_old_3d_url_still_resolves(client):
    """`/sim3d` is named in the design doc, the video script and the deploy
    runbook, and in whatever teammates bookmarked. Serving the twin there costs
    a line; a 404 on a URL in our own writeup costs more than that."""
    response = client.get("/sim3d")
    assert response.status_code == 200
    assert "/static/vendor/three/three.module.min.js" in response.text


def test_the_3d_view_degrades_without_webgl(client):
    """The capability gate and its escape hatch ship in the HTML itself, ahead
    of the 756K of Three.js, so a machine without WebGL is told where to go
    instead of being shown a black rectangle (design doc premise 4)."""
    body = client.get("/").text
    assert "WebGL2RenderingContext" in body
    assert 'id="nogl"' in body
    assert 'href="/2d"' in body, "the fallback must link to the 2D view"


def test_the_webgl_escape_hatch_is_not_a_self_link(client):
    """The failure this whole route swap could have shipped.

    The notice used to link to `/` because `/` was the 2D view. Now `/` is the
    page showing the notice, so a link there is a loop — and it would loop for
    exactly the visitor who has no other way to see the mission. Asserted on the
    escape-hatch paragraph rather than the whole body, since the header's
    "2D view →" link is a different element with the same job.
    """
    body = client.get("/").text
    notice = body[
        body.index('id="nogl"') : body.index("</div>", body.index('id="nogl"'))
    ]
    assert 'href="/2d"' in notice
    assert 'href="/"' not in notice, "the WebGL notice links to itself"


@pytest.mark.parametrize(
    "path",
    [
        "/static/app.js",
        "/static/atlas.js",
        "/static/ui-shared.js",
        "/static/scene3d.js",
        "/static/rigs.js",
        "/static/director.js",
        "/static/vendor/three/three.module.min.js",
        "/static/vendor/three/three.core.min.js",
        "/static/vendor/three/OrbitControls.js",
        "/static/vendor/three/CSS2DRenderer.js",
    ],
)
def test_every_client_module_is_reachable(client, path):
    """Both renderers are plain ES modules with no bundler, so a missing file is
    a 404 at import time and a blank page — with the failure only visible in the
    browser console. The vendored Three.js is included because it is the one
    part a `git clean` or a stray .gitignore rule could silently remove."""
    response = client.get(path)
    assert response.status_code == 200, f"{path} is not being served"
    assert response.content, f"{path} is empty"


# --- parity between the two renderers ----------------------------------------
#
# The twin shipped first and the operator panel, coordination feed and fleet
# panel all landed afterwards, in index.html only. Nothing caught it, because
# every ui-shared.js function no-ops when its root element is missing — the 3D
# page did not throw, it just quietly had fewer features for two days. These
# tests are the thing that would have caught that.

# ids that ui-shared.js looks up. Present on both pages => the shared function
# runs on both pages. Absent on one => that feature silently does not exist
# there, which is exactly the failure mode being guarded.
SHARED_FEATURE_IDS = [
    "memory-rail",  # refreshMemoryRail
    "fleet",  # refreshFleet
    "coordination",  # refreshCoordination
    "intervene-kinds",  # initInterventions
    "intervene-radius",
    "intervene-status",
    "kill-robot",  # initKillRobot
    "compare",  # initCompare
    "comparison",  # refreshComparison
    "console-questions",  # initConsole
    "console-suggest",  # showQuestions — the fold-away toggle
    "console-ask",  # the free-form tier
    "console-steps",
    "ticker",
]


@pytest.mark.parametrize("element_id", SHARED_FEATURE_IDS)
@pytest.mark.parametrize("route", ["/", "/2d"])
def test_both_renderers_carry_every_shared_feature(client, route, element_id):
    """Asserted by id because that is the actual contract.

    `ui-shared.js` reaches for these with getElementById and returns early when
    one is missing, so the id being present is what makes the feature exist.
    """
    assert f'id="{element_id}"' in client.get(route).text, (
        f"{route} is missing #{element_id}; the ui-shared function that fills "
        f"it will no-op there"
    )


def test_the_twin_wires_up_the_shared_features_it_now_has_markup_for(client):
    """Markup without the call is a panel that stays empty forever, which reads
    as "the fleet did nothing" rather than as "nobody called refreshFleet".

    Both spellings are accepted because the polled refreshers are handed to
    setInterval by reference and never appear with parentheses.
    """
    body = client.get("/static/scene3d.js").text
    for name in (
        "initInterventions",
        "initKillRobot",
        "initCompare",
        "refreshMemoryRail",
        "refreshCoordination",
        "refreshFleet",
    ):
        wired = f"{name}(" in body or f"setInterval({name}" in body
        assert wired, f"scene3d.js imports {name} but never wires it up"


def test_the_twin_can_place_an_intervention(client):
    """The operator gesture needs a tile from a pointer, and the 3D view had no
    tile picking at all — only robot picking. Guards the ground-plane raycast,
    since without it the kind buttons arm and then nothing can ever be clicked.
    """
    body = client.get("/static/scene3d.js").text
    assert "intersectPlane" in body, "no ground-plane raycast: tiles unclickable"
    assert "placeIntervention(" in body
    assert "armedIntervention()" in body


# --- the scoreboard and the console, after the readability pass --------------
#
# Both changes are the kind that regress by accretion: a metric is easy to add
# back "just this one", and a panel is easy to reorder while editing something
# near it. These say what the pass decided so a later edit has to argue with it.

# Cut from the strip on purpose. `duplicate effort` and `double work` are
# genuinely different metrics that sound like one, so a viewer trusts neither;
# `seen` and `stabilized` are both inside `rescue`; `tiles seen` counts tiles
# beside numbers that count people. All five are still computed and still on
# /api/runs — this guards the strip, not the measurement.
DROPPED_HUD_IDS = ["m-located", "m-stabilized", "m-median", "m-duplicate", "m-coverage"]

# What a viewer reads at a glance: the mission in lives, the rate, the
# coordination number, and the two integration claims the demo makes out loud.
KEPT_HUD_IDS = ["m-tick", "m-lost", "m-rescue", "m-doublework", "m-recall", "m-bedrock"]


@pytest.mark.parametrize("element_id", DROPPED_HUD_IDS)
@pytest.mark.parametrize("route", ["/", "/2d"])
def test_the_scoreboard_stays_trimmed(client, route, element_id):
    """updateHud no longer writes these, so markup carrying one would render a
    label with a value frozen at its initial 0 — worse than not showing it."""
    assert f'id="{element_id}"' not in client.get(route).text, (
        f"{route} still shows #{element_id}; ui-shared.js stopped filling it, "
        f"so it would sit at its placeholder for the whole mission"
    )


@pytest.mark.parametrize("element_id", KEPT_HUD_IDS)
@pytest.mark.parametrize("route", ["/", "/2d"])
def test_the_scoreboard_keeps_the_six_that_earn_their_place(client, route, element_id):
    assert f'id="{element_id}"' in client.get(route).text


@pytest.mark.parametrize("route", ["/", "/2d"])
def test_the_console_answer_sits_above_the_question_list(client, route):
    """The answer renders directly under the box that asked for it.

    It used to render under the six canned-question buttons, which in the 380px
    rail meant every answer opened below the fold — you asked, and the panel
    appeared not to respond. Order in the DOM is what fixes it, so order in the
    DOM is what this asserts.
    """
    body = client.get(route).text
    assert body.index('id="console-answer"') < body.index('id="console-questions"'), (
        f"{route} renders the console answer below the question list, so an "
        f"answer opens off-screen in a narrow rail"
    )


def test_uuids_are_kept_out_of_everything_a_human_reads():
    """Surrogate keys are elided in the console; robot ids are not.

    The distinction is the whole point: `mission_id = '249456ae-…'` is 36
    characters an operator cannot act on, while `s1` and `m1` are the names on
    the map. A redaction that ate both would make the console useless.
    """
    body = (
        pathlib.Path(__file__).resolve().parents[1] / "client" / "ui-shared.js"
    ).read_text()
    assert "redactIds" in body
    for call in ("redactIds(answer.text)", "redactIds(answer.sql)"):
        assert call in body, f"{call} missing: uuids reach the console again"
    # Anchored at both ends, so it can only match a value that is *entirely* a
    # uuid. Without the anchors it would drop any column containing one.
    assert "UUID_WHOLE = /^[0-9a-f]{8}-" in body
    assert "}$/i" in body
