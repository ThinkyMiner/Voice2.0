"""Films the ad's scenes frame by frame: each scene page (video/ad/scenes/<id>.html, walk scenes through
scenes/walk.html?id=<id>) runs in headless Chrome with window.__RENDERER set, and for every frame i render.py calls
__frame(i / 30), screenshots the page and pipes the JPEG to ffmpeg, so a segment never depends on how fast the
page draws. Output is video only, exactly AD.frames frames: video/out/ad/segments/<id>.mp4 (--draft: 960x540 into
video/out/ad/draft/segments/). Each scene's sound effects (window.__sfx()) go to video/out/ad/sfx/<id>.json.

Walk scenes first get their clip as JPEGs, once: video/out/clips_clean/<clip>.webm (else the captioned
video/out/clips/<clip>.webm) -> video/out/ad/frames/<clip>/00001.jpg, 00002.jpg ... at 30 fps, file k at (k - 1) / 30 s
of the clip (the clip's 0 is the narration's 0). AD.frameDir ("out/ad/frames/<clip>/", relative to video/) and
AD.nframes say where; video/out/ad/timeline.json carries them too, for previews.

A scene is filmed again only when its page, a shared file of video/ad (runtime.js, ad.css, ad-kit.js ...) or
video/cards, its frames or its timeline entry changed since its segment was made, or when it is named.

    uv run --with playwright python video/ad/render.py                  # every scene out of date
    uv run --with playwright python video/ad/render.py ad-open walk-fix # these, always
    uv run --with playwright python video/ad/render.py --jobs 4 --draft
"""
import argparse
import asyncio
import base64
import functools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.async_api import async_playwright

AD = Path(__file__).resolve().parent
VIDEO = AD.parent
OUT = VIDEO / "out"
OUT_AD = OUT / "ad"
FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")
sys.path.insert(0, str(AD))
import timeline  # noqa: E402  (the narration's timing; needs playwright, through capture.py)

FPS = timeline.FPS
# Chrome's JPEGs are full-range BT.601; the film is tagged BT.709 limited range, the HD default every player assumes.
# aq-mode 3 spends bits on dark flat areas, where a near-black stage would otherwise band.
X264 = ["-vf", "scale=in_range=full:in_color_matrix=bt601:out_range=tv:out_color_matrix=bt709,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-x264-params", "aq-mode=3",
        "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv", "-r", str(FPS), "-an"]
CHROME_ARGS = ["--force-color-profile=srgb", "--hide-scrollbars", "--font-render-hinting=none",
               "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
               "--disable-backgrounding-occluded-windows"]


def ff(*args) -> None:
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-nostdin", *map(str, args)], check=True)


# ------------------------------------------------------------------------------------ inputs
def frames(clip: str) -> tuple[str, int]:
    """A walk scene's clip as JPEGs at 30 fps, extracted again only when the clip is newer (or another take)."""
    src = next((p for p in (OUT / "clips_clean" / f"{clip}.webm", OUT / "clips" / f"{clip}.webm") if p.exists()), None)
    if src is None:
        sys.exit(f"{clip}: no clip; film it with VIDEO_CLEAN=1 uv run --with playwright python video/capture.py {clip}")
    d = OUT_AD / "frames" / clip
    done = d / ".done"  # holds the source it was made from
    if not (done.exists() and done.read_text() == str(src) and done.stat().st_mtime > src.stat().st_mtime):
        if src.parent.name == "clips":
            print(f"  {clip}: no clean take in out/clips_clean, using the captioned clip")
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
        ff("-i", src, "-vf", f"fps={FPS}", "-q:v", "2", d / "%05d.jpg")
        done.write_text(str(src))
    return f"out/ad/frames/{clip}/", sum(1 for _ in d.glob("*.jpg"))


def load() -> list[dict]:
    """The timeline, with frameDir/nframes on walk scenes; video/out/ad/timeline.json rewritten only if it changed."""
    scenes = timeline.load()
    for s in scenes:
        if s["kind"] == "walk":
            s["frameDir"], s["nframes"] = frames(s["clip"])
    doc = json.dumps({"fps": FPS, "total": round(scenes[-1]["start"] + scenes[-1]["dur"], 3), "scenes": scenes}, indent=1)
    f = OUT_AD / "timeline.json"
    if not f.exists() or f.read_text() != doc:
        f.write_text(doc)
    return scenes


def page_of(s: dict) -> Path:
    return AD / "scenes" / ("walk.html" if s["kind"] == "walk" else f"{s['id']}.html")


def stamp(s: dict) -> str:
    """What a segment was filmed from: its timeline entry, less where the scene starts in the film."""
    return json.dumps({k: v for k, v in s.items() if k != "start"}, sort_keys=True)


def fresh(s: dict, seg: Path) -> bool:
    st = seg.with_suffix(".json")
    if not (seg.exists() and st.exists() and st.read_text() == stamp(s)):
        return False
    deps = [page_of(s), *(f for d in (AD, VIDEO / "cards") for f in d.rglob("*")
                          if f.is_file() and f.suffix not in (".html", ".py", ".pyc", ".md", ".yaml"))]
    if s["kind"] == "walk":
        deps.append(OUT_AD / "frames" / s["clip"] / ".done")
    return seg.stat().st_mtime > max(f.stat().st_mtime for f in deps)


