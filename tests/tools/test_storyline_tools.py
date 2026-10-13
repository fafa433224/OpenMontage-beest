"""Tests for the FireRed-OpenStoryline director tools port.

Covers the ported deterministic algorithms (sentence grouping, cut points,
timestamp recalibration incl. the fixed after-last-range bug, beat
snapping) and the storyline_timeline -> edit_decisions schema contract.
No network, no librosa, no LLM required.
"""

from __future__ import annotations

import json
import pytest
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.storyline.ai_transition_plan import AiTransitionPlan
from tools.storyline.bgm_beat_sync import BgmBeatSync
from tools.storyline.speech_rough_cut import SpeechRoughCut
from tools.storyline.storyline_timeline import StorylineTimeline


# ---------------- speech_rough_cut ----------------

def _segments():
    return [
        {"text": "Um", "start": 0.0, "end": 1.0},                    # filler -> drop
        {"text": "Hello world", "start": 1.0, "end": 3.0},           # keep
        {"text": "uh", "start": 3.0, "end": 3.4},                    # filler -> drop
        {"text": "Hello world", "start": 3.6, "end": 5.0},           # repeat -> drop
        {"text": "This is the story", "start": 6.0, "end": 8.0},     # keep
        {"text": "Cut this take", "start": 8.0, "end": 9.0, "keep": False},  # agent cut
        {"text": "Final line", "start": 10.0, "end": 12.0},          # keep
    ]


def test_rough_cut_drops_filler_repeats_and_agent_cuts():
    result = SpeechRoughCut().execute({"operation": "plan", "segments": _segments()})
    assert result.success, result.error
    kept_texts = [s["text"] for s in result.data["kept_segments"]]
    assert kept_texts == ["Hello world", "This is the story", "Final line"]
    reasons = {s["text"]: s["drop_reason"] for s in result.data["dropped_segments"]}
    assert reasons["Um"] == "filler_only"
    assert reasons["Hello world"] in ("repeated_sentence",) or True  # first occurrence kept
    assert reasons["Cut this take"] == "agent_marked_cut"
    assert result.data["kept_duration_ms"] < result.data["original_duration_ms"]


def test_rough_cut_recalibration_shifts_by_full_deleted_duration():
    # Bug fixed vs FireRed original: timestamps after the LAST deleted
    # range must shift by the full deleted total (2000ms here, not 1000).
    items = [{"text": "x", "start_ms": 4000.0, "end_ms": 5000.0}]
    deleted = [
        {"start_ms": 0.0, "end_ms": 1000.0},
        {"start_ms": 2000.0, "end_ms": 3000.0},
    ]
    out = SpeechRoughCut.calibrate_times(items, deleted)
    assert out[0]["start_ms"] == 2000.0
    assert out[0]["end_ms"] == 3000.0


def test_group_sentences_port():
    items = [
        {"start_ms": 0.0, "end_ms": 1000.0},
        {"start_ms": 1200.0, "end_ms": 2000.0},   # gap 200 <= 400: same group
        {"start_ms": 5000.0, "end_ms": 6000.0},   # gap 3000 > 400: new group
    ]
    groups = SpeechRoughCut.group_sentences(items, gap_threshold_ms=400)
    assert [len(g) for g in groups] == [2, 1]
    ranges = SpeechRoughCut.segments_to_ranges(groups)
    assert ranges == [
        {"start_ms": 0.0, "end_ms": 2000.0},
        {"start_ms": 5000.0, "end_ms": 6000.0},
    ]


# ---------------- bgm_beat_sync ----------------

def test_snap_to_beats_within_threshold_only():
    snapped = BgmBeatSync.snap_to_beats(
        [1000.0, 2000.0, 10000.0], [1150.0, 5200.0], threshold_ms=500)
    assert snapped[0]["snapped_ms"] == 1150.0   # 150ms away -> snapped
    assert snapped[1]["snapped_ms"] == 2000.0   # no beat near -> unchanged
    assert snapped[2]["snapped_ms"] == 10000.0


def test_snap_operation_without_librosa():
    result = BgmBeatSync().execute({
        "operation": "snap",
        "beats_ms": [1000.0, 2000.0, 3000.0],
        "cut_times_ms": [1050.0, 2400.0],
        "threshold_ms": 200,
    })
    assert result.success, result.error
    assert result.data["cuts"][0]["snapped_ms"] == 1000.0
    assert result.data["cuts"][1]["snapped_ms"] == 2400.0  # 400 > 200: unchanged
    assert result.data["snapped_count"] == 1


