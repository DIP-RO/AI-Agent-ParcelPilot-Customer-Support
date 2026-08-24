"""The SPA static route must not serve files outside frontend/dist."""

from pathlib import Path

from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)

DIST = Path(config.REPO_ROOT) / "frontend" / "dist"


def test_path_traversal_blocked_when_dist_present():
    if not DIST.exists():
        return  # single-service route only mounts when a build exists
    # Encoded traversal that previously leaked backend source / secrets.
    for evil in [
        "/..%2f..%2fbackend%2fapp%2fconfig.py",
        "/..%2f..%2frequirements.txt",
        "/../../backend/app/config.py",
    ]:
        r = client.get(evil)
        body = r.text
        assert "SESSION_SECRET" not in body
        assert "ANTHROPIC" not in body
        # Either a clean 404 or the SPA shell fallback — never file contents.
        assert r.status_code in (200, 404)
        if r.status_code == 200:
            assert "<!doctype html>" in body.lower() or "<div id=\"root\">" in body


def test_real_asset_still_served_if_present():
    if not DIST.exists():
        return
    r = client.get("/index.html")
    assert r.status_code == 200
