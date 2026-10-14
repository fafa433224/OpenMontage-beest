"""Regression tests for synth_broll's crash-safe staging (atomic writes).

Bug history: an interrupted synth_broll run left a 48-byte headers-only
bokeh-cool.mp4; the reuse check trusted ``exists()`` and every later render
staged a corrupt clip. Fixed by (a) encoding to ``.part`` + atomic rename
and (b) a ``_reusable()`` size gate.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "kaggle"))

import synth_broll  # noqa: E402


def test_reusable_rejects_truncated_stub(tmp_path):
    stub = tmp_path / "bokeh-cool.mp4"
    stub.write_bytes(b"\x00" * 48)  # headers-only crash leftover
    assert synth_broll._reusable(stub) is False


def test_reusable_rejects_missing_file(tmp_path):
    assert synth_broll._reusable(tmp_path / "nope.mp4") is False


def test_reusable_accepts_real_clip(tmp_path):
    clip = tmp_path / "bokeh-cool.mp4"
    clip.write_bytes(b"\x00" * 500_000)  # well above the 200 KB floor
    assert synth_broll._reusable(clip) is True


def test_encode_clip_writes_temp_then_renames(tmp_path):
    """encode_clip must write to a .part sibling and rename only on success,
    and must force the mp4 muxer (a .part extension is unknown to ffmpeg)."""
    import numpy as np

    out = tmp_path / "clip.mp4"
    frames = (np.zeros((synth_broll.H, synth_broll.W, 3), dtype=np.uint8)
              for _ in range(15))  # 15 frames @30fps = 0.5s
    synth_broll.encode_clip(frames, out, seconds=1)
    # all-black frames compress to ~3 KB — existence + container validity is
    # the contract here, not size (size gating is covered by _reusable tests)
    assert out.exists() and out.stat().st_size > 1_000
    assert not list(tmp_path.glob("*.part")), "temp file must be renamed away"


def test_encode_clip_failure_leaves_no_final(tmp_path, monkeypatch):
    """A generator that dies mid-stream must not leave a final output file."""
    import numpy as np

    def bad_frames():
        yield np.zeros((synth_broll.H, synth_broll.W, 3), dtype=np.uint8)
        raise RuntimeError("simulated generator crash")

    out = tmp_path / "crash.mp4"
    try:
        synth_broll.encode_clip(bad_frames(), out, seconds=1)
    except RuntimeError:
        pass
    assert not out.exists(), "failed encode must not produce a final file"
    assert not list(tmp_path.glob("*.part")), "failed encode must clean up its temp"
