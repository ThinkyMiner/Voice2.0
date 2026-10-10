"""The demo video's narration: each scene's `say` in video/script.yaml becomes video/out/audio/<id>.wav, spoken by
Sarvam's Bulbul v3 with Sarvam's model API key (SARVAM_API_KEY, from the environment or VIDEO_ENV_FILE, default
the repo's .env). A scene is spoken again only when its text, voice or pace changed. Writes durations.json.

    uv run python video/tts.py            # VIDEO_SPEAKER=shubh; VIDEO_PACE overrides the script's pace (1.0)
    VIDEO_SCRIPT=video/ad/timeline.yaml uv run python video/tts.py   # the ad's own lines (scenes with `say`)
    VIDEO_TTS=openai VIDEO_SPEAKER=marin VIDEO_AUDIO=audio_ad ...    # OpenAI's gpt-audio through OpenRouter, read
                                                                     # as an ad (STYLE), into video/out/audio_ad/
    VIDEO_TTS=eleven VIDEO_SPEAKER=<voice id> VIDEO_SCRIPT=video/ad/timeline.yaml ...   # ElevenLabs (ELEVEN_LABS key)
Every OpenAI take is checked word for word against the script (its own transcript) and spoken again if it drifts.
ElevenLabs hears SAY_AS's spellings (e.g. "India Mart"); captions keep the script's.
"""
import base64
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import wave
from pathlib import Path

import httpx
import yaml

HERE = Path(__file__).resolve().parent
OUT = HERE / "out" / os.environ.get("VIDEO_AUDIO", "audio")
TTS = os.environ.get("VIDEO_TTS", "sarvam")  # sarvam (Bulbul v3), openai (gpt-audio, through OpenRouter) or eleven
ELEVEN_MODEL = os.environ.get("VIDEO_ELEVEN_MODEL", "eleven_multilingual_v2")
SAY_AS = {"IndiaMART": "India Mart"}  # how ElevenLabs should hear a word
SPEAKER = os.environ.get("VIDEO_SPEAKER", "shubh" if TTS == "sarvam" else "marin")
STYLE = ("You are the voice of a premium technology product launch film, in the style of an Apple keynote ad. "
         "Read the user's line exactly, word for word, adding nothing before or after. Delivery: eager, confident and warm, "
         "with quiet excitement that builds; crisp diction; a short dramatic pause at each full stop; never rushed, never flat.")
SCRIPT = Path(os.environ.get("VIDEO_SCRIPT", HERE / "script.yaml"))


def api_key(*names: str) -> str:
    for n in names:
        if k := os.environ.get(n):
            return k
    env = Path(os.environ.get("VIDEO_ENV_FILE", HERE.parent / ".env"))
    for line in env.read_text(encoding="utf-8").splitlines() if env.exists() else []:
        k, _, v = line.partition("=")
        if k.strip() in names and v.strip():
            return v.strip().strip("'\"")
    sys.exit(f"set {' or '.join(names)}, or VIDEO_ENV_FILE to a .env that has it")


def sarvam(key: str, text: str, pace: float) -> bytes:
    r = httpx.post("https://api.sarvam.ai/text-to-speech", headers={"api-subscription-key": key}, timeout=120,
                   json={"text": text, "target_language_code": "en-IN", "model": "bulbul:v3", "speaker": SPEAKER, "pace": pace})
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code} {r.text[:300]}")
    audios = r.json()["audios"]
    if len(audios) != 1:
        sys.exit(f"Sarvam returned {len(audios)} audio parts; split the scene's text")
    return base64.b64decode(audios[0])


def eleven(key: str, text: str) -> bytes:
    for word, spoken in SAY_AS.items():
        text = text.replace(word, spoken)
    r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{SPEAKER}?output_format=mp3_44100_128", timeout=180,
                   headers={"xi-api-key": key}, json={"text": text, "model_id": ELEVEN_MODEL})
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code} {r.text[:300]}")
    pcm = subprocess.run([os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg"), "-loglevel", "error", "-f", "mp3", "-i", "-",
                          "-f", "s16le", "-ac", "1", "-ar", "44100", "-"], input=r.content, capture_output=True, check=True).stdout
    buf = io.BytesIO()  # a wav written through a pipe has no length in its header, so build it here
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100); w.writeframes(pcm)
    return buf.getvalue()


