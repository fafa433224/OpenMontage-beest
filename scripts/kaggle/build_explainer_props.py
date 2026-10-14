"""Build an OpenMontage Explainer props file from a script + voiceover timing.

Kaggle production path: any LLM (or a human) writes script.json (sections
with id, heading, text), TTS renders one MP3 per section plus a concatenated
voiceover.mp3, and this module turns them + measured durations + word
timestamps (from the repo Transcriber) into a Remotion Explainer props file.

v3 — UNIVERSAL creative direction. v2 was still a demo: it hardcoded
OpenMontage's own marketing content into comparison/KPI cards, an
English-only number word list for stat detection, a fixed theme, and pure
index-modulo rotation for every visual choice (same input → byte-identical
video, forever). v3 replaces all of that with the creative director
(``lib/creative_director.py``):

  * per-project seed → different video every project, identical on retry;
  * optional runtime-LLM creative brief (mood / palette family / caption
    style / pacing / b-roll mood words) honoured when an LLM is configured;
  * caption STYLE PACKS (six looks) instead of one hardcoded pill;
  * palettes derived from the mood/family chain instead of the fixed ring;
  * universal stat detection (numeric regex — any language using Arabic
    numerals), comparison/KPI synthesis from actual content;
  * b-roll entries carry their source duration so the renderer loops them
    gaplessly (the v2 "laggy freeze" fix);
  * SFX cue sheet with per-cue windows (long risers no longer truncated).

Usage:
    props = build_props(script, durations, words)          # seeded auto
    props = build_props(script, durations, words,
                        seed="my-project", brief=llm_brief,
                        broll_pool=[{"path": ..., "duration": 8.0}])

Output: props.json for `npx remotion render src/index.tsx Explainer ... --props`.
"""
import json
import re
import sys
from pathlib import Path
from typing import Any, Optional

# Make `lib.creative_director` importable regardless of how this script is
# loaded (direct python execution, importlib from tests, or the Kaggle
# orchestrator) — repo root is two levels up from scripts/kaggle/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.creative_director import (  # noqa: E402
    ALIGNMENTS as ALIGNS,
    BG_STYLES,
    HERO_BG_STYLES,
    TRANSITION_STYLES as TRANSITIONS,
    TYPE_TRANSITIONS,
    CAPTION_STYLE_PACKS,
    CreativeDirector,
    detect_callout,
    detect_list,
    detect_stat,
    llm_creative_brief,
)

__all__ = [
    "build_props", "TRANSITIONS", "BG_STYLES", "ALIGNS", "TYPE_TRANSITIONS",
    "HERO_BG_STYLES", "CAPTION_STYLE_PACKS", "STOPWORDS",
]

# Default b-roll pool (scripts/kaggle/synth_broll.py output). Entries may be
# plain paths (duration assumed 8.0s, matching the synth generator) or dicts
# with explicit durations — real footage from the Open-Sora corpus should
# always pass dicts with probed durations.
DEFAULT_BROLL_POOL: list[Any] = [
    "broll/mesh-flow-cool.mp4", "broll/bokeh-warm.mp4", "broll/aurora-cool.mp4",
    "broll/light-leak-warm.mp4", "broll/wave-ribbons-cool.mp4", "broll/particles-warm.mp4",
    "broll/mesh-flow-warm.mp4", "broll/bokeh-cool.mp4",
]
_SYNTH_BROLL_SECONDS = 8.0

STOPWORDS = set("""a an the and or but if then than that this these those of in on at to for
with without into onto from by as is are was were be been being it its it's we you they he she
them our your their his her not no so such very can will just don should now what which who whom
when where why how all any both each few more most other some only own same too s t don't
shouldn't ourselves hers myself i me my myself we our ours ourselves you your yours yourself
yourselves he him his himself she her hers herself they them their theirs themselves what which
who whom this that these those am is are was were being been having has had do does did doing
would should could ought one two also make made get got go goes going""".split())


def _normalize_broll(pool: list[Any]) -> list[dict[str, Any]]:
    out = []
    for entry in pool:
        if isinstance(entry, dict):
            out.append({
                "path": entry.get("path") or entry.get("src") or "",
                "duration": float(entry.get("duration") or entry.get("seconds") or _SYNTH_BROLL_SECONDS),
            })
        else:
            out.append({"path": str(entry), "duration": _SYNTH_BROLL_SECONDS})
    return [b for b in out if b["path"]]


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?。！？])\s+", text.strip()) if s.strip()]


