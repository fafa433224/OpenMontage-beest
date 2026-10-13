# Storyline Edit — Compose Director

Render `edit_decisions` with `video_compose` (`operation=render`), routed
by the `render_runtime` locked at proposal: `remotion` or `ffmpeg`.
HyperFrames is deferred for this pipeline because it cannot offset into
source footage — if the locked `render_runtime` is `hyperframes`, stop and
escalate rather than rendering.
- User-supplied voiceover: pass `audio_path` (the tool's
  `compose_hints.audio_path`) so the voiceover is muxed under the picture,
  then check the render length against the voiceover length.
- The locked runtime is a commitment. If it is unavailable, escalate per
  AGENT_GUIDE ("Escalate Blockers Explicitly"); never silently swap.
- Mix narration over BGM with `audio_mixer` (duck music under speech).
- Watch the full render once: group pacing, beat landings (if snapped),
  transition continuity. Fail loudly back to edit on any mismatch.
