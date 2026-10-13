"""Tests for Piper runner resolution in piper_tts.

Field report: `piper-tts` was installed, `piper --help` worked from the venv,
and yet `PiperTTS.get_status()` returned UNAVAILABLE and every narration call
failed with "Piper TTS not available. Install Piper TTS: pip install piper-tts".

Root cause: the availability gate was a bare `shutil.which("piper")`. The
piper-tts wheel installs its executable into the *interpreter's* bin directory
(venv/conda), which is on PATH only when the environment is activated. Agent
shells routinely invoke the venv's python directly without activation, so a
correctly installed Piper looked missing. Worse, the failure message then told
the user to install something they already had.

Resolution order under test: PATH -> interpreter bin dir -> `python -m piper`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.audio.piper_tts import PiperTTS  # noqa: E402
from tools.base_tool import ToolStatus  # noqa: E402


@pytest.fixture
def tool():
    return PiperTTS()


def test_path_binary_wins(tool, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/piper")

    assert tool._piper_runner() == ["/usr/local/bin/piper"]
    assert tool.get_status() == ToolStatus.AVAILABLE


def test_falls_back_to_interpreter_bin_dir(tool, tmp_path, monkeypatch):
    # Simulate a venv: sys.executable lives in <venv>/bin with a piper script.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_piper = bin_dir / "piper"
    fake_piper.write_text("#!/bin/sh\n")
    fake_piper.chmod(0o755)
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr("sys.executable", str(bin_dir / "python"))

    assert tool._piper_runner() == [str(fake_piper)]
    assert tool.get_status() == ToolStatus.AVAILABLE


def test_falls_back_to_module_invocation(tool, monkeypatch):
    # Nothing on PATH, nothing next to the interpreter, but the package is
    # importable -> run it as a module rather than declaring Piper missing.
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr("sys.executable", "/nowhere/python")
    monkeypatch.setattr("importlib.util.find_spec", lambda _: object())

    assert tool._piper_runner() == ["/nowhere/python", "-m", "piper"]
    assert tool.get_status() == ToolStatus.AVAILABLE


def test_unavailable_when_truly_missing(tool, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr("sys.executable", "/nowhere/python")
    monkeypatch.setattr("importlib.util.find_spec", lambda _: None)

    assert tool._piper_runner() is None
    assert tool.get_status() == ToolStatus.UNAVAILABLE


def test_generate_uses_resolved_runner(tool, tmp_path, monkeypatch):
    # The invocation must use whatever _piper_runner resolved — the old code
    # hardcoded "piper" and relied on PATH.
    seen = {}

    class FakeProc:
        returncode = 0
        stderr = ""

    def fake_run(cmd, *a, **k):
        seen["cmd"] = cmd
        (tmp_path / "out.wav").write_bytes(b"RIFF")
        return FakeProc()

    monkeypatch.setattr(tool, "_piper_runner", lambda: ["/venv/bin/piper"])
    monkeypatch.setattr(tool, "_voice_has_speakers", lambda _p: False)
    monkeypatch.setattr("subprocess.run", fake_run)

    result = tool._generate({"text": "hello", "output_path": str(tmp_path / "out.wav")}, tmp_path / "voice.onnx")

    assert result.success is True
    assert seen["cmd"][0] == "/venv/bin/piper"


def test_generate_reports_unavailable_instead_of_crashing(tool, tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "_piper_runner", lambda: None)

    result = tool._generate({"text": "hello", "output_path": str(tmp_path / "out.wav")}, tmp_path / "voice.onnx")

    assert result.success is False
    assert "not available" in result.error.lower()
