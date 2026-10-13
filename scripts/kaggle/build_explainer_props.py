"""Build an OpenMontage Explainer props file from a script + voiceover timing.

Kaggle production path: the agent writes script.json (sections with id,
heading, text, visual, cue), TTS renders one MP3 per section plus a
concatenated voiceover.mp3, and this module turns them + measured durations
+ word timestamps (from the repo Transcriber) into a Remotion Explainer
props file.

Usage (from render_voiceover_video.py):
    props = build_props(script, durations, words)

Output: props.json for `npx remotion render src/index.tsx Explainer ... --props`.
"""
import json
import re

THEME = "flat-motion-graphics"
BG = "#0F172A"
NUM_WORDS = ["fourteen", "one hundred", "seven hundred", "twenty", "thirty", "four parts", "four"]


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


def build_section(idx, total, sec, start, dur):
    cuts = []
    t = start
    hero = min(3.5, dur * 0.3)
    cuts.append({
        "id": f"{sec['id']}-hero", "type": "hero_title",
        "in_seconds": round(t, 3), "out_seconds": round(t + hero, 3),
        "text": sec["heading"], "subtitle": f"Part {idx + 1} of {total}",
        "backgroundColor": BG,
    })
    t += hero
    rest = start + dur - t
    groups = group_scenes(sentences(sec["text"]))
    weights = [max(1, sum(len(s) for s in g)) for g in groups]
    scale = rest / sum(weights)
    for gi, (g, w) in enumerate(zip(groups, weights)):
        d = w * scale
        text = " ".join(g)
        sid = f"{sec['id']}-s{gi:02d}"
        base = {"id": sid, "in_seconds": round(t, 3), "out_seconds": round(t + d, 3), "backgroundColor": BG}
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
        cuts.append(cut)
        t += d
    # snap the last cut to the exact section end (rounding drift)
    cuts[-1]["out_seconds"] = round(start + dur, 3)
    return cuts


def build_props(script, durations, words=None):
    secs = script["sections"]
    assert len(secs) == len(durations), "one duration per section"
    cuts, start = [], 0.0
    for i, (sec, d) in enumerate(zip(secs, durations)):
        cuts += build_section(i, len(secs), sec, start, d)
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
        "cuts": cuts,
        "captions": captions,
        "audio": {"narration": {"src": "voiceover.mp3", "volume": 1.0}},
        "durationSeconds": round(start, 3),
    }


if __name__ == "__main__":
    import sys
    script = json.load(open(sys.argv[1]))
    durations = json.load(open(sys.argv[2]))
    words = json.load(open(sys.argv[3])) if len(sys.argv) > 3 else None
    props = build_props(script, durations, words)
    json.dump(props, open(sys.argv[4] if len(sys.argv) > 4 else "props.json", "w"), indent=1, ensure_ascii=False)
    print("cuts", len(props["cuts"]), "captions", len(props["captions"]), "duration", props["durationSeconds"])
