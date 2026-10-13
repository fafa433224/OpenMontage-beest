# Storyline Edit — Edit Director

Build `edit_decisions` with the tool, not by hand:
0. Always pass `renderer_family` (default `documentary-montage`, the
   footage-first family) — `video_compose render` blocks without it — and
   `render_runtime` of `remotion` or `ffmpeg` only.
1. Call `storyline_timeline` with clips + groups (+ each group's measured
   narration duration, + `beats_ms` when BGM was analyzed). It emits the
   schema-valid `edit_decisions` (v1.0) with the locked `render_runtime`.
   *Voiceover-first:* give every group `narration_start_seconds` plus
   `voiceover_path` (and `voiceover_duration_seconds` if known). Group
   windows then tile the user's audio and cuts land on their sentences.
   Read the tool's `warnings`: a "too short for its slot" warning means a
   clip had to be slowed to its floor and sync will drift — add B-roll.
   `in_seconds`/`out_seconds` are a SOURCE trim window, not timeline
   positions; do not edit them by hand.
2. If beats were used, declare beat snapping in the decision log.
3. Subtitles from the recalibrated transcript via `subtitle_gen` when the
   brief requires captions (word-level burn stays a Remotion job).
4. Sanity-check the tool's `total_duration_seconds` against the narration
   and BGM lengths before checkpointing.
