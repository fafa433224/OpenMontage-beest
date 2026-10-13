"""Storyline timeline planner — the FireRed director core, ported.

FireRed origin (Apache-2.0): the ``TimeLine`` logic of
``plan_timeline_pro.py`` (fit clip durations to narration, fit clip
durations to music beats, compute playback speeds) plus the output shape
of ``group_clips.py`` / ``generate_script.py`` (clips grouped into
narrative groups, one script block per group).

What changed for OpenMontage — deliberately:
  - Grouping and script writing are LLM work; in OpenMontage the agent
    does them (guided by ``skills/core/firered-storyline.md``) and passes
    ``groups`` in. This tool is the deterministic half: given clips +
    groups (+ optional narration durations, optional BGM beats) it emits
    a schema-valid ``edit_decisions`` artifact (version 1.0, sequential
    cuts) that ``video_compose`` / ``hyperframes_compose`` can render
    unchanged. FireRed's MoviePy-specific timeline dict is not produced.
  - FireRed's random group margins are replaced by a fixed, declared
    margin so renders are reproducible.
  - Duration allocation is proportional to source duration (FireRed's
    final code allocated equally; proportional preserves the editor's
    relative emphasis and is documented here as a conscious change).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ResumeSupport,
    ToolResult,
    ToolStability,
    ToolTier,
)
from tools.storyline.bgm_beat_sync import BgmBeatSync


class StorylineTimeline(BaseTool):
    name = "storyline_timeline"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "firered-storyline"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = []
    install_instructions = "No installation needed (pure Python)."
    agent_skills = []

    capabilities = [
        "storyline_timeline_planning",
        "clip_group_timeline",
        "narration_fitted_durations",
        "beat_fitted_durations",
        "edit_decisions_generation",
    ]
    best_for = [
        "Turning grouped, described clips into a renderable edit_decisions timeline",
        "Fitting clip durations to narration length and/or BGM accent beats",
    ]
    not_good_for = [
        "Understanding or grouping clips — the agent does that with video_analyzer first",
        "Rendering (video_compose / hyperframes_compose render the output)",
    ]

    input_schema = {
        "type": "object",
        "required": ["clips"],
        "properties": {
            "clips": {
                "type": "array",
                "description": "Clips: {id, source, duration_seconds, type (video|image), group_id?}. source is an asset-manifest ID or path.",
                "items": {"type": "object"},
            },
            "groups": {
                "type": "array",
                "description": "Narrative groups from the agent: {group_id, clip_ids, narration_duration_seconds?, script_text?}. If omitted, clips' group_id fields define groups.",
                "items": {"type": "object"},
            },
            "render_runtime": {"type": "string", "enum": ["remotion", "hyperframes", "ffmpeg"], "default": "remotion"},
            "min_clip_duration_seconds": {"type": "number", "default": 1.0},
            "group_margin_seconds": {"type": "number", "default": 0.4, "description": "Breathing room added after each group's narration"},
            "beats_ms": {"type": "array", "items": {"type": "number"}, "description": "Optional BGM accent beats; cut boundaries snap to them"},
            "beat_threshold_ms": {"type": "number", "default": 500},
            "output_path": {"type": "string", "description": "Optional path to write the edit_decisions JSON artifact"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=100, network_required=False)
    retry_policy = RetryPolicy(max_retries=0)
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["clips", "groups", "render_runtime", "beats_ms"]
    side_effects = ["writes edit_decisions JSON to output_path when given"]
    user_visible_verification = [
        "Check total_duration_seconds against the narration/BGM length",
        "Render with video_compose and watch group pacing before publishing",
    ]

    def _resolve_groups(self, inputs: dict[str, Any]) -> list[dict]:
        groups = inputs.get("groups")
        if groups:
            return groups
        by_group: dict[str, list[str]] = {}
        for clip in inputs["clips"]:
            by_group.setdefault(clip.get("group_id", "main"), []).append(clip["id"])
        return [{"group_id": gid, "clip_ids": ids} for gid, ids in by_group.items()]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        clips = {c["id"]: c for c in inputs.get("clips") or []}
        if not clips:
            return ToolResult(success=False, error="No clips provided")
        groups = self._resolve_groups(inputs)
        min_dur = float(inputs.get("min_clip_duration_seconds", 1.0))
        margin = float(inputs.get("group_margin_seconds", 0.4))

        cuts: list[dict] = []
        group_summaries: list[dict] = []
        cursor = 0.0
        seen: set[str] = set()

        for group in groups:
            clip_ids = [cid for cid in group.get("clip_ids", []) if cid in clips and cid not in seen]
            if not clip_ids:
                continue
            seen.update(clip_ids)
            sources = [clips[cid] for cid in clip_ids]
            source_durations = [max(float(c.get("duration_seconds", 0)) or 3.0, 0.1) for c in sources]

            narration = group.get("narration_duration_seconds")
            if narration:
                target_total = float(narration) + margin
                total_source = sum(source_durations)
                durations = [
                    max(min_dur, target_total * (d / total_source)) for d in source_durations
                ]
                # Re-normalize after min-clamping so the group total stays honest.
                scale = target_total / sum(durations)
                durations = [d * scale for d in durations]
            else:
                durations = source_durations

            group_start = cursor
            for clip, new_dur, src_dur in zip(sources, durations, source_durations):
                is_image = clip.get("type") == "image"
                speed = 1.0
                if not is_image and new_dur > src_dur > 0:
                    speed = round(src_dur / new_dur, 4)  # slow down to fill (FireRed logic)
                cut = {
                    "id": f"cut-{len(cuts) + 1:03d}",
                    "source": clip.get("source", clip["id"]),
                    "in_seconds": round(cursor, 3),
                    "out_seconds": round(cursor + new_dur, 3),
                    "reason": f"storyline group {group.get('group_id', 'main')}",
                }
                if speed != 1.0:
                    cut["speed"] = speed
                cuts.append(cut)
                cursor += new_dur
            group_summaries.append({
                "group_id": group.get("group_id", "main"),
                "clip_ids": clip_ids,
                "start_seconds": round(group_start, 3),
                "end_seconds": round(cursor, 3),
                "script_text": group.get("script_text", ""),
            })

        missing = [cid for cid in clips if cid not in seen]
        if missing:
            return ToolResult(
                success=False,
                error=f"Clips not assigned to any group: {missing}. Group every clip or omit groups to auto-group by group_id.",
            )
        if not cuts:
            return ToolResult(success=False, error="Groups referenced no known clips")

        # Optional: snap internal cut boundaries onto BGM accent beats.
        beats = inputs.get("beats_ms")
        snapped_boundaries: list[dict] = []
        if beats:
            boundaries = [c["out_seconds"] * 1000 for c in cuts[:-1]]
            snapped = BgmBeatSync.snap_to_beats(
                boundaries, beats, float(inputs.get("beat_threshold_ms", 500)))
            snapped_boundaries = snapped
            for cut, snap in zip(cuts[:-1], snapped):
                cut["out_seconds"] = round(snap["snapped_ms"] / 1000, 3)
            for prev, nxt in zip(cuts[:-1], cuts[1:]):
                nxt["in_seconds"] = prev["out_seconds"]
            cursor = cuts[-1]["out_seconds"]

        edit_decisions: dict[str, Any] = {
            "version": "1.0",
            "render_runtime": inputs.get("render_runtime", "remotion"),
            "cuts": cuts,
        }
        artifacts: list[str] = []
        output_path = inputs.get("output_path")
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(json.dumps(edit_decisions, indent=2), encoding="utf-8")
            artifacts.append(str(output_path))

        return ToolResult(
            success=True,
            data={
                "edit_decisions": edit_decisions,
                "groups": group_summaries,
                "total_duration_seconds": round(cursor, 3),
                "cut_count": len(cuts),
                "snapped_boundaries": snapped_boundaries,
            },
            artifacts=artifacts,
            duration_seconds=round(time.time() - start, 2),
        )
