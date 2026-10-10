"""Builds the ad cut from video/ad/render.py's segments and the sound. The segments join in timeline order into one
picture track (concat demuxer, no re-encode: they share codec settings); a scene not filmed yet holds the plain
stage for its length, so everything after it still lines up. The narration is each scene's wav laid at the
scene's first frame + lead. music.py's music (video/out/ad/music.wav) is ducked under the voice by a sidechain
compressor keyed by the narration, its effects (video/out/ad/sfx.wav) sit on top, and the master is normalised to
-14 LUFS, -1.5 dBTP (two-pass loudnorm). Without music or effects the film is built with what there is.
Out: video/out/ad/vani_lab_film.mp4 and contact_sheet.png (--draft: video/out/ad/draft/, from its segments).

    uv run python video/ad/build.py [--draft]
"""
import argparse
import functools
import importlib.util
import json
import math
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

AD = Path(__file__).resolve().parent
VIDEO = AD.parent
OUT_AD = VIDEO / "out" / "ad"
FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")
FPS = 30
STAGE = "0x0B0B0C"  # the ad's plain stage, for scenes not filmed yet
# render.py's encoder settings, so a held scene joins the filmed ones without a re-encode
X264 = ["-vf", "scale=in_range=full:in_color_matrix=bt601:out_range=tv:out_color_matrix=bt709,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-x264-params", "aq-mode=3",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv", "-r", str(FPS), "-an"]
# Each stem's integrated loudness alone, before the mix (LUFS): the voice on top, effects under it, the music well
# under both (and ducked further while the voice speaks). The master then moves as one to -14 LUFS.
VOICE, MUSIC, SFX = -16.0, -24.0, -21.0
DUCK = "sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400"
PREP = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"

# video/build.py's contact sheet (loaded by path: this file is also called build.py)
_spec = importlib.util.spec_from_file_location("plain_build", VIDEO / "build.py")
plain = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plain)


def ff(*args) -> None:
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-nostdin", *map(str, args)], check=True)


def measure(inputs: list, graph: str, label: str = "mix") -> dict:
    """loudnorm's measurement (input_i, input_tp, input_lra, input_thresh, target_offset) of `graph`'s [label]."""
    r = subprocess.run([FFMPEG, "-hide_banner", "-nostats", "-nostdin", *map(str, inputs), "-filter_complex",
                        f"{graph};[{label}]loudnorm=I=-14:TP=-1.5:print_format=json[m]", "-map", "[m]", "-f", "null", "-"],
                       capture_output=True, text=True, check=True)
    return json.loads(r.stderr[r.stderr.rindex("{"): r.stderr.rindex("}") + 1])


@functools.cache  # mix() asks twice
def gain(path: Path, target: float) -> float:
    """dB that bring a stem's integrated loudness to target (0 for silence)."""
    i = float(measure(["-i", path], f"[0:a]{PREP}[mix]")["input_i"])
    return target - i if math.isfinite(i) else 0.0


def narration(scenes: list[dict], out: Path) -> float:
    """Every scene's wav at its first frame + lead, as one wav as long as the picture; returns that length."""
    sr, ch = 48000, 1  # every voice resampled to one format (Sarvam speaks 22.05 kHz, ElevenLabs 44.1 kHz)
    total = sum(s["frames"] for s in scenes) / FPS
    buf, f0 = np.zeros((round(total * sr), ch), np.int16), 0
    for s in scenes:
        pcm = subprocess.run([FFMPEG, "-loglevel", "error", "-nostdin", "-i", str(VIDEO / s["wav"]), "-ac", "1", "-ar", str(sr),
                              "-f", "s16le", "-"], capture_output=True, check=True).stdout
        x = np.frombuffer(pcm, np.int16).reshape(-1, ch)
        at = round((f0 / FPS + s["lead"]) * sr)  # where the scene's first frame really is, not its rounded start
        x = x[: max(0, len(buf) - at)]
        buf[at: at + len(x)] = x  # scenes never overlap: each is lead + narration + pad
        f0 += s["frames"]
    with wave.open(str(out), "wb") as w:
        w.setnchannels(ch), w.setsampwidth(2), w.setframerate(sr)
        w.writeframes(buf.tobytes())
    return total


