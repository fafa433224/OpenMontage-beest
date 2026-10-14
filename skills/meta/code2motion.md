# Code2motion — LLM-authored motion code, validated

Code2motion is the upgrade from "pick a template and parameterize it" to
"author bespoke motion code per video". The creative ceiling of the
templated path is the component vocabulary; code2motion writes NEW
choreography while the validation ladder guarantees the render.

## The doctrine

> The LLM only shifts choices — the seeded director and the validation
> ladder guarantee a valid render.

Concretely: generation is *always optional*. Every code2motion path has a
parameterized fallback that renders. A failed generation costs one log
line, never a video.

## Where it runs

| Surface | Path | Fallback |
|---|---|---|
| HyperFrames intro/outro cards | `lib/code2motion.generate_hyperframes_card` via `render_card` (`scripts/kaggle/render_voiceover_video.py`) | brand-parameterized template (`scripts/kaggle/hyperframes/<kind>/index.html`) |
| HyperFrames body (atelier) | `hyperframes_compose render_existing` with an authored workspace | templated scaffold |
| Remotion bespoke composition | atelier mode (`video_compose._render_via_atelier`, `edit_decisions.bespoke`) | standard RENDERER_FAMILY_MAP compositions |

Enable: `--motion code2motion` (force) or `--motion auto` (default —
generates only when a runtime LLM is configured).

## The validation ladder (HyperFrames cards)

1. **Structural lint** (`lib/code2motion.lint_hyperframes_html`) — the
   contract, checked before any browser starts:
   - one root `<div data-composition-id="root">` with
     `data-duration/width/height/fps`
   - one visual `class="clip"` element with `data-start`,
     `data-duration`, `data-track-index`
   - GSAP loaded from LOCAL `assets/gsap.min.js` (never a CDN — offline
     Kaggle renders)
   - timeline built as `gsap.timeline({paused: true})` and registered at
     `window.__timelines["root"]`
   - `<audio>` leaves carry `id` attributes (media discovery)
   - **determinism**: no `gsap.utils.random()` / `Math.random()` — fixed
     value arrays so parallel frame workers agree
   - **bounded**: no `repeat: -1/Infinity` (seek-by-frame capture never
     ends otherwise)
   - **transform/opacity only**: no layout props (top/left/letterSpacing)
     in tweens — they snap to integers under capture
   - **offline**: no remote assets, no fetch/eval/new Function/iframes
2. **Repair loop** — lint violations go back to the LLM (max 2 rounds)
   with the exact rule list.
3. **Render probe** — `npx hyperframes render` under `timeout`, success
   judged by ffprobe duration of the OUTPUT FILE (the teardown-hang guard).
4. **Fallback** — brand template fills the same slot; provenance is
   recorded in the timing log (`timing.motion.<kind>.rendered_as`).

## Authoring rules (what the prompt enforces — Fable-derived)

- **Fractional timing**: every tween position/duration is a fraction of
  the card duration (`0.18*D`), so choreography survives duration changes.
- **Continuity anchors**: the intro ends on the brand bg/accent the body
  opens with; the outro opens where the body closes — the cut reads as
  one film.
- **Brand envelope**: palette, typography, timing curve and pacing come
  from `channels/<id>/brand.yaml`; the LLM composes WITHIN it.
- **A signature moment** per card: masked wipes, staggered cascades,
  choreographed camera, counter-motion — never a static card with a fade.

## Provenance

Every decision is logged: model, attempts, chars, violations,
`rendered_as` (code2motion / brand_template / raw fallback). Find it in
`<out>/<name>-timing.json` under `motion`, and in
`projects/<id>/harness-summary.json` for harness runs.

## Extending

- New card kinds: add a template under `scripts/kaggle/hyperframes/<kind>/`
  and a branch in `render_card` (fill tokens via
  `lib.code2motion.fill_hyperframes_template`).
- Remotion side: generate atelier projects (`scripts/scaffold_atelier_project.py`)
  and render through `video_compose` atelier mode; keep the
  no-stock-imports + art-direction checks that `_run_atelier_checks`
  enforces.
