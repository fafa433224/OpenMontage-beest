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
3. Assign 100% of kept clips to narrative groups (hook / setup /
   development / climax / close, or the brief's own arc). Ungrouped clips
   will fail the edit stage by design — decide now.
4. Present both render runtimes when both are available, recommend one,
   and log the `render_runtime_selection` decision (see AGENT_GUIDE hard
   rule). Record the target platform, duration, and narration plan.
