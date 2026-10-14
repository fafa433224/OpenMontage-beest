"""Visual audit — the harness must actually LOOK at the render.

These tests build tiny synthetic videos with ffmpeg and assert that the
audit detects the failure modes it was written for. Without them the audit
is just another green check that cannot fail, which is the problem it
exists to fix.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from harness.vision import (  # noqa: E402
    audit_report_text,
    frame_metrics,
    frame_vector,
    hamming,
    scene_spans_from_timing,
    visual_audit,
)

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg required for frame extraction"
)


def _video(path: Path, *, kind: str, seconds: float = 6.0, fps: int = 12) -> Path:
    """Synthesize a video with a known defect (or none) via ffmpeg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "moving":
        # Conway's game of life: deterministic from a seed, and its LAYOUT
        # keeps changing, which is what the variety metric measures (a dHash
        # Hamming distance, i.e. structure — not raw pixel difference).
        #
        # This replaced `gradients`, which picks a random initial field on
        # every run: three identical commands produced three different files
        # and variety ratios swinging 0.49-0.88, so the test failed roughly
        # one run in three and looked like a metric bug. It was the fixture.
        # Byte-identical here across repeated runs, variety 0.82.
        vf = ("life=s=320x180:rate=%d:seed=7"
              ":life_color=white:death_color=black" % fps)
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", vf,
               "-t", str(seconds), "-pix_fmt", "yuv420p", str(path)]
    elif kind == "static":
        # One flat colour for the WHOLE video: no motion, no variety.
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
               "-i", f"color=c=0x2A3550:s=320x180:d={seconds}",
               "-r", str(fps), "-pix_fmt", "yuv420p", str(path)]
    elif kind == "black":
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
               "-i", f"color=c=black:s=320x180:d={seconds}",
               "-r", str(fps), "-pix_fmt", "yuv420p", str(path)]
    elif kind == "lowcontrast":
        # Dark grey card with slightly-less-dark type: the exact defect the
        # first full render produced with outlined type on a dark palette.
        vf = ("color=c=0x14161C:s=320x180:d=%s,"
              "drawtext=text='hello world':fontcolor=0x33363F:fontsize=40:x=20:y=70"
              % seconds)
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", vf,
               "-r", str(fps), "-pix_fmt", "yuv420p", str(path)]
    else:
        raise ValueError(kind)
    subprocess.run(cmd, capture_output=True, timeout=120, check=True)
    return path


def test_frame_metrics_reads_a_real_frame(tmp_path):
    video = _video(tmp_path / "m.mp4", kind="moving")
    from harness.vision import extract_frames

    frames = extract_frames(video, [1.0], tmp_path / "f")
    assert frames and frames[0] is not None
    m = frame_metrics(frames[0])
    assert m is not None
    assert 0.0 <= m["luma_mean"] <= 1.0
    assert m["contrast"] >= 0.0
    assert m["edge_density"] >= 0.0


def test_frame_vector_and_hamming(tmp_path):
    video = _video(tmp_path / "m.mp4", kind="moving")
    from harness.vision import extract_frames

    frames = extract_frames(video, [1.0, 4.0], tmp_path / "f")
    a = frame_vector(frames[0])
    b = frame_vector(frames[1])
    assert a and b
    assert 0 <= hamming(a["dhash"], b["dhash"]) <= 64
    assert hamming(a["dhash"], a["dhash"]) == 0


def test_static_video_is_flagged_as_unmoving(tmp_path):
    """A render that succeeds and never animates must not pass."""
    video = _video(tmp_path / "s.mp4", kind="static")
    report = visual_audit(
        video, tmp_path / "audit",
        scene_spans=[{"index": 0, "start": 0.5, "end": 5.5, "kind": "scene"}],
    )
    assert report["pass"] is False
    assert any("static" in i["problem"] for i in report["issues"])


def test_identical_scenes_are_flagged_as_alike(tmp_path):
    """The anti-template check: six scenes that all look the same."""
    video = _video(tmp_path / "s.mp4", kind="static", seconds=6.0)
    spans = [{"index": i, "start": i, "end": i + 1, "kind": "scene"} for i in range(6)]
    report = visual_audit(video, tmp_path / "audit", scene_spans=spans)
    assert report["pass"] is False
    assert any("look alike" in i["problem"] for i in report["issues"])
    assert report["metrics"]["variety_ratio"] < 0.55