def group_scenes(sents, max_chars=220):
    groups, cur = [], []
    for s in sents:
        if cur and len(" ".join(cur + [s])) > max_chars:
            groups.append(cur)
            cur = []
        cur.append(s)
    if cur:
        groups.append(cur)
    return groups


def keyword_highlights(text, limit=2):
    """Pick 1-2 significant words to accent-color on text cards."""
    words = re.findall(r"[A-Za-z][A-Za-z'-]{5,}", text)
    scored = [(w, len(w) + (4 if w.lower() not in STOPWORDS else -4)) for w in words]
    scored = [w for w, s in scored if s > 4]
    seen, out = set(), []
    for w in scored:
        lw = w.lower()
        if lw not in seen:
            seen.add(lw)
            out.append(w)
        if len(out) >= limit:
            break
    return out


def _synthesise_comparison(text: str) -> Optional[dict[str, str]]:
    """Build a comparison card from a 'X vs Y' or contrastive sentence."""
    m = re.search(
        r"\b(?:(\w+) (?:versus|vs\.?|instead of|rather than|compared to) (\w+))\b",
        text, re.IGNORECASE,
    )
    if m:
        return {
            "title": m.group(0).title()[:80],
            "leftLabel": m.group(1).title(), "leftValue": "the old way",
            "rightLabel": m.group(2).title(), "rightValue": "the better way",
        }
    return None


def _synthesise_kpi(items: list[str]) -> Optional[dict[str, Any]]:
    """Build a KPI grid from a detected enumeration."""
    if not items or len(items) < 3:
        return None
    chart_data = []
    for it in items[:4]:
        stat = detect_stat(it)
        chart_data.append({
            "label": it[:38],
            "value": stat if stat else "•",
        })
    return {"title": "The moving parts", "chartData": chart_data, "columns": 2,
            "chartAnimation": "count-up"}


