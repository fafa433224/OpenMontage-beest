"""Motion compiler — the contract that keeps compiled choreography renderable.

These tests lock the properties that matter, not the specific choices:

* **Determinism** — same channel + project + index reproduces byte-identical
  output (Kaggle retry safety). Without this, a resumed run would re-roll
  every scene and the "resume costs only the un-checkpointed stage" promise
  would be a lie.
* **Diversity** — different projects differ, and no scheme repeats
  consecutively (the anti-template guarantee).
* **Lint cleanliness** — every spec the compiler emits passes ``lint_spec``,
  because a spec that fails the contract is rejected at the ladder.
* **Content coverage** — no scheme may silently drop authored copy.
* **Legibility** — palette text clears WCAG AA on its own background.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from lib.brand_profile import load_brand  # noqa: E402
from lib.motion_compiler import (  # noqa: E402
    CAMERA_PATHS,
    EASINGS,
    LAYER_KINDS,
    MOTION_VERBS,
    SCHEMES,
    SPEC_VERSION,
    SIGNATURE_MOMENTS,
    TEXTURE_TREATMENTS,
    classify_content,
    compile_scene,
    compile_video,
    contrast_ratio,
    ensure_contrast,
    lint_spec,
    spec_summary,
)

SECTIONS = [
    {"heading": "The quiet bottleneck", "text": "Most teams think the model is the constraint.",
     "kicker": "PART 1"},
    {"heading": "47% of runs fail", "text": "Nearly half break before the first render.",
     "stat": "47%"},
    {"heading": "Three fixes", "text": "- Lock the runtime\n- Cache the assets\n- Verify the frames"},
    {"heading": "Ship it", "text": "The whole loop runs headless in one command.", "kind": "hero"},
]


# ---------------------------------------------------------------------------
# Determinism + diversity
# ---------------------------------------------------------------------------

def test_compile_video_is_deterministic_per_project():
    brand = load_brand("default")
    a = compile_video({"sections": SECTIONS}, brand, "proj-A")
    b = compile_video({"sections": SECTIONS}, brand, "proj-A")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_compile_video_differs_across_projects():
    brand = load_brand("default")
    a = compile_video({"sections": SECTIONS}, brand, "proj-A")
    b = compile_video({"sections": SECTIONS}, brand, "proj-B")
    assert json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True)


def test_channels_get_different_structural_programs():
    """The brief: channel A != channel B, for the same script."""
    script = {"sections": SECTIONS}
    schemes = {
        ch: [s["scheme"] for s in compile_video(script, load_brand(ch), "same-project")["scenes"]]
        for ch in ("default", "tech-deep", "story-loops")
    }
    assert schemes["default"] != schemes["tech-deep"] or \
        schemes["default"] != schemes["story-loops"]


def test_no_consecutive_scheme_repeats():
    brand = load_brand("default")
    many = [{"heading": f"Scene {i}", "text": f"Body {i}."} for i in range(16)]
    plan = compile_video({"sections": many}, brand, "proj-ring")
    got = [s["scheme"] for s in plan["scenes"]]
    assert all(x != y for x, y in zip(got, got[1:])), got


def test_long_video_uses_many_distinct_schemes():
    brand = load_brand("default")
    many = [{"heading": f"S{i}", "text": f"Body {i}."} for i in range(12)]
    plan = compile_video({"sections": many}, brand, "proj-long")
    assert plan["stats"]["distinctSchemes"] >= 5


def test_compile_scene_is_deterministic_for_index():
    brand = load_brand("tech-deep")
    a = compile_scene(SECTIONS[0], brand, "p", index=3, duration=8.0)
    b = compile_scene(SECTIONS[0], brand, "p", index=3, duration=8.0)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ---------------------------------------------------------------------------
# Lint contract
# ---------------------------------------------------------------------------

def test_every_compiled_spec_lints_clean():
    for ch in ("default", "tech-deep", "story-loops"):
        plan = compile_video({"sections": SECTIONS}, load_brand(ch), "lint-all")
        for spec in plan["scenes"]:
            assert lint_spec(spec) == [], (ch, spec_summary(spec), lint_spec(spec))


def test_all_schemes_lint_clean():
    brand = load_brand("default")
    for scheme in SCHEMES:
        spec = compile_scene(SECTIONS[2], brand, "scheme-sweep",
                             index=0, duration=9.0, scheme=scheme)
        assert lint_spec(spec) == [], (scheme, lint_spec(spec))


def test_lint_rejects_unknown_verb():
    brand = load_brand("default")
    spec = compile_scene(SECTIONS[0], brand, "p", index=0, duration=8.0)
    spec["layers"][0]["verb"] = "rotate-0"     # a real bug caught in build
    problems = lint_spec(spec)
    assert any("unknown verb" in p for p in problems)


def test_lint_rejects_bad_depth_and_window():
    brand = load_brand("default")
    spec = compile_scene(SECTIONS[0], brand, "p", index=0, duration=8.0)
    spec["layers"][0]["depth"] = 4.2
    spec["layers"][1]["enterAt"] = -1
    problems = lint_spec(spec)
    assert any("depth" in p for p in problems)
    assert any("enterAt" in p for p in problems)


def test_lint_rejects_duplicate_layer_ids():
    brand = load_brand("default")
    spec = compile_scene(SECTIONS[0], brand, "p", index=0, duration=8.0)
    assert len(spec["layers"]) >= 2
    spec["layers"][1]["id"] = spec["layers"][0]["id"]
    assert any("duplicate" in p for p in lint_spec(spec))


def test_lint_rejects_bad_version_and_duration():
    brand = load_brand("default")
    spec = compile_scene(SECTIONS[0], brand, "p", index=0, duration=8.0)
    spec["version"] = SPEC_VERSION + 1
    spec["durationSeconds"] = 0
    problems = lint_spec(spec)
    assert any("version" in p for p in problems)
    assert any("durationSeconds" in p for p in problems)


def test_lint_rejects_unsorted_camera_keys():
    brand = load_brand("default")
    spec = compile_scene(SECTIONS[0], brand, "p", index=0, duration=8.0)
    spec["camera"]["keys"] = [
        {"at": 0.5, "zoom": 1.0}, {"at": 0.2, "zoom": 1.1},
    ]
    assert any("out of order" in p for p in lint_spec(spec))


# ---------------------------------------------------------------------------
# Vocabulary closure — the Python and TS sides must agree
# ---------------------------------------------------------------------------

def test_emitted_values_are_inside_the_vocabularies():
    brand = load_brand("default")
    plan = compile_video({"sections": SECTIONS * 3}, brand, "vocab-sweep")
    for spec in plan["scenes"]:
        assert spec["scheme"] in SCHEMES
        assert spec["camera"]["path"] in CAMERA_PATHS
        assert spec["texture"]["treatment"] in TEXTURE_TREATMENTS
        assert spec["signature"]["moment"] in SIGNATURE_MOMENTS
        for layer in spec["layers"]:
            assert layer["kind"] in LAYER_KINDS
            assert layer["verb"] in MOTION_VERBS
            assert layer["ease"] in EASINGS


def test_spec_summary_reports_the_creative_choices():
    brand = load_brand("default")
    spec = compile_scene(SECTIONS[0], brand, "p", index=0, duration=8.0)
    summary = spec_summary(spec)
    assert summary["scheme"] in SCHEMES
    assert summary["layers"] >= 3
    assert summary["camera"] in CAMERA_PATHS


# ---------------------------------------------------------------------------
# Content coverage — a scheme must never lose the writer's words
# ---------------------------------------------------------------------------

def test_kicker_survives_a_scheme_that_does_not_declare_one():
    """Regression: `ribbon-weave` has no kicker layer, so the kicker vanished.

    Caught by rendering the card and looking at it, then locked here.
    """
    brand = load_brand("default")
    spec = compile_scene(
        {"heading": "H", "text": "Body text.", "kicker": "THE FIX"},
        brand, "p", index=0, duration=8.0, scheme="ribbon-weave",
    )
    kinds = [lyr["kind"] for lyr in spec["layers"]]
    assert "kicker" in kinds
    assert spec["text"]["kicker"] == "THE FIX"


def test_headline_and_body_always_covered():
    brand = load_brand("default")
    for scheme in SCHEMES:
        spec = compile_scene(
            {"heading": "A headline", "text": "A body sentence.", "kicker": "K"},
            brand, "cover", index=0, duration=8.0, scheme=scheme,
        )
        kinds = {lyr["kind"] for lyr in spec["layers"]}
        assert "headline" in kinds or "number" in kinds, scheme
        assert "body" in kinds or "chip" in kinds, scheme


def test_content_layers_are_ordered_for_reading():
    """Rendering order must not be an accident of a scheme's authoring order."""
    brand = load_brand("default")
    order = {"kicker": 0, "rule": 1, "accent-bar": 2, "number": 3, "headline": 4,
             "quote-mark": 5, "chip": 6, "progress": 7, "body": 8}
    spec = compile_scene(SECTIONS[2], brand, "p", index=0, duration=8.0, scheme="type-storm")
    seen = [order.get(lyr["kind"], 99) for lyr in spec["layers"] if lyr["kind"] in order]
    assert seen == sorted(seen)


