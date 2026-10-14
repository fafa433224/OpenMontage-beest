"""Regression tests for the motion-design v2 creative pipeline:

  * scripts/kaggle/build_explainer_props.py — creative direction emission
    (per-scene backgrounds, transitions, SFX cue sheet, b-roll assignment)
  * scripts/kaggle/synth_audio.py — SFX/BGM synthesis invariants
  * scripts/kaggle/synth_broll.py — clip naming/layout contract
  * scripts/kaggle/render_voiceover_video.py — Node version gate

These scripts live outside the tools/ package (they are the Kaggle production
harness), so the tests import them by path, mirroring how
render_voiceover_video.py itself imports build_explainer_props.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
KAGGLE = REPO / "scripts" / "kaggle"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, KAGGLE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def props_mod():
    return _load("build_explainer_props")


@pytest.fixture(scope="module")
def built_props(props_mod):
    script = {
        "sections": [
            {"id": f"ts{i}", "heading": f"Heading {i}", "text": (
                "One two three sentence with words. " * 3).strip()}
            for i in range(3)
        ]
    }
    return props_mod.build_props(script, [10.0, 10.0, 10.0], words=None)


# ---------------------------------------------------------------------------
# build_explainer_props v2
# ---------------------------------------------------------------------------

class TestCreativeDirection:
    def test_every_cut_carries_section_metadata(self, built_props):
        for c in built_props["cuts"]:
            assert "sectionIndex" in c and "sectionCount" in c
            assert 0 <= c["sectionIndex"] < c["sectionCount"] == 3

    def test_every_cut_has_transition(self, built_props):
        from build_explainer_props import TRANSITIONS
        for c in built_props["cuts"]:
            assert c.get("transition") in TRANSITIONS

    def test_non_broll_cuts_have_bgstyle_without_immediate_repeat(self, built_props):
        from build_explainer_props import BG_STYLES
        prev = None
        for c in built_props["cuts"]:
            if c.get("backgroundVideo"):
                prev = None  # b-roll scenes skip the animated background
                continue
            assert c.get("bgStyle") in BG_STYLES
            assert c["bgStyle"] != prev, f"back-to-back bgStyle {c['bgStyle']}"
            prev = c["bgStyle"]

    def test_heroes_get_stock_footage(self, built_props):
        """Heroes keep their b-roll whether they render stock or compiled.

        A compiled hero carries the footage inside its own spec (`media`), so
        the assertion has to follow the role, not the component name.
        """
        from build_explainer_props import cut_role

        heroes = [c for c in built_props["cuts"] if cut_role(c) == "hero_title"]
        assert len(heroes) == 3
        for h in heroes:
            if h["type"] == "bespoke":
                media = h["bespoke"].get("media")
                assert media, "compiled hero must carry its footage"
                assert media["src"].startswith("broll/")
                assert 0 <= media["startSeconds"] < 8
                assert 0.3 <= media["overlay"] <= 0.6
            else:
                assert h["backgroundVideo"].startswith("broll/")
                assert 0 <= h["backgroundVideoStart"] < 8
                assert 0.3 <= h["backgroundOverlay"] <= 0.6

    def test_broll_pool_rotation_uses_many_clips(self, built_props):
        """Footage rotates across the pool, whatever component renders it.

        Reads the source through the same door the renderer does: a compiled
        cut owns its footage in `bespoke.media`, a template cut in
        `backgroundVideo`. Asking only for the template key reported zero
        clips used and looked like a rotation bug when it was a lookup bug.
        """

        def footage_source(cut):
            media = (cut.get("bespoke") or {}).get("media")
            return (media or {}).get("src") or cut.get("backgroundVideo")

        used = {src for src in (footage_source(c) for c in built_props["cuts"]) if src}
        assert len(used) >= 3, f"b-roll should rotate, not repeat one clip (got {used})"

    def test_text_cards_get_alignment_and_keywords(self, props_mod):
        """The template path still applies alignment + keyword highlighting.

        Since the body-scene code2motion change, `build_props` compiles
        bespoke choreography by default and `text_card` survives as the
        ladder's fallback rather than the finished scene. The alignment /
        accentWords contract therefore still has to hold for callers that
        pin `motion_mode="template"`.
        """
        from build_explainer_props import ALIGNS, STOPWORDS
        script = {
            "sections": [
                {"id": f"ts{i}", "heading": f"Heading {i}", "text": (
                    "One two three sentence with words. " * 3).strip()}
                for i in range(3)
            ]
        }
        props = props_mod.build_props(script, [10.0, 10.0, 10.0], words=None,
                                      motion_mode="template")
        cards = [c for c in props["cuts"] if c["type"] == "text_card"]
        assert cards
        aligns = {c.get("align") for c in cards}
        assert aligns <= set(ALIGNS)
        for c in cards:
            for w in c.get("accentWords", []):
                assert w.lower() not in STOPWORDS

    def test_default_mode_compiles_bespoke_body_scenes(self, built_props):
        """The default path must NOT emit stock component scenes.

        This is the anti-template guarantee at the props layer: every
        non-hero cut is a compiled choreography spec, each carrying a valid
        one, and the plan records how many compiled.
        """
        bespoke = [c for c in built_props["cuts"] if c["type"] == "bespoke"]
        assert bespoke, "default motion_mode should compile body scenes"
        for c in bespoke:
            spec = c["bespoke"]
            assert spec["scheme"]
            assert spec["layers"]
            assert spec["version"] == 1
            # The ladder's last rung must always be reachable.
            assert c.get("fallbackType")

    def test_motion_plan_records_the_creative_decisions(self, built_props):
        plan = built_props.get("motionPlan")
        assert plan, "props must carry a motionPlan decision record"
        assert plan["compiled"] >= 1
        assert plan["distinctSchemes"] >= 1
        assert len(plan["schemes"]) == plan["compiled"]

    def test_props_root_fields(self, built_props):
        assert built_props["sectionCount"] == 3
        assert built_props["audio"]["music"]["src"] == "audio/bgm.mp3"
        assert built_props["audio"]["music"]["loop"] is True
        assert 0 < built_props["audio"]["music"]["volume"] <= 0.25
        assert built_props["durationSeconds"] == 30.0


class TestSfxCueSheet:
    def test_whoosh_on_every_cut_boundary_after_first(self, built_props):
        cues = built_props["sfx"]
        whooshes = [s for s in cues if s["src"] == "audio/whoosh.wav"]
        cut_starts = {round(c["in_seconds"], 1) for c in built_props["cuts"][1:]}
        cue_times = {round(s["atSeconds"] + 0.12, 1) for s in whooshes}
        assert cut_starts <= cue_times

    def test_no_negative_cue_times(self, built_props):
        for s in built_props["sfx"]:
            assert s["atSeconds"] >= 0

    def test_heroes_get_impact_and_interior_sections_get_riser(self, built_props):
        from build_explainer_props import cut_role

        cues = built_props["sfx"]
        impacts = [s for s in cues if s["src"] == "audio/impact.wav"]
        risers = [s for s in cues if s["src"] == "audio/riser.wav"]
        heroes = [c for c in built_props["cuts"] if cut_role(c) == "hero_title"]
        assert len(impacts) == len(heroes)
        assert len(risers) == len(heroes) - 1  # no riser before the first

    def test_bespoke_heroes_still_get_impact_and_riser(self, props_mod):
        """Regression: compiling heroes silently dropped their stings.

        The cue sheet keyed on `cut["type"]`, which becomes "bespoke" once a
        hero is compiled — so the impact and the riser vanished from every
        branded video. Role-based lookup is the fix.
        """
        from build_explainer_props import cut_role

        script = {"sections": [
            {"id": f"x{i}", "heading": f"H{i}", "text": "A sentence here. " * 4}
            for i in range(3)
        ]}
        props = props_mod.build_props(script, [10.0, 10.0, 10.0], words=None)
        heroes = [c for c in props["cuts"] if cut_role(c) == "hero_title"]
        assert heroes, "heroes should exist"
        assert all(c["type"] == "bespoke" for c in heroes)
        impacts = [s for s in props["sfx"] if s["src"] == "audio/impact.wav"]
        assert len(impacts) == len(heroes)

    def test_progress_bars_get_chime_at_midpoint(self, built_props):
        for c in built_props["cuts"]:
            if c["type"] == "progress_bar":
                mid = round((c["in_seconds"] + c["out_seconds"]) / 2, 3)
                assert any(
                    s["src"] == "audio/chime.wav" and abs(s["atSeconds"] - mid) < 0.01
                    for s in built_props["sfx"]
                )

    def test_volumes_within_bounds(self, built_props):
        for s in built_props["sfx"]:
            assert 0 < s["volume"] <= 0.6


# ---------------------------------------------------------------------------
# synth_audio
# ---------------------------------------------------------------------------

class TestSynthAudio:
    @pytest.fixture(scope="class")
    def audio_mod(self):
        return _load("synth_audio")

    def test_sfx_durations_and_finiteness(self, audio_mod):
        import numpy as np
        for fn, dur in (
            (audio_mod.synth_whoosh, 0.5),
            (audio_mod.synth_impact, 0.7),
            (audio_mod.synth_riser, 1.5),
            (audio_mod.synth_chime, 2.0),
            (audio_mod.synth_pop, 0.16),
        ):
            sig = fn()
            assert abs(len(sig) / audio_mod.SR - dur) < 0.01
            assert np.isfinite(sig).all()
            assert np.abs(sig).max() > 0.05  # not silence
        assert np.abs(audio_mod.synth_whoosh()).max() <= 12.0  # pre-normalization bound

    def test_bgm_shape_and_stereo(self, audio_mod):
        import numpy as np
        st = audio_mod.synth_bgm(seconds=6.0)
        assert st.shape == (int(6.0 * audio_mod.SR), 2)
        assert abs(np.abs(st).max() - 0.5) < 0.02  # normalized peak


# ---------------------------------------------------------------------------
# synth_broll
# ---------------------------------------------------------------------------

class TestSynthBroll:
    @pytest.fixture(scope="class")
    def broll_mod(self):
        return _load("synth_broll")

    def test_all_styles_registered(self, broll_mod):
        assert set(broll_mod.STYLES) == {
            "mesh-flow", "bokeh", "light-leak", "particles", "wave-ribbons", "aurora"
        }

    def test_frame_generators_yield_valid_frames(self, broll_mod):
        import numpy as np
        for name, gen in broll_mod.STYLES.items():
            frames = gen(broll_mod.COOL, 3)
            for f in frames:
                assert f.shape == (broll_mod.H, broll_mod.W, 3), name
                assert f.dtype == np.uint8, name
                assert f.mean() > 2, f"{name} frame looks empty"

    @pytest.mark.skipif(
        not (KAGGLE / "synth_broll.py").exists(), reason="synth_broll not present"
    )
    def test_encode_writes_h264_with_dense_keyframes(self, broll_mod, tmp_path):
        import subprocess
        out = tmp_path / "test-clip.mp4"
        encode = broll_mod.encode_clip(broll_mod.STYLES["mesh-flow"](broll_mod.COOL, 15), out, 0)
        assert out.exists() and out.stat().st_size > 1000
        gop = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v", "-show_entries",
             "stream=codec_name,width,height", "-of", "csv=p=0", str(out)],
            capture_output=True, text=True).stdout.strip()
        assert gop.startswith("h264")
        assert "1920" in gop and "1080" in gop


# ---------------------------------------------------------------------------
# render_voiceover_video — Node gate
# ---------------------------------------------------------------------------

class TestNodeGate:
    def test_node_major_parses_versions(self, monkeypatch):
        mod = _load("render_voiceover_video")
        import subprocess as sp

        class FakeRun:
            def __init__(self, out):
                self.stdout = out

        for ver, expect in (("v22.11.0", 22), ("v20.19.0", 20), ("v10.0.0", 10)):
            monkeypatch.setattr(sp, "run", lambda *a, **k: FakeRun(ver))
            assert mod.node_major() == expect

    def test_node_major_returns_zero_when_missing(self, monkeypatch):
        mod = _load("render_voiceover_video")
        import subprocess as sp

        def boom(*a, **k):
            raise FileNotFoundError("no node")

        monkeypatch.setattr(sp, "run", boom)
        assert mod.node_major() == 0


# ---------------------------------------------------------------------------
# HyperFrames templates — lint-critical invariants
# ---------------------------------------------------------------------------

class TestHyperframesTemplates:
    @pytest.mark.parametrize("tpl", ["intro", "outro"])
    def test_contract_invariants(self, tpl):
        html = (KAGGLE / "hyperframes" / tpl / "index.html").read_text()
        # Root composition attributes
        assert 'data-composition-id="root"' in html
        assert 'data-width="1920"' in html and 'data-height="1080"' in html
        # Timeline registration
        assert 'window.__timelines = window.__timelines || {}' in html
        assert 'window.__timelines["root"] = tl' in html
        assert "gsap.timeline({ paused: true })" in html
        # Media elements must have ids (else silent in renders)
        import re
        for m in re.finditer(r"<audio\b[^>]*>", html):
            assert 'id="' in m.group(0), f"audio without id in {tpl}"
        # Determinism: no gsap random CALLS (comments may mention the rule)
        import re as _re
        html_no_comments = _re.sub(r"<!--.*?-->", "", html, flags=_re.S)
        assert "gsap.utils.random(" not in html_no_comments
        # Bounded repeats only
        assert "repeat: -1" not in html
        # Vendored GSAP (no CDN dependency)
        assert 'src="assets/gsap.min.js"' in html
        assert "cdn.jsdelivr" not in html and "unpkg" not in html

    def test_vendored_gsap_present(self):
        p = KAGGLE / "hyperframes" / "assets" / "gsap.min.js"
        assert p.exists() and p.stat().st_size > 50000