def build_section(director: CreativeDirector, broll: list[dict[str, Any]],
                  broll_state: dict, idx: int, total: int, sec: dict,
                  start: float, dur: float, cut_counter: int):
    cuts = []
    t = start
    hero = min(3.5, dur * max(0.18, director.hero_duration_fraction()))
    section_palette = director.palette_for_section(idx, total)

    def next_broll() -> dict[str, Any]:
        entry = broll[broll_state["n"] % len(broll)] if broll else None
        broll_state["n"] += 1
        return entry

    hero_cut = {
        "id": f"{sec['id']}-hero", "type": "hero_title",
        "in_seconds": round(t, 3), "out_seconds": round(t + hero, 3),
        "text": sec["heading"],
        "subtitle": f"Part {idx + 1} of {total}" if total > 1 else "",
        "sectionIndex": idx, "sectionCount": total,
        "transition": director.transition("hero_title", cut_counter),
        "bgStyle": director.bg_style(cut_counter, is_hero=True),
        "sweep": True,
    }
    br = next_broll()
    if br:
        start_at = round((broll_state["n"] * 1.7) % max(1.0, br["duration"] * 0.6), 2)
        hero_cut.update({
            "backgroundVideo": br["path"],
            "backgroundVideoStart": start_at,
            "backgroundVideoDuration": br["duration"],
            "backgroundOverlay": 0.42,
        })
    cuts.append(hero_cut)
    t += hero
    rest = start + dur - t
    groups = group_scenes(sentences(sec.get("text", "")))
    if not groups:
        groups = [[sec.get("heading", "")]]
    weights = [max(1, sum(len(s) for s in g)) for g in groups]
    scale = rest / sum(weights)
    kicker_base = f"Part {idx + 1} — {sec['heading']}"[:46] if total > 1 else sec.get("heading", "")[:46]
    accent = section_palette["accent"]

    progress_used = False
    for gi, (g, w) in enumerate(zip(groups, weights)):
        d = w * scale
        text = " ".join(g)
        sid = f"{sec['id']}-s{gi:02d}"
        base = {"id": sid, "in_seconds": round(t, 3), "out_seconds": round(t + d, 3),
                "sectionIndex": idx, "sectionCount": total}

        # ---- Universal cut typing from CONTENT, not hardcoded indices ----
        cut: dict[str, Any]
        stat = detect_stat(text)
        items = detect_list(text)
        comparison = _synthesise_comparison(text)
        kpi = _synthesise_kpi(items) if items else None

        if comparison and gi <= 1:
            cut = {**base, "type": "comparison", "title": comparison["title"],
                   "leftLabel": comparison["leftLabel"], "leftValue": comparison["leftValue"],
                   "rightLabel": comparison["rightLabel"], "rightValue": comparison["rightValue"]}
        elif kpi and gi % 3 == 1:
            cut = {**base, "type": "kpi_grid", **kpi}
        elif stat and gi % 2 == 0:
            cut = {**base, "type": "stat_card", "stat": stat, "subtitle": g[0][:140]}
        elif detect_callout(text) and gi % 2 == 1:
            cut = {**base, "type": "callout", "title": "Key idea", "text": text,
                   "callout_type": "tip"}
        elif (not progress_used) and len(groups) >= 3 and gi == len(groups) - 1 and total > 1:
            progress_used = True
            cut = {**base, "type": "progress_bar",
                   "title": f"Part {idx + 1} of {total}: {sec['heading']}",
                   "progress": round((idx + 1) / total, 3),
                   "progressLabel": f"{round(100 * (idx + 1) / total)}% through",
                   "progressColor": accent}
        else:
            cut = {**base, "type": "text_card", "text": text}

        # ---- Creative direction (seeded, per cut) ----
        ctype = cut["type"]
        cut["transition"] = director.transition(ctype, cut_counter + gi)
        cut["bgStyle"] = director.bg_style(cut_counter + gi + idx)
        if ctype in ("text_card", "callout"):
            cut["color"] = section_palette["text"]
        if ctype in ("stat_card", "callout"):
            cut["accentColor"] = accent
        if ctype == "text_card":
            cut["align"] = director.align(cut_counter + gi)
            if gi % 2 == 0:
                cut["kicker"] = kicker_base
            cut["accentWords"] = keyword_highlights(text)
            # Footage-forward variety: every 3rd text card gets b-roll too
            if (cut_counter + gi) % 3 == 1 and broll:
                br = next_broll()
                if br:
                    start_at = round((broll_state["n"] * 2.3) % max(1.0, br["duration"] * 0.6), 2)
                    cut.update({
                        "backgroundVideo": br["path"],
                        "backgroundVideoStart": start_at,
                        "backgroundVideoDuration": br["duration"],
                        "backgroundOverlay": 0.58,
                    })
        cuts.append(cut)
        t += d
    cuts[-1]["out_seconds"] = round(start + dur, 3)
    return cuts


def build_sfx(cuts):
    """Sound-effect cue sheet derived from the cut plan (with cue windows)."""
    sfx = []
    seen_whoosh = set()
    for i, c in enumerate(cuts):
        t_in = c["in_seconds"]
        if i > 0 and t_in not in seen_whoosh:
            sfx.append({"src": "audio/whoosh.wav", "atSeconds": round(t_in - 0.12, 3),
                        "volume": 0.32, "durationSeconds": 2.0})
            seen_whoosh.add(t_in)
        if c["type"] == "hero_title":
            sfx.append({"src": "audio/impact.wav", "atSeconds": round(t_in + 0.1, 3),
                        "volume": 0.5, "durationSeconds": 2.5})
            if t_in > 1.6:
                sfx.append({"src": "audio/riser.wav", "atSeconds": round(t_in - 1.35, 3),
                            "volume": 0.26, "durationSeconds": 2.0})
        elif c["type"] == "stat_card":
            sfx.append({"src": "audio/pop.wav", "atSeconds": round(t_in + 0.45, 3),
                        "volume": 0.42, "durationSeconds": 1.5})
        elif c["type"] == "progress_bar":
            mid = (c["in_seconds"] + c["out_seconds"]) / 2
            sfx.append({"src": "audio/chime.wav", "atSeconds": round(mid, 3),
                        "volume": 0.34, "durationSeconds": 2.0})
    return sfx