# ----------------------------------------------------------------------------------- filming
async def film(browser, base: str, s: dict, segs: Path, draft: bool) -> list[str]:
    """One scene into segs/<id>.mp4; returns the page's errors and warnings."""
    issues = []
    ctx = await browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1)
    try:
        await ctx.add_init_script(f"window.__RENDERER = true; window.AD = {json.dumps(s)};")
        page = await ctx.new_page()
        page.on("pageerror", lambda e: issues.append(f"page error: {e}"))
        page.on("console", lambda m: m.type in ("error", "warning") and not m.text.startswith("Failed to load resource")
                and issues.append(f"console {m.type}: {m.text}"))  # a failed load is reported with its url below
        page.on("response", lambda r: r.status >= 400 and not r.url.endswith("/favicon.ico") and issues.append(f"HTTP {r.status}: {r.url}"))
        page.on("requestfailed", lambda r: issues.append(f"request failed: {r.url}"))
        cdp = await ctx.new_cdp_session(page)
        if draft:  # Playwright's device_scale_factor stops at 1; CDP's goes lower, so the page draws a quarter of the pixels
            await cdp.send("Emulation.setDeviceMetricsOverride", {"width": 1920, "height": 1080, "deviceScaleFactor": 0.5, "mobile": False})
        url = f"{base}/ad/scenes/" + (f"walk.html?id={s['id']}" if s["kind"] == "walk" else f"{s['id']}.html")
        await page.goto(url)
        await asyncio.wait_for(page.evaluate("window.__ready()"), 60)
        part = segs / f"{s['id']}.part.mp4"
        enc = await asyncio.create_subprocess_exec(
            FFMPEG, "-y", "-loglevel", "error", "-nostdin", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(FPS),
            "-i", "-", *X264, str(part), stdin=asyncio.subprocess.PIPE)
        t0 = time.perf_counter()
        try:
            for i in range(s["frames"]):
                await page.evaluate("(t) => window.__frame(t)", i / FPS)
                shot = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 92, "optimizeForSpeed": True})
                enc.stdin.write(base64.b64decode(shot["data"]))
                await enc.stdin.drain()
        except BaseException:
            enc.stdin.close()
            await enc.wait()
            part.unlink(missing_ok=True)
            raise
        enc.stdin.close()
        if await enc.wait():
            raise RuntimeError("ffmpeg failed")
        ms = (time.perf_counter() - t0) * 1000 / s["frames"]
        part.replace(segs / f"{s['id']}.mp4")
        (segs / f"{s['id']}.json").write_text(stamp(s))
        (OUT_AD / "sfx").mkdir(exist_ok=True)
        (OUT_AD / "sfx" / f"{s['id']}.json").write_text(json.dumps(await page.evaluate("window.__sfx()")))
        print(f"  {s['id']:<13} {s['frames']:4d} frames  {ms:5.0f} ms/frame")
    finally:
        await ctx.close()
    return issues


async def run(scenes: list[dict], jobs: int, draft: bool) -> int:
    segs = OUT_AD / ("draft/segments" if draft else "segments")
    segs.mkdir(parents=True, exist_ok=True)

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(VIDEO)))
    srv.handle_error = lambda *a: None  # a request the browser dropped mid-way
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base, gate, failed = f"http://127.0.0.1:{srv.server_port}", asyncio.Semaphore(jobs), []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chrome", args=CHROME_ARGS)
        except Exception:  # noqa: BLE001 - no Google Chrome: Playwright's cached Chromium
            browser = await p.chromium.launch(args=CHROME_ARGS)

        async def one(s):
            async with gate:
                try:
                    issues = await film(browser, base, s, segs, draft)
                except Exception as e:  # noqa: BLE001 - report it, film the rest
                    issues = [f"FAILED: {type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"]
                    failed.append(s["id"])
                for m in dict.fromkeys(issues):  # once each
                    print(f"    {s['id']}: {m}")

        # the longest first, so the last job to finish is a short one
        await asyncio.gather(*(one(s) for s in sorted(scenes, key=lambda s: -s["frames"])))
        await browser.close()
    srv.shutdown()
    if failed:
        print(f"failed: {', '.join(failed)}")
    return 1 if failed else 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="*", help="scenes to film even if up to date (default: every scene out of date)")
    ap.add_argument("--jobs", type=int, default=1, help="scenes filmed at once")
    ap.add_argument("--draft", action="store_true", help="960x540 into video/out/ad/draft/")
    a = ap.parse_args()
    scenes = load()
    if unknown := set(a.ids) - {s["id"] for s in scenes}:
        sys.exit(f"no such scene: {', '.join(sorted(unknown))}")
    if missing := [i for i in a.ids if not page_of(next(s for s in scenes if s["id"] == i)).exists()]:
        sys.exit(f"no page yet: {', '.join(missing)}")
    segs = OUT_AD / ("draft/segments" if a.draft else "segments")
    todo = []
    for s in scenes:
        if a.ids and s["id"] not in a.ids:
            continue
        if not page_of(s).exists():
            print(f"  {s['id']:<13} no page yet ({page_of(s).relative_to(VIDEO)})")
        elif not a.ids and fresh(s, segs / f"{s['id']}.mp4"):
            print(f"  {s['id']:<13} up to date")
        else:
            todo.append(s)
    t0 = time.time()
    code = asyncio.run(run(todo, max(1, a.jobs), a.draft)) if todo else 0
    print(f"{len(todo)} scenes filmed in {time.time() - t0:.0f} s  ->  {segs}")
    sys.exit(code)


if __name__ == "__main__":
    main()
