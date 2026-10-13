"""Build an OpenMontage Explainer props file from a script + voiceover timing.

Kaggle production path: the agent writes script.json (sections with id,
heading, text, visual, cue), TTS renders one MP3 per section plus a
concatenated voiceover.mp3, and this module turns them + measured durations
+ word timestamps (from the repo Transcriber) into a Remotion Explainer
props file.

v2 — motion design creative direction. The v1 output was functionally
correct but visually monotone (one background for the whole video, no
transitions, no variety). The builder now assigns, deterministically:

  * a palette per section (the video's color world evolves),
  * an animated background style per cut (rotating, no immediate repeats,
    skipped when the cut carries b-roll footage),
  * an entrance/exit transition style per cut (typed: heroes punch, text
    slides, stats flip),
  * text alignment rotation + kicker labels for text cards,
  * keyword accent highlighting,
  * procedural stock footage (b-roll) behind hero scenes and selected text
    scenes,
  * SFX cues (whoosh on every cut, impact on heroes, riser into new
    sections, chime on progress completion, pop on stat landings),
  * an ambient BGM track config with fades and looping.

Usage (from render_voiceover_video.py):
    props = build_props(script, durations, words)

Output: props.json for `npx remotion render src/index.tsx Explainer ... --props`.
"""
import json
import re

THEME = "flat-motion-graphics"
BG = "#0F172A"
NUM_WORDS = ["fourteen", "one hundred", "seven hundred", "twenty", "thirty", "four parts", "four"]

# Motion design vocabulary — mirrors remotion-composer/src/motion/*.
BG_STYLES = ["aurora", "mesh", "beams", "particles", "grid-pulse", "orbits", "waves", "grain"]
HERO_BG_STYLES = ["aurora", "mesh", "beams"]
TRANSITIONS = ["slide-left", "slide-right", "slide-up", "zoom-punch", "wipe", "flip", "glitch", "rise"]
TYPE_TRANSITIONS = {
    "hero_title": ["zoom-punch", "glitch", "wipe"],
    "text_card": ["slide-left", "slide-up", "rise", "slide-right", "wipe"],
    "stat_card": ["flip", "zoom-punch"],
    "callout": ["slide-right", "glitch"],
    "comparison": ["slide-left", "zoom-punch"],
    "kpi_grid": ["wipe", "zoom-punch"],
    "progress_bar": ["wipe", "rise"],
}
ALIGNS = ["left", "center", "right"]
# Procedural stock footage pool (scripts/kaggle/synth_broll.py output).
BROLL_POOL = [
    "broll/mesh-flow-cool.mp4", "broll/bokeh-warm.mp4", "broll/aurora-cool.mp4",
    "broll/light-leak-warm.mp4", "broll/wave-ribbons-cool.mp4", "broll/particles-warm.mp4",
    "broll/mesh-flow-warm.mp4", "broll/bokeh-cool.mp4",
]
STOPWORDS = set("""a an the and or but if then than that this these those of in on at to for
with without into onto from by as is are was were be been being it its it's we you they he she
them our your their his her not no so such very can will just don should now what which who whom
when where why how all any both each few more most other some only own same too s t don't
shouldn't ourselves hers myself i me my myself we our ours ourselves you your yours yourself
yourselves he him his himself she her hers herself they them their theirs themselves what which
who whom this that these those am is are was were being been having has had do does did doing
would should could ought i'm you're he's she's it's we're they're i've you've we've they've i'd
you'd he'd she'd we'd they'd one two also more most make made get got go goes going""".split())


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]


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


def stat_phrase(text):
    low = text.lower()
    for w in NUM_WORDS:
        if w in low:
            return w.title()
    return None


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


def _rotate(seq, i, avoid=None):
    """Deterministic rotation that never repeats the avoided value."""
    pick = seq[i % len(seq)]
    if avoid is not None and pick == avoid and len(seq) > 1:
        pick = seq[(i + 1) % len(seq)]
    return pick


