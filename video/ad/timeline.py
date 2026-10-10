"""The ad's timeline: video/ad/timeline.yaml plus the narration's real timing, written to video/out/ad/timeline.json
for the scene pages (runtime.js), the renderer, the music and the mix. Each scene runs lead + narration + pad
seconds; scenes follow each other with no overlap, so a scene's `start` is the sum of those before it. A walk scene
or a motion scene with `clip` speaks that script.yaml scene's narration (its wav, its captions); its clean capture's
spot beats come from video/out/clips_clean/<clip>.beats.json when it exists. `cues` maps every phrase a scene page
asks for with cue("...") to seconds from the scene's start.

    uv run --with playwright python video/ad/timeline.py
"""
import json
import os
import re
import sys
from pathlib import Path

import yaml

AD = Path(__file__).resolve().parent
VIDEO = AD.parent
OUT = VIDEO / "out"
sys.path.insert(0, str(VIDEO))
from capture import Clock  # noqa: E402  (the narration's clock: pauses in the wav snap the chunk boundaries)

FPS = 30
AUDIO = OUT / os.environ.get("VIDEO_AUDIO", "audio")  # the ad may speak in its own voice (tts.py VIDEO_AUDIO)
DEFAULTS = {"motion": (0.3, 0.8), "walk": (0.5, 0.9)}  # (lead, pad)


def load() -> list[dict]:
    spec = yaml.safe_load((AD / "timeline.yaml").read_text(encoding="utf-8"))["scenes"]
    script = {s["id"]: s for s in yaml.safe_load((VIDEO / "script.yaml").read_text(encoding="utf-8"))["scenes"]}
    durations = json.loads((AUDIO / "durations.json").read_text())
    scenes, start = [], 0.0
    for s in spec:
        voice = s.get("clip") or s["id"]
        say = " ".join((s.get("say") or script[voice]["say"]).split())
        if voice not in durations:
            sys.exit(f"{s['id']}: no narration for {voice}; run VIDEO_SCRIPT=video/ad/timeline.yaml uv run python video/tts.py")
        narr = durations[voice]
        lead, pad = s.get("lead", DEFAULTS[s["kind"]][0]), s.get("pad", DEFAULTS[s["kind"]][1])
        dur = round(lead + narr + pad, 3)
        clock = Clock(say, AUDIO / f"{voice}.wav", narr)
        page = AD / "scenes" / ("walk.html" if s["kind"] == "walk" else f"{s['id']}.html")
        asks = set(re.findall(r"""cue\(\s*["'`]([^"'`]+)["'`]""", page.read_text(encoding="utf-8"))) if page.exists() else set()
        cues = {p: round(lead + clock.at(p), 3) for p in sorted(asks) if p in say}
        beats_f = OUT / "clips_clean" / f"{voice}.beats.json"
        scenes.append({
            **{k: v for k, v in s.items() if k not in ("say",)},
            "say": say, "voice": voice, "start": round(start, 3), "dur": dur, "frames": round(dur * FPS),
            "lead": lead, "narr": narr, "pad": pad, "cues": cues,
            "caps": [[round(a + lead, 3), round(b + lead, 3), c] for a, b, c in clock.captions()],
            "beats": json.loads(beats_f.read_text()) if beats_f.exists() else [],
            "wav": str((AUDIO / f"{voice}.wav").relative_to(VIDEO)),
        })
        start += dur
    return scenes


def main() -> None:
    scenes = load()
    (OUT / "ad").mkdir(parents=True, exist_ok=True)
    total = scenes[-1]["start"] + scenes[-1]["dur"]
    (OUT / "ad" / "timeline.json").write_text(json.dumps({"fps": FPS, "total": round(total, 3), "scenes": scenes}, indent=1))
    for s in scenes:
        page = AD / "scenes" / f"{s['id']}.html"
        asks = re.findall(r"""cue\(\s*["'`]([^"'`]+)["'`]""", page.read_text(encoding="utf-8")) if s["kind"] == "motion" and page.exists() else []
        missing = sorted({p for p in asks if p not in s["say"]})
        print(f"{s['id']:<13} {s['start']:7.2f} +{s['dur']:6.2f} s  {len(s['cues'])} cues{'  NOT SAID: ' + '; '.join(missing) if missing else ''}")
    print(f"total {int(total // 60)}:{total % 60:04.1f}  ->  {OUT / 'ad' / 'timeline.json'}")


if __name__ == "__main__":
    main()
