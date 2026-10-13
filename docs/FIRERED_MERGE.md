# FireRed-OpenStoryline → OpenMontage Merge

This document records the merge of FireRed-OpenStoryline's director
capabilities into OpenMontage. It is the audit trail: what was ported,
what was adapted, what was fixed, and what was deliberately NOT ported.

- **Source:** FireRedTeam/FireRed-OpenStoryline (checked at the clone used
  for this merge; core nodes under `src/open_storyline/nodes/core_nodes/`)
- **License of ported code:** Apache License 2.0 — full text in
  `LICENSES/FireRed-Apache-2.0.txt`. OpenMontage itself is AGPL-3.0; the
  combined work is distributed under AGPL-3.0 with the Apache-2.0 portions
  remaining under their original license, as Apache-2.0 permits.
- **Result:** 4 native tools in `tools/storyline/`, one Layer-2 skill
  (`skills/core/firered-storyline.md`), one pipeline
  (`pipeline_defs/storyline-edit.yaml` + `skills/pipelines/storyline-edit/`),
  and tests in `tests/tools/test_storyline_tools.py`.

## Ported (needed, and missing or only skill-level in OpenMontage)

| FireRed origin | OpenMontage result | Adaptation |
|---|---|---|
| `speech_rough_cut.py` (258 lines): sentence grouping, cut points, deleted ranges, ASR recalibration | `tools/storyline/speech_rough_cut.py` | LLM call removed (the OpenMontage agent marks `keep: false`); deterministic filler-only / repeated-sentence cleanup added; recalibration bug fixed (see below); accepts the `transcriber` tool's segment shape |
| `select_bgm.py`: librosa beat tracking, accent-beat selection | `tools/storyline/bgm_beat_sync.py` (`analyze`) | Track *choice* stays with the agent + existing music tools; this tool only measures the chosen track. librosa is an optional dependency |
| `plan_timeline_pro.py`: fit durations to TTS, fit to beats, speed computation; `replace_with_closest_if_within_threshold` | `tools/storyline/storyline_timeline.py` + `bgm_beat_sync` (`snap`) | Random group margins replaced by a declared fixed margin (reproducibility); equal-share allocation replaced by proportional allocation (documented in the tool); output is OpenMontage's schema-valid `edit_decisions` v1.0, not FireRed's MoviePy timeline dict |
| `group_clips.py` + `generate_script.py` (group → script-from-groups discipline) | Agent work guided by `skills/core/firered-storyline.md`, enforced by `storyline_timeline` (fails on ungrouped clips) | In OpenMontage the agent is the LLM; porting FireRed's internal LLM calls would duplicate it. The *contract* (every clip grouped, script written from groups) is what was merged |
| `generate_ai_transition.py` (571 lines): first/last-frame transition generation, aspect checks | `tools/storyline/ai_transition_plan.py` | Planning half ported; generation delegated to the existing provider-routed `video_selector` stack instead of FireRed's single hard-wired AIGC provider |
| Style Skill archiving (`.storyline/skills/`, `skills_io.py`) | Publish-stage practice in `storyline-edit` (save the working pattern as a style skill) | Uses OpenMontage's existing skill system rather than a second skill store |

## Fixed vs. the original

- **ASR recalibration bug (FireRed `calibrate_asr_times`):** timestamps
  after the last deleted range were shifted by the deleted total *before*
  that range instead of the full deleted total. The port shifts by the
  full total; `test_rough_cut_recalibration_shifts_by_full_deleted_duration`
  locks the correct behavior.

## Deliberately NOT ported (would duplicate or weaken OpenMontage)

| FireRed part | Why not |
|---|---|
| MoviePy `render_video.py` (1,022 lines) | OpenMontage renders via Remotion / HyperFrames / FFmpeg with locked-runtime governance; a second renderer would fork the contract |
| Web UI, FastAPI server, MCP server shell, LangChain agent | OpenMontage is agent-driven by design (`PROJECT_CONTEXT.md`: "No Python orchestrator"); its agent + checkpoint system already provides this layer |
| `search_media.py` (Pexels only) | OpenMontage media search is broader (Pexels, Pixabay, Archive.org/NASA/Wikimedia corpus, CLIP-embedding clip search) |
| `split_shots.py` TransNetV2 weights pipeline | `scene_detect` (PySceneDetect + FFmpeg fallback) already covers shot splitting without a second model download |
| `understand_clips.py` VLM node | `video_analyzer` / `video_understand` already provide clip understanding to the agent |
| `generate_voiceover.py` single-provider TTS wiring | OpenMontage has 20+ TTS tools behind `tts_selector` |
| FireRed `config.toml`, session store, prompt files (zh/en) | OpenMontage config, checkpoints, and skills supersede them; prompts' *discipline* is captured in `skills/core/firered-storyline.md` |

## How the merged system runs

```text
storyline-edit pipeline
  idea      -> inventory + scene_detect + (speech? transcriber + speech_rough_cut) + grouping + runtime lock
  script    -> one script block per group, from the group's clips
  scene_plan-> ordered clips per group (storyline_timeline pre-check)
  assets    -> tts_selector (measured group durations), BGM choice + bgm_beat_sync analyze, optional ai_transition_plan
  edit      -> storyline_timeline -> edit_decisions (schema-valid, beat-snapped if declared)
  compose   -> video_compose / hyperframes_compose with the locked runtime
  publish   -> export_bundle + save the pattern as a reusable style skill
```

## Verification (at merge time)

- `pytest tests/tools/test_storyline_tools.py` — 10 passed (ported
  algorithms, schema validation of emitted `edit_decisions`, ungrouped-clip
  rejection, beat snapping, aspect warnings).
- `pytest tests/contracts/test_pipeline_catalog.py` — the new
  `storyline-edit` manifest loads and its declared stage order/approval
  gates resolve.
- End-to-end smoke (synthetic media, this workspace): 12s speech video →
  `speech_rough_cut` (filler + repeat removed) → 6.4s rough cut;
  `storyline_timeline` → `edit_decisions.json` → `hyperframes_compose`
  render → 9s 1920×1080 MP4. Render engines at smoke time:
  `{'ffmpeg': True, 'remotion': True, 'hyperframes': True}`.

## Known limits

- `bgm_beat_sync analyze` needs `pip install librosa`; `snap` works
  without it when beats are supplied.
- AI transitions still cost generation credits via `video_selector`;
  the planner exists so the spend is reviewed before it happens.
- The storyline pipeline is `stability: beta` until it has real-footage
  productions through it; expect pacing defaults (margins, min clip
  duration) to be tuned by early use.
