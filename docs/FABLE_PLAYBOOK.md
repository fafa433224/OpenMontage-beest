# The Fable Playbook — what we took from Fable 5.1 and how it landed here

Research date: 2026-10. Sources: public reviews, Reddit threads from users
who rendered with Fable 5.1 + Claude, feature write-ups (media.io),
community discussion. This document records the transferable mechanics we
identified and where each one lives in this repo now.

## What Fable 5.1 actually does (the load-bearing observations)

1. **Plan-first, generate-second.** Hooks, scenes, narration, and CTAs are
   decided BEFORE any generation; shot lists and storyboard notes are
   first-class artifacts; QA notes travel with the plan. The tool is
   described as "a planning engine for better AI videos."
2. **Scene continuity — shared frames.** "Every scene opens on the exact
   frame the previous one closed on. Put every shared point." Adjacent
   scenes share boundary state (visual + positional), which is why the
   output reads as one film rather than stitched clips.
3. **Fractional internal timing.** "Internal timings are fractions of the
   scene's duration" — choreography is authored relative to scene length,
   so pacing is portable and edits never break timing.
4. **An agent writes the edit.** Under the hood Claude emits the
   analyze/cut/stitch commands (ffmpeg), i.e. the intelligence is a
   code-writing agent with a deterministic execution layer — not a
   monolithic model call.
5. **QA before delivery** — review passes and cost awareness around
   evaluation ("don't ask for a full project review" = expensive passes).

## What was already here (OpenMontage doctrine parity)

- Plan-first: the pipeline stages (`research → proposal → script →
  scene_plan → assets → edit → compose`) + director skills.
- Agent-writes-the-edit: the atelier path (`edit_decisions.bespoke`,
  authored Remotion/HyperFrames code) — though it needed a generator.
- QA before delivery: pre-compose validation + post-render self-review
  (ffprobe, frame census, word coverage).

## What we added because of this research

| Fable mechanic | Where it landed |
|---|---|
| Plan-first with hook/CTA + QA notes | `harness/inputs.py` SCRIPT_PLAN_SYSTEM_PROMPT (hook first, CTA last, `qa_notes` reviewed by the harness review stage) |
| Scene continuity / shared boundary state | code2motion authoring rules: intro ends on the body's opening palette, outro opens on the body's closing palette (`lib/code2motion.py` CARD_SYSTEM_PROMPT rule 11) + brand palettes shared across the whole video (`lib/brand_profile.py`) |
| Fractional internal timing | HyperFrames intro/outro templates authored with `S = D / reference` scaling — every GSAP position is a fraction of card duration (`scripts/kaggle/hyperframes/*/index.html`); code2motion prompts mandate `0.18*D`-style positions |
| Agent writes the code, ladder validates | `lib/code2motion.py` generation + repair loop + render probe + template fallback; wired via `render_card` (`--motion code2motion|auto`) |
| Cheap QA, expensive review gated | Harness review = deterministic tripwires (fast, free); LLM review is optional and advisory, never a gate |

## What we deliberately did NOT copy

- **No single-model lock-in.** Fable rides one assistant; we kept the
  "any LLM, any provider" universal client (14 presets + custom
  endpoints) — including none (seeded director).
- **No black-box renderer.** Fable's composition is opaque; ours stays
  Remotion + HyperFrames + ffmpeg, inspectable and forkable.
- **No hidden state.** Every harness decision lands in
  `projects/<id>/` (checkpoints, decision log, timing, review) — the
  Backlot board can watch it live.

## Market weak points this stack now answers (2026 tooling critique)

- "AI motion looks smooth but feels wrong" → seeded director +
  code2motion author choreography per video instead of template reuse.
- "Every video looks the same" → per-project seed + per-channel brand
  envelope: same channel recognisable, no two videos identical.
- "Robotic voices on long passages" → voice direction lives in the
  playbooks; word-level captions + pacing rules keep narration honest.
- "Exact control is uneven" → every creative dimension is either a
  parameter (brand yaml) or generated code you can read and edit —
  never a hidden prompt.