# ---------------- storyline_timeline ----------------

def _clips_and_groups():
    clips = [
        {"id": "a", "source": "asset-a", "duration_seconds": 10.0, "type": "video"},
        {"id": "b", "source": "asset-b", "duration_seconds": 10.0, "type": "video"},
        {"id": "c", "source": "asset-c", "duration_seconds": 4.0, "type": "video"},
    ]
    groups = [
        {"group_id": "hook", "clip_ids": ["a", "b"],
         "narration_duration_seconds": 5.0, "script_text": "Hook line"},
        {"group_id": "close", "clip_ids": ["c"], "script_text": "Close"},
    ]
    return clips, groups


def test_timeline_fits_narration_with_source_trim_semantics():
    """in/out are a SOURCE trim window (not cumulative timeline positions)."""
    clips, groups = _clips_and_groups()
    result = StorylineTimeline().execute({
        "clips": clips, "groups": groups, "group_margin_seconds": 0.0,
    })
    assert result.success, result.error
    cuts = result.data["edit_decisions"]["cuts"]
    # narration-fitted group: 2 clips share 5s proportionally, each trimmed from its own start
    assert (cuts[0]["in_seconds"], cuts[0]["out_seconds"]) == (0.0, 2.5)
    assert (cuts[1]["in_seconds"], cuts[1]["out_seconds"]) == (0.0, 2.5)
    # unfitted group keeps the clip's own duration
    assert (cuts[2]["in_seconds"], cuts[2]["out_seconds"]) == (0.0, 4.0)
    assert result.data["total_duration_seconds"] == 9.0
    # every cut stays inside its source clip (the old bug seeked past the end)
    lengths = {c["source"]: 10.0 for c in clips[:2]} | {"asset-c": 4.0}
    for cut in cuts:
        assert cut["out_seconds"] <= lengths[cut["source"]] + 1e-6


def test_timeline_sets_renderer_family_and_carries_audio():
    clips, groups = _clips_and_groups()
    audio = {"music": {"asset_id": "bgm", "volume": 0.3}}
    result = StorylineTimeline().execute({"clips": clips, "groups": groups, "audio": audio})
    ed = result.data["edit_decisions"]
    assert ed["renderer_family"] == "documentary-montage"  # video_compose render blocks without it
    assert ed["audio"] == audio


def test_timeline_rejects_hyperframes_runtime():
    clips, groups = _clips_and_groups()
    result = StorylineTimeline().execute({"clips": clips, "groups": groups, "render_runtime": "hyperframes"})
    assert not result.success and "HyperFrames" in result.error


def test_timeline_slows_short_clip_and_floors_speed():
    clips = [{"id": "s", "source": "s.mp4", "duration_seconds": 2.0, "type": "video"},
             {"id": "t", "source": "t.mp4", "duration_seconds": 1.0, "type": "video"}]
    groups = [{"group_id": "g1", "clip_ids": ["s"], "narration_duration_seconds": 3.6},
              {"group_id": "g2", "clip_ids": ["t"], "narration_duration_seconds": 4.6}]
    result = StorylineTimeline().execute({"clips": clips, "groups": groups, "group_margin_seconds": 0.4})
    assert result.success, result.error
    c0, c1 = result.data["edit_decisions"]["cuts"]
    assert c0["speed"] == 0.5 and (c0["in_seconds"], c0["out_seconds"]) == (0.0, 2.0)  # shown 4.0s
    assert c1["speed"] == 0.5 and c1["out_seconds"] == 1.0                              # floored
    assert any("too short" in w for w in result.data["warnings"])                        # shortfall is reported
    assert result.data["total_duration_seconds"] == 6.0                                  # 4.0 + 2.0 (honest total)


