# Storyline Edit — Asset Director

Produce real assets with real measurements:
- Narration, **user-supplied voiceover**: skip `tts_selector`. Register the
  file in the asset manifest, read its true length with ffprobe, and record
  each group's `narration_start_seconds` (from the transcript) plus the
  total `voiceover_duration_seconds`. Never re-time or re-record it.
- Narration, **generated**: `tts_selector` per group; record each group's measured
  narration duration in the asset manifest (the edit stage fits clips to
  these numbers — guesses here become pacing bugs there).
- Music: pick ONE BGM track (`music_library` / `pixabay_music` /
  `suno_music`), then `bgm_beat_sync` operation=analyze and store
  `beats_ms`. No track = declare it; never fake beats.
- AI transitions (paid, optional): only with delivery-promise
  justification. `ai_transition_plan` for all boundaries, generate ONE
  with `video_selector` (first/last-frame mode), review continuity,
  then batch the rest.
- Generated fill clips, if any group has a declared gap, also go through
  `video_selector` and join that group's clip list before edit.
