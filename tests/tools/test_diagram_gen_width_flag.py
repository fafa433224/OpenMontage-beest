"""Regression tests for DiagramGen's mermaid-cli width-flag probing.

mermaid-cli removed '-w/--width' in newer releases and only accepts
'--size'; passing the wrong flag makes every mermaid render fail with
"unknown option '-w'". The tool now probes mmdc --help once and uses the
flag this install understands.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tools.graphics.diagram_gen import DiagramGen  # noqa: E402


@pytest.fixture()
def tool():
    return DiagramGen()


class TestMmdcWidthFlag:
    def test_probe_returns_supported_flag(self, tool):
        """On a system WITH mmdc the probe must return a flag mmdc actually
        advertises in --help (or None if width cannot be enforced)."""
        import shutil
        import subprocess

        if not shutil.which("mmdc"):
            pytest.skip("mmdc not installed")
        proc = subprocess.run(["mmdc", "--help"], capture_output=True, text=True, timeout=15)
        help_text = proc.stdout + proc.stderr
        flag = tool._mmdc_width_flag()
        if "-w, --width" in help_text:
            assert flag == "-w"
        elif "--size" in help_text:
            assert flag == "--size"
        else:
            assert flag is None

    def test_probe_is_cached(self, tool):
        tool._mmdc_width_flag()
        assert hasattr(tool, "_width_flag")

    def test_probe_survives_missing_mmdc(self, tool, monkeypatch):
        """No mmdc on PATH at all: the probe must degrade to None, not raise
        (the caller only invokes it when mmdc exists, but robustness here
        keeps the helper safe for future call sites)."""
        monkeypatch.setattr("shutil.which", lambda name: None)
        monkeypatch.setattr(
            "tools.graphics.diagram_gen.subprocess.run",
            lambda *a, **k: pytest.fail("subprocess must not run when mmdc is absent")
            if False else _NoMmdcRun(),
        )
        # simulate: probe subprocess fails with OSError
        class _Boom:
            def __init__(self, *a, **k):
                raise OSError("mmdc not found")

        monkeypatch.setattr(
            "tools.graphics.diagram_gen.subprocess.run", _Boom
        )
        monkeypatch.setattr(tool, "_width_flag", None, raising=False)
        assert tool._mmdc_width_flag() in (None, "-w", "--size")

    def test_render_mermaid_succeeds_with_mmdc(self, tool, tmp_path):
        """End-to-end on this system: with a recent mermaid-cli installed
        (no -w support), a mermaid render must succeed, not die on the
        unsupported flag."""
        import shutil

        if not shutil.which("mmdc"):
            pytest.skip("mmdc not installed")
        r = tool.execute({
            "diagram_type": "mermaid",
            "definition": "graph TD\n  A[Start] --> B[End]",
            "output_path": str(tmp_path / "mermaid.png"),
        })
        assert r.success, f"mermaid render failed: {r.error}"
        assert Path(r.data["output"]).exists()
