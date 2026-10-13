"""AI transition planner — ported from FireRed-OpenStoryline.

FireRed origin (Apache-2.0): ``generate_ai_transition.py`` — build a
transition clip from the last frame of clip A, the first frame of clip B,
and a natural-language description, via an external AIGC video service.

What changed for OpenMontage — deliberately: OpenMontage already has a
large, provider-routed video generation stack (``video_selector`` and
friends: Veo, Kling, Seedance, Runway, ...). Re-porting FireRed's single
hard-wired provider would duplicate and weaken that. This tool therefore
ports the *planning* half of FireRed's node — aspect-ratio compatibility
checks and first/last-frame transition request construction — and emits
requests the agent executes with the existing generation stack. The
resulting clips are then inserted into the timeline as normal cuts.
"""

from __future__ import annotations

import time
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

DEFAULT_STYLE = (
    "a smooth, cinematic, physically continuous transition: the camera and "
    "light carry over the cut, the ending subject naturally transforms into "
    "the opening subject, no text, no abrupt jump"
)


class AiTransitionPlan(BaseTool):
    name = "ai_transition_plan"
    version = "1.0.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "firered-storyline"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = []
    install_instructions = "No installation needed (pure Python planner; generation uses video_selector)."
    agent_skills = []

    capabilities = ["ai_transition_planning", "first_last_frame_transition_requests"]
    best_for = [
        "Planning generated transitions between storyline cuts before spending generation credits",
        "Checking aspect-ratio compatibility of adjacent clips for frame-bridged transitions",
    ]
    not_good_for = [
        "Generating the transition video itself (use video_selector with the emitted request)",
        "Cheap/hard cuts — use transition_in/transition_out on the cuts instead",
    ]

    input_schema = {
        "type": "object",
        "required": ["clips"],
        "properties": {
            "clips": {
                "type": "array",
                "description": "Ordered clips: {id, source, description?, width?, height?}",
                "items": {"type": "object"},
            },
            "style_prompt": {"type": "string", "description": "Transition style description; a cinematic default is used otherwise"},
            "duration_seconds": {"type": "number", "default": 2.0, "description": "Requested transition clip duration (1-5s typical)"},
            "only_between_groups": {"type": "boolean", "default": True, "description": "Only plan transitions where group_id changes (hard cuts inside a group)"},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=100, network_required=False)
    retry_policy = RetryPolicy(max_retries=0)
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["clips", "style_prompt", "duration_seconds"]
    side_effects = []
    user_visible_verification = [
        "Review each transition prompt before generating — generation costs money",
        "Generate one transition first and check continuity before batch-generating the rest",
    ]

    @staticmethod
    def _aspect(size: tuple[float, float] | None) -> float | None:
        if not size or not size[1]:
            return None
        return size[0] / size[1]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        clips = inputs.get("clips") or []
        if len(clips) < 2:
            return ToolResult(success=False, error="Need at least 2 clips to plan transitions")
        style = inputs.get("style_prompt") or DEFAULT_STYLE
        duration = float(inputs.get("duration_seconds", 2.0))
        only_between = bool(inputs.get("only_between_groups", True))

        requests: list[dict] = []
        warnings: list[str] = []
        for a, b in zip(clips[:-1], clips[1:]):
            if only_between and a.get("group_id") and a.get("group_id") == b.get("group_id"):
                continue
            size_a = (a.get("width"), a.get("height")) if a.get("width") and a.get("height") else None
            size_b = (b.get("width"), b.get("height")) if b.get("width") and b.get("height") else None
            ar_a, ar_b = self._aspect(size_a), self._aspect(size_b)  # type: ignore[arg-type]
            compatible = True
            if ar_a and ar_b and abs(ar_a - ar_b) / max(ar_a, ar_b) > 0.05:
                compatible = False
                warnings.append(
                    f"Aspect ratio differs between {a.get('id')} ({ar_a:.2f}) and "
                    f"{b.get('id')} ({ar_b:.2f}); reframe/crop before frame-bridging.")
            desc_a = a.get("description", "the ending scene")
            desc_b = b.get("description", "the opening scene")
            requests.append({
                "id": f"transition-{len(requests) + 1:03d}",
                "from_clip": a.get("id"),
                "to_clip": b.get("id"),
                "first_frame_source": a.get("source", a.get("id")),
                "last_frame_source": b.get("source", b.get("id")),
                "duration_seconds": duration,
                "aspect_compatible": compatible,
                "prompt": (
                    f"Transition from a scene described as: {desc_a}. "
                    f"Into a scene described as: {desc_b}. Style: {style}."
                ),
                "execute_with": "video_selector (first/last-frame image-to-video mode)",
                "timeline_insert": "Insert the generated clip between the two cuts and set transition_in='ai_generated' on the following cut.",
            })

        return ToolResult(
            success=True,
            data={"transition_requests": requests, "warnings": warnings,
                  "estimated_generations": len(requests)},
            duration_seconds=round(time.time() - start, 2),
        )
