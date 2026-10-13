# FireRed Storyline Skill (Layer 2)

This is the **OpenMontage-specific** guide to the director capabilities
ported from **FireRed-OpenStoryline** (Apache-2.0). Read
`docs/FIRERED_MERGE.md` for what was ported, what was deliberately left
behind, and why.

FireRed's core insight: a good edit of *existing footage* follows a fixed
director chain. OpenMontage keeps that chain, but the agent (you) performs
the creative steps and the new `tools/storyline/` tools perform the
deterministic steps. Do not skip steps, and do not re-implement the
deterministic steps by hand.

## The storyline chain

```text
1. Load & review      tools: video_downloader / local media + source_media_review
2. Split into shots   tools: scene_detect (PySceneDetect)
3. Understand clips   tools: video_analyzer / video_understand
                      -> one caption/description + duration per clip
4. (Speech footage)   tools: transcriber -> YOU mark keep=false on bad
   rough cut first           takes, then speech_rough_cut (operation=cut)
5. Group into story   YOU: assign every clip to a narrative group
   groups                    (hook / setup / development / climax / close)
                             and write the group's script block.
                             FireRed did this in GroupClips+GenerateScript;
                             here it is agent work, guided by the brief.
6. Fit the timeline   tools: storyline_timeline
                      -> narration durations per group in, schema-valid
                         edit_decisions out. Never hand-compute cut times.
7. Music              tools: music_library / pixabay_music / suno_music
                      -> YOU pick the track, then bgm_beat_sync
                         (analyze, then pass beats_ms to storyline_timeline
                         or snap) so cuts land on accent beats.
8. Transitions        Default: hard cuts inside a group, dissolve between
                      groups. Only if the brief promises generated
                      transitions: ai_transition_plan -> video_selector
                      (first/last-frame mode) -> insert as cuts.
                      Generation costs money: plan all, generate ONE,
                      review, then batch.
9. Compose & publish  tools: video_compose with the render_runtime locked
                      at proposal (remotion | ffmpeg — never hyperframes here).
                      FireRed's MoviePy renderer is NOT used in OpenMontage.
```

## Hard rules

- **Every clip must be grouped.** `storyline_timeline` fails on ungrouped
  clips by design. An ungrouped clip is a story decision you have not made.
- **Script comes from the groups, not before them.** Write each group's
  script after seeing its clips (step 5), then fit durations (step 6).
  Reversing this produces narration that describes footage you don't have.
- **Rough cut before grouping** for speech footage. Grouping filler and
  repeated takes wastes every later step.
- **Beat snapping is optional, never silent.** If you snap cuts to beats,
  say so in the decision log; unsnapped is the honest default.
- **AI transitions are a paid upgrade, not a default.** The
  `delivery_promise` must justify them before `ai_transition_plan` runs.

## Voiceover-first (the user brings their own narration)

1. `transcriber` on the voiceover -> sentences with timestamps.
2. Group the sentences into story beats; for each beat note the time its
   first sentence starts (`narration_start_seconds`).
3. Assign clips to beats exactly as in step 5 above.
4. `storyline_timeline` with `groups[].narration_start_seconds`,
   `voiceover_path` and `voiceover_duration_seconds`. Each group's window
   runs from its start to the next group's start; the first group also
   covers any lead-in; the last runs to the end of the audio.
5. Compose with `audio_path = compose_hints.audio_path`.

If a clip is too short for its window it is slowed down to
`min_playback_speed` (default 0.5) and the tool warns that sync will drift:
fix that with more B-roll, not by hand-editing cut times.

## edit_decisions contract (do not break)

For video, `in_seconds`/`out_seconds` is a **source trim window** and the cut
is shown for `(out - in) / speed` seconds. This matches `video_compose`
(ffmpeg), the Remotion CinematicRenderer adapter and documentary-montage.
HyperFrames reads these fields as timeline positions and cannot offset into
footage, which is why this pipeline does not use it.

## Saving a style for reuse

When a storyline edit works, save the *pattern* (group structure, margins,
beat policy, transition policy, playbook) as a project style skill — the
OpenMontage equivalent of FireRed's archived Style Skills — so the next
video with the same shape is a batch job, not a new invention.

## When NOT to use this chain

- Topic → fully generated video (explainer, cinematic, avatar pipelines):
  use those pipelines directly; this chain is for editing existing media.
- Simple concat/trim with no story: `video_compose` with FFmpeg is enough.