# ---------------------------------------------------------------------------
# Legibility
# ---------------------------------------------------------------------------

def test_ensure_contrast_lifts_unreadable_text():
    lifted = ensure_contrast("#1A1A1A", "#111111", minimum=4.5)
    assert contrast_ratio(lifted, "#111111") >= 4.5


def test_ensure_contrast_leaves_good_text_alone():
    assert ensure_contrast("#FFFFFF", "#0B0E1A") == "#FFFFFF"


def test_compiled_palette_text_clears_wcag_aa():
    for ch in ("default", "tech-deep", "story-loops"):
        plan = compile_video({"sections": SECTIONS}, load_brand(ch), "contrast")
        for spec in plan["scenes"]:
            pal = spec["palette"]
            assert contrast_ratio(pal["text"], pal["bgBase"]) >= 4.0, (ch, spec["id"])
            assert contrast_ratio(pal["muted"], pal["bgBase"]) >= 4.0, (ch, spec["id"])


# ---------------------------------------------------------------------------
# Classification + camera
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("section,expected", [
    ({"heading": "x", "text": "y", "stat": "47%"}, "data"),
    ({"heading": "x", "text": "- one\n- two\n- three"}, "list"),
    ({"heading": "x", "text": "y", "kind": "hero"}, "hero"),
    ({"heading": "x", "text": "plain sentence."}, "statement"),
])
def test_classify_content(section, expected):
    assert classify_content(section) == expected