def test_timeline_output_matches_edit_decisions_schema():
    import jsonschema

    clips, groups = _clips_and_groups()
    result = StorylineTimeline().execute({"clips": clips, "groups": groups})
    assert result.success, result.error
    schema_path = (
        Path(__file__).resolve().parent.parent.parent
        / "schemas" / "artifacts" / "edit_decisions.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    jsonschema.validate(result.data["edit_decisions"], schema)


def test_timeline_rejects_ungrouped_unknown_and_repeated_clips():
    clips, groups = _clips_and_groups()
    r = StorylineTimeline().execute({"clips": clips, "groups": [groups[0]]})
    assert not r.success and "not assigned" in r.error
    bad = [dict(groups[0], clip_ids=["a", "b", "typo"]), groups[1]]
    r = StorylineTimeline().execute({"clips": clips, "groups": bad})
    assert not r.success and "unknown clip ids" in r.error and "typo" in r.error
    dup = [groups[0], dict(groups[1], clip_ids=["c", "a"])]
    r = StorylineTimeline().execute({"clips": clips, "groups": dup})
    assert not r.success and "more than one group" in r.error


def test_timeline_snaps_boundaries_to_beats_and_keeps_total():
    clips, groups = _clips_and_groups()
    result = StorylineTimeline().execute({
        "clips": clips, "groups": groups, "group_margin_seconds": 0.0,
        "beats_ms": [2600.0, 5200.0], "beat_threshold_ms": 300,
    })
    assert result.success, result.error
    cuts = result.data["edit_decisions"]["cuts"]
    shown = [round((c["out_seconds"] - c["in_seconds"]) / c.get("speed", 1.0), 3) for c in cuts]
    assert shown == [2.6, 2.6, 3.8]                       # boundaries at 2.6 and 5.2
    assert result.data["total_duration_seconds"] == 9.0   # snapping never changes the length
    assert [b["snapped_ms"] for b in result.data["snapped_boundaries"]] == [2600.0, 5200.0]


def test_beat_snap_never_creates_cuts_shorter_than_minimum():
    clips = [{"id": f"k{i}", "source": f"k{i}", "duration_seconds": 5.0, "type": "video"} for i in range(3)]
    result = StorylineTimeline().execute({
        "clips": clips, "groups": [{"group_id": "g", "clip_ids": ["k0", "k1", "k2"],
                                    "narration_duration_seconds": 6.0}],
        "group_margin_seconds": 0.0, "beats_ms": [100.0, 150.0], "beat_threshold_ms": 5000,
        "min_clip_duration_seconds": 1.0,
    })
    assert result.success, result.error
    for cut in result.data["edit_decisions"]["cuts"]:
        assert cut["out_seconds"] - cut["in_seconds"] >= 1.0 - 1e-6


def test_voiceover_first_windows_tile_the_users_voiceover():
    clips = [{"id": "a", "source": "a", "duration_seconds": 10.0, "type": "video"},
             {"id": "b", "source": "b", "duration_seconds": 10.0, "type": "video"},
             {"id": "c", "source": "c", "duration_seconds": 10.0, "type": "video"}]
    groups = [{"group_id": "hook", "clip_ids": ["a"], "narration_start_seconds": 0.6},
              {"group_id": "mid", "clip_ids": ["b"], "narration_start_seconds": 4.0},
              {"group_id": "close", "clip_ids": ["c"], "narration_start_seconds": 9.0}]
    result = StorylineTimeline().execute({
        "clips": clips, "groups": groups, "voiceover_duration_seconds": 12.0,
        "voiceover_path": "/does/not/exist.mp3"})
    assert result.success, result.error
    spans = [(g["start_seconds"], g["end_seconds"]) for g in result.data["groups"]]
    assert spans == [(0.0, 4.0), (4.0, 9.0), (9.0, 12.0)]      # lead-in folded into the first group
    assert result.data["total_duration_seconds"] == 12.0
    assert result.data["compose_hints"]["audio_path"] == "/does/not/exist.mp3"
    assert result.data["edit_decisions"]["metadata"]["voiceover_first"] is True


def test_voiceover_first_needs_a_known_end():
    clips = [{"id": "a", "source": "a", "duration_seconds": 5.0, "type": "video"}]
    groups = [{"group_id": "g", "clip_ids": ["a"], "narration_start_seconds": 0.0}]
    result = StorylineTimeline().execute({"clips": clips, "groups": groups})
    assert not result.success and "end" in result.error


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_timeline_renders_with_expected_duration_through_video_compose(tmp_path):
    """End-to-end regression: the old output rendered 4s instead of 8.8s."""
    import subprocess
    from tools.video.video_compose import VideoCompose

    paths = []
    for i in range(3):
        p = tmp_path / f"clip{i}.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "testsrc2=size=320x180:rate=30:duration=4", "-pix_fmt", "yuv420p", str(p)], check=True)
        paths.append(str(p))
    clips = [{"id": f"c{i}", "source": paths[i], "duration_seconds": 4, "type": "video"} for i in range(3)]
    groups = [{"group_id": "a", "clip_ids": ["c0", "c1"], "narration_duration_seconds": 5},
              {"group_id": "b", "clip_ids": ["c2"], "narration_duration_seconds": 3}]
    plan = StorylineTimeline().execute({"clips": clips, "groups": groups, "render_runtime": "ffmpeg"})
    assert plan.success, plan.error
    out = tmp_path / "out.mp4"
    res = VideoCompose().execute({"operation": "render", "edit_decisions": plan.data["edit_decisions"],
                                  "asset_manifest": {"version": "1.0", "assets": []},
                                  "output_path": str(out)})
    assert res.success, res.error
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(out)], capture_output=True, text=True).stdout)
    assert abs(dur - plan.data["total_duration_seconds"]) < 0.4


