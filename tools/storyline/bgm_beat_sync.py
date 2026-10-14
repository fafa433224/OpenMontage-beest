"""BGM beat analysis and cut snapping — ported from FireRed-OpenStoryline.

FireRed origin (Apache-2.0):
  - Music analysis: ``select_bgm.py`` ``analyze_music_metrics`` /
    ``_compute_accent_beats`` (librosa beat tracking + accent-beat picking).
  - Cut snapping: ``plan_timeline_pro.py``
    ``replace_with_closest_if_within_threshold``.

In OpenMontage this splits into two operations because the agent, not this
tool, chooses the track (see the ``music_library`` / ``freesound_music`` /
``suno_music`` tools):
  - analyze: BPM, accent beats, energy of one chosen BGM file (needs librosa)
  - snap:    move timeline cut boundaries onto the nearest accent beat
             within a threshold. Pure math — no librosa needed, so timeline
             planning works even when the optional dependency is absent.
"""

from __future__ import annotations

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
    ToolStatus,
)


class BgmBeatSync(BaseTool):
    name = "bgm_beat_sync"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "audio"
    provider = "firered-storyline"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = []  # librosa ships in requirements.txt; `snap` works without it
    install_instructions = (
        "Beat analysis needs librosa: pip install librosa. "
        "The `snap` operation works without it (pass beats explicitly)."
    )
    agent_skills = ["ffmpeg"]

    capabilities = ["bgm_beat_analysis", "accent_beat_detection", "cut_beat_snapping"]
    best_for = [
        "Cutting a montage so scene changes land on strong music beats",
        "Getting BPM / accent beats of a chosen BGM track for timeline planning",
    ]
    not_good_for = [
        "Choosing the music itself (agent + music tools do that)",
        "Beat analysis when librosa is not installed (snap still works)",
    ]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {"type": "string", "enum": ["analyze", "snap"]},
            "audio_path": {"type": "string", "description": "BGM file (analyze; optional for snap if beats given)"},
            "beats_ms": {"type": "array", "items": {"type": "number"}, "description": "Accent beats in ms (snap input, e.g. from a prior analyze)"},
            "cut_times_ms": {"type": "array", "items": {"type": "number"}, "description": "Timeline cut boundary times to snap (snap)"},
            "threshold_ms": {"type": "number", "default": 500, "description": "Only snap a cut if a beat is within this distance"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=2048, vram_mb=0, disk_mb=500, network_required=False)
    retry_policy = RetryPolicy(max_retries=0)
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["operation", "audio_path", "cut_times_ms", "threshold_ms"]
    side_effects = []
    user_visible_verification = [
        "Play the rendered video and confirm cuts land on audible beats",
    ]

    def get_status(self) -> ToolStatus:
        try:
            import librosa  # noqa: F401
            return ToolStatus.AVAILABLE
        except ImportError:
            return ToolStatus.DEGRADED  # snap still works

    # ---- Ported FireRed logic (Apache-2.0) ----

    @staticmethod
    def snap_to_beats(cut_times_ms: list[float], beats_ms: list[float],
                      threshold_ms: float = 500) -> list[dict]:
        """Snap each cut to the closest beat within threshold.

        Port of PlanTimelinePro.replace_with_closest_if_within_threshold,
        extended to return an audit trail per cut.
        """
        beats = sorted(float(b) for b in beats_ms)
        results = []
        for t in cut_times_ms:
            t = float(t)
            closest, best_diff = None, None
            for b in beats:
                diff = abs(b - t)
                if best_diff is None or diff < best_diff:
                    closest, best_diff = b, diff
            if closest is not None and best_diff is not None and best_diff <= threshold_ms:
                results.append({"original_ms": t, "snapped_ms": closest, "shift_ms": closest - t})
            else:
                results.append({"original_ms": t, "snapped_ms": t, "shift_ms": 0.0})
        return results

    @staticmethod
    def analyze_beats(audio_path: Path) -> dict[str, Any]:
        """BPM + accent beats + energy. Port of FireRed analyze_music_metrics."""
        import librosa
        import numpy as np

        y, sr = librosa.load(str(audio_path), sr=22050, mono=True)
        duration_ms = int(librosa.get_duration(y=y, sr=sr) * 1000)
        if y.size < 2048:
            raise RuntimeError("The selected background music is too short.")

        onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
        bpm, beat_frames = librosa.beat.beat_track(
            onset_envelope=onset_env, sr=sr, hop_length=512, units="frames")
        bpm_val = float(np.atleast_1d(bpm)[0])
        beat_frames = np.asarray(beat_frames, dtype=int)

        # Accent beats: strongest onsets, locally normalized (FireRed port).
        onset_at_beats = onset_env[np.clip(beat_frames, 0, len(onset_env) - 1)].astype(float)
        accent_times: list[float] = []
        if onset_at_beats.size:
            win = 8
            strengths = []
            for i, v in enumerate(onset_at_beats):
                lo, hi = max(0, i - win), min(len(onset_at_beats), i + win + 1)
                local_max = onset_at_beats[lo:hi].max() or 1.0
                strengths.append(v / local_max)
            strengths_arr = np.asarray(strengths)
            threshold = np.percentile(strengths_arr, 75)
            for i, s in enumerate(strengths_arr):
                is_peak = (i == 0 or s >= strengths_arr[i - 1]) and \
                          (i == len(strengths_arr) - 1 or s >= strengths_arr[i + 1])
                if s >= threshold and is_peak:
                    accent_times.append(float(librosa.frames_to_time(
                        beat_frames[i], sr=sr, hop_length=512) * 1000))

        rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=512)[0]
        return {
            "path": str(audio_path),
            "duration_ms": duration_ms,
            "bpm": bpm_val,
            "beats_ms": accent_times,
            "beat_count": len(accent_times),
            "energy_mean": float(np.mean(rms)),
        }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        operation = inputs["operation"]

        if operation == "analyze":
            audio_path = inputs.get("audio_path")
            if not audio_path or not Path(audio_path).exists():
                return ToolResult(success=False, error=f"BGM file not found: {audio_path}")
            try:
                data = self.analyze_beats(Path(audio_path))
            except ImportError:
                return ToolResult(success=False, error=(
                    "librosa is not installed; beat analysis unavailable. "
                    "Install with: pip install librosa — or use operation=snap with beats_ms."))
            except Exception as exc:
                return ToolResult(success=False, error=f"Beat analysis failed: {exc}")
            return ToolResult(success=True, data=data,
                              duration_seconds=round(time.time() - start, 2))

        # snap
        beats = inputs.get("beats_ms")
        if beats is None and inputs.get("audio_path"):
            try:
                beats = self.analyze_beats(Path(inputs["audio_path"]))["beats_ms"]
            except Exception as exc:
                return ToolResult(success=False, error=f"Beat analysis failed: {exc}")
        if not beats:
            return ToolResult(success=False, error="snap requires beats_ms or an analyzable audio_path")
        cuts = inputs.get("cut_times_ms") or []
        snapped = self.snap_to_beats(cuts, beats, float(inputs.get("threshold_ms", 500)))
        return ToolResult(
            success=True,
            data={"cuts": snapped, "snapped_count": sum(1 for c in snapped if c["shift_ms"] != 0)},
            duration_seconds=round(time.time() - start, 2),
        )
