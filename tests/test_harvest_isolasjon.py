"""Harvest isolation: local staging with atomic publish to yield_dir, stale cleanup, and the entrypoint modes."""

import os
import subprocess
import time
from pathlib import Path

import pytest

from podimo import main

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    staging = tmp_path / "staging"
    yield_dir = tmp_path / "yield" / "alpha"
    yield_dir.mkdir(parents=True)
    monkeypatch.setenv(main.STAGING_ENV, str(staging))
    return staging, yield_dir


class FakeResponse:
    def __init__(self, chunks, fail_after=None):
        self.chunks, self.fail_after = chunks, fail_after

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, size):
        for i, c in enumerate(self.chunks):
            if self.fail_after is not None and i >= self.fail_after:
                raise ConnectionError("connection reset mid-download")
            yield c


class FakeScraper:
    def __init__(self, response):
        self.response = response

    def get(self, url, stream, timeout):
        return self.response


def test_interrupted_download_leaves_no_file_in_yield_dir(dirs):
    staging, yield_dir = dirs
    final = yield_dir / "ep1.mp3"
    scraper = FakeScraper(FakeResponse([b"a" * 1000, b"b" * 1000], fail_after=1))
    with pytest.raises(ConnectionError):
        main._download_file_sync(scraper, "https://x/ep1.mp3", final)
    assert not final.exists()
    assert list(yield_dir.iterdir()) == []  # no final name and no .part on the share
    assert list(staging.iterdir()) == []  # staging is cleaned up too


def test_completed_download_appears_in_one_step(dirs, monkeypatch):
    staging, yield_dir = dirs
    final = yield_dir / "ep1.mp3"
    seen_before_replace = []
    real_replace = os.replace

    def watching_replace(src, dst):
        # at the moment of the rename the final name does not exist yet and the data is complete
        seen_before_replace.append((Path(src).name, Path(dst).exists(), Path(src).stat().st_size))
        return real_replace(src, dst)

    monkeypatch.setattr(main.os, "replace", watching_replace)
    scraper = FakeScraper(FakeResponse([b"a" * 1000, b"b" * 1000]))
    main._download_file_sync(scraper, "https://x/ep1.mp3", final)
    assert seen_before_replace == [("ep1.mp3.part", False, 2000)]
    assert final.read_bytes() == b"a" * 1000 + b"b" * 1000
    assert sorted(p.name for p in yield_dir.iterdir()) == ["ep1.mp3"]  # no .part left
    assert list(staging.iterdir()) == []


def test_publish_failure_removes_part(dirs, monkeypatch):
    staging, yield_dir = dirs
    src = staging / "x"
    staging.mkdir(exist_ok=True)
    src.write_bytes(b"data")

    def failing_replace(a, b):
        raise OSError("share went away")

    monkeypatch.setattr(main.os, "replace", failing_replace)
    with pytest.raises(OSError):
        main.publish_file(src, yield_dir / "ep.mp3")
    assert list(yield_dir.iterdir()) == []


def test_feed_and_status_writes_are_atomic(dirs):
    _, yield_dir = dirs
    feed = yield_dir / "feed.xml"
    main.write_text_atomic(feed, "<rss>1</rss>")
    main.write_text_atomic(feed, "<rss>2</rss>")
    assert feed.read_text() == "<rss>2</rss>"
    assert sorted(p.name for p in yield_dir.iterdir()) == ["feed.xml"]


def test_stale_cleanup_ignores_files_newer_than_an_hour(dirs):
    staging, yield_dir = dirs
    staging.mkdir(exist_ok=True)
    old, new = staging / "old", staging / "new"
    old.write_bytes(b"x")
    new.write_bytes(b"x")
    two_hours_ago = time.time() - 7200
    os.utime(old, (two_hours_ago, two_hours_ago))
    main.clean_staging()
    assert not old.exists() and new.exists()  # a newer file may belong to a running harvest
    part_old, part_new = yield_dir / "a.mp3.part", yield_dir / "b.mp3.part"
    part_old.write_bytes(b"x")
    part_new.write_bytes(b"x")
    os.utime(part_old, (two_hours_ago, two_hours_ago))
    assert main.remove_stale(yield_dir, "*.part") == [part_old]
    assert part_new.exists()


def test_part_files_are_not_counted_as_episodes(tmp_path):
    from podimo.config import Auth, Config, Podcast

    cfg = Config(host="h", auth=Auth("a@b.c", "pw"), podcasts={"alpha": Podcast("a")}, yield_dir=str(tmp_path))
    d = tmp_path / "alpha"
    d.mkdir()
    (d / "ep1.mp3").write_bytes(b"x")
    (d / "ep2.mp3.part").write_bytes(b"x")
    assert main.harvested_episode_ids(cfg, "alpha") == ["ep1"]


# --- entrypoint modes --------------------------------------------------------------------------


def _run_entrypoint(tmp_path, mode):
    """Run docker/entrypoint.sh with stub commands on PATH; each stub records that it was started."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    for name in ("podimo", "cron", "crontab", "tail"):
        stub = bin_dir / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> {calls}\n')
        stub.chmod(0o755)
    crontab_src = tmp_path / "crontab"
    crontab_src.write_text("17 */4 * * * podimo harvest\n")
    script = ENTRYPOINT.read_text().replace("/etc/cron.d/podimo-crontab", str(crontab_src))
    ep = tmp_path / "entrypoint.sh"
    ep.write_text(script)
    env = {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "STAGING_DIR": str(tmp_path / "staging"),
        **({"PODIMO_MODE": mode} if mode else {}),
    }
    r = subprocess.run(["bash", str(ep), "--host", "0.0.0.0"], env=env, capture_output=True, text=True, timeout=20)
    time.sleep(0.2)  # the background tail stub
    started = [line.split()[0] for line in calls.read_text().splitlines()] if calls.exists() else []
    return r, started, (calls.read_text() if calls.exists() else "")


@pytest.mark.parametrize("mode", [None, "web"])
def test_web_mode_starts_only_the_server(tmp_path, mode):
    r, started, raw = _run_entrypoint(tmp_path, mode)
    assert r.returncode == 0, r.stderr
    assert started == ["podimo"] and "podimo serve --host 0.0.0.0" in raw


def test_harvest_mode_starts_only_cron(tmp_path):
    r, started, raw = _run_entrypoint(tmp_path, "harvest")
    assert r.returncode == 0, r.stderr
    assert "podimo" not in started  # no server
    assert "cron" in started and "cron -f" in raw
    assert "crontab" in started


def test_unknown_mode_fails(tmp_path):
    r, started, _ = _run_entrypoint(tmp_path, "both")
    assert r.returncode == 64 and started == []
