"""Tests for the Remotion headless-browser pre-warm in video_compose.

``npx remotion render`` lazily downloads chrome-headless-shell on first use.
When two renders start concurrently on a cold cache they race to unpack the
same zip into ``node_modules/.remotion`` and the slower process dies with
``ENOENT: no such file or directory, unlink ...zip?clear`` — observed killing
one of two parallel reel renders on a 4-vCPU Kaggle box. The fix runs
``npx remotion browser ensure`` (idempotent) under an exclusive lock before
every Remotion render. These tests pin the behaviour down.
"""

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.video.video_compose import VideoCompose  # noqa: E402


@pytest.fixture
def tool(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/npx")
    return VideoCompose()


def test_ensure_browser_runs_idempotent_command(tool, tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        tool,
        "run_command",
        lambda cmd, **k: seen.update(cmd=list(cmd), **k) or None,
    )

    tool._ensure_remotion_browser(tmp_path)

    assert seen["cmd"][:4] == ["npx", "remotion", "browser", "ensure"]
    # Ran from inside the composer dir so npx resolves the local binary.
    assert seen.get("cwd") == tmp_path
    # The lock file lives next to the browser cache it protects.
    lock = tmp_path / "node_modules" / ".remotion" / "browser-ensure.lock"
    assert lock.exists()


def test_render_calls_browser_ensure_before_render(tool, tmp_path, monkeypatch):
    calls = []

    def fake_run_command(cmd, **kwargs):
        calls.append(list(cmd))

    monkeypatch.setattr(tool, "run_command", fake_run_command)
    tool._remotion_render(
        {
            "composition_data": {"cuts": []},
            "output_path": str(tmp_path / "out.mp4"),
        }
    )

    assert calls, "render produced no subprocess calls"
    first = calls[0]
    assert first[:4] == ["npx", "remotion", "browser", "ensure"]
    renders = [c for c in calls if c[:3] == ["npx", "remotion", "render"]]
    assert renders, "no render command issued"
    # Every browser-ensure call precedes the first render command.
    assert calls.index(renders[0]) > 0


def test_browser_ensure_failure_does_not_kill_render(tool, tmp_path, monkeypatch):
    """A broken ensure step must degrade to a warning, not break rendering.

    If the browser is already present, `browser ensure` failing (offline
    box, proxy hiccup) is not fatal — the render will retry the setup
    itself and either succeed or report the real error.
    """

    def flaky_run_command(cmd, **kwargs):
        if cmd[:4] == ["npx", "remotion", "browser", "ensure"]:
            raise subprocess.CalledProcessError(1, cmd, stderr="boom")
        return None

    monkeypatch.setattr(tool, "run_command", flaky_run_command)
    # Must not raise.
    tool._ensure_remotion_browser(tmp_path)


def test_browser_ensure_timeout_does_not_kill_render(tool, tmp_path, monkeypatch):
    def slow_run_command(cmd, **kwargs):
        if cmd[:4] == ["npx", "remotion", "browser", "ensure"]:
            raise subprocess.TimeoutExpired(cmd, 900)
        return None

    monkeypatch.setattr(tool, "run_command", slow_run_command)
    tool._ensure_remotion_browser(tmp_path)  # must not raise


def test_missing_node_modules_is_silently_tolerated(tool, tmp_path, monkeypatch):
    """A composer dir without node_modules is the render's error to report."""
    called = []
    monkeypatch.setattr(
        tool, "run_command", lambda cmd, **k: called.append(cmd) or None
    )
    # Make mkdir fail as if the parent is read-only / does not exist.
    def refuse_mkdir(*args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "mkdir", refuse_mkdir)
    composer = tmp_path / "no-such-dir" / "composer"
    tool._ensure_remotion_browser(composer)
    assert not called
