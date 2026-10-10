"""Films each scene of video/script.yaml into video/out/clips/<id>.webm (1920x1080, 30 fps, its narration plus
1.7 s), with the narration burned in as captions. Card scenes load video/cards/<id>.html; page scenes open the
platform, wait until it has rendered, then play the scene's beats: smooth scrolls, a soft accent spotlight on
what the narration names, and the clicks the scene needs. Frames come from Chrome's screencast (lossless
enough to read), so nothing before the page is ready is ever filmed.

    uv run --with playwright python video/capture.py              # every scene
    uv run --with playwright python video/capture.py 10-fix 13-approve
    uv run --with playwright python video/capture.py --check     # every cue is in the narration; films nothing

Sources: VIDEO_PLATFORM_URL (default http://127.0.0.1:8660) for platform scenes, VIDEO_DEMO_URL (default
http://127.0.0.1:8661) for the demo copy's invented calls (06-call, 08-rubric), and VIDEO_LOOP_URL (default
VIDEO_PLATFORM_URL) for the loop scenes. VIDEO_ACTOR (default kartik) is the name 13-approve types.
VIDEO_CLEAN=1 films the ad cut's clean takes instead (video/out/clips_clean/: no captions), each with <id>.beats.json:
where the spotlight was ([t, x, y, w, h, on] in 1920x1080 px, 20 times a second) and when each beat ran.
Call lists on platform and loop scenes are blurred before the first frame (BLUR).
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

import numpy as np
import yaml
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
FFMPEG = os.environ.get("FFMPEG", "/opt/homebrew/bin/ffmpeg")
PLATFORM = os.environ.get("VIDEO_PLATFORM_URL", "http://127.0.0.1:8660").rstrip("/")
DEMO = os.environ.get("VIDEO_DEMO_URL", "http://127.0.0.1:8661").rstrip("/")
LOOP = os.environ.get("VIDEO_LOOP_URL", PLATFORM).rstrip("/")
ACTOR = os.environ.get("VIDEO_ACTOR", "kartik")
FPS, TAIL = 30, 1.7  # each scene holds 1.7 s past its narration (the film runs past 5 minutes)
CLEAN = os.environ.get("VIDEO_CLEAN") == "1"
CLIPS = OUT / ("clips_clean" if CLEAN else "clips")
# Everything on the real platform that shows a call id, a call summary or call text (docs/demo_script.md,
# "Customer calls on camera"): the Audit page's calls to listen to first, Issues' example calls, Fixes' example
# calls and "Cases that changed", the search palette, the call list, a call page and the rubric test's picker.
BLUR = ", ".join([
    "#palette", "#risky .risk-list", "#risky .see-all", ".ix-ex", ".ix-all", ".fix-links .chip.id", ".fix-cards .chip.id",
    ".calls-card", ".call-sum", "#record", ".card.try select", "a.chip.id[href^='#/calls/']",
])


# --------------------------------------------------------------------------- the narration's clock
def pauses(wav: Path) -> list[tuple[float, float]]:
    """(start, end) of every pause of 0.12 s or more: 20 ms windows quieter than -40 dBFS."""
    with wave.open(str(wav)) as w:
        sr, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(float).reshape(-1, ch).mean(axis=1)
    hop = sr // 50
    db = 20 * np.log10(np.sqrt(np.mean(x[: len(x) // hop * hop].reshape(-1, hop) ** 2, axis=1)) / 32768 + 1e-9)
    out, s = [], None
    for i, quiet in enumerate([*(db < -40), False]):
        if quiet and s is None:
            s = i
        elif not quiet and s is not None:
            if i - s >= 6:
                out.append((s / 50, i / 50))
            s = None
    return out


def chunks(text: str, n: int = 88) -> list[str]:
    """Sentence-sized caption chunks; a long sentence splits at the comma or colon nearest its middle."""
    def split(s):
        if len(s) <= n:
            return [s]
        cuts = [m.end() for m in re.finditer(r"[,:;] ", s)] or [m.start() for m in re.finditer(" ", s)]
        c = min(cuts, key=lambda c: abs(c - len(s) / 2))
        return split(s[:c].strip()) + split(s[c:].strip())
    return [c for s in re.split(r"(?<=[.!?])\s+", text) for c in split(s)]


class Clock:
    """When each chunk of a scene's narration is spoken: chunk lengths spread over the speech, each boundary
    snapped to the nearest real pause in the wav."""

    def __init__(self, text: str, wav: Path, dur: float):
        self.text, self.dur, self.chunks = text, dur, chunks(text)
        ps = pauses(wav)
        start = ps[0][1] if ps and ps[0][0] < 0.05 else 0.0
        end = ps[-1][0] if ps and ps[-1][1] > dur - 0.05 else dur
        inner = [p for p in ps if start + 0.05 < p[0] and p[1] < end - 0.05]
        w = [len(c) + 6 * len(re.findall(r"[,:;]", c)) for c in self.chunks]
        self.spans, cur, rest = [], start, sum(w)
        for i, wi in enumerate(w):
            if i == len(w) - 1:
                self.spans.append((cur, end))
                break
            est = cur + (end - cur) * wi / rest
            rest -= wi
            mid = lambda p: (p[0] + p[1]) / 2  # noqa: E731
            near = [p for p in inner if cur + 0.4 < mid(p) < end - 0.3 and abs(mid(p) - est) < 1.5]
            if near:
                p = max(near, key=lambda p: (p[1] - p[0]) - 0.4 * abs(mid(p) - est))
                self.spans.append((cur, p[0]))
                cur = p[1]
            else:
                self.spans.append((cur, est))
                cur = est
        self.offsets, pos = [], 0
        for c in self.chunks:
            pos = text.index(c, pos)
            self.offsets.append(pos)
            pos += len(c)

    def captions(self) -> list[list]:
        """[from, to, text]: each chunk from the middle of the pause before it to the middle of the next one."""
        out = []
        for i, (c, (a, b)) in enumerate(zip(self.chunks, self.spans)):
            show = 0.0 if i == 0 else (self.spans[i - 1][1] + a) / 2
            out.append([round(show, 3), None, c])
        for i in range(len(out)):
            out[i][1] = out[i + 1][0] if i + 1 < len(out) else self.dur + TAIL
        return out

    def at(self, when) -> float:
        """A beat's time: seconds, a phrase of the narration (when it starts), or (phrase, offset)."""
        if isinstance(when, (int, float)):
            return float(when)
        phrase, off = when if isinstance(when, tuple) else (when, 0.0)
        k = self.text.find(phrase)
        if k < 0:
            sys.exit(f"cue {phrase!r} is not in the narration: {self.text[:80]}...")
        i = max(j for j, o in enumerate(self.offsets) if o <= k)
        a, b = self.spans[i]
        return a + (b - a) * min(1, (k - self.offsets[i]) / max(1, len(self.chunks[i]))) + off


