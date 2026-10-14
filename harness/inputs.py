"""Input detection and script derivation — any input becomes script.json.

Supported inputs (auto-detected by extension/content):

=====================  =======================================  ==============
input                  needs LLM?                               produces
=====================  =======================================  ==============
script.json            no (respected verbatim)                 script + text
*.txt / *.md           optional (better sections, heuristics   script + text
                       otherwise)
voiceover audio        optional (section titles; sentence      script + audio
(.mp3/.wav/.m4a/...)   grouping otherwise)
short free text        yes (error with guidance without one)   script + text
=====================  =======================================  ==============

The PLAN contract (Fable-derived): hook first, 2-5 body sections, CTA
last; every section gets a heading and 1-4 sentences of narration text;
b-roll mood words for retrieval; a QA checklist the review stage checks.
The LLM only shapes the plan — the schema is enforced here, and the
seeded director guarantees a valid render with or without it.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
TEXT_EXTENSIONS = {".txt", ".md", ".markdown"}

SCRIPT_PLAN_SYSTEM_PROMPT = """You are the showrunner of a short-form motion-graphics video channel.
Turn the user's material into a production-ready narration script.

Rules:
- 3 to 6 sections. The FIRST section is the hook (make me care in one
  sentence). The LAST section lands the takeaway or CTA.
- Each section: a "heading" (max 6 words, title case) and "text" (1-4
  spoken sentences, plain words, no stage directions, no markdown).
- Total narration must fit the requested duration at ~2.5 words/second.
- Ground facts in the user's material; do not invent statistics.
- "broll_moods": 3-6 abstract visual mood phrases for stock footage
  retrieval (e.g. "neon city rain", "macro water droplets").
- "qa_notes": 2-4 concrete checks a reviewer should verify later.