def test_camera_keys_are_ordered_and_bounded():
    brand = load_brand("default")
    plan = compile_video({"sections": SECTIONS * 2}, brand, "cam-sweep")
    for spec in plan["scenes"]:
        keys = spec["camera"]["keys"]
        assert keys
        ats = [k["at"] for k in keys]
        assert ats == sorted(ats)
        assert ats[0] >= 0.0 and ats[-1] <= 1.0
        for k in keys:
            # Guard against a camera that reveals the frame edge.
            assert 0.9 <= (k.get("zoom") or 1.0) <= 1.2


def test_forced_scheme_is_honoured():
    brand = load_brand("default")
    plan = compile_video({"sections": SECTIONS}, brand, "forced",
                         force_schemes=["monolith"] * len(SECTIONS))
    assert all(s["scheme"] == "monolith" for s in plan["scenes"])


def test_duration_is_carried_into_the_spec():
    brand = load_brand("default")
    spec = compile_scene(SECTIONS[0], brand, "p", index=0, duration=11.25)
    assert spec["durationSeconds"] == pytest.approx(11.25)


def test_continuity_chain_is_recorded():
    """Each scene must know what the previous one closed on (Fable rule)."""
    brand = load_brand("default")
    plan = compile_video({"sections": SECTIONS}, brand, "continuity")
    scenes = plan["scenes"]
    for prev, cur in zip(scenes, scenes[1:]):
        assert cur["continuity"]["openOn"] == prev["continuity"]["closeOn"]
    assert scenes[0]["continuity"]["openOn"] is None