def mix(voice: Path, music: Path | None, sfx: Path | None, total: float, first: int) -> tuple[list, str]:
    """The audio inputs and the graph that mixes them into [mix]; `first` is the voice's input index."""
    inputs, k = ["-i", voice], {"v": first}
    for name, p in (("m", music), ("s", sfx)):
        if p:
            k[name] = first + len(inputs) // 2
            inputs += ["-i", p]
    g = [f"[{k['v']}:a]{PREP},volume={gain(voice, VOICE):.2f}dB" + (",asplit[v][key]" if music else "[v]")]
    if music:
        g.append(f"[{k['m']}:a]{PREP},volume={gain(music, MUSIC):.2f}dB[m0];[m0][key]{DUCK}[m]")
    if sfx:
        g.append(f"[{k['s']}:a]{PREP},volume={gain(sfx, SFX):.2f}dB[s]")
    stems = "[v]" + ("[m]" if music else "") + ("[s]" if sfx else "")
    g.append(f"{stems}amix=inputs={len(stems) // 3}:normalize=0:duration=longest,atrim=end={total:.4f}[mix]")
    return inputs, ";".join(g)


def build(scenes: list[dict], root: Path, music: Path | None, sfx: Path | None, size: str) -> None:
    segs = []
    for s in scenes:
        seg = root / "segments" / f"{s['id']}.mp4"
        if not seg.exists():
            seg = root / "segments" / "_hold" / f"{s['id']}.{s['frames']}.mp4"
            if not seg.exists():
                print(f"  {s['id']}: not filmed yet, holding the stage for {s['frames'] / FPS:.1f} s")
                seg.parent.mkdir(parents=True, exist_ok=True)
                ff("-f", "lavfi", "-i", f"color=c={STAGE}:s={size}:r={FPS},format=rgb24", "-frames:v", s["frames"], *X264, seg)
        segs.append(seg)
    concat = root / "concat.txt"
    concat.write_text("".join(f"file '{p}'\n" for p in segs))
    voice = root / "narration.wav"
    total = narration(scenes, voice)

    # The master: the mix raised as one to -14 LUFS with its peaks held under -3 dBFS (the gain corrected once for
    # what the limiter takes back), so loudnorm's two-pass trim stays linear and under -1.5 dBTP. Measured alone,
    # then the same graph behind the picture.
    inputs, graph = mix(voice, music, sfx, total, 0)
    pre = lambda g: f"[mix]volume={g:.2f}dB,alimiter=limit=0.708:level=0[pre]"  # noqa: E731
    g = -14 - float(measure(inputs, graph)["input_i"])
    g += -14 - float(measure(inputs, f"{graph};{pre(g)}", "pre")["input_i"])
    pre = pre(g)
    m = measure(inputs, f"{graph};{pre}", "pre")
    lra = max(11.0, min(50.0, float(m["input_lra"]) + 1))  # at or above the mix's own range, or loudnorm goes dynamic
    ln = (f"loudnorm=I=-14:TP=-1.5:LRA={lra:.1f}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
          f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true")
    inputs, graph = mix(voice, music, sfx, total, 1)
    film = root / "vani_lab_film.mp4"
    ff("-f", "concat", "-safe", "0", "-i", concat, *inputs, "-filter_complex", f"{graph};{pre};[pre]{ln},aresample=48000[a]",
       "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
       "-movflags", "+faststart", film)
    out = measure(["-i", film], "[0:a]anull[mix]")
    print(f"{film}: {len(scenes)} scenes, {int(total // 60)}:{total % 60:04.1f}, "
          f"{out['input_i']} LUFS, {out['input_tp']} dBTP (music: {'yes' if music else 'none'}, effects: {'yes' if sfx else 'none'})")

    plain.OUT = root  # its contact_sheet keeps its scratch frame in OUT/segments
    plain.contact_sheet([s["id"] for s in scenes], segs, [s["frames"] / FPS for s in scenes], root / "contact_sheet.png")
    print(root / "contact_sheet.png")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--draft", action="store_true", help="from video/out/ad/draft/segments (960x540) into video/out/ad/draft/")
    a = ap.parse_args()
    tl = OUT_AD / "timeline.json"
    if not tl.exists():
        sys.exit(f"no {tl}; run video/ad/render.py first")
    music, sfx = OUT_AD / "music.wav", OUT_AD / "sfx.wav"
    for p in (music, sfx):
        if not p.exists():
            print(f"  no {p.relative_to(VIDEO)} yet (music.py): building without it")
    build(json.loads(tl.read_text())["scenes"], OUT_AD / "draft" if a.draft else OUT_AD,
          music if music.exists() else None, sfx if sfx.exists() else None, "960x540" if a.draft else "1920x1080")


if __name__ == "__main__":
    main()
