"""Narration synthesis — Piper TTS per section + voiceover concat.

Only used when the input has no voiceover audio. Mirrors the exact
contract the Kaggle notebook and the agent pipeline produce:
``script.json`` + ``sections/<id>.mp3`` + ``voiceover.mp3``.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Optional


def piper_available() -> bool:
    try:
        import piper  # noqa: F401

        return True
    except Exception:
        return False


def synthesize_narration(
    script: dict,
    work_dir: Path,
    voice: str = "en_US-lessac-medium",
    voices_dir: Optional[Path] = None,
) -> Path:
    """Synthesize sections/*.mp3 and voiceover.mp3 into ``work_dir``.

    Returns the voiceover path. Raises RuntimeError with actionable
    guidance when Piper is unavailable.
    """
    if not piper_available():
        raise RuntimeError(
            "no voiceover supplied and piper-tts is not installed.\n"
            "Either provide a voiceover file as input, or install it:\n"
            "  python -m pip install piper-tts"
        )

    sections_dir = work_dir / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)

    if voices_dir is not None:
        voices_dir.mkdir(parents=True, exist_ok=True)
        import os

        os.environ.setdefault("PIPER_VOICE_DIR", str(voices_dir))
        subprocess.run(
            ["python", "-m", "piper.download_voices", voice,
             "--data-dir", str(voices_dir)],
            check=False,  # already-downloaded voices make this a no-op that errors
        )

    from tools.audio.piper_tts import PiperTTS  # repo import (sys.path set by runner)

    tts = PiperTTS()
    parts: list[str] = []
    for sec in script["sections"]:
        dest = sections_dir / f"{sec['id']}.mp3"
        if not dest.exists():  # resumable
            result = tts.execute(
                {"text": sec["text"], "output_path": str(dest), "model": voice}
            )
            if not result.success:
                raise RuntimeError(f"piper failed for section {sec['id']}: {result.error}")
        parts.append(str(dest))

    voiceover = work_dir / "voiceover.mp3"
    if not voiceover.exists():
        listf = work_dir / "concat.txt"
        listf.write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
             "-i", str(listf), "-c:a", "libmp3lame", "-b:a", "128k", "-ar", "22050",
             str(voiceover)],
            check=True,
        )
    return voiceover


def transcribe_words(audio: Path, out_dir: Path, gpu_id: Optional[int] = None) -> list[dict]:
    """Whisper word timestamps (GPU when available). Raises on failure."""
    import os
    import sys

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    if gpu_id is not None:
        os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    from tools.analysis.transcriber import Transcriber

    result = Transcriber().execute(
        {"input_path": str(audio), "model_size": "medium", "language": "en",
         "output_dir": str(out_dir / "transcript")}
    )
    if not result.success:
        raise RuntimeError(f"transcription failed: {result.error}")
    return result.data["word_timestamps"]