def test_moving_video_is_not_flagged_for_variety(tmp_path):
    video = _video(tmp_path / "m.mp4", kind="moving", seconds=8.0)
    spans = [{"index": i, "start": i, "end": i + 1, "kind": "scene"} for i in range(8)]
    report = visual_audit(video, tmp_path / "audit", scene_spans=spans)
    assert report["metrics"]["variety_ratio"] > 0.55
    assert not any("look alike" in i["problem"] for i in report["issues"])


def test_black_frames_are_flagged(tmp_path):
    video = _video(tmp_path / "b.mp4", kind="black")
    report = visual_audit(
        video, tmp_path / "audit",
        scene_spans=[{"index": 0, "start": 1.0, "end": 5.0, "kind": "scene"}],
    )
    assert report["metrics"]["dead_frames"] > 0
    assert report["pass"] is False


def test_low_contrast_is_flagged(tmp_path):
    video = _video(tmp_path / "lc.mp4", kind="lowcontrast")
    report = visual_audit(
        video, tmp_path / "audit",
        scene_spans=[{"index": 0, "start": 1.0, "end": 5.0, "kind": "scene"}],
    )
    assert any("contrast" in i["problem"] for i in report["issues"])


def test_missing_video_fails_cleanly(tmp_path):
    report = visual_audit(tmp_path / "nope.mp4", tmp_path / "audit")
    assert report["pass"] is False
    assert any("missing" in i["problem"] for i in report["issues"])


def test_report_is_written_and_text_renders(tmp_path):
    video = _video(tmp_path / "m.mp4", kind="moving")
    out = tmp_path / "audit"
    report = visual_audit(
        video, out,
        scene_spans=[{"index": 0, "start": 1.0, "end": 5.0, "kind": "scene"}],
    )
    assert (out / "visual-audit.json").exists()
    saved = json.loads((out / "visual-audit.json").read_text())
    assert saved["metrics"]["frames_sampled"] >= 1
    text = audit_report_text(report)
    assert "visual audit" in text
    assert "variety" in text


def test_scene_spans_from_timing_offsets_by_intro():
    spans = scene_spans_from_timing(
        {"hyperframes_intro_s": 5.4, "hyperframes_outro_s": 4.8}, duration=30.0,
    )
    assert spans[0]["kind"] == "intro"
    assert spans[0]["end"] == pytest.approx(5.4)
    assert spans[-1]["kind"] == "outro"


def test_scene_spans_from_explicit_cuts():
    spans = scene_spans_from_timing(
        {"hyperframes_intro_s": 2.0,
         "cuts": [{"in_seconds": 0.0, "out_seconds": 4.0},
                  {"in_seconds": 4.0, "out_seconds": 9.0}]},
        duration=13.0,
    )
    assert len(spans) == 2
    assert spans[0]["start"] == pytest.approx(2.0)
    assert spans[1]["end"] == pytest.approx(11.0)


def test_vision_critique_is_skipped_without_an_llm(tmp_path):
    video = _video(tmp_path / "m.mp4", kind="moving")
    report = visual_audit(
        video, tmp_path / "audit",
        scene_spans=[{"index": 0, "start": 1.0, "end": 5.0, "kind": "scene"}],
    )
    assert report["critique"] is None


def test_vision_critique_failure_never_breaks_the_audit(tmp_path):
    """A broken vision model must degrade to deterministic metrics only."""
    class ExplodingLLM:
        def chat_vision_json(self, *a, **k):
            raise RuntimeError("model is down")

    video = _video(tmp_path / "m.mp4", kind="moving")
    report = visual_audit(
        video, tmp_path / "audit",
        scene_spans=[{"index": 0, "start": 1.0, "end": 5.0, "kind": "scene"}],
        llm=ExplodingLLM(),
    )
    assert report["critique"] is None
    assert report["metrics"]["frames_sampled"] >= 1


def test_vision_critique_issues_are_merged(tmp_path):
    class FakeVision:
        def chat_vision_json(self, *a, **k):
            return {
                "pass": False, "score": 4, "structure_variety": 3,
                "legibility": 5, "coherence": 6,
                "issues": [{"scene": 0, "severity": "high",
                            "problem": "headline is clipped", "fix": "raise safe margin"}],
                "wins": [], "next_video_notes": [],
            }

    video = _video(tmp_path / "m.mp4", kind="moving")
    report = visual_audit(
        video, tmp_path / "audit",
        scene_spans=[{"index": 0, "start": 1.0, "end": 5.0, "kind": "scene"}],
        llm=FakeVision(),
    )
    assert report["critique"]["score"] == 4
    assert any("headline is clipped" in i["problem"] for i in report["issues"])
    assert report["pass"] is False
