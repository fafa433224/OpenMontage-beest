"""Tests for lib/brand_profile.py — the channel brand envelope.

The contract under test (the reason the module exists):

1. same channel + same project → identical decisions (retry-safe)
2. same channel + different projects → same anchors, different drift
3. different channels → different envelopes (distinct look)
4. brand vocabulary containment: director picks never leave the pools
5. invalid brand files fail fast with readable errors
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from lib.brand_profile import (  # noqa: E402
    BrandError,
    BrandProfile,
    list_channels,
    load_brand,
)
from lib.creative_director import (  # noqa: E402
    BG_STYLES,
    CAPTION_STYLE_PACKS,
    TRANSITION_STYLES,
    CreativeDirector,
)

CHANNELS = list_channels()


@pytest.fixture(scope="module")
def tech_deep():
    return load_brand("tech-deep")


@pytest.fixture(scope="module")
def story_loops():
    return load_brand("story-loops")


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------


def test_preset_channels_ship():
    assert set(CHANNELS) >= {"default", "tech-deep", "story-loops"}


def test_brand_summary_shape(tech_deep):
    s = tech_deep.summary()
    assert s["channel_id"] == "tech-deep"
    assert s["caption_style"] in CAPTION_STYLE_PACKS
    assert s["font_pairing"] == "mono-tech"


def test_unknown_channel_raises_readably():
    with pytest.raises(BrandError, match="not found"):
        load_brand("no-such-channel")


def test_invalid_pool_value_raises():
    with pytest.raises(BrandError, match="unknown styles"):
        BrandProfile.from_dict(
            {"identity": {"name": "x"},
             "motion": {"transition_pool": ["slide-left", "star-teleport"]}},
            "bad",
        )


def test_invalid_caption_style_raises():
    with pytest.raises(BrandError, match="caption"):
        BrandProfile.from_dict(
            {"identity": {"name": "x"}, "caption": {"style": "neon-glow"}}, "bad"
        )


# ---------------------------------------------------------------------------
# Seeds: determinism + channel binding
# ---------------------------------------------------------------------------


def test_project_seed_binds_channel(tech_deep, story_loops):
    assert tech_deep.project_seed("v1") == "tech-deep:v1"
    assert story_loops.project_seed("v1") == "story-loops:v1"
    assert tech_deep.project_seed("v1") != story_loops.project_seed("v1")


def test_same_channel_same_project_deterministic(tech_deep):
    rng_a = tech_deep.project_rng("video-9")
    rng_b = tech_deep.project_rng("video-9")
    assert [rng_a.random() for _ in range(20)] == [rng_b.random() for _ in range(20)]


def test_same_channel_different_project_varies(tech_deep):
    a = [tech_deep.project_rng("video-A").random() for _ in range(10)]
    b = [tech_deep.project_rng("video-B").random() for _ in range(10)]
    assert a != b


# ---------------------------------------------------------------------------
# Director integration: anchors + drift + containment
# ---------------------------------------------------------------------------


def _cuts_for(director, n=12):
    return [
        (director.transition("text_card", i), director.bg_style(i), director.align(i))
        for i in range(n)
    ]


def test_director_brand_caption_anchor(tech_deep):
    for project in ("a", "b", "c"):
        d = CreativeDirector(seed=tech_deep.project_seed(project),
                             brand=tech_deep.director_overrides(project)["brand"])
        assert d.caption_style()["style"] == "boxed"


def test_director_transitions_stay_in_brand_pool(tech_deep):
    brand_pool = set(tech_deep.transition_pool)
    for project in ("a", "b", "c", "d"):
        d = CreativeDirector(seed=tech_deep.project_seed(project),
                             brand=tech_deep.director_overrides(project)["brand"])
        picks = [t for t, _, _ in _cuts_for(d, 16)]
        assert set(picks) <= brand_pool, f"{set(picks) - brand_pool} escaped the brand pool"


def test_director_bg_stays_in_brand_pool(tech_deep):
    pool = set(tech_deep.bg_pool) | set(tech_deep.hero_bg_pool)
    for project in ("a", "b"):
        d = CreativeDirector(seed=tech_deep.project_seed(project),
                             brand=tech_deep.director_overrides(project)["brand"])
        bgs = [b for _, b, _ in _cuts_for(d, 16)] + [d.bg_style(i, is_hero=True) for i in range(4)]
        assert set(bgs) <= pool


def test_director_deterministic_per_project(tech_deep):
    def build(project):
        d = CreativeDirector(seed=tech_deep.project_seed(project),
                             brand=tech_deep.director_overrides(project)["brand"])
        return _cuts_for(d, 16)

    assert build("same") == build("same")
    assert build("one") != build("two")  # drift across projects


def test_director_unbranded_unchanged():
    """No brand → legacy behavior: full pools, no containment error."""
    d = CreativeDirector(seed="legacy")
    for i in range(16):
        assert d.transition("text_card", i) in TRANSITION_STYLES
        assert d.bg_style(i) in BG_STYLES


def test_cross_channel_directors_differ(tech_deep, story_loops):
    d1 = CreativeDirector(seed="tech-deep:x", brand=tech_deep.director_overrides("x")["brand"])
    d2 = CreativeDirector(seed="story-loops:x", brand=story_loops.director_overrides("x")["brand"])
    assert d1.caption_style()["style"] != d2.caption_style()["style"]


# ---------------------------------------------------------------------------
# Bridges: theme + CSS vars + template fill
# ---------------------------------------------------------------------------


def test_theme_config_partial_keys(tech_deep):
    theme = tech_deep.theme_config()
    for key in ("primaryColor", "accentColor", "headingFont", "springConfig"):
        assert key in theme
    assert theme["primaryColor"] == tech_deep.primary


def test_css_vars_shape(tech_deep):
    css = tech_deep.css_vars()
    assert css["--color-primary"] == tech_deep.primary
    assert css["--font-heading"].startswith("'")


def test_fill_html_template_tokens(tech_deep):
    html = "<h1>{{WORDMARK}}</h1><p>{{TAGLINE}}</p><a>{{URL}}</a>"
    filled = tech_deep.fill_html_template(html)
    assert "{{" not in filled
    assert tech_deep.intro_config["wordmark"] in filled


def test_fill_keeps_unknown_tokens(tech_deep):
    filled = tech_deep.fill_html_template("<div>{{MY_CUSTOM_TOKEN}}</div>")
    assert "{{MY_CUSTOM_TOKEN}}" in filled


def test_hyperframes_intro_outro_config(tech_deep):
    cfg = tech_deep.hyperframes_intro_outro_config()
    assert cfg["intro"]["expected_duration_s"] == tech_deep.intro_config["duration_s"]
    assert cfg["outro"]["expected_duration_s"] == tech_deep.outro_config["duration_s"]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


def test_brand_schema_validates_all_presets():
    for channel_id in CHANNELS:
        brand = load_brand(channel_id)  # load() validates against the schema
        assert brand.channel_id == channel_id