def build_props(
    script: dict,
    durations: list,
    words: Optional[list] = None,
    *,
    seed: Any = None,
    brief: Optional[dict] = None,
    llm_client: Any = None,
    broll_pool: Optional[list] = None,
    theme: str = "flat-motion-graphics",
    bg: str = "#0F172A",
) -> dict:
    """Build Explainer props.

    Parameters
    ----------
    seed:
        Project seed for the creative director. Defaults to the script's
        topic (or first heading) — stable per script, distinct per project.
    brief:
        Creative brief (see CreativeDirector). When ``llm_client`` is given
        and no brief is supplied, one is requested from the LLM.
    llm_client:
        Optional `lib.llm_client.LLMClient` — used ONLY to shift creative
        choices; failures fall back to seeded sampling silently.
    broll_pool:
        B-roll entries: paths (synth, 8s assumed) or dicts with
        ``path``/``duration`` (real footage — durations probed by caller).
    """
    secs = script["sections"]
    assert len(secs) == len(durations), "one duration per section"

    # Seed default: script identity → same script retries identically,
    # different scripts look different.
    if seed is None:
        seed = script.get("topic") or script.get("title") or secs[0].get("heading", "project")

    # Optional LLM creative direction (never fatal).
    if brief is None and llm_client is not None:
        brief = llm_creative_brief(script, llm_client)

    director = CreativeDirector(seed=seed, brief=brief)
    broll = _normalize_broll(broll_pool if broll_pool is not None else DEFAULT_BROLL_POOL)

    cuts, start = [], 0.0
    cut_counter = 0
    broll_state = {"n": 0}
    for i, (sec, d) in enumerate(zip(secs, durations)):
        section_cuts = build_section(director, broll, broll_state, i, len(secs), sec, start, d,
                                     cut_counter)
        cuts += section_cuts
        cut_counter += len(section_cuts)
        start += d

    captions = []
    if words:
        for w in words:
            entry = {"word": w["word"].strip(), "startMs": int(w["start"] * 1000),
                     "endMs": int(w["end"] * 1000)}
            if re.search(r"[.!?。！？]$", w["word"].strip()):
                entry["pageBreakAfter"] = True
            captions.append(entry)

    # Caption style pack + palettes + pacing — the anti-"same look" bundle.
    cap = director.caption_style()
    caption_style = {
        "look": cap["style"], "wordsPerPage": cap["wordsPerPage"],
        "fontSize": cap["fontSize"], "animation": cap["animation"],
        "position": cap["position"],
    }
    custom_palettes = [director.palette_for_section(i, len(secs)) for i in range(len(secs))]
    transition_speed = {"calm": 1.4, "standard": 1.0, "energetic": 0.75}[director.pacing]

    return {
        "theme": theme,
        "sectionCount": len(secs),
        "cuts": cuts,
        "captions": captions,
        "captionStyle": caption_style,
        "customPalettes": custom_palettes,
        "creativeSeed": director.seed_int,
        "transitionSpeed": transition_speed,
        "sfx": build_sfx(cuts),
        "audio": {
            "narration": {"src": "voiceover.mp3", "volume": 1.0},
            "music": {"src": "audio/bgm.mp3", "volume": 0.16,
                      "fadeInSeconds": 1.5, "fadeOutSeconds": 3.5, "loop": True},
        },
        "durationSeconds": round(start, 3),
        "creativeBrief": {
            "seed": str(seed), "mood": director.brief.get("mood", ""),
            "pacing": director.pacing,
            "captionStyle": caption_style["look"],
            "brollMoods": director.broll_moods,
            "rationale": director.brief.get("rationale", ""),
        },
    }


if __name__ == "__main__":
    import sys
    script = json.load(open(sys.argv[1]))
    durations = json.load(open(sys.argv[2]))
    words = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else None
    props = build_props(script, durations, words)
    json.dump(props, open(sys.argv[4] if len(sys.argv) > 4 else "props.json", "w"),
              indent=1, ensure_ascii=False)
    print("cuts", len(props["cuts"]), "captions", len(props["captions"]),
          "sfx", len(props["sfx"]), "duration", props["durationSeconds"],
          "caption", props["captionStyle"]["look"],
          "mood", props["creativeBrief"]["mood"])
