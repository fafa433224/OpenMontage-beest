"""Tests for lib/code2motion.py — the LLM-authored motion engine.

The contract under test: the validation ladder never lets bad code
through, and generation failure always degrades to the template (never
to a failed render).
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.brand_profile import load_brand  # noqa: E402
from lib.code2motion import (  # noqa: E402
    fill_hyperframes_template,
    generate_hyperframes_card,
    lint_hyperframes_html,
)

TEMPLATES = REPO_ROOT / "scripts" / "kaggle" / "hyperframes"


@pytest.fixture(scope="module")
def brand():
    return load_brand("default")


@pytest.fixture(scope="module")
def filled_intro(brand):
    raw = (TEMPLATES / "intro" / "index.html").read_text(encoding="utf-8")
    return fill_hyperframes_template(raw, brand, "intro")


# ---------------------------------------------------------------------------
# Template fill
# ---------------------------------------------------------------------------


def test_fill_replaces_every_token(filled_intro):
    assert "{{" not in filled_intro, "unfilled tokens remain"


def test_fill_default_reproduces_original_values(brand):
    raw = (TEMPLATES / "intro" / "index.html").read_text(encoding="utf-8")
    filled = fill_hyperframes_template(raw, brand, "intro")
    assert 'data-duration="5.4"' in filled
    assert brand.intro_config["wordmark"] in filled
    assert brand.intro_config["kicker"] in filled


def test_fill_scales_sfx_positions(brand):
    raw = (TEMPLATES / "intro" / "index.html").read_text(encoding="utf-8")
    filled = fill_hyperframes_template(raw, brand, "intro")
    # default duration == reference → unscaled positions
    assert 'data-start="0.15"' in filled
    assert 'data-start="4.45"' in filled


def test_fill_custom_duration(brand):
    fast = load_brand("tech-deep")
    raw = (TEMPLATES / "intro" / "index.html").read_text(encoding="utf-8")
    filled = fill_hyperframes_template(raw, fast, "intro")
    assert f'data-duration="{fast.intro_config["duration_s"]:g}"' in filled


def test_fill_rejects_bad_kind(brand):
    with pytest.raises(ValueError):
        fill_hyperframes_template("<html></html>", brand, "middle")


# ---------------------------------------------------------------------------
# Lint — clean templates
# ---------------------------------------------------------------------------


def test_all_filled_templates_lint_clean():
    for channel_id in ("default", "tech-deep", "story-loops"):
        b = load_brand(channel_id)
        for kind in ("intro", "outro"):
            raw = (TEMPLATES / kind / "index.html").read_text(encoding="utf-8")
            filled = fill_hyperframes_template(raw, b, kind)
            assert lint_hyperframes_html(filled) == [], f"{channel_id}/{kind}"


# ---------------------------------------------------------------------------
# Lint — catches real violations
# ---------------------------------------------------------------------------


GOOD_CARD = """<!DOCTYPE html><html><head>
<style>[data-composition-id="root"]{width:1920px;height:1080px}</style>
<script src="assets/gsap.min.js"></script></head><body>
<div data-composition-id="root" data-start="0" data-duration="4" data-width="1920" data-height="1080" data-fps="30">
  <div id="scene" class="clip" data-start="0" data-duration="4" data-track-index="0">
    <div class="card">Hello</div>
  </div>
  <audio id="sfx-whoosh" data-start="3" data-duration="0.6" data-track-index="3" src="assets/whoosh.wav" data-volume="0.4"></audio>
  <script>
    window.__timelines = window.__timelines || {};
    const tl = gsap.timeline({ paused: true });
    tl.fromTo(".card", {y: 40, opacity: 0}, {y: 0, opacity: 1, duration: 0.8}, 0);
    window.__timelines["root"] = tl;
  </script>
