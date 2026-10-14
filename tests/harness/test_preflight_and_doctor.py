"""Preflight healing + harness doctor coverage.

Preflight exists because Remotion hard-fails a whole render when any audio or
video asset is missing, and it fails tens of seconds in. On a Kaggle session
that is a lost run for a missing sting. These tests lock the two behaviours:
required assets fail fast and clearly; optional assets are healed so the
render survives.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# render_voiceover_video.py lives under scripts/kaggle (a script dir, not a
# package) — load it by path, the way the harness invokes it.
_spec = importlib.util.spec_from_file_location(
    "render_voiceover_video",
    REPO_ROOT / "scripts" / "kaggle" / "render_voiceover_video.py",
)
rvv = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("render_voiceover_video", rvv)
try:
    _spec.loader.exec_module(rvv)
except SystemExit:  # pragma: no cover - module is import-safe
    pass

preflight_props = rvv.preflight_props


@pytest.fixture()
def composer(tmp_path):
    """A pretend remotion-composer with only the assets we create."""
    c = tmp_path / "remotion-composer"
    (c / "public" / "audio").mkdir(parents=True)
    return c


def _props(**over):
    props = {
        "audio": {
            "narration": {"src": "voiceover.mp3", "volume": 1.0},
            "music": {"src": "audio/bgm.mp3", "volume": 0.16},
        },
        "sfx": [
            {"src": "audio/whoosh.wav", "atSeconds": 1.0},
            {"src": "audio/impact.wav", "atSeconds": 2.0},
        ],
        "cuts": [{"id": "a", "backgroundVideo": "broll/x.mp4"}],
    }
    props.update(over)
    return props


def _touch(composer: Path, rel: str) -> None:
    p = composer / "public" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")


def test_missing_narration_is_a_hard_failure(composer):
    props = _props()
    _touch(composer, "audio/bgm.mp3")
    failures = preflight_props(props, composer)
    assert any("narration" in f for f in failures)


def test_missing_music_is_healed_not_fatal(composer):
    props = _props()
    _touch(composer, "voiceover.mp3")
    failures = preflight_props(props, composer)
    assert failures == []
    assert "music" not in props["audio"]


def test_missing_sfx_cues_are_dropped(composer):
    props = _props()
    _touch(composer, "voiceover.mp3")
    _touch(composer, "audio/whoosh.wav")
    failures = preflight_props(props, composer)
    assert failures == []
    assert [c["src"] for c in props["sfx"]] == ["audio/whoosh.wav"]


def test_missing_broll_is_dropped_from_the_cut(composer):
    props = _props()
    _touch(composer, "voiceover.mp3")
    failures = preflight_props(props, composer)
    assert failures == []
    assert "backgroundVideo" not in props["cuts"][0]


def test_all_assets_present_changes_nothing(composer):
    props = _props()
    for rel in ("voiceover.mp3", "audio/bgm.mp3", "audio/whoosh.wav",
                "audio/impact.wav", "broll/x.mp4"):
        _touch(composer, rel)
    before = repr(props)
    assert preflight_props(props, composer) == []
    assert repr(props) == before


def test_tolerates_public_prefixed_and_absolute_paths(composer):
    props = _props(
        audio={"narration": {"src": "/public/voiceover.mp3"}},
        sfx=[], cuts=[],
    )
    _touch(composer, "voiceover.mp3")
    assert preflight_props(props, composer) == []
    assert props["audio"]["narration"]["src"] == "/public/voiceover.mp3"


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def test_doctor_reports_the_new_subsystems():
    """doctor must cover the compiled-motion + audit + figma stack."""
    r = subprocess.run([sys.executable, "-m", "harness", "doctor"],
                       cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180)
    out = r.stdout
    assert r.returncode in (0, 1), r.stderr
    for label in ("motion-compiler", "bespoke-renderer", "visual-audit",
                  "vision-review", "hyperframes-cards", "channels"):
        assert label in out, f"doctor does not report {label}"


def test_doctor_exits_clean_when_the_core_stack_is_present():
    r = subprocess.run([sys.executable, "-m", "harness", "doctor"],
                       cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180)
    assert "motion-compiler" in r.stdout
    # The compiler probe is core: it must not be the thing that fails.
    line = next(l for l in r.stdout.splitlines() if "motion-compiler" in l)
    assert line.startswith("OK"), line
