# Storyline Edit — Idea Director (brief + inventory + grouping)

Goal: a brief that knows exactly what media exists and how it groups into
a story. Steps:
1. Inventory every source (user files first; stock search only to fill
   declared gaps). Use `video_analyzer` / `video_understand` for a caption
   and true duration per clip; `scene_detect` to split long sources into
   shots.
2. Speech footage: `transcriber` -> read the transcript, mark bad takes,
   filler-only and repeated segments with `keep: false`, then run
   `speech_rough_cut` (operation=cut) BEFORE grouping anything.
   **User brought their own voiceover** (a recorded narration file)? Run
   `transcriber` on it, split the transcript into the sentences that
   belong to each story beat, and keep each beat's start time. Do NOT
   generate TTS for it. This is *voiceover-first* mode (see
   `skills/core/firered-storyline.md`): the user's audio is the master
   clock and the picture is cut to it.
3. Assign 100% of kept clips to narrative groups (hook / setup /
   development / climax / close, or the brief's own arc). Ungrouped clips
   will fail the edit stage by design — decide now.
4. Present the render runtimes this pipeline supports — **Remotion or
   FFmpeg**. `hyperframes` is deferred for this pipeline (it cannot offset
   into source footage, so footage cuts would render wrong); tell the user
   that if they ask for it. Recommend one, and log the
   `render_runtime_selection` decision (see AGENT_GUIDE hard rule). Record the target platform, duration, and narration plan.
