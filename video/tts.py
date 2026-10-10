"""The demo video's narration: each scene's `say` in video/script.yaml becomes video/out/audio/<id>.wav, spoken by
Sarvam's Bulbul v3 with Sarvam's model API key (SARVAM_API_KEY, from the environment or VIDEO_ENV_FILE, default
the repo's .env). A scene is spoken again only when its text, voice or pace changed. Writes durations.json.

    uv run python video/tts.py            # VIDEO_SPEAKER=shubh; VIDEO_PACE overrides the script's pace (1.0)
    VIDEO_SCRIPT=video/ad/timeline.yaml uv run python video/tts.py   # the ad's own lines (scenes with `say`)
"""
import base64
import hashlib
import json
import os
import sys
import wave
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / "audio"
SPEAKER = os.environ.get("VIDEO_SPEAKER", "shubh")
SCRIPT = Path(os.environ.get("VIDEO_SCRIPT", HERE / "script.yaml"))


def api_key() -> str:
    if k := os.environ.get("SARVAM_API_KEY"):
        return k
    env = Path(os.environ.get("VIDEO_ENV_FILE", HERE.parent / ".env"))
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        k, _, v = line.partition("=")
        if k.strip() == "SARVAM_API_KEY" and v.strip():
            return v.strip().strip("'\"")
    sys.exit("set SARVAM_API_KEY (Sarvam's model API key), or VIDEO_ENV_FILE to a .env that has it")


def main() -> None:
    doc = yaml.safe_load(SCRIPT.read_text(encoding="utf-8"))
    pace = float(os.environ.get("VIDEO_PACE", doc.get("pace", 1.0)))  # the script's own pace, unless overridden
    scenes = [s for s in doc["scenes"] if s.get("say")]
    OUT.mkdir(parents=True, exist_ok=True)
    dfile = OUT / "durations.json"  # shared by both scripts: each run updates its own scenes
    key, durations = api_key(), json.loads(dfile.read_text()) if dfile.exists() else {}
    for s in scenes:
        text = " ".join(s["say"].split())
        if "<" in text:
            print(f"{s['id']}: has a <placeholder>, spoken as written")
        tag = hashlib.sha256(f"{SPEAKER}|{pace}|{text}".encode()).hexdigest()[:16]
        wav, stamp = OUT / f"{s['id']}.wav", OUT / f"{s['id']}.tag"
        if not (wav.exists() and stamp.exists() and stamp.read_text() == tag):
            r = httpx.post("https://api.sarvam.ai/text-to-speech", headers={"api-subscription-key": key}, timeout=120,
                           json={"text": text, "target_language_code": "en-IN", "model": "bulbul:v3",
                                 "speaker": SPEAKER, "pace": pace})
            if r.status_code != 200:
                sys.exit(f"{s['id']}: HTTP {r.status_code} {r.text[:300]}")
            audios = r.json()["audios"]
            if len(audios) != 1:
                sys.exit(f"{s['id']}: Sarvam returned {len(audios)} audio parts; split the scene's text")
            wav.write_bytes(base64.b64decode(audios[0]))
            stamp.write_text(tag)
            print(f"{s['id']}: spoken")
        with wave.open(str(wav)) as w:
            durations[s["id"]] = round(w.getnframes() / w.getframerate(), 3)
    dfile.write_text(json.dumps(durations, indent=1))
    mine = [durations[s["id"]] for s in scenes]
    print(f"{len(mine)} scenes, {sum(mine):.0f} s of narration")


if __name__ == "__main__":
    main()