# ------------------------------------------------------------------------------------ the scenes
def api(base: str, path: str):
    """GET a platform route; a static demo copy answers from its demo.json."""
    try:
        with urllib.request.urlopen(base + path, timeout=10) as r:
            if "json" in r.headers.get("content-type", ""):
                return json.load(r)
    except OSError:
        pass
    with urllib.request.urlopen(base + "/demo.json", timeout=10) as r:
        return json.load(r)[path]


def cycle_fix(n: int = 1) -> str:
    c = next((c for c in api(LOOP, "/api/overview")["cycles"] if c["n"] == n), None)
    if not c or not c.get("fix_id"):
        sys.exit(f"cycle {n} has no fix on {LOOP}")
    return c["fix_id"]


def pending() -> str:
    xs = [x for x in api(LOOP, "/api/experiments")["items"] if x.get("cycle") is not None and x["state"] == "PENDING_APPROVAL"]
    if not xs:
        sys.exit(f"no loop experiment is waiting for approval on {LOOP}: restore the snapshot taken before the "
                 "approval (docs/demo_script.md), then film 13-approve again")
    return xs[0]["experiment_id"]


# A page scene: src (platform | demo | loop), path (a str, or a function for ids read off the platform), ready
# (the selector that means the page has rendered), pre (setup before the first frame) and beats: (when, action,
# *args) with `when` in seconds, a phrase of the narration, or (phrase, offset).
PAGES = {
    "05-home": dict(src="loop", path="#/", ready=".ring-wrap svg .st", beats=[
        (0.3, "spot", ".ring-wrap", 4),
        ("cycle one has run", "spot", ".status-main h1"),
        ("Let's follow it", "scroll", "#current", "start", 1400),
        (("Let's follow it", 0.2), "spot", ".stations"),
    ]),
    "06-call": dict(src="demo", path="#/calls/sample-0001", ready="#record .turn", beats=[
        (0.3, "spot", "#player"),
        ("This one is an invented demo call", "spot", "#demo-banner", 0),
        ("The judge grades each call", "scroll", "#scorecard", "start", 1300),
        (("The judge grades each call", 0.2), "spot", ".score-card"),
        ("Every failed grade quotes", "scroll", ".turn[data-k='6']", "center", 1300),
        (("Every failed grade quotes", 0.2), "spot", ".turn[data-k='6']"),
        ("the QA team's own grade", "scroll", "#scorecard", "start", 1300),
        (("the QA team's own grade", 0.2), "spot", ".scorecard tbody tr:nth-child(-n+2)", 8),
    ]),
    "07-audit": dict(src="loop", path="#/audit", ready=".trust", beats=[
        (0.3, "spot", ".trust"),
        ("On six checks", "spot", ".trust-checks", 8),
        ("On a call's overall grade", "spot", ".trust-why", 8),
        ("The platform says so", "scroll", "#by-param", "start", 1300),
        (("The platform says so", 0.2), "spot", ".ag-table"),
    ]),
    "08-rubric": dict(src="demo", path="#/rubrics/new", ready="#form", pre=[
        ("fill", "#form [name=name]", "Confirms the unit back"),
        ("fill", "#form [name=question]", "Did the bot repeat the quantity back with the unit the buyer said?"),
        ("fill", "#form [name=fatal_when]", "The bot records a different unit from the one the buyer said."),
        ("fill", "#form [name=applies_when]", "The buyer gives a quantity."),
        ("chip", ".chips-input[data-k=examples_fail] input", "do sau meter"),
        ("scroll", "#preview", "start"),
    ], beats=[
        (0.2, "spot", "#card"),
        ("test them on a call", "spot", ".card.try"),
        (("test them on a call", 0.1), "select", "#call", "sample-0001"),
        (("test them on a call", 0.9), "click", "#run"),
        (("The next cycle", 0.1), "scroll", "#verdict", "end", 800),
        (("The next cycle", 0.2), "spot", "#verdict"),
    ]),
    "09-issues": dict(src="loop", path="#/issues?version=v1", ready="#ix-list .cause", beats=[
        (0.3, "spot", "#ix-list > .cause:first-child"),
        ("ranked by a published formula", "click", ".ix-how summary"),
        (("ranked by a published formula", 0.9), "spot", ".ix-how"),
        ("Cycle one tried the top cause", "scroll", "#ix-list > .cause:first-child", "start", 1200),
        (("Cycle one tried the top cause", 0.2), "spot", "#ix-list > .cause:first-child .ix-head"),
        ("and took the next", "scroll", "#ix-list > .cause:nth-child(2)", "start", 1100),
        (("and took the next", 0.2), "spot", "#ix-list > .cause:nth-child(2) .ix-head"),
    ]),
    "10-fix": dict(src="loop", path=lambda: f"#/fixes/{cycle_fix(1)}", ready=".redline", beats=[
        (0.3, "spot", ".fix-head h1"),
        ("shown as a redline", "scroll", ".redline .hunk", "start", 1300),
        (("shown as a redline", 0.2), "spot", ".redline .hunk"),
        ("a safety gate running on Jev", "scroll", ".fix-cards", "start", 1300),
        (("a safety gate running on Jev", 0.2), "spot", ".fix-cards > .card:first-child"),
        ("Then the same buyers", "spot", ".fix-cards > .card:nth-child(2)"),
        ("The failure fell", "spot", ".fix-cards .ba"),
        ("and looping got worse", "spot", ".fix-cards > .card:nth-child(2)"),
    ]),
    "11-engine": dict(src="platform", path="#/experiments/vani-meeting-v1-harm", ready="#z svg", beats=[
        (0.3, "spot", ".xhead"),
        ("a sequential test checks", "scroll", "#z", "center", 1300),
        (("a sequential test checks", 0.2), "spot", "#z"),
        ("Here, B secretly lost", "scrolly", 0, 1200),
        (("Here, B secretly lost", 0.2), "spot", ".xsim"),
        ("The engine stopped it", "spot", ".xchips, .xverdict"),
        ("only one lead in ten", "scroll", "#z", "center", 1200),
        (("only one lead in ten", 0.2), "spot", "#z"),
        (("Here, B was really better", -0.5), "goto", "#/experiments/vani-meeting-v1-win", "#z svg"),
        (("Here, B was really better", 0.3), "spot", ".xchips, .xverdict"),
        ("waited for the call duration guardrail", "spot", ".xkpis .kpi:nth-child(2)"),
        ("worth about two hundred and forty", "spot", ".xkpis .kpi:nth-child(5)"),
        (("And when the effect", -0.5), "goto", "#/experiments/vani-meeting-v1-inconclusive", "#z svg", "#z"),
        (("And when the effect", 0.3), "spot", "#z"),
        ("and A stays", "spot", "#z"),
    ]),
    "13-approve": dict(src="loop", path=lambda: f"#/experiments/{pending()}", ready="#decide", beats=[
        (0.3, "spot", ".xsim"),
        (("It crossed the win line", -0.4), "spot", "#decide"),
        (("so it waits for a person", 0.3), "type", "#actor", ACTOR),
        ("The person has seen the replay", "spot", ".card.change"),
        (("So, reject.", -0.7), "click", "#reject"),
        (("So, reject.", -0.2), "raise"),
        (("So, reject.", -0.15), "spot", "dialog.confirm", 6),
        (("So, reject.", 0.55), "click", "dialog.confirm button[value=ok]"),  # the click lands on "reject"
        ("Version one stays", "spot", ".xchips"),
    ]),
    "14-live": dict(src="platform", path="#/experiments/vani-live-c1", ready=".card.change", pre=[
        ("badge", os.environ.get("VIDEO_LIVE_BADGE", "Offline rehearsal · simulated Sarvam · invented calls")),
    ], beats=[
        (0.3, "spot", ".xtitle"),
        ("Here, Vani runs as a Sarvam voice agent", "spot", ".xsim"),
        ("Each lead is hashed", "scroll", "section.section > .card.flush", "center", 1300),
        (("Each lead is hashed", 0.2), "spot", "section.section > .card.flush"),
        ("A rings on agent version one", "scroll", ".card.change", "center", 1200),
        (("A rings on agent version one", 0.2), "spot", ".card.change"),
        ("Every outcome comes back", "scroll", "section.section > .card.flush", "center", 1200),
        (("Every outcome comes back", 0.2), "spot", "section.section > .card.flush table"),
        (("With a handful of calls", -0.3), "unspot"),
        (("With a handful of calls", -0.3), "terminal", OUT / "terminal" / "14-live.txt"),
    ]),
    "16-again": dict(src="loop", path="#/", ready=".ring-wrap svg .st", beats=[
        (0.25, "spot", ".status-main h1"),
        (("to try the next cause", 0.1), "spot", ".ring-wrap", 4),
        (("Everything is documented", -0.3), "goto", "#/docs", ".docs"),
        (("Everything is documented", 0.4), "spot", ".nav a[data-route=docs]", 4),
    ]),
}
CARDS = {"01-title", "02-problem", "03-cost", "04-loop", "12-split", "15-adoption", "17-close"}


