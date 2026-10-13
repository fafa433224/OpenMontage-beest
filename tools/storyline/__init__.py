"""Storyline director tools ported from FireRed-OpenStoryline.

This package ports the *director* capabilities of FireRed-OpenStoryline
(clip-first storyline planning, speech rough cut, BGM beat sync, and AI
transition planning) into OpenMontage as native ``BaseTool`` tools.

Deliberately NOT ported (OpenMontage already does these better):
  - FireRed's MoviePy renderer (OpenMontage renders via Remotion/HyperFrames/FFmpeg)
  - FireRed's Web UI / FastAPI / MCP server shell
  - FireRed's LangChain agent (the OpenMontage agent itself is the driver)
  - FireRed's media generation (OpenMontage has 100+ generation tools)

Attribution: original logic from FireRedTeam/FireRed-OpenStoryline,
Apache License 2.0. See ``docs/FIRERED_MERGE.md`` and
``LICENSES/FireRed-Apache-2.0.txt`` in this repository.
"""
