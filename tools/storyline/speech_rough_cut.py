"""Speech rough cut tool — ported from FireRed-OpenStoryline.

FireRed origin: ``src/open_storyline/nodes/core_nodes/speech_rough_cut.py``
(Apache-2.0). The sentence-grouping, cut-point, deleted-range, and ASR
timestamp-recalibration algorithms are ported from that node. Two changes
were made for OpenMontage, both deliberate:

1. The LLM call inside FireRed's node is removed. In OpenMontage the agent
   IS the intelligence: it reads the transcript (see the ``transcriber``
   tool) and optionally marks segments with ``keep: false`` before calling
   this tool. Everything after that decision — grouping, cutting, and
   timestamp recalibration — is deterministic and lives here.
2. A bug in FireRed's ``calibrate_asr_times`` remap for timestamps after
   the last deleted range (it subtracted the total deleted *before* the
   last range instead of the full total) is fixed; see ``remap_time``.

Deterministic cleanups applied even without agent marks:
  - segments whose text is only filler words ("um", "uh", "you know", ...)
  - empty segments
  - immediately repeated sentences (same normalized text within a window)

Modes:
  - plan: return kept ranges / deleted ranges / recalibrated transcript
  - cut:  also cut the video with FFmpeg into one rough-cut MP4
"""

from __future__ import annotations

import re
import tempfile
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

DEFAULT_FILLER_WORDS = [
    "um", "uh", "uhm", "er", "ah", "hmm", "mm", "mhm",
    "like", "you know", "i mean", "basically", "actually", "literally",
    "right", "so yeah", "yeah so",
]


