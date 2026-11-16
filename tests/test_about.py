"""About Rota: every signed-in user sees the running release, build, engines and schema, and the worker's own report."""
import json
from fastapi.testclient import TestClient
from apps.api.schema import supported_revision
from apps.api.version import __version__
from apps.worker.main import process_one, _beat
from packages.domain import fairness, score
from packages.domain.generation import ALGORITHM_VERSION
from packages.domain.solver_rules import COMPILER_VERSION
from packages.domain.validation import ENGINE_VERSION


def test_about_names_the_build_engines_schema_and_the_worker(h, monkeypatch):
    monkeypatch.setenv("ROTA_BUILD_COMMIT", "0123456789abcdef")
    monkeypatch.setenv("ROTA_BUILD_DATE", "2026-10-09T08:00:00Z")
    monkeypatch.setenv("ROTA_IMAGE_DIGEST", "ghcr.io/example-org/rota@sha256:" + "ab" * 32)
    about = h.get("staff", "/api/about")
    assert about["version"] == __version__ and about["commit"] == "0123456"
    assert about["release"] == __version__ + "+0123456" and about["build_date"] == "2026-10-09T08:00:00Z"
    assert about["image_digest"] == "sha256:abababababab" and about["migration"] == supported_revision()
    assert about["engines"] == {"compiler": COMPILER_VERSION, "validator": ENGINE_VERSION, "greedy": ALGORITHM_VERSION,
                                "score": score.VERSION, "fairness": fairness.VERSION}
    assert about["deployment"] == h.settings.deployment and about["worker"] is None
    assert "://" not in json.dumps(about) and "ghcr.io" not in json.dumps(about)  # no hostnames or URLs
    me = h.get("staff", "/api/me")
    assert me["version"] == __version__ and me["release"] == __version__ + "+0123456"

    # The worker reports the release it runs; a worker on another build shows as a different release.
    monkeypatch.setenv("ROTA_BUILD_COMMIT", "fedcba9876543210")
    _beat.clear()
    process_one(h.app.state.engine)
    worker = h.get("staff", "/api/about")["worker"]
    assert worker["release"] == __version__ + "+fedcba9" and worker["last_seen_at"]


def test_about_without_a_build_and_signed_out(h, monkeypatch):
    monkeypatch.delenv("ROTA_BUILD_COMMIT", raising=False)
    monkeypatch.delenv("ROTA_BUILD_DATE", raising=False)
    monkeypatch.delenv("ROTA_IMAGE_DIGEST", raising=False)
    about = h.get("auditor", "/api/about")
    assert about["release"] == about["version"] == __version__
    assert about["commit"] is None and about["build_date"] is None and about["image_digest"] is None
    with TestClient(h.app, base_url=h.settings.public_origin) as anonymous:
        assert anonymous.get("/api/about").status_code == 401
