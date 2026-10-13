"""Tests for Remotion render concurrency passthrough in video_compose.

Offthread frame extraction from many video sources in parallel is a common
cause of the compositor's "No frame found at position N for source ..."
failures on CPU/RAM-constrained machines. Pinning `concurrency` is the
standard mitigation, but the templated Remotion path previously ignored it
(only the atelier/bespoke path forwarded `--concurrency`). Worse, the
high-level render path (execute -> _render) rebuilds the remotion inputs
dict and dropped `options` entirely, so even a wired-through flag would
have been silently lost. These tests pin both halves down.
"""

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


def _capture_cmd(tool, monkeypatch, payload):
    seen = {}

    def fake_run_command(cmd, *a, **k):
        seen["cmd"] = cmd
        return None  # output file intentionally absent

    monkeypatch.setattr(tool, "run_command", fake_run_command)
    tool._remotion_render(payload)
    return seen["cmd"]


def test_concurrency_flag_passed_from_top_level_options(tool, tmp_path, monkeypatch):
    cmd = _capture_cmd(
        tool,
        monkeypatch,
        {
            "composition_data": {"cuts": []},
            "output_path": str(tmp_path / "out.mp4"),
            "options": {"concurrency": 1},
        },
    )

    assert "--concurrency=1" in cmd


def test_concurrency_flag_passed_from_composition_data(tool, tmp_path, monkeypatch):
    cmd = _capture_cmd(
        tool,
        monkeypatch,
        {
            "composition_data": {"cuts": [], "concurrency": 2},
            "output_path": str(tmp_path / "out.mp4"),
        },
    )

    assert "--concurrency=2" in cmd


def test_top_level_options_win_over_composition_data(tool, tmp_path, monkeypatch):
    cmd = _capture_cmd(
        tool,
        monkeypatch,
        {
            "composition_data": {"cuts": [], "concurrency": 8},
            "output_path": str(tmp_path / "out.mp4"),
            "options": {"concurrency": 1},
        },
    )

    assert "--concurrency=1" in cmd
    assert "--concurrency=8" not in cmd


def test_no_concurrency_flag_when_not_requested(tool, tmp_path, monkeypatch):
    cmd = _capture_cmd(
        tool,
        monkeypatch,
        {"composition_data": {"cuts": []}, "output_path": str(tmp_path / "out.mp4")},
    )

    assert not any(str(c).startswith("--concurrency") for c in cmd)


def test_invalid_concurrency_value_is_ignored(tool, tmp_path, monkeypatch):
    cmd = _capture_cmd(
        tool,
        monkeypatch,
        {
            "composition_data": {"cuts": []},
            "output_path": str(tmp_path / "out.mp4"),
            "options": {"concurrency": "as-many-as-possible"},
        },
    )

    assert not any(str(c).startswith("--concurrency") for c in cmd)


def test_high_level_render_forwards_options_to_remotion(tool, tmp_path, monkeypatch):
    # The gap this pins: execute(operation="render") -> _render() rebuilds the
    # remotion inputs dict, so anything _remotion_render() reads off `options`
    # must be forwarded there, not only on a direct _remotion_render() call.
    captured = {}
    monkeypatch.setattr(tool, "_pre_compose_validation", lambda *a, **k: None)
    monkeypatch.setattr(tool, "_needs_remotion", lambda *a, **k: True)

    def fake_remotion_render(inputs):
        captured.update(inputs)
        from tools.base_tool import ToolResult

        return ToolResult(success=True, data={}, artifacts=[])

    monkeypatch.setattr(tool, "_remotion_render", fake_remotion_render)
    monkeypatch.setattr(tool, "_run_final_review", lambda *a, **k: {})

    tool._render(
        {
            "edit_decisions": {
                "render_runtime": "remotion",
                "renderer_family": "explainer-data",
                "cuts": [{"id": "c1", "source": "a1", "in_seconds": 0, "out_seconds": 2}],
            },
            "asset_manifest": {"assets": [{"id": "a1", "path": "/tmp/a1.mp4"}]},
            "output_path": str(tmp_path / "out.mp4"),
            "options": {"concurrency": 1},
        }
    )

    assert captured.get("options") == {"concurrency": 1}
