"""Recordings -> role-labelled transcripts (PLAN_LOOP.md §L2.3, §L4 L1, §L9).

`download` fetches the recordings (URLs cannot be passed to batch STT, so the worker downloads
first). `transcribe` sends them to the provider in chunks of 20 (one batch job each; the provider
holds the job-level concurrency, poll interval, back-off and per-file cache), labels roles, and
appends one Transcript JSON line per call to a file. That file is the resume point: a second run
skips every call_id already in it, so an interrupted or partly failed run costs nothing twice.
Transcripts are personal data and live under data/derived/loop/ (gitignored), never in reports.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pandas as pd

from abtest.loop import roles
from abtest.loop.contracts import LOOP_DATA_DIR, RECORDINGS_DIR, Provider, Rubric, Transcript
from abtest.loop.sarvam import ProviderError

log = logging.getLogger(__name__)

CHUNK = 20            # files per batch job (Sarvam's limit)
TRIES = 3             # download attempts per recording
BACKOFF_S = (1, 2)    # sleep before the 2nd and 3rd attempt
WORKERS = 4           # parallel downloads
_RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _sleep(seconds: float) -> None:
    """Indirection so tests can run the retries without waiting."""
    time.sleep(seconds)


def _make_client() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0), follow_redirects=True)


def _fetch(client: httpx.Client, url: str, dest: Path) -> str | None:
    """Download `url` to `dest` (via a temp file, so a crash never leaves a torn mp3). Returns None on
    success, else a short error. The URL is never put in the error: recording links may carry a signature."""
    error = "no attempt"
    for attempt in range(TRIES):
        try:
            r = client.get(url)
        except httpx.HTTPError as e:
            error = f"{type(e).__name__}"
        else:
            if r.status_code == 200 and r.content:
                tmp = dest.with_name(dest.name + ".part")
                tmp.write_bytes(r.content)
                tmp.replace(dest)
                return None
            error = f"HTTP {r.status_code}" if r.status_code != 200 else "empty body"
            if r.status_code not in _RETRY_STATUS and r.status_code != 200:
                return error  # 403/404 will not heal on retry
        if attempt < TRIES - 1:
            _sleep(BACKOFF_S[attempt])
    return f"{error} after {TRIES} tries"


def download(calls: pd.DataFrame, out_dir: Path = RECORDINGS_DIR) -> pd.DataFrame:
    """Adds `audio_path` (None on failure) and `download_error`; skips files already present.
    Each recording goes to out_dir/<call_id>.mp3; 3 tries per file with a short back-off, 4 files at a time."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    result = calls.copy()
    paths: list[str | None] = [None] * len(result)
    errors: list[str | None] = [None] * len(result)
    todo: list[tuple[int, str, Path]] = []
    for i, (call_id, url) in enumerate(zip(result["call_id"], result.get("recording_url", [None] * len(result)))):
        dest = out_dir / f"{call_id}.mp3"
        if dest.exists() and dest.stat().st_size > 0:
            paths[i] = str(dest)
        elif not isinstance(url, str) or not url.strip():
            errors[i] = "no recording_url"
        else:
            todo.append((i, url.strip(), dest))
    if todo:
        with _make_client() as client, ThreadPoolExecutor(max_workers=WORKERS) as pool:
            outcomes = list(pool.map(lambda t: _fetch(client, t[1], t[2]), todo))
        for (i, _, dest), err in zip(todo, outcomes):
            if err is None:
                paths[i] = str(dest)
            else:
                errors[i] = err
    result["audio_path"] = pd.Series(paths, index=result.index, dtype=object)
    result["download_error"] = pd.Series(errors, index=result.index, dtype=object)
    return result


def _read_done(out: Path) -> dict[str, Transcript]:
    done: dict[str, Transcript] = {}
    if not out.exists():
        return done
    for line in out.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            t = Transcript.model_validate_json(line)
        except ValueError:
            continue  # a torn last line from an interrupted run: that call is simply redone
        done[t.call_id] = t
    return done


def _ensure_trailing_newline(out: Path) -> None:
    """A run killed mid-write leaves a torn last line without its newline; the next append must not
    glue a good record onto it."""
    if out.exists() and out.stat().st_size > 0:
        with out.open("rb+") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                f.write(b"\n")


def transcribe(calls: pd.DataFrame, provider: Provider, rubric: Rubric | None = None,
               min_duration_s: float = 10.0, out: Path = LOOP_DATA_DIR / "transcripts.jsonl") -> list[Transcript]:
    """Transcribes calls >= min_duration_s with a downloaded file, labels roles, appends to `out`
    (one Transcript JSON per line), resumes by skipping call_ids already in `out`.

    Roles use roles.SELLER_ANCHORS (the corpus is seller-side; no prompt is given). `rubric` is
    unused: it is kept for the contract signature. Returns the transcripts of every eligible call
    in the order of `calls`, both the ones already in `out` and the new ones. A chunk the provider
    fails on is logged and skipped (a rerun retries it); SpendCapExceeded is never swallowed."""
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = _read_done(out)
    _ensure_trailing_newline(out)

    paths = calls["audio_path"] if "audio_path" in calls.columns else pd.Series([None] * len(calls), index=calls.index)
    eligible: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for call_id, duration, path in zip(calls["call_id"], calls["duration_s"], paths):
        cid = str(call_id)
        if cid in seen or pd.isna(duration) or float(duration) < min_duration_s:
            continue
        if not isinstance(path, str) or not Path(path).exists():
            continue
        seen.add(cid)
        eligible.append((cid, Path(path)))

    todo = [(cid, p) for cid, p in eligible if cid not in done]
    failed: list[str] = []
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        try:
            diarized = provider.transcribe_batch([p for _, p in chunk], [c for c, _ in chunk])
        except ProviderError as e:
            failed += [c for c, _ in chunk]
            log.warning("transcription failed for a chunk of %d calls (%s); a rerun retries them", len(chunk), e)
            continue
        fresh = []
        for d in diarized:
            if not d.segments:
                failed.append(d.call_id)  # no speech came back; not written, so a rerun retries it
                continue
            fresh.append(roles.label_roles(d, roles.SELLER_ANCHORS, source="corpus"))
        if fresh:
            with out.open("a", encoding="utf-8") as f:
                for t in fresh:
                    f.write(t.model_dump_json() + "\n")
            done.update({t.call_id: t for t in fresh})
    if failed:
        log.warning("%d of %d eligible calls have no transcript", len(failed), len(eligible))
    return [done[cid] for cid, _ in eligible if cid in done]
