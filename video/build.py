"""Builds the demo video from the filmed clips and the narration: each scene's clip and wav become an H.264 +
AAC segment at 30 fps (video/out/segments/<id>.mp4, remade only when its clip or wav is newer), the segments
join with 0.35 s crossfades (video and audio) into video/out/vani_lab_demo.mp4, and video/out/contact_sheet.png
shows one frame from the middle of each scene, labelled with its id. Captions are already in the clips (this
ffmpeg has no drawtext or subtitles filter).

    uv run python video/build.py
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")
FPS, TAIL, XF = 30, 0.6, 0.35


def ff(*args) -> None:
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-nostdin", *map(str, args)], check=True)


def segment(sid: str, dur: float) -> Path:
    clip, wav, seg = OUT / "clips" / f"{sid}.webm", OUT / "audio" / f"{sid}.wav", OUT / "segments" / f"{sid}.mp4"
    if not clip.exists():
        sys.exit(f"{sid}: no clip; run video/capture.py {sid}")
    if seg.exists() and seg.stat().st_mtime > max(clip.stat().st_mtime, wav.stat().st_mtime):
        return seg
    seg.parent.mkdir(parents=True, exist_ok=True)
    ff("-i", clip, "-i", wav, "-filter_complex",
       f"[0:v]fps={FPS},tpad=stop_mode=clone:stop_duration=1,trim=duration={dur:.3f},setpts=PTS-STARTPTS,format=yuv420p[v];"
       f"[1:a]aresample=48000,apad,atrim=duration={dur:.3f},asetpts=PTS-STARTPTS[a]",
       "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "medium", "-crf", "15", "-r", FPS,
       "-c:a", "aac", "-b:a", "192k", "-ac", "2", seg)
    return seg


def join(segs: list[Path], durs: list[float], out: Path) -> float:
    fc, v, a, t = [], "[0:v]", "[0:a]", durs[0]
    for i in range(1, len(segs)):
        off = t - XF
        fc += [f"{v}[{i}:v]xfade=transition=fade:duration={XF}:offset={off:.3f}[v{i}]",
               f"{a}[{i}:a]acrossfade=d={XF}:c1=tri:c2=tri[a{i}]"]
        v, a, t = f"[v{i}]", f"[a{i}]", off + durs[i]
    fc += [f"{v}fade=t=in:st=0:d=0.5,fade=t=out:st={t - 0.9:.3f}:d=0.9,format=yuv420p[vo]",
           f"{a}afade=t=in:st=0:d=0.2,afade=t=out:st={t - 0.9:.3f}:d=0.9[ao]"]
    ff(*[x for s in segs for x in ("-i", s)], "-filter_complex", ";".join(fc), "-map", "[vo]", "-map", "[ao]",
       "-c:v", "libx264", "-preset", "slow", "-crf", "17", "-r", FPS, "-c:a", "aac", "-b:a", "192k",
       "-movflags", "+faststart", out)
    return t


def contact_sheet(ids: list[str], segs: list[Path], durs: list[float], out: Path) -> None:
    W, H, BAR, COLS = 480, 270, 34, 4
    rows = -(-len(ids) // COLS)
    sheet = Image.new("RGB", (COLS * W + (COLS + 1) * 12, rows * (H + BAR) + (rows + 1) * 12), "#F5F2EA")
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 17)
    except OSError:
        font = ImageFont.load_default()
    tmp = OUT / "segments" / "_mid.png"
    for k, (sid, seg, d) in enumerate(zip(ids, segs, durs)):
        ff("-ss", f"{d / 2:.2f}", "-i", seg, "-frames:v", "1", "-vf", f"scale={W}:{H}", tmp)
        x, y = 12 + (k % COLS) * (W + 12), 12 + (k // COLS) * (H + BAR + 12)
        sheet.paste(Image.open(tmp).convert("RGB"), (x, y))
        ImageDraw.Draw(sheet).rectangle([x, y + H, x + W, y + H + BAR], fill="#16140F")
        ImageDraw.Draw(sheet).text((x + 10, y + H + 8), f"{sid}  ·  {d:.1f} s", fill="#EDEAE2", font=font)
    tmp.unlink(missing_ok=True)
    sheet.save(out)


def main() -> None:
    ids = [s["id"] for s in yaml.safe_load((HERE / "script.yaml").read_text(encoding="utf-8"))["scenes"]]
    narr = json.loads((OUT / "audio" / "durations.json").read_text())
    durs = [round(narr[i] + TAIL, 3) for i in ids]
    segs = [segment(i, d) for i, d in zip(ids, durs)]
    total = join(segs, durs, OUT / "vani_lab_demo.mp4")
    contact_sheet(ids, segs, durs, OUT / "contact_sheet.png")
    print(f"{OUT / 'vani_lab_demo.mp4'}: {len(ids)} scenes, {int(total // 60)}:{total % 60:04.1f}")
    print(f"{OUT / 'contact_sheet.png'}")


if __name__ == "__main__":
    main()
