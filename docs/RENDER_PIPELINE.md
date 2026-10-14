# Render pipeline — the rules that only a real render could teach

Every rule below was paid for. Each one is a defect that reached a rendered
frame, was diagnosed from the pixels, and then fixed at the layer that owned
it. They are written down because they are all *invisible to green tests*:
the unit suite passed the entire time these were broken.

If you change the media path, read this first.

---

## 1. One owner per footage asset

**Rule.** A cut renders its footage exactly once. A compiled (`bespoke`) cut
owns it in `bespoke.media`; a template cut owns it in `backgroundVideo`. Never
both.

**Symptom.** `Compositor error: No frame found at position 10666 for source
…/bokeh-warm.mp4`, on a file that ffmpeg decodes without complaint.

**Why.** Two readers, one file, different trim points. The compositor does not
take two conflicting views of the same asset kindly.

**Enforced.** `build_explainer_props.py` strips `backgroundVideo*` from every
compiled cut, and `SceneRenderer.maybeWrapWithBg` returns early for a bespoke
cut that carries a spec — so hand-edited or older props cannot reintroduce it.

---

## 2. Bake the loop; never re-derive it per frame

**Rule.** A scene that needs footage gets a clip that is already exactly the
scene's length, written at prep time by `scripts/kaggle/scene_media.py`.

**Why.** The render-time alternative — `trimBefore` + `trimAfter` + `<Loop>` —
is correct on paper and racy in practice. Several things must agree about
where the loop phase is, and when they disagree the compositor loses a frame.

**What the bake does.** Cuts the source window once, applies up to 2x ambient
slow-motion toward the scene length (`MAX_SLOWDOWN`), repeats the remainder
only if it still falls short, and reports `slowdown` / `loops` so the numbers
behind the look are auditable after the fact.

It also **never upscales** — a 3.5 s scene takes 3.5 s of source, not the whole
8 s window.

---

## 3. Baked clips live on the compositor's timeline

**Rule.** `time_base` must be **1/12800**, `start_time` must be **0**.

**Symptom.** `No frame found at position 43008` for a request at 3.36 s —
i.e. `3.36 × 12800`. The lookup misses.

**Why.** Remotion's compositor addresses media in a fixed 1/12800 timescale
(the value it prints in the error, and the time_base of every clip the
composer ships). ffmpeg instead derives a timescale from the output frame
rate: a 30 fps bake came out `1/15360` with `start_time=0.033`, so every
position the compositor computed referred to a timeline the file did not have.

**Enforced.** `bake_scene_clip` pins `-video_track_timescale 12800` and puts
the first frame at 0 with `setpts=PTS-STARTPTS`. Clips are also encoded
all-intra, so no lookup depends on walking a GOP.

---

## 4. Play whole clips with `<Video>`, not `<OffthreadVideo>`

**Rule.** A pre-baked clip is rendered with `<Video>` (the browser decoder).
`<OffthreadVideo>` is reserved for media that genuinely needs frame-accurate
extraction — the looping fallback.

**Why.** `<OffthreadVideo>` extracts every frame through Remotion's bundled
Rust ffmpeg (`remotion::ffmpeg::extract_frame`), which in a constrained
headless container fails at arbitrary positions on files ffmpeg itself reads
cleanly. A clip that is already the scene's length needs no seeking, so the
browser decoder is both sufficient and reliable.

The two APIs have *different* failure modes, and it is worth knowing both:

| API | Fails as | Cause |
|---|---|---|
| `<OffthreadVideo>` | `No frame found at position N` | Rust extractor, at random |
| `<Video>` | `Rendering <Html5Video /> …` timeout | browser seek exceeds `delayRender` budget |

`<Video>` seeks rather than plays when the renderer random-accesses frames, so
its budget has to be generous. `OPENMONTAGE_RENDER_TIMEOUT_MS` (default
420000) overrides `--timeout`.

---

## 5. Concurrency is a resource question, not a correctness one

Measured on this repo's 2-vCPU box, same props, same command:

| workers | result |
|---|---|
| 2 | 4 failures / 6 attempts |
| 1 | 0 failures / 4 attempts |

After rule 4 landed, 2 workers rendered clean twice in a row **and 8x faster**
(≈40 s vs ≈318 s). The race was the renderer, not the worker count.

