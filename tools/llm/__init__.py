"""Runtime LLM tools — any provider, one interface.

This package registers the runtime-LLM capability that the original
agent-first design left to the IDE assistant. With it, headless runs
(Kaggle notebooks, CI, cron) can do creative work: script writing,
scene planning, creative direction.

See ``lib/llm_client.py`` for the connection contract (14 provider
presets + any OpenAI-compatible endpoint) and ``lib/creative_director.py``
for the seeded fallback that keeps output creative without an LLM.
"""