def build_section(idx, total, sec, start, dur, cut_counter, broll_state, section_start=False):
    cuts = []
    t = start
    hero = min(3.5, dur * 0.3)
    hero_cut = {
        "id": f"{sec['id']}-hero", "type": "hero_title",
        "in_seconds": round(t, 3), "out_seconds": round(t + hero, 3),
        "text": sec["heading"], "subtitle": f"Part {idx + 1} of {total}",
        "backgroundColor": BG,
        # Creative direction
        "sectionIndex": idx, "sectionCount": total,
        "transition": _rotate(TYPE_TRANSITIONS["hero_title"], idx),
        "sweep": True,
        # Stock footage behind the hero (pool advances per use so all
        # clips get screen time across the video)
        "backgroundVideo": BROLL_POOL[broll_state["n"] % len(BROLL_POOL)],
        "backgroundVideoStart": round((broll_state["n"] * 1.7) % 5.0, 2),
        "backgroundOverlay": 0.42,
    }
    broll_state["n"] += 1
    cuts.append(hero_cut)
    t += hero
    rest = start + dur - t
    groups = group_scenes(sentences(sec["text"]))
    weights = [max(1, sum(len(s) for s in g)) for g in groups]
    scale = rest / sum(weights)
    kicker_base = f"Part {idx + 1} — {sec['heading']}"[:46]
    for gi, (g, w) in enumerate(zip(groups, weights)):
        d = w * scale
        text = " ".join(g)
        sid = f"{sec['id']}-s{gi:02d}"
        base = {"id": sid, "in_seconds": round(t, 3), "out_seconds": round(t + d, 3),
                "backgroundColor": BG,
                "sectionIndex": idx, "sectionCount": total}
        if idx == 1 and gi == 0:
            cut = {**base, "type": "comparison", "title": "Scattered tools versus one pipeline",
                   "leftLabel": "Separate apps", "leftValue": "6 or more",
                   "rightLabel": "OpenMontage", "rightValue": "1 pipeline"}
        elif idx == 2 and gi == 0:
            cut = {**base, "type": "kpi_grid", "title": "Inside the repository",
                   "chartData": [
                       {"label": "Pipeline definitions", "value": 14},
                       {"label": "Tools", "value": 100, "suffix": "+"},
                       {"label": "Skill and reference files", "value": 700, "suffix": "+"},
                       {"label": "Core parts", "value": 4}],
                   "columns": 2, "chartAnimation": "count-up"}
        elif gi == 2 and len(groups) >= 3:
            cut = {**base, "type": "progress_bar", "title": f"Part {idx + 1} of {total}: {sec['heading']}",
                   "progress": round((idx + 1) / total, 3),
                   "progressLabel": f"{round(100 * (idx + 1) / total)}% through the video",
                   "progressColor": "#34D399"}
        else:
            sp = stat_phrase(text)
            if sp and gi % 2 == 0:
                cut = {**base, "type": "stat_card", "stat": sp, "subtitle": g[0][:140],
                       "accentColor": "#22D3EE", "color": "#F8FAFC"}
            elif gi % 2 == 1 and re.search(r"should|rule|habit|remember|simple|useful", text, re.I):
                cut = {**base, "type": "callout", "title": "Key idea", "text": text,
                       "callout_type": "tip", "accentColor": "#22D3EE", "color": "#F8FAFC"}
            else:
                cut = {**base, "type": "text_card", "text": text, "color": "#F8FAFC"}
        # ---- Creative direction for non-hero cuts ----
        ctype = cut["type"]
        cut["transition"] = _rotate(TYPE_TRANSITIONS.get(ctype, TRANSITIONS), cut_counter + gi)
        cut["bgStyle"] = _rotate(BG_STYLES, cut_counter + gi + idx)
        if ctype == "text_card":
            cut["align"] = _rotate(ALIGNS, cut_counter + gi)
            if gi % 2 == 0:
                cut["kicker"] = kicker_base
            cut["accentWords"] = keyword_highlights(text)
            # Footage-forward variety: every 3rd text card gets b-roll too
            if (cut_counter + gi) % 3 == 1:
                cut["backgroundVideo"] = BROLL_POOL[broll_state["n"] % len(BROLL_POOL)]
                cut["backgroundVideoStart"] = round((broll_state["n"] * 2.3) % 5.0, 2)
                cut["backgroundOverlay"] = 0.58
                broll_state["n"] += 1
        cuts.append(cut)
        t += d
    # snap the last cut to the exact section end (rounding drift)
    cuts[-1]["out_seconds"] = round(start + dur, 3)
    return cuts


