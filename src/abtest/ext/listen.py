"""Listen, the loop's first stage: call recordings -> diarised Transcripts (see ext/models.py).

Two call sets feed it: `production_calls()`, today's sample on prompt v1, and `audited_calls()`, the
SquadStack inbound calls IndiaMART's QA team graded, whose grades the judge is measured against.

Recordings, transcripts and QA remarks are real customer data: they stay under LOOP_DIR (git-ignored),
and the CLI sends audio to the model provider only with --yes-send-real-audio, the user's decision.

    python -m abtest.ext.listen --source production|audited [--limit N] [--workers 6] [--ids a,b]
                                [--model M] --yes-send-real-audio
"""

import argparse
import csv
import math
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx

from abtest.ext import llm
from abtest.ext.models import (AUDIT_CSV, CALLS_CSV, RECORDINGS_DIR, TRANSCRIPTS_DIR, CallSource, Grade,
                               Transcript, Turn, load, save)

VENDOR = "SQUADSTACK"                  # both call sets; Sarvam's audit rows have no downloadable audio
SAFE_META = ("bot", "hotlead_type")    # all of a call's metadata a Transcript may keep
TIME_SLACK_S = 1.0                     # turns may start this much before the previous one (crosstalk)
PHONE = re.compile(r"\+?\d(?:[\s().-]{0,2}\d){9,}")   # 10+ digits: a phone number, never a quantity
LANGUAGES = ("hi", "en", "te", "mixed")
NATIVE_SCRIPT = re.compile("[ऀ-ॿఀ-౿]")  # Devanagari, Telugu: auditors write Latin

QA_COLUMNS = {  # normalised QA checklist column -> its remarks column, in the sheet's own spelling
    "Outcome Accuracy (Intent)": "Outcome Accuracy Remarks",
    "Quantity Accuracy (Numeric)": "Quantity Accuracy (Numeric) Remarks",
    "Quantity Unit Accuracy": "Quantity Unit Accuracy Remarks",
    "Quantity Probing Error": "Quantity Probing Error Remarks",
    "Quantity Missed Opportunity (Capture)": "Quantity Missed Opportunity Remarks",
    "Spec1 Accuracy": "Spec1 Accuracy Remarks",
    "Spec2 Accuracy": "Spec2 Accuracy Remarks",
    "Spec Missed Opportunity": "Spec Missed Opportunity Remarks",
    "Additional Details Accuracy": "Additional_details remarks",
    "Additional Details Missed Opportunity": None,
    "Looping Behavior": "Looping Behavior Remarks",
    "WER/STT": "WER/STT Remarks",
    "Dead air": "Dead air remarks",
    "Pronunciation": "Pronunciation Remarks",
    "Product Change/ No Product Accuracy": "Product Change/ No Product Accuracy Reamarks",
    "MCAT/ Search errors": "MCAT/ Search errors Remarks",
    "Category ISQ's Error": "Category ISQ's Error Remarks",
    "OnCall AstBuy Error (Transferred)": "OnCall AstBuy Remarks",
}

INSTRUCTIONS = """\
Transcribe this IndiaMART phone call verbatim, as a diarised transcript.

Speakers, exactly two:
- "bot": Vani, IndiaMART's voice assistant (a synthetic voice). It is the voice that answers and \
greets first, then asks about the buyer's requirement: product, quantity and unit, specifications, \
other details.
- "buyer": the other person on the call.
Start a new turn whenever the speaker changes.

Text:
- Verbatim: every word as spoken, with repetitions, fillers and false starts. Do not correct, \
summarise or translate.
- Hindi and Hinglish in Latin script, the way IndiaMART auditors write it, for example "Aapko kitni \
quantity chahiye?". English as spoken. Telugu in Latin script. Never use Devanagari or Telugu script.
- Write "[unclear]" for audio you cannot make out; never guess.
- Never invent turns or words that are not in the audio.
- Write every phone number as "[number]".

start_s and end_s: when the turn starts and ends, in seconds from the start of the recording.
language: "hi" (Hindi or Hinglish), "en" (English), "te" (Telugu), or "mixed" (the call switches \
between them).
Reply with the JSON object only."""

