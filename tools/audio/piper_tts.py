"""Piper local text-to-speech provider tool."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class PiperTTS(BaseTool):
    name = "piper_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "piper"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["cmd:piper"]
    install_instructions = (
        "Install Piper TTS:\n"
        "  pip install piper-tts\n"
        "Or download from https://github.com/rhasspy/piper/releases\n"
        "Then download a voice model:\n"
        "  piper --download-dir ~/.piper/models --model en_US-lessac-medium"
    )
    agent_skills = ["text-to-speech"]

    capabilities = [
        "text_to_speech",
        "offline_generation",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": False,
        "offline": True,
        "native_audio": True,
    }
    best_for = [
        "offline narration fallback",
        "privacy-sensitive local-only workflows",
    ]
    not_good_for = [
        "best-in-class expressive voice quality",
        "voice clone matching",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "model": {
                "type": "string",
                "default": "en_US-lessac-medium",
            },
            "speaker_id": {
                "type": "integer",
                "default": 0,
            },
            "length_scale": {
                "type": "number",
                "default": 1.0,
            },
            "sentence_silence": {
                "type": "number",
                "default": 0.3,
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=512, vram_mb=0, disk_mb=200, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=[])
    idempotency_key_fields = ["text", "model", "speaker_id", "length_scale"]
    side_effects = ["writes audio file to output_path"]
    user_visible_verification = ["Listen to generated audio for intelligibility"]

    def get_status(self) -> ToolStatus:
        if self._piper_runner() is not None:
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    @staticmethod
    def _piper_runner() -> list[str] | None:
        """Command prefix that runs Piper, or None when it is unavailable.

        `shutil.which("piper")` alone is not enough: `piper-tts` installs the
        executable into the *interpreter's* environment (venv/conda bin), and
        PATH does not necessarily contain it — e.g. an agent shell that calls
        the venv's python directly without activating it, which is how the
        repo's own tooling is invoked. That made a correctly installed Piper
        report UNAVAILABLE. Resolution order:

          1. `piper` on PATH
          2. `piper` next to the running interpreter (venv/conda bin dir)
          3. the `piper` Python module via `python -m piper`
        """
        exe = shutil.which("piper")
        if exe:
            return [exe]

        candidates = [Path(sys.executable).parent / ("piper.exe" if os.name == "nt" else "piper")]
        for cand in candidates:
            if cand.is_file() and os.access(cand, os.X_OK):
                return [str(cand)]

        try:
            import importlib.util

            if importlib.util.find_spec("piper") is not None:
                return [sys.executable, "-m", "piper"]
        except (ImportError, ValueError):
            pass

        return None

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    # ------------------------------------------------------------------
    # Voice model resolution
    #
    # Piper >=1.2 (including piper-tts 1.8) accepts either an explicit path
    # to a voice `.onnx` file or a voice *name* that is already present in
    # the `--data-dir` directory. A bare name that has never been downloaded
    # fails with "Unable to find voice: <name>". Resolve the model before
    # invoking the CLI, downloading the voice on first use when needed.
    # ------------------------------------------------------------------
    _DEFAULT_MODEL = "en_US-lessac-medium"
    _VOICE_SUFFIXES = (".onnx",)

    @classmethod
    def _voice_data_dirs(cls) -> list[Path]:
        """Candidate voice directories, most explicit first.

        An explicit `PIPER_VOICE_DIR` / `PIPER_DATA_DIR` is authoritative —
        when set it is the only directory consulted, so resolution is
        predictable. Otherwise the standard Piper locations are searched.
        """
        env_dir = os.environ.get("PIPER_VOICE_DIR") or os.environ.get("PIPER_DATA_DIR")
        if env_dir:
            return [Path(env_dir).expanduser()]
        return [
            Path.home() / ".piper" / "voices",
            Path.home() / ".piper" / "models",
            Path.home() / ".local" / "share" / "piper",
        ]

    @classmethod
    def _resolve_model(cls, model: str) -> Path | None:
        """Return a usable .onnx path for `model`, or None if not found.

        `model` may be an explicit path (returned if it exists) or a voice
        name such as `en_US-lessac-medium`.
        """
        candidate = Path(model).expanduser()
        if candidate.suffix == ".onnx":
            return candidate if candidate.exists() else None
        for voice_dir in cls._voice_data_dirs():
            onnx = voice_dir / f"{model}.onnx"
            if onnx.exists():
                return onnx
        return None

    @classmethod
    def _download_voice(cls, model: str) -> Path | None:
        """Download a voice model into the primary voice data dir.

        Uses `piper.download_voices` when importable, then falls back to a
        direct download from the upstream HuggingFace voice repository.
        Returns the resolved .onnx path, or None on failure.
        """
        target_dir = cls._voice_data_dirs()[0]
        target_dir.mkdir(parents=True, exist_ok=True)

        # Preferred: bundled downloader (piper-tts >= 1.2).
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "piper.download_voices", model,
                 "--data-dir", str(target_dir)],
                capture_output=True,
                text=True,
                timeout=600,
            )
            onnx = target_dir / f"{model}.onnx"
            if proc.returncode == 0 and onnx.exists():
                return onnx
        except Exception:
            pass

        # Fallback: direct fetch from the upstream voice repository.
        base = "https://huggingface.co/rhasspy/piper-voices/resolve/main"
        lang_full, _, voice = model.partition("-")
        lang = lang_full.split("_")[0]
        quality = model.rsplit("-", 1)[-1]
        url_dir = f"{base}/{lang}/{lang_full}/{voice}/{quality}/{model}"
        try:
            for suffix in (".onnx", ".onnx.json"):
                dest = target_dir / f"{model}{suffix}"
                if dest.exists():
                    continue
                urllib.request.urlretrieve(f"{url_dir}{suffix}", dest)
            onnx = target_dir / f"{model}.onnx"
            if onnx.exists():
                return onnx
        except Exception:
            pass

        return None

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(success=False, error="Piper TTS not available. " + self.install_instructions)

        model_name = inputs.get("model") or self._DEFAULT_MODEL
        resolved = self._resolve_model(model_name)
        if resolved is None:
            resolved = self._download_voice(model_name)
        if resolved is None:
            return ToolResult(
                success=False,
                error=(
                    f"Piper voice '{model_name}' is not downloaded and could not be "
                    "fetched automatically. Download it once with:\n"
                    f"  {sys.executable} -m piper.download_voices {model_name} "
                    f"--data-dir {self._voice_data_dirs()[0]}\n"
                    "Or pass an explicit path to a .onnx voice file as `model`."
                ),
            )

        start = time.time()
        try:
            result = self._generate(inputs, resolved)
        except Exception as exc:
            return ToolResult(success=False, error=f"Local TTS generation failed: {exc}")

        result.duration_seconds = round(time.time() - start, 2)
        return result

    def _generate(self, inputs: dict[str, Any], model_path: Path) -> ToolResult:
        output_path = Path(inputs.get("output_path", "tts_output.wav"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        model_name = inputs.get("model") or self._DEFAULT_MODEL

        runner = self._piper_runner()
        if runner is None:
            return ToolResult(success=False, error="Piper TTS not available. " + self.install_instructions)

        cmd = [
            *runner,
            "--model", str(model_path),
            "--length-scale", str(inputs.get("length_scale", 1.0)),
            "--sentence-silence", str(inputs.get("sentence_silence", 0.3)),
            "--output_file", str(output_path),
        ]

        # Single-speaker voices (e.g. lessac) reject an explicit --speaker
        # flag, so only pass it when the voice actually exposes speakers.
        speaker_id = inputs.get("speaker_id")
        if speaker_id is not None and self._voice_has_speakers(model_path):
            cmd[2:2] = ["--speaker", str(speaker_id)]

        proc = subprocess.run(
            cmd,
            input=inputs["text"],
            capture_output=True,
            text=True,
            timeout=300,
        )

        if proc.returncode != 0:
            return ToolResult(success=False, error=f"Piper failed (exit {proc.returncode}): {proc.stderr}")
        if not output_path.exists():
            return ToolResult(success=False, error=f"Piper output file missing: {output_path}")

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "model": model_name,
                "model_path": str(model_path),
                "speaker_id": speaker_id,
                "text_length": len(inputs["text"]),
                "output": str(output_path),
                "format": "wav",
            },
            artifacts=[str(output_path)],
            model=model_name,
        )

    @staticmethod
    def _voice_has_speakers(model_path: Path) -> bool:
        """True when the voice's config declares a multi-speaker model."""
        config_path = model_path.with_suffix(".onnx.json")
        if not config_path.exists():
            return False
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                config = json.load(fh)
            return int(config.get("num_speakers", 1)) > 1
        except Exception:
            return False
