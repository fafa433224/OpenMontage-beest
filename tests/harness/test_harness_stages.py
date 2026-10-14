"""Tests for the harness package — input derivation + state machine.

These are hermetic: no LLM, no GPU, no rendering. The full stack is
covered by the verified Kaggle run and the local harness-verify e2e.
"""
import json
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "kaggle"))

from harness import inputs as harness_inputs  # noqa: E402


# ---------------------------------------------------------------------------
# Input detection
# ---------------------------------------------------------------------------


def test_detect_script_json(tmp_path):
    p = tmp_path / "script.json"
    p.write_text('{"sections": []}')
    assert harness_inputs.detect_input(p) == "script_json"


def test_detect_text_and_audio(tmp_path):
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    assert harness_inputs.detect_input(txt) == "text"

    mp3 = tmp_path / "voice.mp3"
    mp3.write_bytes(b"\x00" * 16)
    assert harness_inputs.detect_input(mp3) == "audio"


def test_detect_script_dir(tmp_path):
    (tmp_path / "script.json").write_text('{"sections": []}')
    assert harness_inputs.detect_input(tmp_path) == "script_dir"


def test_detect_missing_raises(tmp_path):
    with pytest.raises(harness_inputs.InputError):
        harness_inputs.detect_input(tmp_path / "ghost.json")


# ---------------------------------------------------------------------------
# Script normalization
# ---------------------------------------------------------------------------


def test_normalize_script_rejects_empty():
    with pytest.raises(harness_inputs.InputError):
        harness_inputs.normalize_script({"sections": []})


def test_normalize_script_fills_ids_and_headings():
    s = harness_inputs.normalize_script({"sections": [{"text": "Hello world."}]})
    assert s["sections"][0]["id"] == "s0"
    assert s["sections"][0]["heading"]
    assert s["topic"]


# ---------------------------------------------------------------------------
# Text → sections (heuristic, no LLM)
# ---------------------------------------------------------------------------


def test_heuristic_paragraph_split(tmp_path):
    text = "First paragraph here.\n\nSecond paragraph follows.\n\nThird one closes."
    p = tmp_path / "input.txt"
    p.write_text(text, encoding="utf-8")
    script = harness_inputs.script_from_text(p, None)
    assert script["source"] == "heuristic"
    assert len(script["sections"]) == 3


def test_heuristic_sentence_split_single_paragraph(tmp_path):
    text = "One. Two. Three. Four. Five. Six. Seven. Eight. Nine. Ten."
    p = tmp_path / "input.txt"
    p.write_text(text, encoding="utf-8")
    script = harness_inputs.script_from_text(p, None)
    assert len(script["sections"]) >= 2


def test_llm_plan_used_when_client_present(tmp_path):
    class _PlanClient:
        def chat_json(self, messages, system=None, **k):
            return {
                "title": "T", "topic": "T",
                "sections": [
                    {"id": "s0", "heading": "Hook", "text": "Watch this."},
                    {"id": "s1", "heading": "Body", "text": "The point."},
                    {"id": "s2", "heading": "CTA", "text": "Subscribe."},
                ],
                "broll_moods": ["neon city"],
            }

    p = tmp_path / "input.txt"
    p.write_text("anything", encoding="utf-8")
    script = harness_inputs.script_from_text(p, _PlanClient())
    assert script["source"] == "llm_plan"
    assert script["sections"][0]["heading"] == "Hook"


def test_llm_plan_failure_falls_back_to_heuristic(tmp_path):
    class _Broken:
        def chat_json(self, *a, **k):
            raise RuntimeError("nope")

    p = tmp_path / "input.txt"
    p.write_text("Some text here.", encoding="utf-8")
    script = harness_inputs.script_from_text(p, _Broken())
    assert script["source"] == "heuristic"


def test_idea_without_llm_raises_with_guidance():
    with pytest.raises(harness_inputs.InputError, match="LLM"):
        harness_inputs.script_from_idea("a video about courage", None)