STRICT = """

Your previous reply was rejected: {problem}. Follow the format exactly: one JSON object with \
"language" and a non-empty "turns" list in time order; every turn has "speaker" ("bot" or \
"buyer"), "start_s" and "end_s" in seconds with start_s <= end_s, and non-empty "text". \
Reply with the JSON object only."""

SCHEMA = {
    "type": "object",
    "properties": {
        "language": {"type": "string", "enum": list(LANGUAGES)},
        "turns": {"type": "array", "items": {
            "type": "object",
            "properties": {"speaker": {"type": "string", "enum": ["bot", "buyer"]},
                           "start_s": {"type": "number"}, "end_s": {"type": "number"},
                           "text": {"type": "string"}},
            "required": ["speaker", "start_s", "end_s", "text"],
            "additionalProperties": False}},
    },
    "required": ["language", "turns"],
    "additionalProperties": False,
}

REFUSAL = f"""Refusing: transcription sends real customer call recordings to the model provider
(OpenRouter, then Google). Only the user can approve that. Once they have, re-run with
--yes-send-real-audio. Recordings and transcripts stay on this machine under {RECORDINGS_DIR.parent}."""


# --- call sets ---------------------------------------------------------------------------

def _rows(path: Path) -> list[dict[str, str]]:
    """CSV rows keyed by normalised header: whitespace collapsed, and the markdown escapes of the
    audit export dropped ("fk\\_gl\\_module\\_id" -> "fk_gl_module_id"). Short rows lack keys."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        head = [" ".join(h.replace("\\", "").split()) for h in next(reader)]
        return [dict(zip(head, row)) for row in reader]


def _grade(value: str | None) -> Grade:
    """The sheet's grades, including "Fatal- Product Change Case"; "NA", empty and unknown -> na."""
    v = (value or "").strip().lower()
    return next((g for prefix, g in (("pass", "pass"), ("non-fatal", "non_fatal"), ("fatal", "fatal"))
                 if v.startswith(prefix)), "na")


def production_calls() -> list[dict]:
    """The production sample (prompt v1). glid is dropped: it identifies a person."""
    return [{"call_id": r["call_id"].strip(), "recording_url": r["recording_url"].strip(),
             "duration_s": float(r["duration_sec"]) if (r.get("duration_sec") or "").strip() else None,
             "meta": {k: (r.get(k) or "").strip() for k in ("bot", "hotlead_type", "call_start_ist")}}
            for r in _rows(CALLS_CSV)]


def audited_calls() -> list[dict]:
    """SquadStack INBOUND audit rows with an S3 recording, in file order (first row per call), with the
    human QA grades. Remarks quote customers: keep them in memory and on this machine."""
    calls: dict[str, dict] = {}
    for r in _rows(AUDIT_CSV):
        module = next((v for k, v in r.items() if k.startswith("fk")), "")
        url = (r.get("Call Recording") or "").strip()
        u = urlsplit(url)
        if ((r.get("Vendor") or "").strip().upper() != VENDOR or module.strip().upper() != "INBOUND"
                or u.scheme != "https" or not u.netloc.endswith(".amazonaws.com")
                or not u.path.lower().endswith(".mp3")):
            continue
        call_id = PurePosixPath(u.path).stem
        remarks = {col: (r.get(rc) or "").strip() for col, rc in QA_COLUMNS.items() if rc}
        calls.setdefault(call_id, {"call_id": call_id, "recording_url": url, "human": {
            "overall": _grade(r.get("Overall Quality")),
            "grades": {col: _grade(r.get(col)) for col in QA_COLUMNS},
            "remarks": {col: text for col, text in remarks.items() if text}}})
    return list(calls.values())


# --- download and transcribe -------------------------------------------------------------

