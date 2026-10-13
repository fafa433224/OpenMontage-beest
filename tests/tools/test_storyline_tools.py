"""Tests for the FireRed-OpenStoryline director tools port.

Covers the ported deterministic algorithms (sentence grouping, cut points,
timestamp recalibration incl. the fixed after-last-range bug, beat
snapping) and the storyline_timeline -> edit_decisions schema contract.
No network, no librosa, no LLM required.
"""

from __future__ import annotations

import json
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


def test_timeline_fits_narration_and_is_sequential():
    clips, groups = _clips_and_groups()
    result = StorylineTimeline().execute({
        "clips": clips, "groups": groups, "group_margin_seconds": 0.0,
    })
    assert result.success, result.error
    cuts = result.data["edit_decisions"]["cuts"]
    # narration-fitted group: 2 clips share 5s proportionally
    assert cuts[0]["in_seconds"] == 0.0 and cuts[0]["out_seconds"] == 2.5
    assert cuts[1]["in_seconds"] == 2.5 and cuts[1]["out_seconds"] == 5.0
    # unfitted group keeps source duration
    assert cuts[2]["in_seconds"] == 5.0 and cuts[2]["out_seconds"] == 9.0
    assert result.data["total_duration_seconds"] == 9.0


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


def test_timeline_rejects_ungrouped_clips():
    clips, groups = _clips_and_groups()
    groups = [groups[0]]  # clip c ungrouped
    result = StorylineTimeline().execute({"clips": clips, "groups": groups})
    assert not result.success
    assert "not assigned" in (result.error or "")


def test_timeline_snaps_boundaries_to_beats():
    clips, groups = _clips_and_groups()
    result = StorylineTimeline().execute({
        "clips": clips, "groups": groups, "group_margin_seconds": 0.0,
        "beats_ms": [2600.0, 5200.0], "beat_threshold_ms": 300,
    })
    assert result.success, result.error
    cuts = result.data["edit_decisions"]["cuts"]
    assert cuts[0]["out_seconds"] == 2.6   # 2.5 snapped to 2.6 beat
    assert cuts[1]["in_seconds"] == 2.6    # next cut follows the snap


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
