"""Piper voice-model resolution.

`piper-tts` >= 1.2 resolves `--model` against a voice that is already on
disk; passing a bare voice name that was never downloaded dies with
"Unable to find voice: <name>". `PiperTTS` must resolve names to concrete
`.onnx` paths, bootstrap the download on first use, and only pass
`--speaker` for voices that actually declare multiple speakers.
"""

import json
import subprocess
from pathlib import Path

from tools.audio.piper_tts import PiperTTS

VOICE = "en_US-lessac-medium"


def _write_voice(directory: Path, name: str = VOICE, num_speakers: int = 1) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    onnx = directory / f"{name}.onnx"
    onnx.write_bytes(b"fake-model")
    (directory / f"{name}.onnx.json").write_text(
        json.dumps({"num_speakers": num_speakers}), encoding="utf-8"
    )
    return onnx


def test_resolve_model_finds_downloaded_voice(monkeypatch, tmp_path):
    voice_dir = tmp_path / "voices"
    onnx = _write_voice(voice_dir)
    monkeypatch.setenv("PIPER_VOICE_DIR", str(voice_dir))

    assert PiperTTS._resolve_model(VOICE) == onnx


def test_resolve_model_accepts_explicit_path(monkeypatch, tmp_path):
    onnx = _write_voice(tmp_path / "voices")

    assert PiperTTS._resolve_model(str(onnx)) == onnx
    assert PiperTTS._resolve_model(str(tmp_path / "missing.onnx")) is None


def test_resolve_model_returns_none_when_not_downloaded(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPER_VOICE_DIR", str(tmp_path / "empty"))

    assert PiperTTS._resolve_model(VOICE) is None


def test_execute_downloads_voice_on_first_use(monkeypatch, tmp_path):
    """A missing voice is fetched, not reported as a hard failure."""
    voice_dir = tmp_path / "voices"
    monkeypatch.setenv("PIPER_VOICE_DIR", str(voice_dir))
    monkeypatch.setattr(PiperTTS, "get_status", lambda self: "available")

    def fake_run(cmd, **kwargs):
        # cmd[0] is whatever _piper_runner() resolved (absolute path when piper
        # lives next to the interpreter) — match on the executable name.
        if Path(cmd[0]).name in ("piper", "piper.exe"):
            Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"RIFF")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        _write_voice(voice_dir)
        return subprocess.CompletedProcess(cmd, 0, "downloaded", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = PiperTTS().execute({"text": "hello", "output_path": str(tmp_path / "out.wav")})

    assert result.success, result.error
    assert result.data["model_path"] == str(voice_dir / f"{VOICE}.onnx")


def test_execute_reports_actionable_error_when_download_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("PIPER_VOICE_DIR", str(tmp_path / "voices"))
    monkeypatch.setattr(PiperTTS, "get_status", lambda self: "available")
    monkeypatch.setattr(PiperTTS, "_download_voice", classmethod(lambda cls, model: None))

    result = PiperTTS().execute({"text": "hello", "output_path": str(tmp_path / "out.wav")})

    assert not result.success
    assert VOICE in result.error
    assert "download_voices" in result.error


def test_single_speaker_voice_omits_speaker_flag(monkeypatch, tmp_path):
    voice_dir = tmp_path / "voices"
    _write_voice(voice_dir, num_speakers=1)
    monkeypatch.setenv("PIPER_VOICE_DIR", str(voice_dir))
    monkeypatch.setattr(PiperTTS, "get_status", lambda self: "available")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = PiperTTS().execute(
        {"text": "hello", "speaker_id": 0, "output_path": str(tmp_path / "out.wav")}
    )

    assert result.success, result.error
    assert "--speaker" not in captured["cmd"]


def test_multi_speaker_voice_passes_speaker_flag(monkeypatch, tmp_path):
    voice_dir = tmp_path / "voices"
    _write_voice(voice_dir, num_speakers=5)
    monkeypatch.setenv("PIPER_VOICE_DIR", str(voice_dir))
    monkeypatch.setattr(PiperTTS, "get_status", lambda self: "available")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        Path(cmd[cmd.index("--output_file") + 1]).write_bytes(b"RIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = PiperTTS().execute(
        {"text": "hello", "speaker_id": 2, "output_path": str(tmp_path / "out.wav")}
    )

    assert result.success, result.error
    assert captured["cmd"][captured["cmd"].index("--speaker") + 1] == "2"