# ------------------------------------------------------------------------------------- filming
class Film:
    """Chrome's screencast frames with their timestamps, turned into constant 30 fps."""

    def __init__(self, ctx, page):
        self.frames, self.cdp = [], ctx.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self._frame)

    def _frame(self, p):
        self.frames.append((p["metadata"]["timestamp"], p["data"]))
        try:
            self.cdp.send("Page.screencastFrameAck", {"sessionId": p["sessionId"]})
        except Exception:  # noqa: BLE001 - the session closed while a frame was in flight
            pass

    def start(self):
        self.cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 94, "maxWidth": 1920, "maxHeight": 1080})

    def stop(self):
        self.cdp.send("Page.stopScreencast")

    def write(self, t0: float, seconds: float, path: Path):
        if not self.frames:
            sys.exit(f"{path.stem}: no frames came from the screencast")
        tmp = path.with_suffix(".part.webm")
        p = subprocess.Popen([FFMPEG, "-y", "-loglevel", "error", "-f", "image2pipe", "-c:v", "mjpeg", "-framerate", str(FPS),
                              "-i", "-", "-vf", "scale=1920:1080:flags=lanczos,format=yuv420p", "-c:v", "libvpx-vp9", "-crf", "12",
                              "-b:v", "0", "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1", str(tmp)], stdin=subprocess.PIPE)
        j = 0
        for k in range(round(seconds * FPS)):
            t = t0 + k / FPS
            while j + 1 < len(self.frames) and self.frames[j + 1][0] <= t:
                j += 1
            p.stdin.write(base64.b64decode(self.frames[j][1]))
        p.stdin.close()
        if p.wait():
            sys.exit(f"{path.stem}: ffmpeg failed")
        tmp.replace(path)


