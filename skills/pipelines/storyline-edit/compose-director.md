# Storyline Edit — Compose Director

Render `edit_decisions` with `video_compose` (or `hyperframes_compose`
when the locked runtime is hyperframes):
- The locked runtime is a commitment. If it is unavailable, escalate per
  AGENT_GUIDE ("Escalate Blockers Explicitly"); never silently swap.
- Mix narration over BGM with `audio_mixer` (duck music under speech).
- Watch the full render once: group pacing, beat landings (if snapped),
  transition continuity. Fail loudly back to edit on any mismatch.