def _normalize_text(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


class SpeechRoughCut(BaseTool):
    name = "speech_rough_cut"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "firered-storyline"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["cmd:ffmpeg"]
    install_instructions = "Install FFmpeg: https://ffmpeg.org/download.html"
    agent_skills = ["ffmpeg"]

    capabilities = [
        "speech_rough_cut",
        "filler_word_removal",
        "repeated_sentence_removal",
        "asr_timestamp_recalibration",
    ]
    best_for = [
        "Talking-head / speech footage: tighten a transcript into a rough cut",
        "Removing filler-only and repeated segments before timeline planning",
    ]
    not_good_for = [
        "Deciding *what* to cut creatively — the agent marks keep=false for that",
        "Non-speech footage (use scene_detect + storyline_timeline instead)",
    ]

    input_schema = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["plan", "cut"], "default": "plan"},
            "segments": {
                "type": "array",
                "description": "Transcript segments: {text, start, end} plus optional keep/drop flags. Matches the transcriber tool's segment shape.",
                "items": {"type": "object"},
            },
            "time_unit": {"type": "string", "enum": ["seconds", "ms"], "default": "seconds"},
            "input_path": {"type": "string", "description": "Source video (required for operation=cut)"},
            "output_path": {"type": "string", "description": "Rough-cut MP4 (required for operation=cut)"},
            "gap_threshold_ms": {"type": "number", "default": 400, "description": "Group sentences closer than this gap into one kept range"},
            "padding_ms": {"type": "number", "default": 80, "description": "Safety padding kept on each side of a kept range"},
            "filler_words": {"type": "array", "items": {"type": "string"}, "description": "Override the default filler word/phrase list"},
            "duplicate_window_ms": {"type": "number", "default": 5000, "description": "Drop a sentence identical to the previous kept one if it starts within this window"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=4000, network_required=False)
    retry_policy = RetryPolicy(max_retries=0)
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["segments", "gap_threshold_ms", "padding_ms"]
    side_effects = ["writes rough-cut MP4 to output_path when operation=cut"]
    user_visible_verification = [
        "Watch cut points for clipped word starts/ends; increase padding_ms if clipped",
        "Compare recalibrated transcript timestamps against the rough-cut video",
    ]

    # ---- Ported FireRed algorithms (Apache-2.0) ----

    @staticmethod
    def group_sentences(items: list[dict], gap_threshold_ms: float = 400) -> list[list[dict]]:
        """Group sentences into segments by gap threshold (ms). FireRed port."""
        segments: list[list[dict]] = []
        if not items:
            return segments
        current = [items[0]]
        for i in range(len(items) - 1):
            cur, nxt = items[i], items[i + 1]
            if nxt["start_ms"] - cur["end_ms"] > gap_threshold_ms:
                segments.append(current)
                current = [nxt]
            else:
                current.append(nxt)
        if current:
            segments.append(current)
        return segments

    @staticmethod
    def segments_to_ranges(segments: list[list[dict]]) -> list[dict]:
        """Convert grouped sentence segments to start/end ranges. FireRed port."""
        return [
            {"start_ms": seg[0]["start_ms"], "end_ms": seg[-1]["end_ms"]}
            for seg in segments if seg
        ]

    @staticmethod
    def ranges_to_cut_points(ranges: list[dict], buffer_ms: float = 100) -> list[float]:
        """Ranges -> ffmpeg cut points, buffer added, overlaps prevented. FireRed port."""
        if not ranges:
            return []
        cuts: list[float] = []
        for i in range(len(ranges) - 1):
            end_cut = ranges[i]["end_ms"] + buffer_ms
            start_cut = ranges[i + 1]["start_ms"] - buffer_ms
            if start_cut < end_cut:
                mid = (start_cut + end_cut) / 2
                end_cut = mid
                start_cut = mid
            cuts.append(end_cut)
            cuts.append(start_cut)
        return [max(ranges[0]["start_ms"] - buffer_ms, 0)] + cuts + [ranges[-1]["end_ms"] + buffer_ms]

    @staticmethod
    def compute_deleted_ranges(ranges: list[dict]) -> list[dict]:
        """Time ranges removed between kept ranges (incl. before the first)."""
        deleted: list[dict] = []
        prev_end = 0.0
        for r in ranges:
            if r["start_ms"] > prev_end:
                deleted.append({"start_ms": prev_end, "end_ms": r["start_ms"]})
            prev_end = max(prev_end, r["end_ms"])
        return deleted

    @staticmethod
    def calibrate_times(items: list[dict], deleted_ranges: list[dict]) -> list[dict]:
        """Recalibrate timestamps after deletions.

        FireRed port with the after-last-range bug fixed: a timestamp after
        all deleted ranges must shift by the *full* deleted duration.
        """
        if not deleted_ranges:
            return [dict(it) for it in items]
        deleted = sorted(deleted_ranges, key=lambda r: r["start_ms"])

        def remap(t: float) -> float:
            shift = 0.0
            for r in deleted:
                if t >= r["end_ms"]:
                    shift += r["end_ms"] - r["start_ms"]
                elif t > r["start_ms"]:
                    return r["start_ms"] - shift  # inside a deleted range
                else:
                    break
            return t - shift

        out = []
        for item in items:
            new_item = dict(item)
            new_item["start_ms"] = remap(float(item["start_ms"]))
            new_item["end_ms"] = remap(float(item["end_ms"]))
            out.append(new_item)
        return out

    # ---- Tool execution ----

    def _normalize_segments(self, inputs: dict[str, Any]) -> list[dict]:
        # Unit is decided per key: *_ms is always ms, *_seconds is always
        # seconds; only the bare start/end keys follow ``time_unit``. (Before
        # this fix, start_ms values were also multiplied by 1000 under the
        # default time_unit, turning a 5 s clip into 5,000 s.)
        bare_scale = 1000.0 if inputs.get("time_unit", "seconds") == "seconds" else 1.0

        def to_ms(seg: dict, ms_key: str, sec_key: str, bare_key: str) -> float:
            if seg.get(ms_key) is not None:
                return float(seg[ms_key])
            if seg.get(sec_key) is not None:
                return float(seg[sec_key]) * 1000.0
            return float(seg.get(bare_key, 0) or 0) * bare_scale

        normalized = []
        for seg in inputs.get("segments") or []:
            start = to_ms(seg, "start_ms", "start_seconds", "start")
            end = to_ms(seg, "end_ms", "end_seconds", "end")
            normalized.append({
                "text": seg.get("text", ""),
                "start_ms": start,
                "end_ms": end,
                "keep": seg.get("keep"),
                "drop": bool(seg.get("drop", False)) or seg.get("action") == "cut",
            })
        return sorted(normalized, key=lambda s: s["start_ms"])

    def _select_kept(self, segments: list[dict], inputs: dict[str, Any]) -> tuple[list[dict], list[dict]]:
        fillers = set(inputs.get("filler_words") or DEFAULT_FILLER_WORDS)
        dup_window = float(inputs.get("duplicate_window_ms", 5000))
        kept, dropped = [], []
        for seg in segments:
            norm = _normalize_text(seg["text"])
            reason = None
            if seg["drop"] or seg["keep"] is False:
                reason = "agent_marked_cut"
            elif not norm:
                reason = "empty"
            elif norm in fillers or all(tok in fillers for tok in norm.split()):
                reason = "filler_only"
            elif kept and norm == _normalize_text(kept[-1]["text"]) and \
                    seg["start_ms"] - kept[-1]["end_ms"] <= dup_window:
                reason = "repeated_sentence"
            if reason:
                dropped.append({**seg, "drop_reason": reason})
            else:
                kept.append(seg)
        return kept, dropped

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start_time = time.time()
        operation = inputs.get("operation", "plan")
        segments = self._normalize_segments(inputs)
        if not segments:
            return ToolResult(success=False, error="No transcript segments provided")

        kept, dropped = self._select_kept(segments, inputs)
        if not kept:
            return ToolResult(success=False, error="All segments were cut; nothing left to keep")

        groups = self.group_sentences(kept, float(inputs.get("gap_threshold_ms", 400)))
        padding = float(inputs.get("padding_ms", 80))
        ranges = self.segments_to_ranges(groups)
        for r in ranges:  # apply safety padding, then merge overlaps
            r["start_ms"] = max(0.0, r["start_ms"] - padding)
            r["end_ms"] = r["end_ms"] + padding
        ranges.sort(key=lambda r: r["start_ms"])
        merged: list[dict] = []
        for r in ranges:
            if merged and r["start_ms"] <= merged[-1]["end_ms"]:
                merged[-1]["end_ms"] = max(merged[-1]["end_ms"], r["end_ms"])
            else:
                merged.append(dict(r))

        deleted = self.compute_deleted_ranges(merged)
        recalibrated = self.calibrate_times(kept, deleted)
        data: dict[str, Any] = {
            "kept_ranges_ms": merged,
            "cut_points_ms": self.ranges_to_cut_points(merged, padding),
            "deleted_ranges_ms": deleted,
            "kept_segments": recalibrated,
            "dropped_segments": dropped,
            "original_duration_ms": max(s["end_ms"] for s in segments),
            "kept_duration_ms": sum(r["end_ms"] - r["start_ms"] for r in merged),
        }
        artifacts: list[str] = []

        if operation == "cut":
            input_path, output_path = inputs.get("input_path"), inputs.get("output_path")
            if not input_path or not output_path:
                return ToolResult(success=False, error="operation=cut requires input_path and output_path")
            if not Path(input_path).exists():
                return ToolResult(success=False, error=f"Input not found: {input_path}")
            try:
                self._cut_video(Path(input_path), Path(output_path), merged)
            except Exception as exc:  # ToolCommandError etc.
                return ToolResult(success=False, error=f"FFmpeg rough cut failed: {exc}")
            data["output"] = str(output_path)
            artifacts.append(str(output_path))

        return ToolResult(
            success=True, data=data, artifacts=artifacts,
            duration_seconds=round(time.time() - start_time, 2),
        )

    def _cut_video(self, input_path: Path, output_path: Path, ranges_ms: list[dict]) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_dir = Path(tmp)
            list_file = tmp_dir / "concat.txt"
            lines = []
            for i, r in enumerate(ranges_ms):
                seg_path = tmp_dir / f"seg_{i:04d}.mp4"
                self.run_command([
                    "ffmpeg", "-y", "-ss", f"{r['start_ms'] / 1000:.3f}",
                    "-to", f"{r['end_ms'] / 1000:.3f}", "-i", str(input_path),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(seg_path),
                ], timeout=600)
                lines.append(f"file '{seg_path}'")
            list_file.write_text("\n".join(lines), encoding="utf-8")
            self.run_command([
                "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_file),
                "-c", "copy", str(output_path),
            ], timeout=600)