def _safe_id(call_id: str) -> str:
    """Call ids become file names: refuse anything that could leave the directory."""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", call_id):
        raise ValueError(f"unsafe call id {call_id!r}")
    return call_id


def _is_mp3(body: bytes) -> bool:
    """The bytes decide, not Content-Type: S3 may label mp3 binary/octet-stream, an error page can claim
    anything, and the file is sent to the model as mp3. ID3 tag or an MPEG frame sync."""
    return body[:3] == b"ID3" or (len(body) > 1 and body[0] == 0xFF and body[1] & 0xE0 == 0xE0)


def download(call_id: str, url: str, *, tries: int = 3, timeout: float = 60.0) -> Path:
    """Fetch a recording to RECORDINGS_DIR/<call_id>.mp3. The write is atomic (temp file + rename), so a
    present file is complete and reruns skip it. Timeouts, 429 and 5xx are retried; other errors raise."""
    dest = RECORDINGS_DIR / f"{_safe_id(call_id)}.mp3"
    if dest.exists():
        return dest
    for attempt in range(tries):
        if attempt:
            time.sleep(2 ** attempt)
        try:
            r = httpx.get(url, timeout=timeout, follow_redirects=True)
        except httpx.TransportError:  # timeouts included
            if attempt == tries - 1:
                raise
            continue
        if r.status_code not in (429, 500, 502, 503, 504):
            break
    if r.status_code != 200:
        raise RuntimeError(f"recording {call_id}: HTTP {r.status_code}")
    if not _is_mp3(r.content):
        raise RuntimeError(f"recording {call_id}: not an mp3 ({r.headers.get('content-type')}, "
                           f"{len(r.content)} bytes)")
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=RECORDINGS_DIR, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(r.content)
        os.replace(tmp, dest)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return dest


def _problem(data) -> str | None:
    """Why a model reply is not a usable transcript; None when it is."""
    turns = data.get("turns") if isinstance(data, dict) else None
    if not isinstance(turns, list) or not turns:
        return "no turns"
    prev = 0.0
    for i, t in enumerate(turns):
        if not isinstance(t, dict) or t.get("speaker") not in ("bot", "buyer"):
            return f"turn {i}: speaker must be bot or buyer"
        if not isinstance(t.get("text"), str) or not t["text"].strip():
            return f"turn {i}: empty text"
        if NATIVE_SCRIPT.search(t["text"]):
            return f"turn {i}: Devanagari or Telugu script; write Hindi and Telugu in Latin script"
        start, end = t.get("start_s"), t.get("end_s")
        if any(x is not None and (isinstance(x, bool) or not isinstance(x, int | float) or not 0 <= x < math.inf)
               for x in (start, end)):
            return f"turn {i}: times must be seconds (finite, not negative)"
        if start is not None and end is not None and end < start:
            return f"turn {i}: ends before it starts"
        if start is not None:
            if start < prev - TIME_SLACK_S:
                return f"turn {i}: starts before the turn before it"
            prev = max(prev, start)
    return None


def transcribe(path: Path, call_id: str, source: CallSource, *, model: str | None = None,
               **fields) -> Transcript:
    """One audio-capable model call -> a diarised Transcript, saved to TRANSCRIPTS_DIR/<call_id>.json.
    A call already transcribed is loaded, never re-sent. A malformed reply gets one retry with a
    stricter instruction. `fields` are further Transcript fields (recording_url, duration_s, meta...);
    meta keeps SAFE_META only."""
    out = TRANSCRIPTS_DIR / f"{_safe_id(call_id)}.json"
    if out.exists():
        return load(out, Transcript)
    model = model or llm.model_for("transcribe")
    audio = llm.audio_part(Path(path))
    problem = None
    for _ in range(2):
        text = INSTRUCTIONS + (STRICT.format(problem=problem) if problem else "")
        try:
            data = llm.chat(model, [{"role": "user", "content": [{"type": "text", "text": text}, audio]}],
                            schema=SCHEMA, schema_name="transcript", max_tokens=16000).data
            problem = _problem(data)
        except ValueError as e:  # the reply was not JSON
            problem = f"not JSON ({e})"
        if problem is None:
            break
    else:
        raise ValueError(f"{call_id}: no usable transcript from {model}: {problem}")
    meta = {k: str(v) for k, v in (fields.pop("meta", None) or {}).items() if k in SAFE_META and v}
    lang = data.get("language")
    transcript = Transcript(
        call_id=call_id, source=source, language=lang if lang in LANGUAGES else None,
        turns=tuple(Turn(speaker=t["speaker"], text=PHONE.sub("[number]", t["text"].strip()),
                         start_s=t.get("start_s"), end_s=t.get("end_s")) for t in data["turns"]),
        provider=f"openrouter:{model}", meta=meta, **fields)
    save(out, transcript)
    return transcript


