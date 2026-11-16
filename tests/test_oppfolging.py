"""Follow-ups from review: missing config gives 503 on /status, --workers > 1 needs a config file."""

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from podimo import api as api_module
from podimo import cli
from podimo.api import api, api_config


def test_status_without_config_is_503_not_500(monkeypatch):
    monkeypatch.setattr(api_module, "_status_cache", None)
    api.dependency_overrides[api_config] = lambda: None
    try:
        r = TestClient(api).get("/status")
    finally:
        api.dependency_overrides.clear()
    assert r.status_code == 503 and r.json()["problems"] == ["no configuration loaded"]


@pytest.mark.parametrize(
    "args, n",
    [([], 1), (["--port", "1"], 1), (["--workers", "2"], 2), (["--workers=3"], 3), (["--workers", "x"], 1)],
)
def test_workers_requested(args, n):
    assert cli.workers_requested(args) == n


def test_serve_refuses_workers_without_config_file(monkeypatch):
    started = []
    monkeypatch.setattr(cli.uvicorn.main, "main", lambda args: started.append(args))
    r = CliRunner().invoke(cli.cli, ["serve", "-c", "-", "--workers", "2"], input="host: http://h\nauth: {email: a@b.c, password: pw}\nyield_dir: y\npodcasts:\n  alpha: {podcast_id: a}\n")
    flat = "".join(ch for ch in r.output if ch.isalnum() or ch in "->")  # rich wraps the message in a box
    assert r.exit_code == 2 and "needstheconfigurationinafile" in flat
    assert started == []
    r = CliRunner().invoke(cli.cli, ["serve", "-c", "-"], input="host: http://h\nauth: {email: a@b.c, password: pw}\nyield_dir: y\npodcasts:\n  alpha: {podcast_id: a}\n")  # one worker from stdin is fine
    assert r.exit_code == 0 and len(started) == 1
