"""OpenMontage headless production harness.

One command turns a script, a text file, a bare idea, or a finished
voiceover into a rendered, QC-passed video — no IDE agent, no UI:

    python -m harness run <input> [--channel tech-deep] [--out DIR]
    python -m harness resume <project-id>
    python -m harness status <project-id>
    python -m harness channels
    python -m harness doctor

Architecture — PLAN -> EXECUTE -> REVIEW (the harness recipe: a loop with
permissions, a context file, a budget, and crash recovery):

* PLAN     the runtime LLM (any of the 14 presets in lib/llm_client)
  turns free text / an idea / a transcript into a structured script
  (hook -> sections -> CTA, Fable-style). No LLM -> deterministic
  heuristics; an explicit script.json is respected verbatim.
* EXECUTE  the proven deterministic stack: Piper narration (when no
  voiceover is supplied), whisper word captions, Open-Sora b-roll on the
  second GPU, the branded creative director, the Remotion body, the
  HyperFrames intro/outro (brand template or code2motion), concat, QC.
* REVIEW   QC tripwires (duration, word coverage, codecs) plus an
  optional LLM review round of the QC artifacts.

Every stage checkpoints into projects/<id>/ so a Kaggle session death
(30 h/week quota, 9-12 h sessions) costs only the un-checkpointed stage.

The harness never asks a human anything by default (--approval auto);
--approval gate freezes the plan to disk for editing and resumes later.
"""
from harness.runner import run_project, resume_project, project_status
from harness.cli import main

__version__ = "1.0.0"
__all__ = ["run_project", "resume_project", "project_status", "main", "__version__"]