Answer with ONLY a JSON object:
{
  "title": "video title",
  "topic": "one-line topic",
  "sections": [{"id": "s0", "heading": "...", "text": "..."}],
  "broll_moods": ["..."],
  "qa_notes": ["..."]
}"""


class InputError(ValueError):
    """Raised when the input cannot be turned into a script."""


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def detect_input(path: Path) -> str:
    if not path.exists():
        raise InputError(f"input not found: {path}")
    if path.is_dir():
        if (path / "script.json").exists():
            return "script_dir"
        if any((path / "sections").glob("*.mp3")):
            return "audio_dir"
        raise InputError(f"input dir has no script.json or sections/*.mp3: {path}")
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "script_json"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in TEXT_EXTENSIONS:
        return "text"
    # Short string content without extension → treat as a raw idea.
    if path.stat().st_size < 4000 and not suffix:
        return "idea"
    return "text"


def is_audio_file(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXTENSIONS


def audio_duration(path: Path) -> Optional[float]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=15,
        ).stdout.strip()
        return float(out)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Validation + normalization
# ---------------------------------------------------------------------------


def normalize_script(script: dict[str, Any], fallback_topic: str = "") -> dict[str, Any]:
    """Validate/repair the script.json structure into the canonical shape."""
    if not isinstance(script, dict):
        raise InputError("script.json must be a JSON object")
    sections = script.get("sections")
    if not isinstance(sections, list) or not sections:
        raise InputError("script.json needs a non-empty 'sections' array")
    clean = []
    for i, sec in enumerate(sections):
        if not isinstance(sec, dict):
            continue
        text = str(sec.get("text") or "").strip()
        heading = str(sec.get("heading") or "").strip()
        if not text:
            continue
        clean.append({
            "id": str(sec.get("id") or f"s{i}"),
            "heading": heading or f"Part {i + 1}",
            "text": text,
        })
    if not clean:
        raise InputError("no section with non-empty 'text' found in script.json")
    return {
        "topic": str(script.get("topic") or fallback_topic or clean[0]["heading"]),
        "title": str(script.get("title") or script.get("topic") or clean[0]["heading"]),
        "sections": clean,
        "broll_moods": [str(m) for m in (script.get("broll_moods") or []) if str(m).strip()],
    }


# ---------------------------------------------------------------------------
# Text → sections (LLM plan or heuristic)
# ---------------------------------------------------------------------------


def _heuristic_sections(text: str) -> list[dict[str, str]]:
    """Deterministic split: paragraphs (or sentences) → sections."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(paragraphs) < 2:
        # One blob: split by sentence into ~3 groups.
        sentences = [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+", text) if s.strip()]
        if len(sentences) < 2:
            return [{"id": "s0", "heading": "Introduction", "text": text.strip()}]
        groups = max(2, min(5, len(sentences) // 3 or 2))
        per = (len(sentences) + groups - 1) // groups
        paragraphs = [" ".join(sentences[i:i + per]) for i in range(0, len(sentences), per)]
    sections = []
    for i, para in enumerate(paragraphs):
        words = para.split()
        heading = " ".join(words[:5]).strip(" ,.:;-—") or f"Part {i + 1}"
        sections.append({
            "id": f"s{i}",
            "heading": heading[:60],
            "text": para,
        })
    return sections


def llm_plan(text: str, llm_client: Any, duration_hint_s: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Ask the LLM for the production plan. None on any failure."""
    if llm_client is None:
        return None
    try:
        budget = ""
        if duration_hint_s:
            words = int(duration_hint_s * 2.5)
            budget = f"\nTarget duration {int(duration_hint_s)}s — roughly {words} narration words total."
        plan = llm_client.chat_json(
            [{"role": "user", "content": f"Material:\n{text[:6000]}{budget}"}],
            system=SCRIPT_PLAN_SYSTEM_PROMPT,
            temperature=0.7,
            max_tokens=2500,
        )
        if not isinstance(plan, dict):
            return None
        return normalize_script(plan, fallback_topic=text[:60])
    except Exception:
        return None


def script_from_text(path: Path, llm_client: Any, duration_hint_s: Optional[float] = None) -> dict[str, Any]:
    """Text file → script. LLM plan when available, heuristics otherwise."""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise InputError(f"text file is empty: {path}")
    plan = llm_plan(text, llm_client, duration_hint_s)
    if plan:
        plan["source"] = "llm_plan"
        return plan
    script = normalize_script(
        {"sections": _heuristic_sections(text), "topic": text.split("\n")[0][:60]},
        fallback_topic=text[:60],
    )
    script["source"] = "heuristic"
    return script


def script_from_idea(idea: str, llm_client: Any, duration_hint_s: Optional[float] = None) -> dict[str, Any]:
    """A bare idea needs the LLM — offline there is nothing to narrate."""
    plan = llm_plan(idea, llm_client, duration_hint_s)
    if plan:
        plan["source"] = "llm_plan"
        return plan
    raise InputError(
        "a bare idea needs a runtime LLM to write the script (set LLM_BASE_URL/"
        "LLM_API_KEY/LLM_MODEL or any provider key), or supply a script.json/"
        "*.txt/voiceover file instead"
    )


# ---------------------------------------------------------------------------
# Voiceover-first: transcript → sections + per-section audio cuts
# ---------------------------------------------------------------------------


def script_from_transcript(
    words: list[dict[str, Any]],
    audio_path: Path,
    llm_client: Any,
    work_dir: Path,
) -> dict[str, dict[str, Any]]:
    """Voiceover is the clock: transcribe → group → cut section audio.

    Returns ``{"script": ..., "section_cuts": [{id, start, end}]}`` where
    the cuts are seconds into the source voiceover (gapless, sum ≈ total).
    """
    if not words:
        raise InputError("transcription returned no words")

    # Sentence grouping over the word stream.
    sentences: list[dict[str, Any]] = []
    current: dict[str, Any] = {"words": [], "start": words[0]["start"]}
    for w in words:
        if current["start"] is None:
            current["start"] = w["start"]
        current["words"].append(w)
        if re.search(r"[.!?。！？]$", str(w.get("word", "")).strip()):
            current["end"] = w["end"]
            sentences.append(current)
            current = {"words": [], "start": None}
    if current["words"]:
        current["end"] = current["words"][-1]["end"]
        sentences.append(current)

    # Group sentences into 3-6 sections.
    target_sections = max(3, min(6, len(sentences) // 3 or 3))
    per = (len(sentences) + target_sections - 1) // target_sections
    groups = [sentences[i:i + per] for i in range(0, len(sentences), per)]

    sections, cuts = [], []
    for i, group in enumerate(groups):
        text = " ".join(str(w["word"]) for s in group for w in s["words"])
        text = re.sub(r"\s+([,.!?;:])", r"\1", text).strip()
        heading = " ".join(str(w["word"]) for w in group[0]["words"][:5]).strip(" ,.:;-—")
        sec_id = f"s{i}"
        sections.append({
            "id": sec_id,
            "heading": (heading or f"Part {i + 1}")[:60],
            "text": text,
        })
        cuts.append({
            "id": sec_id,
            "start": round(float(group[0]["start"]), 3),
            "end": round(float(group[-1]["end"]), 3),
        })

    # LLM titles: upgrade the mechanical headings when a model is present.
    if llm_client is not None:
        try:
            titled = llm_client.chat_json(
                [{"role": "user", "content": json.dumps(
                    {"headings_to_improve": [s["heading"] for s in sections],
                     "section_texts": [s["text"][:200] for s in sections]},
                    ensure_ascii=False)}],
                system=(
                    "Rewrite each video section heading: max 6 words, title "
                    "case, specific and evocative. Answer ONLY a JSON object "
                    '{"headings": ["...", "..."]} with the same count.'
                ),
                temperature=0.7,
                max_tokens=300,
            )
            new_headings = titled.get("headings") if isinstance(titled, dict) else None
            if isinstance(new_headings, list) and len(new_headings) == len(sections):
                for sec, new_h in zip(sections, new_headings):
                    cleaned = str(new_h).strip()
                    if cleaned:
                        sec["heading"] = cleaned[:60]
        except Exception:
            pass  # headings are cosmetic — mechanical ones are fine

    script = normalize_script(
        {"sections": sections, "topic": sections[0]["heading"]}, fallback_topic="voiceover"
    )
    script["source"] = "voiceover_transcript"
    return {"script": script, "section_cuts": cuts}


def cut_section_audio(audio_path: Path, cuts: list[dict[str, Any]], sections_dir: Path) -> list[Path]:
    """Cut per-section mp3s from the voiceover at the given seconds."""
    sections_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for cut in cuts:
        dest = sections_dir / f"{cut['id']}.mp3"
        if dest.exists():
            out.append(dest)
            continue
        dur = max(0.2, cut["end"] - cut["start"])
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error",
             "-ss", str(cut["start"]), "-i", str(audio_path),
             "-t", str(dur), "-c:a", "libmp3lame", "-b:a", "128k", str(dest)],
            check=True,
        )
        out.append(dest)
    return out
