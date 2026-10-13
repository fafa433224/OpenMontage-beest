# Storyline Edit — Edit Director

Build `edit_decisions` with the tool, not by hand:
1. Call `storyline_timeline` with clips + groups (+ each group's measured
   narration duration, + `beats_ms` when BGM was analyzed). It emits the
   schema-valid `edit_decisions` (v1.0) with the locked `render_runtime`.
2. If beats were used, declare beat snapping in the decision log.
3. Subtitles from the recalibrated transcript via `subtitle_gen` when the
   brief requires captions (word-level burn stays a Remotion job — see
   `skills/core/hyperframes.md` parity notes).
4. Sanity-check the tool's `total_duration_seconds` against the narration
   and BGM lengths before checkpointing.
