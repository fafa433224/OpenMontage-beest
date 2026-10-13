"""Tests for Remotion temp-space diagnostics in video_compose.

Field report: four full renders of a 60s hybrid project died with the
compositor error "No frame found at position <N> for source <clip>", with N
wandering between runs (5600 -> 5632 -> 3072) and the same innocent clip
named every time. Re-encoding clips to CFR and pinning concurrency both
failed to fix it; the actual constraint was the ~1 GB tmpfs holding
Remotion's working set (staged media copies + per-frame cache + audio
preprocessing + Chrome profile), which the render filled.

These tests pin the diagnostics that turn that dead end into a one-line fix.
"""

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.video import video_compose  # noqa: E402
from tools.video.video_compose import VideoCompose  # noqa: E402


@pytest.fixture
def tool(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/npx")
    return VideoCompose()


def test_temp_free_helper_reports_free_space(tool):
    free_mb = tool._remotion_temp_free_mb()

    assert free_mb is None or free_mb >= 0


def test_temp_free_helper_returns_none_when_uninspectable(tool, monkeypatch):
    def boom(_path):
        raise OSError("no such path")

    monkeypatch.setattr("shutil.disk_usage", boom)

    assert tool._remotion_temp_free_mb() is None


def test_no_frame_error_includes_temp_dir_hint(tool, tmp_path, monkeypatch):
    stderr = (
        "Rendering frames 137/1800\n"
        "Error  Compositor error: No frame found at position 5600 for source "
        "/tmp/remotion-assets-dir/6492991559207439.mp4 (original source = "
        "http://localhost:3000/public/44c28fa82cda-reveal_city.mp4)\n"
    )

    def fake_run_command(cmd, *a, **k):
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd, output="", stderr=stderr)

    monkeypatch.setattr(tool, "run_command", fake_run_command)
    result = tool._remotion_render(
        {"composition_data": {"cuts": []}, "output_path": str(tmp_path / "out.mp4")}
    )

    assert result.success is False
    assert "No frame found at position 5600" in result.error
    assert "TMPDIR" in result.error
    # The hint must steer away from the tempting-but-wrong fix.
    assert "re-encoding" in result.error


def test_unrelated_failure_gets_no_temp_hint(tool, tmp_path, monkeypatch):
    def fake_run_command(cmd, *a, **k):
        raise subprocess.CalledProcessError(
            returncode=1, cmd=cmd, output="", stderr="Cannot find module './src/index.tsx'"
        )

    monkeypatch.setattr(tool, "run_command", fake_run_command)
    result = tool._remotion_render(
        {"composition_data": {"cuts": []}, "output_path": str(tmp_path / "out.mp4")}
    )

    assert result.success is False
    assert "TMPDIR" not in result.error


def test_low_temp_space_warns_before_render(tool, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(tool, "_remotion_temp_free_mb", lambda: 128)
    monkeypatch.setattr(tool, "run_command", lambda cmd, *a, **k: None)

    with caplog.at_level("WARNING"):
        tool._remotion_render(
            {"composition_data": {"cuts": []}, "output_path": str(tmp_path / "out.mp4")}
        )

    assert any("only 128 MB free" in rec.message for rec in caplog.records)


def test_ample_temp_space_is_silent(tool, tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(tool, "_remotion_temp_free_mb", lambda: 8192)
    monkeypatch.setattr(tool, "run_command", lambda cmd, *a, **k: None)

    with caplog.at_level("WARNING"):
        tool._remotion_render(
            {"composition_data": {"cuts": []}, "output_path": str(tmp_path / "out.mp4")}
        )

    assert not any("MB free" in rec.message for rec in caplog.records)


def test_threshold_is_sane():
    # Calibration against the field report: the failing host had a ~1 GB
    # RAM-backed temp dir with only a few hundred MB free while a full render
    # was running, so the threshold must be high enough to fire there (>=512)
    # but low enough not to nag every healthy build host (<=4096).
    assert 512 <= video_compose._MIN_REMOTION_TEMP_FREE_MB <= 4096