def load_transcripts(source: CallSource | None = None) -> list[Transcript]:
    """Every saved transcript in call-id order, optionally of one source only."""
    ts = [load(p, Transcript) for p in sorted(TRANSCRIPTS_DIR.glob("*.json"))]
    return [t for t in ts if source is None or t.source == source]


# --- CLI ---------------------------------------------------------------------------------

def _listen(call: dict, source: CallSource, model: str | None) -> Transcript:
    path = download(call["call_id"], call["recording_url"])
    fields = {k: call[k] for k in ("recording_url", "duration_s", "meta") if call.get(k) is not None}
    return transcribe(path, call["call_id"], source, model=model, vendor=VENDOR, **fields)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m abtest.ext.listen",
                                description="Download and transcribe Vani call recordings.")
    p.add_argument("--source", choices=["production", "audited"], required=True)
    p.add_argument("--limit", type=int, help="the first N calls of the source only")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--ids", help="comma-separated call ids")
    p.add_argument("--model", help=f"default {llm.model_for('transcribe')}")
    p.add_argument("--yes-send-real-audio", action="store_true",
                   help="the user approved sending real recordings to the model provider")
    a = p.parse_args(argv)
    if not a.yes_send_real_audio:
        print(REFUSAL, file=sys.stderr)
        return 2

    calls = production_calls() if a.source == "production" else audited_calls()
    if a.ids:
        wanted = {i.strip() for i in a.ids.split(",")}
        calls = [c for c in calls if c["call_id"] in wanted]
    calls = calls[:a.limit]
    todo = [c for c in calls if not (TRANSCRIPTS_DIR / f"{c['call_id']}.json").exists()]
    print(f"{a.source}: {len(calls)} calls, {len(calls) - len(todo)} already transcribed, "
          f"{len(todo)} to do, model {a.model or llm.model_for('transcribe')}")
    start, ok, failed = llm.spent_usd(), 0, []
    pool = ThreadPoolExecutor(max(1, a.workers))
    try:
        futures = {pool.submit(_listen, c, a.source, a.model): c["call_id"] for c in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            call_id, spent = futures[fut], llm.spent_usd() - start
            try:
                t = fut.result()
                ok += 1
                print(f"[{i}/{len(todo)}] {call_id}: {len(t.turns)} turns, {t.language} | ${spent:.3f}")
            except Exception as e:
                failed.append(call_id)
                print(f"[{i}/{len(todo)}] {call_id}: FAILED {type(e).__name__}: {str(e)[:300]} | ${spent:.3f}")
                if isinstance(e, llm.BudgetExceeded):
                    print("LLM budget reached: stopping. Raise LLM_BUDGET_USD to continue.")
                    break
    finally:
        pool.shutdown(cancel_futures=True)  # after a break or Ctrl+C, queued calls never start
    total = llm.spent_usd()
    print(f"done: {ok} transcribed, {len(failed)} failed, {len(todo) - ok - len(failed)} not run; "
          f"spend ${total - start:.4f} this run, ${total:.4f} in total")
    if failed:
        print("failed:", ",".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