def build_sfx(cuts):
    """Sound-effect cue sheet derived from the cut plan."""
    sfx = []
    seen_whoosh = set()
    for i, c in enumerate(cuts):
        t_in = c["in_seconds"]
        # Whoosh on every cut boundary except the very first frame.
        if i > 0 and t_in not in seen_whoosh:
            sfx.append({"src": "audio/whoosh.wav", "atSeconds": round(t_in - 0.12, 3), "volume": 0.32})
            seen_whoosh.add(t_in)
        if c["type"] == "hero_title":
            sfx.append({"src": "audio/impact.wav", "atSeconds": round(t_in + 0.1, 3), "volume": 0.5})
            # Riser leads into every section hero except the first.
            if t_in > 1.6:
                sfx.append({"src": "audio/riser.wav", "atSeconds": round(t_in - 1.35, 3), "volume": 0.26})
        elif c["type"] == "stat_card":
            sfx.append({"src": "audio/pop.wav", "atSeconds": round(t_in + 0.45, 3), "volume": 0.42})
        elif c["type"] == "progress_bar":
            mid = (c["in_seconds"] + c["out_seconds"]) / 2
            sfx.append({"src": "audio/chime.wav", "atSeconds": round(mid, 3), "volume": 0.34})
    return sfx


def build_props(script, durations, words=None):
    secs = script["sections"]
    assert len(secs) == len(durations), "one duration per section"
    cuts, start = [], 0.0
    cut_counter = 0
    broll_state = {"n": 0}
    for i, (sec, d) in enumerate(zip(secs, durations)):
        section_cuts = build_section(i, len(secs), sec, start, d, cut_counter, broll_state,
                                     section_start=(i > 0))
        cuts += section_cuts
        cut_counter += len(section_cuts)
        start += d
    captions = []
    if words:
        for i, w in enumerate(words):
            entry = {"word": w["word"].strip(), "startMs": int(w["start"] * 1000), "endMs": int(w["end"] * 1000)}
            if re.search(r"[.!?]$", w["word"].strip()):
                entry["pageBreakAfter"] = True
            captions.append(entry)
    return {
        "theme": THEME,
        "sectionCount": len(secs),
        "cuts": cuts,
        "captions": captions,
        "sfx": build_sfx(cuts),
        "audio": {
            "narration": {"src": "voiceover.mp3", "volume": 1.0},
            "music": {"src": "audio/bgm.mp3", "volume": 0.16,
                      "fadeInSeconds": 1.5, "fadeOutSeconds": 3.5, "loop": True},
        },
        "durationSeconds": round(start, 3),
    }


if __name__ == "__main__":
    import sys
    script = json.load(open(sys.argv[1]))
    durations = json.load(open(sys.argv[2]))
    words = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else None
    props = build_props(script, durations, words)
    json.dump(props, open(sys.argv[4] if len(sys.argv) > 4 else "props.json", "w"), indent=1, ensure_ascii=False)
    print("cuts", len(props["cuts"]), "captions", len(props["captions"]),
          "sfx", len(props["sfx"]), "duration", props["durationSeconds"])