The render stage therefore uses Remotion's own default (one process per core)
and keeps a retry: if a multi-worker run fails for either reason in the table
above, it retries single-worker and says which cause it saw. Do not remove the
retry — the looping fallback still uses `<OffthreadVideo>`.

---

## 6. Text has an opacity floor

**Rule.** `TEXT_LAYER_KINDS` never render below `TEXT_OPACITY_FLOOR` (0.85).

**Symptom.** Body copy that reads as "washed out" — because it is.

**Why.** Schemes authored body copy at 0.28–0.55. `#9FB8BC` on `#09191B` is
8.6:1 as a swatch and **3.45:1** at 0.55, which fails WCAG AA. Transparency is
a decoration tool; using it on the words is a legibility bug.

**Enforced.** `compile_scene` clamps, and `lint_spec` rejects — so a spec
that arrives from anywhere else is still caught. `palette_for_spec` checks
contrast on the **composite**, via `ensure_contrast_at_alpha`, because the
colour a viewer sees is the blend, not the swatch.

---

## 7. The texture grade goes under the type

**Rule.** The texture pass renders before the content layers.

**Why.** It rendered last — over the words. Film grain is luminance-neutral
noise, so over the whole frame is right. A line-based treatment that only ever
darkens is not, and at `amount 0.388` it chewed 46 px body copy into mid-grey.

---

## 8. Measure the type, not the background glow

**Rule.** Contrast is `p99 - p05` luma with a floor of `0.30`. **Not**
`p95 - p05`.

**Why.** Type covers well under 5% of a frame's pixels, so p95 lands on the
background glow and measures how much internal contrast the *plate* has —
not whether the words are readable. Measured on real frames of one scene:

| frame | p95−p05 | p99−p05 |
|---|---|---|
| full-scale, glyphs `#F4F6FB` on `#09191B` (16:1) | 0.258 | 0.533 |
| genuinely washed | 0.143 | 0.165 |

p95 ranks the legible frame worse than the washed one. p99 ranks them
correctly.

---

## 9. Audit at delivery resolution

**Rule.** Sample at ≥1280 px wide, and never upscale a smaller source.

**Why.** Two separate traps:

* A half-scale preview (`--scale=0.5`) is not a legibility test. One-pixel
  scanlines do not downscale, so thin type is destroyed by resampling and the
  audit reports failures on scenes whose full-scale glyphs measure 16:1.
* `scale=W:-2` **upscales** a smaller source, which invents no detail and
  smooths adjacent frames into looking alike — it made a genuinely moving
  video measure *less* varied the larger it was asked to be.

`extract_frames` uses `scale=min(iw,W):-2` and the report records
`frame_width` (actually read) alongside `requested_frame_width`. Findings
below 1280 px are downgraded to advisory `info` with the reason attached,
because a downscaled sample cannot settle a legibility question.

---

## 10. A short scene must not spend half its life blank

**Rule.** In scenes under `SHORT_SCENE_SECONDS` (6.0), every text layer is on
screen by `SHORT_SCENE_TEXT_DEADLINE` (0.55). Decor keeps its own pacing.

**Symptom.** A frame with decor, a caption and no words — found by looking at
a render at 20 s.

**Why.** Staging authored for a long scene was reused unchanged on a 2.7 s
title card, whose kicker then landed at 55% — after the scene was half over.

---

## 11. A missing asset is a warning, a missing narration is a failure

`preflight_props` heals absent music, SFX and b-roll (dropping the cue and
saying so) because Remotion hard-fails the whole render on a 404 — tens of
seconds in, after ffmpeg and npm have already run. Narration is different:
without it there is no video, so it fails fast and loudly.

The same principle covers degraded inputs: if `faster-whisper` is
unavailable, word timings are reconstructed from the script and the measured
section durations, and `timing.word_source` records `estimated` rather than
`whisper`. The degradation is never silent.

---

## Verifying a change here

Green tests prove nothing about a render. The loop is:

```bash
python -m pytest tests/harness/test_render_path_regressions.py tests/harness/test_vision_audit.py -q
python -m harness run <script.json> --channel <id> --motion code2motion --out /tmp/qc
```

then **look at the frames** — `read_file` on `qc-frames/sceneNNN/fNNNN.png` —
and read `visual-audit.json`. Every rule above was found by a human-equivalent
looking at a PNG, not by a passing suite.