</div></body></html>"""


def test_good_card_passes():
    assert lint_hyperframes_html(GOOD_CARD) == []


@pytest.mark.parametrize(
    "mutate,expect_rule",
    [
        # CDN gsap → offline violation
        (lambda h: h.replace('src="assets/gsap.min.js"', 'src="https://cdn.example/gsap.js"'),
         "gsap_local"),
        # unregistered timeline
        (lambda h: h.replace('window.__timelines["root"] = tl;', ""),
         "timeline_registered"),
        # unbounded repeat
        (lambda h: h.replace("duration: 0.8}, 0);", "duration: 0.8, repeat: -1}, 0);"),
         "bounded_repeats"),
        # random values break parallel determinism
        (lambda h: h.replace("y: 40", "y: gsap.utils.random(10, 80)"),
         "determinism"),
        (lambda h: h.replace("opacity: 0}", "opacity: Math.random()}"),
         "determinism"),
        # layout property animation snaps under frame capture
        (lambda h: h.replace("{y: 40, opacity: 0}", "{letterSpacing: '2px'}"),
         "transform_only"),
        # remote asset
        (lambda h: h.replace('src="assets/whoosh.wav"', 'src="https://x.example/w.wav"'),
         "offline_assets"),
        # forbidden API
        (lambda h: h.replace("const tl =", "fetch('http://x'); const tl ="),
         "forbidden_api"),
        # multiple roots
        (lambda h: h.replace("<body>", '<body><div data-composition-id="root"></div>'),
         "root_composition"),
        # audio without id
        (lambda h: h.replace('id="sfx-whoosh" ', ""),
         "audio_ids"),
    ],
)
def test_lint_catches_violations(mutate, expect_rule):
    html = mutate(GOOD_CARD)
    violations = lint_hyperframes_html(html)
    assert any(v.startswith(expect_rule) for v in violations), (
        f"{expect_rule} not caught; got {violations}"
    )


def test_lint_ignores_comment_mentions():
    """HTML comments that DOCUMENT the forbidden APIs must not trip rules."""
    documented = GOOD_CARD.replace(
        "<!DOCTYPE html>",
        "<!DOCTYPE html>\n<!-- authoring rules: no gsap.utils.random(), no repeat: -1 -->",
    )
    assert lint_hyperframes_html(documented) == []


# ---------------------------------------------------------------------------
# Generation ladder — fallback behavior without an LLM
# ---------------------------------------------------------------------------


def test_generation_without_llm_returns_fallback(brand):
    script = {"topic": "t", "sections": [{"id": "s0", "heading": "H", "text": "Text."}]}
    result = generate_hyperframes_card("intro", brand, script, None)
    assert result["html"] is None
    assert result["provenance"]["reason"] == "no llm client"


class _FailingClient:
    """LLM client whose chat() always raises — outage simulation."""

    model = "fake-model"

    def chat(self, *a, **k):
        raise RuntimeError("backend down")


def test_generation_llm_outage_falls_back(brand):
    script = {"topic": "t", "sections": [{"id": "s0", "heading": "H", "text": "Text."}]}
    result = generate_hyperframes_card("outro", brand, script, _FailingClient())
    assert result["html"] is None
    assert "llm error" in result["provenance"]["reason"]


class _BrokenCardClient:
    """LLM that returns contract-violating HTML every round."""

    model = "bad-model"

    def chat(self, *a, **k):
        return "<html><body><script>tl.to('.x', {top: 10, repeat: -1});</script></body></html>"


def test_generation_repair_loop_ends_in_fallback(brand):
    script = {"topic": "t", "sections": [{"id": "s0", "heading": "H", "text": "Text."}]}
    result = generate_hyperframes_card("intro", brand, script, _BrokenCardClient(),
                                       max_repairs=1)
    assert result["html"] is None
    assert result["provenance"]["status"] == "fallback_after_repairs"
    assert result["provenance"]["attempts"] == 2  # 1 + 1 repair


class _GoodCardClient:
    """LLM that returns a contract-clean card on the first try."""

    model = "good-model"

    def chat(self, *a, **k):
        return GOOD_CARD


def test_generation_clean_card_accepted(brand):
    script = {"topic": "t", "sections": [{"id": "s0", "heading": "H", "text": "Text."}]}
    result = generate_hyperframes_card("intro", brand, script, _GoodCardClient())
    assert result["html"] is not None
    assert result["provenance"]["status"] == "generated_clean"
    assert lint_hyperframes_html(result["html"]) == []


def test_generation_extracts_html_from_fences(brand):
    class _Fenced:
        model = "fenced"

        def chat(self, *a, **k):
            return f"Sure! Here is the card:\n```html\n{GOOD_CARD}\n```\nHope it helps."

    script = {"topic": "t", "sections": [{"id": "s0", "heading": "H", "text": "Text."}]}
    result = generate_hyperframes_card("intro", brand, script, _Fenced())
    assert result["html"] is not None
    assert result["html"].startswith("<!DOCTYPE html>")