def openai(key: str, text: str) -> bytes:
    """gpt-audio streams 24 kHz PCM and its own transcript; a take whose words differ from the script is redone."""
    words = lambda t: re.sub(r"[^a-z0-9 ]", "", t.lower().replace("-", " ")).split()  # noqa: E731
    for attempt in range(4):
        body = {"model": "openai/gpt-audio", "modalities": ["text", "audio"], "audio": {"voice": SPEAKER, "format": "pcm16"},
                "stream": True, "messages": [{"role": "system", "content": STYLE}, {"role": "user", "content": text}]}
        pcm, said = bytearray(), []
        with httpx.stream("POST", "https://openrouter.ai/api/v1/chat/completions", json=body, timeout=180,
                          headers={"Authorization": f"Bearer {key}"}) as r:
            if r.status_code != 200:
                sys.exit(f"HTTP {r.status_code} {r.read()[:300]!r}")
            for line in r.iter_lines():
                if line.startswith("data: ") and line != "data: [DONE]":
                    for c in json.loads(line[6:]).get("choices", []):
                        a = (c.get("delta") or {}).get("audio") or {}
                        pcm += base64.b64decode(a["data"]) if a.get("data") else b""
                        said.append(a.get("transcript") or "")
        if pcm and words("".join(said)) == words(text):
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(bytes(pcm))
            return buf.getvalue()
        print(f"    take {attempt + 1} drifted: {''.join(said)[:120]!r}")
    sys.exit("four takes drifted from the script")


def main() -> None:
    doc = yaml.safe_load(SCRIPT.read_text(encoding="utf-8"))
    pace = float(os.environ.get("VIDEO_PACE", doc.get("pace", 1.0)))  # the script's own pace, unless overridden
    scenes = [s for s in doc["scenes"] if s.get("say")]
    OUT.mkdir(parents=True, exist_ok=True)
    dfile = OUT / "durations.json"  # shared by both scripts: each run updates its own scenes
    key = {"sarvam": lambda: api_key("SARVAM_API_KEY"), "openai": lambda: api_key("OPENROUTER_API_KEY", "OPENROUTER"),
           "eleven": lambda: api_key("ELEVENLABS_API_KEY", "ELEVEN_LABS")}[TTS]()
    durations = json.loads(dfile.read_text()) if dfile.exists() else {}
    for s in scenes:
        text = " ".join(s["say"].split())
        if "<" in text:
            print(f"{s['id']}: has a <placeholder>, spoken as written")
        voice = {"sarvam": f"{SPEAKER}|{pace}", "openai": f"openai|{SPEAKER}|{STYLE}",
                 "eleven": f"eleven|{SPEAKER}|{ELEVEN_MODEL}|{SAY_AS}"}[TTS]
        tag = hashlib.sha256(f"{voice}|{text}".encode()).hexdigest()[:16]
        wav, stamp = OUT / f"{s['id']}.wav", OUT / f"{s['id']}.tag"
        if not (wav.exists() and stamp.exists() and stamp.read_text() == tag):
            print(f"{s['id']}: speaking", flush=True)
            wav.write_bytes({"sarvam": lambda: sarvam(key, text, pace), "openai": lambda: openai(key, text),
                             "eleven": lambda: eleven(key, text)}[TTS]())
            stamp.write_text(tag)
            print(f"{s['id']}: spoken")
        with wave.open(str(wav)) as w:
            durations[s["id"]] = round(w.getnframes() / w.getframerate(), 3)
    dfile.write_text(json.dumps(durations, indent=1))
    mine = [durations[s["id"]] for s in scenes]
    print(f"{len(mine)} scenes, {sum(mine):.0f} s of narration")


if __name__ == "__main__":
    main()
