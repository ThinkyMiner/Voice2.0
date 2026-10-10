# The demo video

One story in 17 scenes (`script.yaml`): the problem, the loop, why it makes a prompt change adoptable, and
the A/B split on real calls through a Sarvam voice agent playing Vani. Each scene has `say` (the narration)
and `show` (what is on screen). Output goes to `video/out/` (git-ignored).

| Step | What it makes |
|---|---|
| `tts.py` | `out/audio/<id>.wav` and `durations.json`: the narration, Sarvam Bulbul v3, cached per scene |
| `capture.py` | `out/clips/<id>.webm`: each scene filmed at 1920x1080, 30 fps, its narration plus 0.6 s, captions burned in |
| `build.py` | `out/segments/<id>.mp4`, `out/vani_lab_demo.mp4` (0.35 s crossfades) and `out/contact_sheet.png` |

Seven scenes are cards (`cards/<id>.html`: 01, 02, 03, 04, 12, 15, 17), in the platform's own type and
colours; open one in a browser to see it play (`?d=` sets its length in seconds). 12-split's figures are
`reports/split_win.md` at the pinned seed (1101); if the seed is re-pinned, update the card. The other ten film the
platform. `overlay.js` is what capture.py injects into every page: the captions, the spotlight, the cursor,
the paper curtain between pages, the terminal card and the blur list. Nothing before a page has rendered is
filmed: frames come from Chrome's screencast, started only once the page is ready.

## Sources

| Variable | Default | Scenes |
|---|---|---|
| `VIDEO_PLATFORM_URL` | `http://127.0.0.1:8660` | 11-engine, 14-live |
| `VIDEO_LOOP_URL` | `VIDEO_PLATFORM_URL` | 05-home, 07-audit, 09-issues, 10-fix, 13-approve, 16-again |
| `VIDEO_DEMO_URL` | `http://127.0.0.1:8661` | 06-call, 08-rubric (invented calls only) |
| `VIDEO_ACTOR` | `kartik` | the name 13-approve types on the decision log |

On every platform and loop scene, everything that shows a call id, a call summary or call text is blurred
before the first frame (`BLUR` in capture.py): the Audit page's calls to listen to first, Issues' example
calls, Fixes' example calls and "Cases that changed", the search palette, call lists and the rubric test's
picker. 14-live ends on a terminal card typing `out/terminal/14-live.txt`.

**13-approve clicks Approve B for real** on whatever `VIDEO_LOOP_URL` serves: on a platform it makes Prompt
B production and opens the next cycle. Film it on a copy, never on the live platform, and keep the copy's
snapshot from before the approval for a retake (docs/demo_script.md). Scenes are filmed in script order, so
09-issues and 10-fix are filmed before the approval and 16-again after it.

## Render

From the repo root, with the sources running:

```bash
python3 -m http.server -d src/abtest/ext/web 8661 --bind 127.0.0.1    # the demo copy
nice -n 10 uv run uvicorn abtest.ext.service:app --host 127.0.0.1 --port 8660   # a platform (a copy)

uv run python video/tts.py                                     # only scenes whose text changed are spoken again
uv run --with playwright python video/capture.py --check       # every cue is still in the narration
nice -n 10 uv run --with playwright python video/capture.py    # every scene, about 6 minutes
nice -n 10 uv run python video/build.py                        # about 4 minutes
```

capture.py uses the installed Google Chrome, else Playwright's cached Chromium. Playwright is not a project
dependency: `uv run --with playwright` brings it for the run.

**One scene again:** `uv run --with playwright python video/capture.py 10-fix`, then `build.py` (it remakes
only the segments whose clip or wav is newer, then joins them all).

**Changed narration:** edit `say` in script.yaml, run tts.py (it re-speaks only that scene), `capture.py
--check`, film that scene, build. A beat's cue is a phrase of its scene's `say` (capture.py `PAGES`, a
card's `data-cue`): if the words change, change the cue.

## The draft (sandbox sources)

No real data: the demo copy on 8661, and on 8660 a platform from this worktree with its own store: the
provided read-only files symlinked into `data/` (never `data/live` or `data/derived`), `make demo ACTOR=vani`
for the engine scenarios, and a fake live experiment (`fake_live.py`: invented numbers, the test suite's fake
Sarvam, offline; it refuses to run unless the store is this worktree's, there is no `.env` and both proxies
point at the dead `http://127.0.0.1:9`):

```bash
HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 uv run python video/fake_live.py
VIDEO_LOOP_URL=http://127.0.0.1:8661 nice -n 10 uv run --with playwright python video/capture.py
```

The loop scenes then come from the demo copy (cycle 3, v2 in production), so their figures and wording
differ from the narration until the final render.

## The final render (from a copy of the real platform)

1. After the real run (docs/runbook.md, step 3), with cycle 1 at PENDING_APPROVAL and the platform
   stopped, make a copy to film: a separate checkout (a git worktree) with the provided data files
   symlinked into its `data/` as for the draft, and the platform's state copied in (`abtest.db*`,
   `data/derived/loop`, `logs/decisions.jsonl`). Serve the copy on its own port, e.g. 8662, without
   `LOOP_ALLOW_REAL`. It needs the engine scenarios (`vani-meeting-v1-harm`, `-win`, `-inconclusive`) and
   `vani-live-c1`.
2. Fill the narration's placeholders in script.yaml from that copy's screens: 09-issues `<cause>` and `<n>`
   (Issues, the top prompt cause and its calls), 10-fix `<x>` and `<y>` (the fix's replay, before and
   after). Run tts.py and `capture.py --check`.
3. Save the real `make calls ARGS=status` output as `video/out/terminal/14-live.txt` (first line
   `$ make calls ARGS=status`).
4. `VIDEO_PLATFORM_URL=http://127.0.0.1:8662 nice -n 10 uv run --with playwright python video/capture.py`
   (the demo copy still serves 06 and 08), then `build.py`. Check the frames: `ffmpeg -i
   video/out/vani_lab_demo.mp4 -vf fps=1/4 /tmp/frames/f%03d.png`.
