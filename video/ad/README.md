# The ad cut

The demo as a premium product film: motion-graphics scenes around the same platform walkthrough, on a dark
stage. `timeline.yaml` lists the scenes, their narration (`say`, or a `clip`'s narration from `../script.yaml`)
and their art direction (`show`). Output goes to `video/out/ad/` (git-ignored). Scenes join with hard cuts, so
every scene starts from and ends on the plain stage.

| Step | What it makes |
|---|---|
| `VIDEO_SCRIPT=video/ad/timeline.yaml uv run python video/tts.py` | `out/audio/<id>.wav` for the ad's own lines |
| `VIDEO_CLEAN=1 uv run --with playwright python video/capture.py <clip>` | `out/clips_clean/<clip>.webm` and `.beats.json`: the platform, no captions |
| `timeline.py` | `out/ad/timeline.json`: per scene start, dur, frames, lead, narr, pad, cues, caps, beats |
| `render.py` | `out/ad/segments/<id>.mp4` (video only) and `out/ad/sfx/<id>.json`, every frame filmed from its page |
| `music.py` | `out/ad/music.wav` and `out/ad/sfx.wav` |
| `build.py` | `out/ad/vani_lab_film.mp4` and `out/ad/contact_sheet.png` |

## Scene pages

`scenes/<id>.html` for each motion scene, `scenes/walk.html?id=<id>` for every walk scene. Each follows
`runtime.js`: `scene({init, render, sfx})`, where `render(t)` draws the frame at t seconds from t alone (no CSS
transitions, timers or `performance.now()`), and `cue("phrase")` is when the narration says that phrase (run
timeline.py after adding one). Preview a page from `video/` served statically:

```bash
cd video && python3 -m http.server 8684 --bind 127.0.0.1
# http://127.0.0.1:8684/ad/scenes/ad-open.html          plays in real time, looping
# http://127.0.0.1:8684/ad/scenes/ad-open.html?t=4.2    frozen at 4.2 s
# http://127.0.0.1:8684/ad/scenes/walk.html?id=walk-fix&t=6
```

## Render

From the repo root:

```bash
uv run --with playwright python video/ad/render.py --jobs 4 --draft    # 960x540 into out/ad/draft/, a quick look
uv run python video/ad/build.py --draft                                # out/ad/draft/vani_lab_film.mp4
uv run --with playwright python video/ad/render.py --jobs 4            # 1920x1080
uv run python video/ad/build.py
```

**render.py** rewrites `timeline.json` (only when it changed), then films every scene that is out of date:
its segment is older than its page, a shared file in `video/ad` or `video/cards` (runtime.js, ad.css,
ad-kit.js ...) or its frames, or its timeline entry changed. Scenes named on the command line are filmed
regardless. Each frame is `__frame(i / 30)` then a JPEG screenshot (CDP, quality 92) piped to ffmpeg (x264
crf 16, aq-mode 3 for the dark stage, tagged BT.709), exactly `AD.frames` frames; it prints ms per frame and
each page's errors, warnings and failed requests. Walk scenes first get their clip as JPEGs, once:
`out/clips_clean/<clip>.webm`, else the captioned `out/clips/<clip>.webm`, into `out/ad/frames/<clip>/00001.jpg`
... (file k is (k - 1) / 30 s of the clip; extracted again when the clip is newer), passed as `AD.frameDir`
and `AD.nframes` (in timeline.json too, for previews). They take about 1.6 GB of disk for all walk scenes.
Google Chrome is used if installed, else Playwright's Chromium; pages need the network for Google Fonts.

**build.py** joins the segments in timeline order without re-encoding; a scene not filmed yet holds the plain
stage (`out/ad/segments/_hold/`) so everything after it still lines up. The narration is each scene's wav at its
first frame + lead. Stems are levelled alone (voice -16, effects -21, music -24 LUFS; `VOICE`, `SFX`, `MUSIC`),
the music is ducked under the voice (sidechain compressor keyed by the narration: threshold 0.03, ratio 6,
attack 20 ms, release 400 ms), and the master goes to -14 LUFS, -1.5 dBTP: one gain and a -3 dBFS limiter, then
a two-pass linear loudnorm. AAC 256k, 48 kHz stereo, faststart. Without `music.wav` or `sfx.wav` it builds with
what there is and says so. It prints the film's measured loudness and true peak.