def wait_ready(page, ready: str, timeout: int = 30000):
    """Until the view holds a freshly rendered page with `ready` in it and the fonts are in."""
    page.wait_for_function("""(sel) => { const v = document.querySelector('#view > div');
        return v && !v.dataset.vlOld && v.querySelector(sel) && !v.querySelector('.skeleton') && document.fonts.status === 'loaded'; }""",
                           arg=ready, timeout=timeout)


def run(page, act: str, *a):
    if act == "spot":
        if not page.evaluate("([s, p]) => vl.spot(s, p)", [a[0], a[1] if len(a) > 1 else 14]):
            print(f"    spot: nothing matches {a[0]!r}")
    elif act == "unspot":
        page.evaluate("vl.unspot()")
    elif act == "scroll":
        if not page.evaluate("([s, b, ms]) => vl.scroll(s, b, ms)", [a[0], a[1] if len(a) > 1 else "start", a[2] if len(a) > 2 else 1100]):
            print(f"    scroll: nothing matches {a[0]!r}")
    elif act == "scrolly":
        page.evaluate("([y, ms]) => vl.scrollY(y, ms)", [a[0], a[1] if len(a) > 1 else 1100])
    elif act in ("click", "type", "select"):
        page.wait_for_timeout(page.evaluate("(s) => vl.cursor(s, 500)", a[0]) + 40)
        page.evaluate("vl.tap()")
        if act == "click":
            page.click(a[0])
        elif act == "type":
            page.click(a[0])
            page.keyboard.type(a[1], delay=70)
        else:
            page.select_option(a[0], a[1])
    elif act == "raise":
        page.evaluate("vl.raise()")
    elif act == "goto":  # a page change behind the paper curtain, so no loading state is filmed
        page.evaluate("vl.unspot(); vl.curtain(true)")
        page.wait_for_timeout(330)
        page.evaluate("(h) => { const v = document.querySelector('#view > div'); if (v) v.dataset.vlOld = 1; vl.unspot(); location.hash = h; }", a[0])
        wait_ready(page, a[1])
        page.wait_for_timeout(120)
        if len(a) > 2:
            page.evaluate("(s) => vl.scroll(s, 'center', 0)", a[2])
        page.evaluate("vl.curtain(false)")
    elif act == "terminal":
        page.evaluate("([t, ms]) => vl.terminal(t, ms)", [Path(a[0]).read_text(encoding="utf-8"), 2600])
    else:
        sys.exit(f"unknown action {act}")


