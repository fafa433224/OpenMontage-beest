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

edit_decisions contract (IMPORTANT — fixed in the OpenMontage-beest audit)
  For video sources ``in_seconds`` / ``out_seconds`` are a SOURCE trim
  window and ``speed`` stretches it, so a cut is shown for
  ``(out - in) / speed`` seconds. This is the canonical footage-first
  contract used by documentary-montage, ``video_compose`` (ffmpeg) and
  the Remotion CinematicRenderer adapter. Earlier versions emitted
  cumulative *timeline* positions, which only HyperFrames understands and
  which made ffmpeg/Remotion seek past the end of the source clip.
  HyperFrames has no source-offset support for footage, so it is rejected
  for this tool rather than rendered wrong.

Voiceover-first mode
  If every group carries ``narration_start_seconds`` (taken from the
  transcript of the user's own voiceover), group windows tile the
  voiceover: group k lasts from its start to the next group's start, and
  the last group runs to ``voiceover_duration_seconds`` (or its own
  ``narration_end_seconds``). Cuts then land on the speaker's sentence
  boundaries instead of on generated-TTS durations.
"""

from __future__ import annotations

import json
import shutil
import subprocess
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

    SUPPORTED_RUNTIMES = ("remotion", "ffmpeg")
    SUPPORTED_FAMILIES = (
        "documentary-montage", "cinematic-trailer", "explainer-data",
        "explainer-teacher", "product-reveal", "screen-demo", "presenter",
        "animation-first",
    )

    input_schema = {
        "type": "object",
        "required": ["clips"],
        "properties": {
            "clips": {
                "type": "array",
                "description": (
                    "Clips: {id, source, duration_seconds, type (video|image), "
                    "group_id?, source_in_seconds?}. source is an asset-manifest ID or path. "
                    "duration_seconds is the usable length of the source file."
                ),
                "items": {"type": "object"},
            },
            "groups": {
                "type": "array",
                "description": (
                    "Narrative groups from the agent: {group_id, clip_ids, narration_duration_seconds?, "
                    "narration_start_seconds?, narration_end_seconds?, script_text?}. "
                    "If omitted, clips' group_id fields define groups."
                ),
                "items": {"type": "object"},
            },
            "render_runtime": {"type": "string", "enum": ["remotion", "ffmpeg"], "default": "remotion",
                               "description": "hyperframes is not supported: it cannot offset into footage."},
            "renderer_family": {"type": "string", "enum": list(SUPPORTED_FAMILIES), "default": "documentary-montage",
                                "description": "Required by video_compose render. documentary-montage = footage-first."},
            "min_clip_duration_seconds": {"type": "number", "default": 1.0},
            "min_playback_speed": {"type": "number", "default": 0.5, "minimum": 0.1,
                                   "description": "Slowest allowed slow-down when a clip is shorter than its slot"},
            "group_margin_seconds": {"type": "number", "default": 0.4,
                                     "description": "Breathing room added after each group's narration (ignored in voiceover-first mode)"},
            "voiceover_path": {"type": "string", "description": "User's own voiceover audio; passed on as compose_hints.audio_path"},
            "voiceover_duration_seconds": {"type": "number", "description": "Total voiceover length (probed with ffprobe from voiceover_path when omitted)"},
            "audio": {"type": "object", "description": "Optional edit_decisions.audio block (narration segments / music), passed through unchanged"},
            "subtitles": {"type": "object", "description": "Optional edit_decisions.subtitles block, passed through unchanged"},
            "beats_ms": {"type": "array", "items": {"type": "number"}, "description": "Optional BGM accent beats; cut boundaries snap to them"},
            "beat_threshold_ms": {"type": "number", "default": 500},
            "output_path": {"type": "string", "description": "Optional path to write the edit_decisions JSON artifact"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=100, network_required=False)
    retry_policy = RetryPolicy(max_retries=0)
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["clips", "groups", "render_runtime", "renderer_family", "beats_ms", "voiceover_duration_seconds"]
    side_effects = ["writes edit_decisions JSON to output_path when given"]
    user_visible_verification = [
        "Check total_duration_seconds against the narration/BGM length",
        "Render with video_compose and watch group pacing before publishing",
    ]

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _resolve_groups(self, inputs: dict[str, Any]) -> list[dict]:
        groups = inputs.get("groups")
        if groups:
            return groups
        by_group: dict[str, list[str]] = {}
        for clip in inputs["clips"]:
            by_group.setdefault(clip.get("group_id", "main"), []).append(clip["id"])
        return [{"group_id": gid, "clip_ids": ids} for gid, ids in by_group.items()]

    @staticmethod
    def _probe_duration(path: str) -> float | None:
        if not path or not Path(path).exists() or not shutil.which("ffprobe"):
            return None
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            return float(out) if out else None
        except (OSError, ValueError, subprocess.SubprocessError):
            return None

    @staticmethod
    def _fit_cut(clip: dict, target: float, source_dur: float, min_speed: float) -> dict:
        """Fit one clip into a timeline slot of ``target`` seconds.

        Returns in/out (source trim), speed and the *actual* displayed
        duration (``(out - in) / speed``), which is shorter than ``target``
        only when the source is too short and the slow-down floor is hit.
        """
        if clip.get("type") == "image":
            return {"in": 0.0, "out": round(target, 3), "speed": 1.0, "shown": round(target, 3)}
        src_in = max(float(clip.get("source_in_seconds", 0) or 0), 0.0)
        available = max(source_dur - src_in, 0.1)
        if target <= available + 1e-6:
            return {"in": round(src_in, 3), "out": round(src_in + target, 3),
                    "speed": 1.0, "shown": round(target, 3)}
        speed = max(min_speed, available / target)  # slow down to fill (FireRed logic), floored
        out = round(src_in + available, 3)
        span = out - round(src_in, 3)
        return {"in": round(src_in, 3), "out": out, "speed": round(speed, 4),
                "shown": round(span / round(speed, 4), 3)}

    @staticmethod
    def _snap_boundaries(boundaries: list[float], beats: list[float], threshold: float,
                         total: float, min_dur: float) -> list[dict]:
        """Snap internal boundaries (seconds) to beats, never creating a cut
        shorter than ``min_dur`` and never reordering boundaries."""
        beats_s = sorted(float(b) / 1000.0 for b in beats)
        thr = threshold / 1000.0
        results: list[dict] = []
        prev = 0.0
        for i, original in enumerate(boundaries):
            nxt = boundaries[i + 1] if i + 1 < len(boundaries) else total
            lo, hi = prev + min_dur, nxt - min_dur
            best = None
            for b in beats_s:
                if abs(b - original) <= thr and lo <= b <= hi:
                    if best is None or abs(b - original) < abs(best - original):
                        best = b
            snapped = best if best is not None else original
            results.append({
                "original_ms": round(original * 1000, 1),
                "snapped_ms": round(snapped * 1000, 1),
                "shift_ms": round((snapped - original) * 1000, 1),
            })
            prev = snapped
        return results

    # ------------------------------------------------------------------
    # execute
    # ------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        warnings: list[str] = []

        runtime = inputs.get("render_runtime", "remotion")
        if runtime not in self.SUPPORTED_RUNTIMES:
            return ToolResult(success=False, error=(
                f"render_runtime {runtime!r} is not supported by storyline_timeline. "
                "HyperFrames cannot offset into source footage, so footage cuts would render wrong. "
                "Lock render_runtime to 'remotion' or 'ffmpeg' for storyline-edit."))
        family = inputs.get("renderer_family", "documentary-montage")
        if family not in self.SUPPORTED_FAMILIES:
            return ToolResult(success=False, error=f"Unknown renderer_family {family!r}")

        clip_list = inputs.get("clips") or []
        if not clip_list:
            return ToolResult(success=False, error="No clips provided")
        ids = [c.get("id") for c in clip_list]
        if any(i is None for i in ids):
            return ToolResult(success=False, error="Every clip needs an id")
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            return ToolResult(success=False, error=f"Duplicate clip ids: {dupes}")
        clips = {c["id"]: c for c in clip_list}

        groups = self._resolve_groups(inputs)
        min_dur = float(inputs.get("min_clip_duration_seconds", 1.0))
        min_speed = float(inputs.get("min_playback_speed", 0.5))
        margin = float(inputs.get("group_margin_seconds", 0.4))

        # ---- strict group validation (typos must not silently drop clips) ----
        seen: set[str] = set()
        unknown: list[str] = []
        repeated: list[str] = []
        for g in groups:
            for cid in g.get("clip_ids", []):
                if cid not in clips:
                    unknown.append(cid)
                elif cid in seen:
                    repeated.append(cid)
                seen.add(cid)
        if unknown:
            return ToolResult(success=False, error=f"Groups reference unknown clip ids: {sorted(set(unknown))}")
        if repeated:
            return ToolResult(success=False, error=f"Clips assigned to more than one group: {sorted(set(repeated))}")
        missing = [cid for cid in clips if cid not in seen]
        if missing:
            return ToolResult(success=False, error=(
                f"Clips not assigned to any group: {missing}. "
                "Group every clip or omit groups to auto-group by group_id."))
        groups = [g for g in groups if g.get("clip_ids")]
        if not groups:
            return ToolResult(success=False, error="Groups referenced no known clips")

        # ---- source durations ----
        src_dur: dict[str, float] = {}
        for cid, clip in clips.items():
            raw = clip.get("duration_seconds")
            if clip.get("type") == "image":
                src_dur[cid] = float(raw or 3.0)
                continue
            if not raw:
                probed = self._probe_duration(str(clip.get("source", "")))
                if probed:
                    raw = probed
                else:
                    raw = 3.0
                    warnings.append(f"Clip {cid} has no duration_seconds; assumed 3.0s. Provide the real length.")
            src_dur[cid] = max(float(raw), 0.1)

        # ---- voiceover-first windows ----
        vo_path = inputs.get("voiceover_path")
        vo_dur = inputs.get("voiceover_duration_seconds")
        if vo_dur is None and vo_path:
            vo_dur = self._probe_duration(str(vo_path))
        voiceover_mode = all(g.get("narration_start_seconds") is not None for g in groups)
        windows: list[float | None] = [None] * len(groups)
        if voiceover_mode:
            order = sorted(range(len(groups)), key=lambda i: float(groups[i]["narration_start_seconds"]))
            groups = [groups[i] for i in order]
            starts = [float(g["narration_start_seconds"]) for g in groups]
            starts[0] = 0.0  # first group also covers any lead-in before the first spoken word
            for k in range(len(groups) - 1):
                windows[k] = starts[k + 1] - starts[k]
            last = groups[-1]
            if last.get("narration_end_seconds") is not None:
                end_t = float(last["narration_end_seconds"]) + margin
            elif vo_dur:
                end_t = float(vo_dur)
            elif last.get("narration_duration_seconds"):
                end_t = float(last["narration_start_seconds"]) + float(last["narration_duration_seconds"]) + margin
            else:
                return ToolResult(success=False, error=(
                    "Voiceover-first mode needs the last group's end: give narration_end_seconds, "
                    "narration_duration_seconds, voiceover_duration_seconds or a readable voiceover_path."))
            windows[-1] = end_t - starts[-1]
            if any(w is not None and w <= 0 for w in windows):
                return ToolResult(success=False, error="narration_start_seconds must increase from group to group")

        # ---- allocate durations and fit cuts ----
        plan: list[dict] = []   # one entry per cut, in order
        group_ranges: list[tuple[dict, list[int]]] = []
        for gi, group in enumerate(groups):
            cids = list(group["clip_ids"])
            native = [src_dur[c] for c in cids]
            narration = group.get("narration_duration_seconds")
            if windows[gi] is not None:
                target_total: float | None = windows[gi]
            elif narration:
                target_total = float(narration) + margin
            else:
                target_total = None
            if target_total is not None:
                needed = min_dur * len(cids)
                if target_total < needed:
                    warnings.append(
                        f"Group {group.get('group_id', 'main')}: {len(cids)} clips cannot fit "
                        f"{target_total:.2f}s at min_clip_duration {min_dur}s; using {needed:.2f}s.")
                    target_total = needed
                total_native = sum(native)
                raw_t = [max(min_dur, target_total * (d / total_native)) for d in native]
                scale = target_total / sum(raw_t)
                targets = [t * scale for t in raw_t]
            else:
                targets = native
            idxs = []
            for cid, tgt in zip(cids, targets):
                fit = self._fit_cut(clips[cid], tgt, src_dur[cid], min_speed)
                if fit["shown"] + 0.01 < tgt:
                    warnings.append(
                        f"Clip {cid} is too short for its {tgt:.2f}s slot (shown {fit['shown']:.2f}s at "
                        f"speed {fit['speed']}); add B-roll or split the group. "
                        + ("Voiceover sync will drift." if voiceover_mode else ""))
                idxs.append(len(plan))
                plan.append({"clip": clips[cid], "target": tgt, "fit": fit, "group": group})
            group_ranges.append((group, idxs))

        # ---- optional beat snapping (timeline domain, safe) ----
        snapped_info: list[dict] = []
        beats = inputs.get("beats_ms")
        if beats and len(plan) > 1:
            bounds, acc = [], 0.0
            for item in plan[:-1]:
                acc += item["fit"]["shown"]
                bounds.append(acc)
            total_now = acc + plan[-1]["fit"]["shown"]
            snapped_info = self._snap_boundaries(
                bounds, beats, float(inputs.get("beat_threshold_ms", 500)), total_now, min_dur)
            prev = 0.0
            for i, item in enumerate(plan):
                end_t = snapped_info[i]["snapped_ms"] / 1000.0 if i < len(snapped_info) else total_now
                new_target = end_t - prev
                prev = end_t
                if abs(new_target - item["fit"]["shown"]) > 1e-3:
                    item["fit"] = self._fit_cut(item["clip"], new_target, src_dur[item["clip"]["id"]], min_speed)

        # ---- build cuts ----
        cuts: list[dict] = []
        cursor = 0.0
        shown_by_index: list[tuple[float, float]] = []
        for item in plan:
            clip, fit, group = item["clip"], item["fit"], item["group"]
            cut = {
                "id": f"cut-{len(cuts) + 1:03d}",
                "source": clip.get("source", clip["id"]),
                "in_seconds": fit["in"],
                "out_seconds": fit["out"],
                "reason": f"storyline group {group.get('group_id', 'main')}",
            }
            if fit["speed"] != 1.0:
                cut["speed"] = fit["speed"]
            cuts.append(cut)
            shown_by_index.append((cursor, cursor + fit["shown"]))
            cursor += fit["shown"]

        group_summaries = []
        for group, idxs in group_ranges:
            group_summaries.append({
                "group_id": group.get("group_id", "main"),
                "clip_ids": [plan[i]["clip"]["id"] for i in idxs],
                "start_seconds": round(shown_by_index[idxs[0]][0], 3),
                "end_seconds": round(shown_by_index[idxs[-1]][1], 3),
                "script_text": group.get("script_text", ""),
            })

        edit_decisions: dict[str, Any] = {
            "version": "1.0",
            "renderer_family": family,
            "render_runtime": runtime,
            "cuts": cuts,
            "metadata": {
                "generated_by": "storyline_timeline",
                "cut_semantics": "source_trim_window; shown_duration=(out-in)/speed",
                "total_duration_seconds": round(cursor, 3),
                "voiceover_first": voiceover_mode,
            },
        }
        for key in ("audio", "subtitles"):
            if inputs.get(key):
                edit_decisions[key] = inputs[key]

        # Fail loudly if we ever emit something the pipeline schema rejects.
        try:
            import jsonschema
            schema_path = Path(__file__).resolve().parents[2] / "schemas" / "artifacts" / "edit_decisions.schema.json"
            jsonschema.validate(edit_decisions, json.loads(schema_path.read_text(encoding="utf-8")))
        except ImportError:
            pass
        except Exception as exc:  # jsonschema.ValidationError and friends
            return ToolResult(success=False, error=f"Generated edit_decisions failed schema validation: {exc}")

        artifacts: list[str] = []
        output_path = inputs.get("output_path")
        if output_path:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_text(json.dumps(edit_decisions, indent=2), encoding="utf-8")
            artifacts.append(str(output_path))

        data: dict[str, Any] = {
            "edit_decisions": edit_decisions,
            "groups": group_summaries,
            "total_duration_seconds": round(cursor, 3),
            "cut_count": len(cuts),
            "snapped_boundaries": snapped_info,
            "warnings": warnings,
        }
        if vo_path:
            data["compose_hints"] = {
                "audio_path": str(vo_path),
                "note": "Pass audio_path to video_compose render so the user's voiceover is muxed under the picture.",
            }
            if vo_dur and abs(float(vo_dur) - cursor) > 0.5:
                warnings.append(
                    f"Timeline is {cursor:.2f}s but the voiceover is {float(vo_dur):.2f}s; "
                    "extend the clips or trim the voiceover.")
        return ToolResult(success=True, data=data, artifacts=artifacts,
                          duration_seconds=round(time.time() - start, 2))