# ---------------------------------------------------------------------------
# Voiceover-first: transcript grouping
# ---------------------------------------------------------------------------

WORDS = []
_t = 0.0
for sentence in ["Alpha one two.", "Beta three four.", "Gamma five six.",
                 "Delta seven.", "Epsilon eight nine."]:
    for w in sentence.rstrip(".").split():
        WORDS.append({"word": w, "start": _t, "end": _t + 0.4})
        _t += 0.45
    WORDS[-1]["word"] += "."


def test_transcript_groups_into_sections(tmp_path):
    derived = harness_inputs.script_from_transcript(WORDS, Path("/x.mp3"), None, tmp_path)
    script = derived["script"]
    assert script["source"] == "voiceover_transcript"
    assert 2 <= len(script["sections"]) <= 6
    # cut windows are ordered and cover the transcript
    cuts = derived["section_cuts"]
    assert cuts[0]["start"] == pytest.approx(0.0)
    assert all(cuts[i]["end"] <= cuts[i + 1]["start"] + 0.01 for i in range(len(cuts) - 1))


def test_transcript_llm_titles_applied(tmp_path):
    class _Titles:
        def chat_json(self, *a, **k):
            return {"headings": ["The Opening", "The Middle", "The Close"]}

    derived = harness_inputs.script_from_transcript(WORDS, Path("/x.mp3"), _Titles(), tmp_path)
    headings = [s["heading"] for s in derived["script"]["sections"]]
    assert "The Opening" in headings


def test_transcript_llm_title_failure_is_fine(tmp_path):
    class _Broken:
        def chat_json(self, *a, **k):
            raise RuntimeError("down")

    derived = harness_inputs.script_from_transcript(WORDS, Path("/x.mp3"), _Broken(), tmp_path)
    assert derived["script"]["sections"][0]["heading"]


# ---------------------------------------------------------------------------
# Runner state machine (no render)
# ---------------------------------------------------------------------------


def test_state_roundtrip(tmp_path, monkeypatch):
    from lib import paths

    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path)
    from harness import runner

    state = {"project_id": "p1", "completed": [], "data": {}}
    runner._save_state(state)
    loaded = runner._load_state("p1")
    assert loaded["project_id"] == "p1"
    runner._mark(loaded, "input", script_source="user_script")
    assert runner._load_state("p1")["completed"] == ["input"]


def test_new_project_id_is_unique(tmp_path, monkeypatch):
    from lib import paths

    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path)
    from harness import runner

    first = runner._new_project_id("my video")
    (tmp_path / first).mkdir(parents=True)
    second = runner._new_project_id("my video")
    assert first != second


def test_approval_decision_recorded(tmp_path, monkeypatch):
    from lib import paths

    monkeypatch.setattr(paths, "PROJECTS_DIR", tmp_path)
    from harness import runner

    runner._record_approval("proj", "auto")
    log = json.loads((tmp_path / "proj" / "decision_log.json").read_text())
    assert log[-1]["category"] == "approval_policy"
    assert log[-1]["value"] == "auto"


# ---------------------------------------------------------------------------
# Review tripwires (deterministic layer, fake artifacts)
# ---------------------------------------------------------------------------


def test_review_fails_on_missing_final(tmp_path):
    from harness.review import review_project

    verdict = review_project(
        project_id="rev", out_dir=tmp_path, name="x",
        script={"sections": []}, llm=None, final=tmp_path / "ghost.mp4",
    )
    assert verdict["pass"] is False


def test_review_checks_qc_word_coverage(tmp_path):
    from harness.review import review_project

    (tmp_path / "x-qc.json").write_text(json.dumps({
        "expected_duration_s": 10.0, "word_coverage_pct": 50.0}))
    verdict = review_project(
        project_id="rev", out_dir=tmp_path, name="x",
        script={"sections": []}, llm=None, final=tmp_path / "ghost.mp4",
    )
    assert any(c["check"] == "word_coverage" and not c["ok"] for c in verdict["checks"])