def setup(page, act: str, *a):
    if act == "fill":
        page.fill(a[0], a[1])
    elif act == "chip":
        page.fill(a[0], a[1])
        page.press(a[0], "Enter")
    elif act == "scroll":
        page.evaluate("([s, b]) => vl.scroll(s, b, 0)", [a[0], a[1]])
    elif act == "badge" and a[0]:  # VIDEO_LIVE_BADGE= (empty) once the live scene films real team calls
        page.evaluate("(t) => vl.badge(t)", a[0])
    page.wait_for_timeout(80)


def film(browser, sid: str, clock: Clock, overlay: str):
    seconds = clock.dur + TAIL
    card = sid in CARDS
    spec = None if card else PAGES[sid]
    if card:
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1, color_scheme="light")
        url = (HERE / "cards" / f"{sid}.html").as_uri() + f"?d={seconds:.2f}&auto=0"
        cfg = {}
    else:  # 1440x810 at 4/3: the platform's own layout, every pixel of text a third larger on a 1080p screen
        ctx = browser.new_context(viewport={"width": 1440, "height": 810}, device_scale_factor=4 / 3, color_scheme="light")
        base = {"platform": PLATFORM, "demo": DEMO, "loop": LOOP}[spec["src"]]
        path = spec["path"]() if callable(spec["path"]) else spec["path"]
        url = f"{base}/{path}"
        cfg = {"blur": BLUR} if spec["src"] != "demo" else {}
    ctx.add_init_script(f"window.VL_CFG = {json.dumps(cfg)};\n{overlay}")
    page = ctx.new_page()
    page.on("pageerror", lambda e: print(f"    page error: {e}"))
    print(f"  {sid}: {url}")
    page.goto(url)
    if card:
        page.wait_for_function("document.fonts.status === 'loaded' && typeof window.play === 'function'")
        cues = page.evaluate("cues()")
        times, last = [], 0.0
        for c in cues:  # a step without a cue follows the one before it
            last = clock.at(c) if c else (last + 0.3 if times else 0.15)
            times.append(round(last, 3))
        page.wait_for_timeout(400)
    else:
        wait_ready(page, spec["ready"])
        page.evaluate("vl.stickyBanner()")  # only where the demo banner shows
        for step in spec.get("pre", []):
            setup(page, *step)
        page.wait_for_timeout(900)
    page.evaluate("vl.raise()")
    page.evaluate("""() => { window.__spotlog = []; setInterval(() => { const el = document.querySelector('.vl-spot');
        if (!el) return; const r = el.getBoundingClientRect();
        window.__spotlog.push([performance.now(), r.x, r.y, r.width, r.height, el.classList.contains('on') ? 1 : 0]); }, 50); }""")
    f = Film(ctx, page)
    f.start()
    page.wait_for_timeout(250)
    t0 = time.time()
    p0 = page.evaluate("([caps, times]) => { vl.start(caps); if (times) window.play(times); return performance.now(); }",
                       [[] if CLEAN else clock.captions(), times if card else None])
    events = []
    for when, *act in sorted(spec["beats"] if spec else [], key=lambda b: clock.at(b[0])):
        at = clock.at(when)
        page.wait_for_timeout(max(0, (t0 + at - time.time()) * 1000))
        events.append([round(at, 3), act[0], act[1] if len(act) > 1 and isinstance(act[1], str) else None])
        run(page, *act)
    page.wait_for_timeout(max(0, (t0 + seconds + 0.3 - time.time()) * 1000))
    spot = page.evaluate("window.__spotlog || []")
    f.stop()
    ctx.close()
    CLIPS.mkdir(parents=True, exist_ok=True)
    f.write(t0, seconds, CLIPS / f"{sid}.webm")
    k = 1920 / (1440 if not card else 1920)  # CSS px to frame px
    (CLIPS / f"{sid}.beats.json").write_text(json.dumps({"events": events, "spot": [
        [round((t - p0) / 1000, 3), *(round(v * k, 1) for v in r), on] for t, *r, on in spot if 0 <= (t - p0) / 1000 <= seconds]}))
    print(f"  {sid}: {seconds:.2f} s, {len(f.frames)} frames from the screencast")


