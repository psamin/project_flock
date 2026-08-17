"""The two pages actually load (§4.8, docs/designs/3d-simulation-view.md).

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


def test_the_2d_view_is_served(client):
    """`/` is the fallback the 3D route's WebGL check links to, so it has to
    work on a machine that could not render /sim3d."""
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "<title>Colony" in body
    assert "/static/app.js" in body


def test_the_3d_view_is_served(client):
    """/sim3d carries the submission video. A typo in the filename is a 500 on
    the URL in the writeup."""
    response = client.get("/sim3d")
    assert response.status_code == 200
    body = response.text
    assert "<title>Colony" in body
    # The import map is what lets the vendored Three.js addons resolve their
    # bare 'three' specifier with no bundler. Without it the page is blank.
    assert '"three":' in body
    assert "/static/vendor/three/three.module.min.js" in body


def test_the_3d_view_degrades_without_webgl(client):
    """The capability gate and its escape hatch ship in the HTML itself, ahead
    of the 756K of Three.js, so a machine without WebGL is told where to go
    instead of being shown a black rectangle (design doc premise 4)."""
    body = client.get("/sim3d").text
    assert "WebGL2RenderingContext" in body
    assert 'id="nogl"' in body
    assert 'href="/"' in body, "the fallback must link to the 2D view"


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
