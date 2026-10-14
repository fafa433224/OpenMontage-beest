# Harness — headless production without an IDE agent

The harness is the code orchestrator for runs where no human-driven
coding assistant is present: Kaggle notebooks, CI, cron, batch renders.

```
python -m harness run <input> [--channel <id>] [--motion auto|template|code2motion]
python -m harness resume <project-id>
python -m harness status <project-id>
python -m harness channels
python -m harness doctor
```

## When to use which runner

| Runner | Use when |
|---|---|
| **Agent pipeline** (pipeline_defs + checkpoints, per `AGENT_GUIDE.md`) | A human is driving an IDE assistant; creative gates matter |
| **Harness** (`python -m harness`) | Nobody is watching. Input file → finished video, unattended |
| **Direct renderer** (`scripts/kaggle/render_voiceover_video.py`) | You already have `script.json + voiceover.mp3 + sections/*.mp3` and want the render only |

The harness is PLAN → EXECUTE → REVIEW: it reuses the SAME deterministic
execution stack as the agent pipeline — it does not invent a parallel
one.

## Inputs (auto-detected)

- `script.json` — sections `{id, heading, text}`; respected verbatim
- `*.txt` / `*.md` — LLM plans sections (hook → body → CTA) when a runtime
  LLM is configured; heuristic paragraph split otherwise
- voiceover audio (`.mp3/.wav/...`) — voiceover-first: whisper transcribes,
  sentences group into sections, the audio is cut per section; the
  voiceover is the clock
- a bare idea (short text) — requires a runtime LLM to write the script

## What the harness adds over the raw renderer

1. **Input intelligence** — any of the above becomes the narration contract
   (`script.json + sections/*.mp3 + voiceover.mp3`), with Piper TTS when no
   voiceover is supplied (free, offline).
2. **Channel branding** — `--channel <id>` anchors the whole look (see
   `channels/<id>/brand.yaml`; `python -m harness channels` lists them).
3. **Code2motion** — `--motion code2motion|auto` lets the runtime LLM
   author bespoke HyperFrames intro/outro cards (validated + template
   fallback; see `skills/meta/code2motion.md`).
4. **Checkpoints** — every stage lands in `projects/<id>/`; after a Kaggle
   session death, `python -m harness resume <id>` continues.
5. **Review tripwires** — duration sanity, word coverage ≥ 80%, codecs,
   resolution; plus an optional LLM review written to
   `projects/<id>/artifacts/harness_review.json`.

## Approval policies

- `--approval auto` (default): headless pre-authorization, recorded in the
  project's `decision_log.json` (category `approval_policy`) per the
  checkpoint protocol.
- `--approval gate`: the plan is frozen to
  `projects/<id>/plan-awaiting-approval.json`; edit it, then
  `python -m harness resume <id>`.
- `--approval ask`: interactive prompt (not for headless boxes).

## Environment

- `harness doctor` verifies everything: python deps, ffmpeg, Node ≥ 22
  (Remotion 4 + HyperFrames), torch/faster-whisper (GPU transcription),
  piper (narration), lancedb (Open-Sora corpus), the runtime LLM,
  `OPEN_SORA_DIR`, available channels.
- Runtime LLM: any of the 14 presets in `lib/llm_client.py` —
  `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` (or a provider key). No
  LLM → seeded creative director + heuristics; the video still renders.
- On Kaggle T4 x2: whisper owns GPU 0 while the Open-Sora CLIP collector
  owns GPU 1 (inherited from the renderer). CPU boxes run everything
  serially.