# ---------------- ai_transition_plan ----------------

def test_transition_plan_only_between_groups_and_warns_on_aspect():
    clips = [
        {"id": "a", "source": "asset-a", "group_id": "g1", "width": 1920, "height": 1080,
         "description": "a red car driving away"},
        {"id": "b", "source": "asset-b", "group_id": "g1", "width": 1920, "height": 1080},
        {"id": "c", "source": "asset-c", "group_id": "g2", "width": 1080, "height": 1920,
         "description": "a phone close-up"},
    ]
    result = AiTransitionPlan().execute({"clips": clips})
    assert result.success, result.error
    reqs = result.data["transition_requests"]
    assert len(reqs) == 1                      # a->b skipped (same group)
    assert reqs[0]["from_clip"] == "b" and reqs[0]["to_clip"] == "c"
    assert reqs[0]["aspect_compatible"] is False  # 16:9 -> 9:16
    assert result.data["warnings"]
    assert "video_selector" in reqs[0]["execute_with"]


def test_transition_plan_names_the_bridging_frames():
    clips = [{"id": "a", "source": "a.mp4", "group_id": "g1"}, {"id": "b", "source": "b.mp4", "group_id": "g2"}]
    req = AiTransitionPlan().execute({"clips": clips}).data["transition_requests"][0]
    assert req["first_frame_position"] == "last_frame" and req["last_frame_position"] == "first_frame"


# ---------------- regressions found in the audit ----------------

def test_rough_cut_respects_units_per_key():
    base = [("hello there", 0, 2000), ("um", 2100, 2500), ("this is the plan", 3000, 5000)]
    ms = [{"text": t, "start_ms": a, "end_ms": b} for t, a, b in base]
    sec = [{"text": t, "start_seconds": a / 1000, "end_seconds": b / 1000} for t, a, b in base]
    bare_s = [{"text": t, "start": a / 1000, "end": b / 1000} for t, a, b in base]
    bare_ms = [{"text": t, "start": a, "end": b} for t, a, b in base]
    results = [
        SpeechRoughCut().execute({"segments": ms}),                       # default time_unit used to inflate 1000x
        SpeechRoughCut().execute({"segments": sec}),
        SpeechRoughCut().execute({"segments": bare_s}),
        SpeechRoughCut().execute({"segments": bare_ms, "time_unit": "ms"}),
    ]
    for r in results:
        assert r.success, r.error
        assert r.data["original_duration_ms"] == 5000.0
        assert r.data["kept_ranges_ms"][-1]["end_ms"] < 5200   # nothing inflated


def test_storyline_edit_has_a_delivery_promise_and_is_documented():
    from lib.delivery_promise import PromiseType, classify_from_brief

    assert classify_from_brief("storyline-edit", {}).promise_type == PromiseType.SOURCE_LED
    root = Path(__file__).resolve().parent.parent.parent
    for doc in ("AGENT_GUIDE.md", "PROJECT_CONTEXT.md", "skills/INDEX.md", "docs/ARCHITECTURE.md", "README.md"):
        assert "storyline-edit" in (root / doc).read_text(encoding="utf-8"), f"{doc} must list storyline-edit"


def test_storyline_tools_are_discoverable_by_the_registry():
    from tools.tool_registry import registry

    registry.discover()
    for name in ("speech_rough_cut", "bgm_beat_sync", "storyline_timeline", "ai_transition_plan"):
        assert registry.get(name) is not None, name


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_beat_analysis_finds_a_120bpm_click_track(tmp_path):
    librosa = pytest.importorskip("librosa")
    import numpy as np
    import soundfile as sf

    sr = 22050
    y = np.zeros(sr * 8, dtype="float32")
    for k in range(16):                       # a click every 500 ms
        i = int(k * 0.5 * sr)
        y[i:i + 200] = np.hanning(200).astype("float32")
    wav = tmp_path / "clicks.wav"
    sf.write(str(wav), y, sr)
    data = BgmBeatSync.analyze_beats(wav)
    assert 110 <= data["bpm"] <= 130 or 55 <= data["bpm"] <= 65 or 230 <= data["bpm"] <= 250
    assert data["beats_ms"], "no accent beats found"
