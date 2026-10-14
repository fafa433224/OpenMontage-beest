"""Regressions for the render-path bugs found by actually rendering.

Every test here corresponds to a defect that reached a real render. They are
grouped because they share one cause: a value that was assumed to be right
instead of measured. Each one names the symptom it prevents.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "kaggle"))

from lib.motion_compiler import (  # noqa: E402
    TEXT_LAYER_KINDS,
    TEXT_OPACITY_FLOOR,
    compile_video,
    composite_over,
    contrast_ratio,
    ensure_contrast_at_alpha,
    lint_spec,
)


# ---------------------------------------------------------------------------
# "type at 55% opacity is unreadable on a dark plate"
# ---------------------------------------------------------------------------

def test_composited_contrast_is_not_the_swatch_contrast():
    """#9FB8BC on #09191B is 8.6:1 as a swatch and 3.45:1 at 0.55 opacity."""
    swatch = contrast_ratio("#9FB8BC", "#09191B")
    composited = contrast_ratio(composite_over("#9FB8BC", "#09191B", 0.55), "#09191B")
    assert swatch > 8.0
    assert composited < 4.5, "the whole point: the swatch passes, the render does not"


def test_ensure_contrast_at_alpha_lifts_the_composite_over_the_line():
    fixed = ensure_contrast_at_alpha("#9FB8BC", "#09191B", TEXT_OPACITY_FLOOR, 4.5)
    assert contrast_ratio(composite_over(fixed, "#09191B", TEXT_OPACITY_FLOOR),
                          "#09191B") >= 4.5


def test_lint_rejects_a_faint_text_layer():
    spec = compile_video(
        {"sections": [{"heading": "H", "text": "Some readable narration here."}]},
        None, "lint-probe",
    )["scenes"][0]
    spec["layers"][0]["kind"] = "body"
    spec["layers"][0]["opacity"] = 0.4
    problems = lint_spec(spec)
    assert any("text opacity" in p for p in problems), problems


def test_every_compiled_text_layer_clears_the_floor():
    """Where the bug actually bit: scheme programs authored 0.28-0.55."""
    out = compile_video(
        {"sections": [
            {"heading": "A quiet bottleneck", "text": "The pipeline is where the time goes."},
            {"heading": "Where it breaks", "text": "47% failed before the first frame."},
        ]},
        None, "floor-probe",
    )
    for spec in out["scenes"]:
        assert lint_spec(spec) == [], lint_spec(spec)
        for layer in spec["layers"]:
            if layer["kind"] in TEXT_LAYER_KINDS:
                assert layer.get("opacity", 1.0) >= TEXT_OPACITY_FLOOR


# ---------------------------------------------------------------------------
# "one asset, one owner" — two readers of one file crash the compositor
# ---------------------------------------------------------------------------

def test_bespoke_cuts_do_not_keep_the_template_footage_keys(tmp_path):
    from build_explainer_props import build_props

    broll = tmp_path / "bokeh.mp4"
    broll.write_bytes(b"\x00" * 16)
    props = build_props(
        {"title": "T", "sections": [
            {"id": "s1", "heading": "H", "text": "One sentence of narration."},
        ]},
        [12.0], None, seed="owner-probe", motion_mode="code2motion",
        broll_pool=[{"path": "broll/bokeh.mp4", "duration": 8.0}],
    )
    for cut in props["cuts"]:
        if cut.get("bespoke", {}).get("media"):
            assert "backgroundVideo" not in cut, (
                "a compiled cut carrying both spec.media and backgroundVideo "
                "pointed two readers at one file at different trims"
            )


# ---------------------------------------------------------------------------
# "bake the loop, do not re-derive it per frame"
# ---------------------------------------------------------------------------

def test_scene_media_pins_the_compositor_timescale(tmp_path):
    """ffmpeg picks a timescale from the frame rate; the compositor does not.

    A 30 fps bake came out 1/15360 with start_time 0.033. Remotion addresses
    media in 1/12800 units, so its lookup for 3.36 s (position 43008) missed
    and the render died with "No frame found at position 43008".
    """
    ffmpeg = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True)
    if ffmpeg.returncode != 0:
        pytest.skip("ffmpeg not installed")

    from scene_media import COMPOSITOR_TIMESCALE, bake_scene_clip

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "testsrc=size=320x180:rate=25:duration=6", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    out = tmp_path / "out.mp4"
    meta = bake_scene_clip(src, out, scene_seconds=4.0, start_seconds=0.5)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=time_base,start_time,duration",
         "-of", "json", str(out)],
        capture_output=True, text=True, check=True,
    )
    stream = json.loads(probe.stdout)["streams"][0]
    assert stream["time_base"] == f"1/{COMPOSITOR_TIMESCALE}", stream
    assert abs(float(stream["start_time"])) < 0.005, (
        "a non-zero start_time reads as a missing opening frame"
    )
    assert abs(float(stream["duration"]) - 4.0) < 0.2, stream
    assert meta["sceneSeconds"] == 4.0


def test_scene_media_reports_slowdown_and_repeats(tmp_path):
    """A scene far longer than its footage gets slow-mo first, then repeats."""
    ffmpeg = subprocess.run(["which", "ffmpeg"], capture_output=True, text=True)
    if ffmpeg.returncode != 0:
        pytest.skip("ffmpeg not installed")

    from scene_media import MAX_SLOWDOWN, bake_scene_clip

    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "testsrc=size=320x180:rate=25:duration=3", "-pix_fmt", "yuv420p", str(src)],
        check=True,
    )
    out = tmp_path / "out.mp4"
    meta = bake_scene_clip(src, out, scene_seconds=20.0, start_seconds=0.0)
    assert meta["slowdown"] == MAX_SLOWDOWN
    assert meta["loops"] >= 1
    assert meta["sceneSeconds"] == 20.0


# ---------------------------------------------------------------------------
# "measure the type, not the background glow"
# ---------------------------------------------------------------------------

def test_contrast_statistic_reaches_the_type():
    """p95 cannot see small type; p99 can.

    The audit used p95-p05, which on a type-light frame lands on the
    background glow and measures the PLATE's internal contrast. A frame whose
    glyphs are #F4F6FB on #09191B scored 0.258 that way and failed a 0.16
    floor's cousin, while a genuinely washed frame scored 0.143. Thresholding
    p99-p05 ranks them the other way round, which is the correct order.
    """
    from harness.vision import CONTRAST_FLOOR

    assert CONTRAST_FLOOR >= 0.25, (
        "calibrated between 0.165 (washed) and 0.44 (legible) on real frames"
    )


def test_legibility_findings_are_advisory_on_downscaled_samples(tmp_path):
    """A 960-wide preview cannot settle a legibility question."""
    from harness.vision import visual_audit

    video = tmp_path / "flat.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
         "color=c=gray:s=320x180:d=2", "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )
    spans = [{"index": 0, "start": 0.0, "end": 2.0, "kind": "scene"}]
    report = visual_audit(video, tmp_path / "out", scene_spans=spans, frame_width=640)
    contrast = [i for i in report["issues"] if "low contrast" in i["problem"]]
    for issue in contrast:
        assert issue["severity"] == "info"
        assert "advisory" in issue["problem"]
    assert report["metrics"]["frame_width"] == 640