def check(scenes, durations) -> None:
    """Every cue, of every beat and every card step, is a phrase its scene's narration says (no browser)."""
    for s in scenes:
        clock = Clock(" ".join(s["say"].split()), OUT / "audio" / f"{s['id']}.wav", durations[s["id"]])
        if s["id"] in CARDS:
            cues = re.findall(r'data-cue="([^"]+)"', (HERE / "cards" / f"{s['id']}.html").read_text(encoding="utf-8"))
        else:
            cues = [b[0] for b in PAGES[s["id"]]["beats"]]
        times = [clock.at(c) for c in cues]  # exits on a cue the narration does not say
        print(f"{s['id']}: {len(cues)} cues, {len(clock.chunks)} captions, last cue at {max(times, default=0):.1f} of {clock.dur:.1f} s")


def main(argv: list[str]) -> None:
    scenes = yaml.safe_load((HERE / "script.yaml").read_text(encoding="utf-8"))["scenes"]
    durations = json.loads((OUT / "audio" / "durations.json").read_text())
    if argv == ["--check"]:
        return check(scenes, durations)
    want = argv or [s["id"] for s in scenes]
    if unknown := set(want) - {s["id"] for s in scenes}:
        sys.exit(f"no such scene: {', '.join(sorted(unknown))}")
    overlay = (HERE / "overlay.js").read_text(encoding="utf-8")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception:  # noqa: BLE001 - no Google Chrome: Playwright's cached Chromium
            browser = p.chromium.launch()
        try:
            for s in scenes:
                if s["id"] in want:
                    film(browser, s["id"], Clock(" ".join(s["say"].split()), OUT / "audio" / f"{s['id']}.wav", durations[s["id"]]), overlay)
        finally:
            browser.close()


if __name__ == "__main__":
    main(sys.argv[1:])
