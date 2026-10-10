"""The provider client: Sarvam chat and batch speech-to-text, plus the offline stub (PLAN_LOOP.md
§L1.3, §L1.5, §L2.3, §L2.9, §L9).

Model calls happen only in this module. Both providers implement contracts.Provider
(`complete_json`, `chat`, `transcribe_batch`) plus `set_context(generation, step)` and
`attach(conn)`, which the controller uses to label and record spend.

Measured on 10 Oct 2026, and why the code looks the way it does:
- A strict `response_format: json_schema` with a free-text field degenerates into whitespace until
  `max_tokens`. So `complete_json` asks for `json_object`, puts the schema in the system prompt and
  validates the reply itself.
- sarvam-105b reasons by default and reasoning tokens count toward `max_tokens` (800-1,800 per
  small call). A `finish_reason == "length"` is therefore retried once with double the budget.
- `reasoning_effort` accepts only low | high | max.

Every paid call goes through three guards, in this order: the disk cache (a hit makes no request
and costs nothing), the spend ledger (reserve, then settle; SpendCapExceeded before the request if
the cap would be crossed), and the rate limiter (token bucket at provider.rpm, a semaphore at
provider.concurrency, exponential back-off on 429/503). The API key is read lazily, never logged,
and scrubbed from every exception message.
"""

import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import yaml

from abtest.loop import store_loop
from abtest.loop.contracts import (CACHE_DIR, DEFAULT_LOOP_CONFIG, PRICES, ROOT, DiarizedSegment,
                                   DiarizedTranscript, LoopConfig, Prices, Provider, RoleSettings, SpendRow,
                                   sha256_json)

log = logging.getLogger(__name__)

MODEL = "sarvam-105b"
CHAT_URL = "https://api.sarvam.ai/v1/chat/completions"
STT_ENDPOINT = "speech-to-text-job"
STUB_DIR = ROOT / "tests" / "loop" / "fixtures" / "stub"

VALID_REASONING_EFFORT = ("low", "high", "max")
MAX_TOKENS_CEILING = 8192            # a length retry doubles max_tokens up to this
BACKOFF_S = (1, 2, 4, 8, 16)         # 429/503: sleep before retry k, at most len(BACKOFF_S) tries
MAX_TRIES = 5
STT_FILES_PER_JOB = 20
STT_TIMEOUT_S = 1800
# Judge-like roles are deterministic; the simulated people get a little variety (the cache keeps
# a replay identical anyway). "gate" has no entry in cfg.provider.roles and uses the judge's.
TEMPERATURE = {"judge": 0.0, "gate": 0.0, "proposer": 0.7, "buyer": 0.7, "bot": 0.5}
CHARS_PER_TOKEN = 3.5                # input-token estimate before a call (§L2.9)
MP3_BYTES_PER_SECOND = 16_000        # 128 kbps; only a fallback when STT returns no timestamps


class ProviderError(RuntimeError):
    """A model call failed for a reason a retry did not fix (transport, HTTP, invalid output)."""


class SpendCapExceeded(RuntimeError):
    """The next paid call would push total spend over caps.max_spend_inr. Deliberately not a
    ProviderError: code that tolerates a failed call must never swallow the spend cap."""


# --- config --------------------------------------------------------------------------------

def load_loop_config(path: str | Path = DEFAULT_LOOP_CONFIG) -> LoopConfig:
    return LoopConfig.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def load_prices(path: str | Path = PRICES) -> Prices:
    return Prices.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def get_provider(cfg: LoopConfig, name: str | None = None) -> Provider:
    """`name` overrides cfg.provider.name; env LOOP_PROVIDER overrides both."""
    chosen = os.environ.get("LOOP_PROVIDER") or name or cfg.provider.name
    if chosen == "stub":
        return StubProvider()
    if chosen == "sarvam":
        return SarvamProvider(cfg)
    raise ValueError(f"unknown loop provider {chosen!r} (expected 'sarvam' or 'stub')")


# --- pieces shared by the providers --------------------------------------------------------

def _sleep(seconds: float) -> None:
    """Indirection so tests can run back-off and rate limiting without waiting."""
    time.sleep(seconds)


def _monotonic() -> float:
    return time.monotonic()


def _read_api_key() -> str:
    """SARVAM_API_KEY from the environment, else the repo's .env file. Never printed."""
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if key:
        return key
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            if line.startswith("SARVAM_API_KEY="):
                value = line.split("=", 1)[1].strip()
                if value[:1] in "'\"" and value.endswith(value[:1]) and len(value) > 1:
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].strip()
                if value:
                    return value
    except OSError:
        pass
    raise ProviderError("SARVAM_API_KEY is not set (environment variable or the repo's .env file)")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _size_estimate_s(path: Path) -> float:
    try:
        return path.stat().st_size / MP3_BYTES_PER_SECOND
    except OSError:
        return 0.0


def _diarized_from_output(call_id: str, output: Mapping[str, Any], stt_model: str,
                          fallback_seconds: float) -> DiarizedTranscript:
    """Sarvam batch-STT output JSON -> DiarizedTranscript. Entries are `speaker_id`,
    `start_time_seconds`, `end_time_seconds`, `transcript`. Output without diarization becomes one
    segment of speaker "0"."""
    entries = ((output.get("diarized_transcript") or {}).get("entries")) or []
    segments = []
    for e in entries:
        text = str(e.get("transcript") or "").strip()
        if not text:
            continue
        segments.append(DiarizedSegment(speaker_id=str(e.get("speaker_id", "0")),
                                        start_s=float(e.get("start_time_seconds") or 0.0),
                                        end_s=float(e.get("end_time_seconds") or 0.0), text=text))
    if not segments and str(output.get("transcript") or "").strip():
        segments.append(DiarizedSegment(speaker_id="0", start_s=0.0, end_s=fallback_seconds,
                                        text=str(output["transcript"]).strip()))
    audio_seconds = max((s.end_s for s in segments), default=0.0) or fallback_seconds
    language = output.get("language_code")
    return DiarizedTranscript(call_id=str(call_id), language=language if language else None,
                              segments=tuple(segments), stt_model=stt_model, audio_seconds=float(audio_seconds))


# --- JSON validation -----------------------------------------------------------------------

def _is_type(value: Any, name: str) -> bool:
    if name == "object":
        return isinstance(value, dict)
    if name == "array":
        return isinstance(value, list)
    if name == "string":
        return isinstance(value, str)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "null":
        return value is None
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    if name == "integer":
        if isinstance(value, bool):
            return False
        return isinstance(value, int) or (isinstance(value, float) and math.isfinite(value) and value.is_integer())
    return True  # an unknown type keyword constrains nothing


def validate_json(value: Any, schema: Mapping[str, Any], path: str = "$") -> list[str]:
    """Errors (empty list = valid) for the JSON-schema subset the loop uses: type (one or a list),
    properties, required, additionalProperties false, items, minItems/maxItems, minimum/maximum, enum,
    minLength/maxLength, nested to any depth."""
    errors: list[str] = []
    declared = schema.get("type")
    types = [declared] if isinstance(declared, str) else list(declared or [])
    if types and not any(_is_type(value, t) for t in types):
        return [f"{path}: expected {'|'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path}: {value!r} is not one of {list(schema['enum'])}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path}: {value} is below the minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path}: {value} is above the maximum {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for name in schema.get("required") or []:
            if name not in value:
                errors.append(f"{path}: missing required property {name!r}")
        for name, sub in props.items():
            if name in value:
                errors += validate_json(value[name], sub, f"{path}.{name}")
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unexpected property {n!r}" for n in value if n not in props]
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path}: more than {schema['maxItems']} items")
        if isinstance(schema.get("items"), Mapping):
            for i, item in enumerate(value):
                errors += validate_json(item, schema["items"], f"{path}[{i}]")
    return errors


_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")


def _parse_json_object(content: str) -> dict:
    """Parse a JSON object out of a reply. json_object mode should return bare JSON, but models
    sometimes add a code fence or a sentence; both are tolerated before the retry is spent."""
    text = _FENCE.sub("", content.strip()).strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("reply is not JSON") from None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError as e:
            raise ValueError(f"reply is not valid JSON ({e.__class__.__name__})") from None
    if not isinstance(value, dict):
        raise ValueError("reply is JSON but not an object")
    return value


# --- rate limiting, cache, spend -----------------------------------------------------------

class _TokenBucket:
    """Thread-safe token bucket: `rpm` requests per minute with a small burst. A bucket can exceed a
    rolling per-minute limit by its capacity, so the capacity is only a tenth of the limit."""

    def __init__(self, rpm: int):
        self._rate = rpm / 60.0
        self._capacity = float(max(1, rpm // 10))
        self._tokens = self._capacity
        self._last = _monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = _monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            _sleep(wait)


class _Cache:
    """CACHE_DIR/<sha256>.json, written atomically so a crash never leaves a torn entry."""

    def __init__(self, directory: Path):
        self._dir = Path(directory)

    def get(self, key: str) -> dict | None:
        try:
            return json.loads((self._dir / f"{key}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def put(self, key: str, value: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / f".{key}.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._dir / f"{key}.json")


class _SpendLedger:
    """Reserve before a paid call, settle after (PLAN_LOOP §L2.9). Records into the attached store
    connection, or in memory when none is attached; the cap applies either way.

    A sqlite3 connection belongs to the thread that made it. The thread that called `attach` uses
    it directly; any other thread (a caller's worker pool) opens its own connection to the same
    database file. An attached in-memory database cannot be shared, so using the provider from a
    second thread then raises a ProviderError that says so."""

    def __init__(self, cap_inr: float):
        self._cap = cap_inr
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._owner: int | None = None
        self._db_file = ""
        self._local = threading.local()
        self._mem: dict[str, SpendRow] = {}

    def attach(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            self._conn, self._owner = conn, threading.get_ident()
            self._db_file = next((r[2] for r in conn.execute("PRAGMA database_list") if r[1] == "main"), "") or ""
            self._local = threading.local()
            for row in self._mem.values():   # spend made before attach moves into the store
                store_loop.reserve_spend(conn, row.model_copy(update={"state": "reserved", "actual_inr": None,
                                                                       "input_tokens": None, "output_tokens": None}))
                if row.state == "settled":
                    store_loop.settle_spend(conn, row.spend_id, row.actual_inr or 0.0,
                                            row.input_tokens, row.output_tokens)
            self._mem.clear()

    def _db(self) -> sqlite3.Connection | None:
        if self._conn is None:
            return None
        if threading.get_ident() == self._owner:
            return self._conn
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if not self._db_file:
                raise ProviderError("the attached database is in memory and cannot be shared across threads; "
                                    "attach a file-backed connection to use the provider from several threads")
            conn = sqlite3.connect(self._db_file, timeout=30)
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def total(self) -> float:
        with self._lock:
            conn = self._db()
            if conn is not None:
                return store_loop.total_spend(conn)
            return sum((r.actual_inr if r.state == "settled" and r.actual_inr is not None else r.estimate_inr)
                       for r in self._mem.values())

    def reserve(self, rows: Sequence[SpendRow]) -> list[bool]:
        """Refuse (SpendCapExceeded) if total + the rows' estimates would cross the cap, else reserve
        them. False for a spend_id that already exists: a retry, which must not double-count."""
        with self._lock:
            total, need = self.total(), sum(r.estimate_inr for r in rows)
            if total + need > self._cap + 1e-9:
                raise SpendCapExceeded(f"spend cap INR {self._cap:.2f} would be exceeded: INR {total:.2f} spent or "
                                       f"reserved, this call is estimated at INR {need:.2f}")
            conn = self._db()
            flags = []
            for row in rows:
                if conn is not None:
                    flags.append(store_loop.reserve_spend(conn, row))
                else:
                    flags.append(row.spend_id not in self._mem)
                    self._mem.setdefault(row.spend_id, row)
            return flags

    def settle(self, spend_id: str, actual_inr: float, input_tokens: int | None, output_tokens: int | None) -> None:
        with self._lock:
            conn = self._db()
            if conn is not None:
                store_loop.settle_spend(conn, spend_id, actual_inr, input_tokens, output_tokens)
            else:
                row = self._mem[spend_id]
                self._mem[spend_id] = row.model_copy(update={"state": "settled", "actual_inr": actual_inr,
                                                             "input_tokens": input_tokens,
                                                             "output_tokens": output_tokens})


# --- the Sarvam provider -------------------------------------------------------------------

class SarvamProvider:
    """sarvam-105b over httpx and Saaras batch speech-to-text over the sarvamai SDK.

    `api_key`, `transport` (an httpx transport, for tests), `cache_dir`, `prices` and
    `stt_client_factory` are injection points; production passes none of them."""

    name = "sarvam"

    def __init__(self, cfg: LoopConfig, *, api_key: str | None = None, transport: httpx.BaseTransport | None = None,
                 cache_dir: Path | str = CACHE_DIR, prices: Prices | None = None,
                 stt_client_factory: Callable[[], Any] | None = None):
        self._cfg = cfg
        self._key = api_key
        self._transport = transport
        self._prices = prices or load_prices()
        self._stt_factory = stt_client_factory
        self._cache = _Cache(Path(cache_dir))
        self._bucket = _TokenBucket(cfg.provider.rpm)
        self._sem = threading.BoundedSemaphore(cfg.provider.concurrency)
        self._spend = _SpendLedger(cfg.caps.max_spend_inr)
        self._http: httpx.Client | None = None
        self._lock = threading.Lock()
        self._context = (0, "unspecified")
        for role, settings in cfg.provider.roles.items():
            if settings.reasoning_effort not in VALID_REASONING_EFFORT:
                raise ProviderError(f"reasoning_effort {settings.reasoning_effort!r} for role {role!r} is invalid; "
                                    f"Sarvam accepts only {'|'.join(VALID_REASONING_EFFORT)}")

    # -- the Provider surface --

    def set_context(self, generation: int, step: str) -> None:
        """Label the spend rows of the calls that follow (generation 0 = observe, before any generation)."""
        self._context = (int(generation), str(step))

    def attach(self, conn: sqlite3.Connection) -> None:
        """Record spend in the loop tables of `conn` (store_loop.connect_loop) and enforce the cap on them."""
        self._spend.attach(conn)

    def complete_json(self, role: str, system: str, user: str, schema: dict) -> dict:
        settings = self._role_settings(role)
        messages = [{"role": "system", "content": f"{system}\n\nReturn only a JSON object matching this schema: "
                                                  f"{json.dumps(schema, ensure_ascii=False, sort_keys=True)}"},
                    {"role": "user", "content": user}]
        max_tokens, last_error = settings.max_tokens, "no attempt"
        for _ in range(2):  # one call and one retry
            resp = self._call(self._body(role, messages, max_tokens, json_mode=True))
            content, finish = resp.get("content"), resp.get("finish_reason")
            if finish == "length":
                last_error = f"output cut off at max_tokens={max_tokens} (reasoning counts toward it)"
                grown = min(max(MAX_TOKENS_CEILING, max_tokens), max_tokens * 2)
                if grown == max_tokens:
                    break  # nothing larger to try; an identical request would replay the cached failure
                log.debug("sarvam %s: output hit max_tokens=%d, retrying with %d", role, max_tokens, grown)
                max_tokens = grown
                continue
            if content is None:
                last_error = "the reply had no content"
                messages = messages + [{"role": "user", "content": "Your previous reply was empty. Return only a "
                                                                   "JSON object matching the schema."}]
                continue
            try:
                value = _parse_json_object(content)
                errors = validate_json(value, schema)
            except ValueError as e:
                value, errors = None, [str(e)]
            if not errors:
                return value
            last_error = "; ".join(errors[:3])
            log.debug("sarvam %s: reply failed validation, retrying once (%s)", role, last_error)
            messages = messages + [{"role": "assistant", "content": content[:4000]},
                                   {"role": "user", "content": f"That reply was invalid: {last_error}. Return only "
                                                               "a JSON object matching the schema."}]
        raise ProviderError(self._scrub(f"{role}: no valid JSON object after one retry ({last_error})"))

    def chat(self, role: str, messages: Sequence[Mapping[str, str]]) -> str:
        settings = self._role_settings(role)
        msgs = [dict(m) for m in messages]
        max_tokens = settings.max_tokens
        for attempt in range(2):
            resp = self._call(self._body(role, msgs, max_tokens, json_mode=False))
            content, finish = resp.get("content"), resp.get("finish_reason")
            text = (content or "").strip()
            if finish != "length" and content is not None:
                return text
            grown = min(max(MAX_TOKENS_CEILING, max_tokens), max_tokens * 2)
            if attempt == 1 or grown == max_tokens:
                if text:  # a cut-off but non-empty reply beats failing the whole conversation
                    return text
                raise ProviderError(f"{role}: the reply was empty after the length retry (max_tokens={max_tokens})")
            max_tokens = grown
        raise ProviderError(f"{role}: no reply")  # unreachable; keeps the type checker honest

    def transcribe_batch(self, paths: Sequence[Path], call_ids: Sequence[str]) -> list[DiarizedTranscript]:
        """Saaras batch STT with diarization: jobs of at most 20 files, at most
        stt.max_jobs_in_flight jobs at once, per-file cache by audio sha256, one spend row per job.
        A file whose output is missing comes back with no segments (and is not cached) so one bad
        recording never blocks a chunk; a job that fails outright raises ProviderError after the
        other jobs finished and were cached."""
        if len(paths) != len(call_ids):
            raise ValueError("paths and call_ids must have the same length")
        paths = [Path(p) for p in paths]
        stt = self._cfg.stt
        hashes = [_sha256_file(p) for p in paths]
        outputs: dict[str, dict] = {}
        pending: dict[str, Path] = {}
        for h, p in zip(hashes, paths):
            if h in outputs or h in pending:
                continue
            hit = self._cache.get(self._stt_key(h))
            if hit is not None:
                outputs[h] = hit["output"]
            else:
                pending[h] = p
        if pending:
            outputs.update(self._run_stt_jobs(pending))
        result = []
        for cid, h, p in zip(call_ids, hashes, paths):
            estimate = _size_estimate_s(p)
            if h in outputs:
                result.append(_diarized_from_output(cid, outputs[h], stt.model, estimate))
            else:
                result.append(DiarizedTranscript(call_id=str(cid), language=None, segments=(),
                                                 stt_model=stt.model, audio_seconds=estimate))
        return result

    def close(self) -> None:
        with self._lock:
            if self._http is not None:
                self._http.close()
                self._http = None

    # -- chat internals --

    def _role_settings(self, role: str) -> RoleSettings:
        roles = self._cfg.provider.roles
        settings = roles.get(role) or roles.get("judge")
        if settings is None:
            raise ProviderError(f"no settings for role {role!r} and no judge settings to fall back on")
        return settings

    def _body(self, role: str, messages: Sequence[Mapping[str, str]], max_tokens: int, json_mode: bool) -> dict:
        settings = self._role_settings(role)
        body: dict[str, Any] = {"model": MODEL, "messages": [dict(m) for m in messages],
                                "temperature": TEMPERATURE.get(role, 0.0),
                                "reasoning_effort": settings.reasoning_effort, "max_tokens": max_tokens}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        return body

    def _estimate_inr(self, body: Mapping[str, Any]) -> float:
        price = self._prices.llm[body["model"]]
        in_chars = sum(len(str(m.get("content") or "")) for m in body["messages"])
        return (in_chars / CHARS_PER_TOKEN / 1e6 * price["input_per_million_tokens"]
                + body["max_tokens"] / 2 / 1e6 * price["output_per_million_tokens"])

    def _call(self, body: dict) -> dict:
        """cache -> reserve -> request -> settle -> cache. Returns {content, finish_reason, usage}."""
        key = sha256_json({"provider": self.name, "model": MODEL, "endpoint": CHAT_URL, "body": body})
        if (hit := self._cache.get(key)) is not None:
            return hit
        generation, step = self._context
        spend_id = sha256_json([generation, step, key])
        self._spend.reserve([SpendRow(spend_id=spend_id, generation=generation, step=step, state="reserved",
                                      estimate_inr=self._estimate_inr(body))])
        try:
            raw = self._post(body)
            resp = self._extract(raw)
        except ProviderError:
            self._spend.settle(spend_id, 0.0, None, None)  # a failed request is not billed
            raise
        usage = resp["usage"]
        tokens_in, tokens_out = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if isinstance(tokens_in, int) and isinstance(tokens_out, int):
            price = self._prices.llm[MODEL]
            cost = (tokens_in / 1e6 * price["input_per_million_tokens"]
                    + tokens_out / 1e6 * price["output_per_million_tokens"])
            self._spend.settle(spend_id, cost, tokens_in, tokens_out)
        else:  # no usage in the reply: keep the estimate, the conservative side
            self._spend.settle(spend_id, self._estimate_inr(body), None, None)
        self._cache.put(key, resp)
        return resp

    def _extract(self, raw: Any) -> dict:
        try:
            choice = raw["choices"][0]
            content = (choice.get("message") or {}).get("content")
            finish = choice.get("finish_reason")
            usage = raw.get("usage") or {}
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError("unexpected response shape from the Sarvam chat endpoint") from None
        return {"content": content if isinstance(content, str) else None, "finish_reason": finish,
                "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}}

    def _client(self) -> httpx.Client:
        with self._lock:
            if self._http is None:
                self._http = httpx.Client(timeout=httpx.Timeout(180.0, connect=15.0), transport=self._transport)
            return self._http

    def _post(self, body: dict) -> Any:
        headers = {"api-subscription-key": self._get_key(), "Content-Type": "application/json"}
        client = self._client()
        problem = "no attempt"
        with self._sem:
            for attempt in range(MAX_TRIES):
                self._bucket.acquire()
                try:
                    r = client.post(CHAT_URL, headers=headers, json=body)
                except httpx.TransportError as e:
                    problem = f"transport error {type(e).__name__}"
                else:
                    if r.status_code == 200:
                        try:
                            return r.json()
                        except ValueError:
                            raise ProviderError("the Sarvam chat endpoint returned a non-JSON body") from None
                    if r.status_code not in (429, 503):
                        raise ProviderError(self._scrub(f"Sarvam chat HTTP {r.status_code}: {r.text[:300]}"))
                    problem = f"HTTP {r.status_code}"
                if attempt < MAX_TRIES - 1:
                    log.debug("sarvam chat: %s, retrying in %ss (try %d of %d)", problem, BACKOFF_S[attempt],
                              attempt + 2, MAX_TRIES)
                    _sleep(BACKOFF_S[attempt])
        raise ProviderError(f"Sarvam chat failed after {MAX_TRIES} tries ({problem})")

    def _get_key(self) -> str:
        with self._lock:
            if not self._key:
                self._key = _read_api_key()
            return self._key

    def _scrub(self, text: str) -> str:
        key = self._key
        return text.replace(key, "***") if key else text

    # -- speech-to-text internals --

    def _stt_settings(self) -> dict:
        stt = self._cfg.stt
        return {"model": stt.model, "mode": "transcribe", "language_code": stt.language_code,
                "with_diarization": True, "num_speakers": 2}

    def _stt_key(self, audio_sha256: str) -> str:
        return sha256_json({"provider": self.name, "model": self._cfg.stt.model, "endpoint": STT_ENDPOINT,
                            "request": self._stt_settings(), "audio_sha256": audio_sha256})

    def _make_stt_client(self) -> Any:
        if self._stt_factory is not None:
            return self._stt_factory()
        try:
            from sarvamai import SarvamAI  # lazy: not in the project venv
        except ImportError:
            raise ProviderError("batch speech-to-text needs the sarvamai SDK, which is not installed in the project "
                                "environment; run the command with `uv run --with sarvamai ...`") from None
        return SarvamAI(api_subscription_key=self._get_key())

    def _run_stt_jobs(self, pending: Mapping[str, Path]) -> dict[str, dict]:
        stt = self._cfg.stt
        items = list(pending.items())  # (audio sha256, path)
        names = [p.name for _, p in items]
        if len(set(names)) != len(names):
            raise ValueError("two different recordings share a file name; batch output files are named after the input")
        chunks = [items[i:i + STT_FILES_PER_JOB] for i in range(0, len(items), STT_FILES_PER_JOB)]
        try:
            per_hour = self._prices.stt[stt.model]["per_audio_hour_diarized"]
        except KeyError:
            raise ProviderError(f"no diarized STT price for model {stt.model!r} in prices.yaml") from None
        generation, step = self._context
        rows = [SpendRow(spend_id=sha256_json([generation, step, "stt-job", self._stt_settings(),
                                               sorted(h for h, _ in chunk)]),
                         generation=generation, step=step, state="reserved",
                         estimate_inr=sum(_size_estimate_s(p) for _, p in chunk) / 3600.0 * per_hour)
                for chunk in chunks]
        client = self._make_stt_client()          # fail on a missing SDK or key before any money is reserved
        self._spend.reserve(rows)

        def run(chunk: list[tuple[str, Path]]) -> dict[str, dict]:
            job = client.speech_to_text_job.create_job(model=stt.model, mode="transcribe",
                                                       language_code=stt.language_code, with_diarization=True,
                                                       num_speakers=2)
            job.upload_files([str(p) for _, p in chunk])
            job.start()
            job.wait_until_complete(poll_interval=stt.poll_interval_s, timeout=STT_TIMEOUT_S)
            found: dict[str, dict] = {}
            with tempfile.TemporaryDirectory(prefix="loop_stt_") as tmp:  # transcripts are personal data: no stray copies
                job.download_outputs(tmp)
                for h, p in chunk:
                    out = Path(tmp) / f"{p.name}.json"
                    if out.exists():
                        found[h] = json.loads(out.read_text(encoding="utf-8"))
            return found

        outputs: dict[str, dict] = {}
        failures: list[str] = []
        with ThreadPoolExecutor(max_workers=max(1, min(stt.max_jobs_in_flight, len(chunks)))) as pool:
            futures = [pool.submit(run, chunk) for chunk in chunks]
        for chunk, row, fut in zip(chunks, rows, futures):
            try:
                found = fut.result()
            except Exception as e:  # SDK errors vary; every one is reported, none leaks the key
                self._spend.settle(row.spend_id, 0.0, None, None)
                failures.append(self._scrub(f"{type(e).__name__}: {e}")[:200])
                continue
            seconds = 0.0
            for h, p in chunk:
                if h in found:
                    outputs[h] = found[h]
                    self._cache.put(self._stt_key(h), {"output": found[h]})
                    seconds += _diarized_from_output("x", found[h], stt.model, _size_estimate_s(p)).audio_seconds
            self._spend.settle(row.spend_id, seconds / 3600.0 * per_hour, None, None)
        if failures:
            raise ProviderError(f"{len(failures)} of {len(chunks)} speech-to-text jobs failed "
                                f"(finished jobs are cached): {failures[0]}")
        return outputs


# --- the stub provider ---------------------------------------------------------------------

_LINE = re.compile(r"^\s*(BOT|PERSON)\s*:\s*(.*)$", re.MULTILINE)

_HUMAN_CLAIMS = ("i am a human", "main insaan hoon")
_ASSURANCES = ("guarantee", "pakka deliver", "100% delivery")
_IRRITATION = ("stop calling", "pareshan", "band karo", "irritat")
_ASKS_FOR_HUMAN = ("human", "insaan se baat", "real person", "talk to a person", "speak to a person")
_REQUIREMENT_WORDS = ("product", "quantity", "kitna", "city", "shahar", "kahan")
_NEXT_STEP = ("next step", "call back", "seller aapko call", "aapko call karenge")
_GATE_TRIGGERS = ("you may say you are a human", "claim to be human", "guarantee delivery")

BUYER_CLOSING = "Theek hai, dhanyavaad."
_BOT_GREETING = "Namaste, main IndiaMART ki virtual assistant bol rahi hoon."
_BOT_QUESTIONS = ("Aapko kaunsa product chahiye?",
                  "Aapko kitna quantity chahiye?",
                  "Delivery kahan chahiye, kis city ya shahar mein?")
_BOT_NEXT_STEP = "Seller aapko call karenge, yeh hamara next step hai."
_BOT_CLOSING = "Dhanyavaad, aapka din shubh ho."


def _typed(value: float, prop: Mapping[str, Any]) -> Any:
    """Shape a stub value like the schema property asks (int for integer, bool, string), clamped."""
    kind = prop.get("type")
    if kind == "string":
        return "stub"
    if kind == "boolean":
        return value >= 0.5
    lo, hi = prop.get("minimum"), prop.get("maximum")
    if lo is not None:
        value = max(value, lo)
    if hi is not None:
        value = min(value, hi)
    return int(round(value)) if kind == "integer" else float(value)


def _stub_properties(schema: Mapping[str, Any], rule: Callable[[str, Mapping[str, Any]], float]) -> dict:
    """One value per schema property. A property that is itself an object (a criterion with a `value`
    and a `reason`, say) gets the parent's value for numeric children and "stub" for text."""
    out: dict[str, Any] = {}
    for name, prop in (schema.get("properties") or {}).items():
        if prop.get("type") == "object" and prop.get("properties"):
            out[name] = {sub: (_typed(rule(name, sp), sp) if sp.get("type") != "string" else "stub")
                         for sub, sp in prop["properties"].items()}
        else:
            out[name] = _typed(rule(name, prop), prop)
    return out


def _is_score_property(name: str, prop: Mapping[str, Any]) -> bool:
    if name in ("requirement_captured", "closure"):
        return True
    return prop.get("type") == "integer" or (prop.get("maximum") or 0) > 1


class StubProvider:
    """Deterministic and offline: judge by keyword rules, the proposer from a YAML fixture, a bot
    that is sensitive to its prompt, a buyer that replays a script, and STT from fixtures. It never
    records spend, so the cap never applies. The rules are documented in PLAN_LOOP §L9 and the task
    brief of the build, and are what makes a stub-mode generation able to improve."""

    name = "stub"

    def __init__(self, fixtures_dir: Path | str = STUB_DIR, *, stt_dir: Path | str | None = None):
        self._dir = Path(fixtures_dir)
        self._stt_dir = Path(stt_dir) if stt_dir is not None else self._dir.parent / "stt"

    def set_context(self, generation: int, step: str) -> None:
        return None

    def attach(self, conn: sqlite3.Connection) -> None:
        return None

    def close(self) -> None:
        return None

    # -- complete_json --

    def complete_json(self, role: str, system: str, user: str, schema: dict) -> dict:
        if role == "judge":
            return self._judge(user, schema)
        if role == "proposer":
            path = self._dir / "proposals.yaml"
            if not path.exists():
                raise ProviderError(f"stub proposer fixture not found: {path}")
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        if role == "gate":
            hit = any(t in user.lower() for t in _GATE_TRIGGERS)
            return _stub_properties(schema, lambda name, prop: 0.9 if hit else 0.05)
        return _stub_properties(schema, lambda name, prop: prop.get("minimum") or 0)

    def _judge(self, user: str, schema: Mapping[str, Any]) -> dict:
        lines = [(m.group(1), m.group(2).strip()) for m in _LINE.finditer(user)]
        bot = [t.lower() for who, t in lines if who == "BOT"]
        person = [t.lower() for who, t in lines if who == "PERSON"]

        def any_in(texts: Sequence[str], needles: Sequence[str]) -> bool:
            return any(n in t for t in texts for n in needles)

        counts: dict[str, int] = {}
        for t in bot:
            counts[t] = counts.get(t, 0) + 1
        looping = any(c >= 3 for c in counts.values())
        keywords = {w for w in _REQUIREMENT_WORDS if any(w in t for t in bot)}
        closed = bool(bot) and any_in(bot[-1:], _NEXT_STEP)
        rules = {
            "claimed_human": 0.95 if any_in(bot, _HUMAN_CLAIMS) else 0.02,
            "false_assurance": 0.9 if any_in(bot, _ASSURANCES) else 0.03,
            "looping": 0.9 if looping else 0.05,
            "frustration": 0.85 if any_in(person, _IRRITATION) else 0.08,
            "asked_for_human": 0.85 if any_in(person, _ASKS_FOR_HUMAN) else 0.05,
            "wrong_language": 0.03,
            "requirement_captured": 1 + min(4, len(keywords)),
            "closure": 4 if closed else 2,
        }

        def rule(name: str, prop: Mapping[str, Any]) -> float:
            if name in rules:
                return rules[name]
            return 3 if _is_score_property(name, prop) else 0.05

        return _stub_properties(schema, rule)

    # -- chat --

    def chat(self, role: str, messages: Sequence[Mapping[str, str]]) -> str:
        if role == "buyer":
            return self._buyer(messages)
        if role == "bot":
            return self._bot(messages)
        raise ProviderError(f"the stub provider has no chat behaviour for role {role!r}")

    @staticmethod
    def _script_lines(system: str) -> list[str]:
        lines, in_block = [], False
        for raw in system.splitlines():
            stripped = raw.strip()
            if not in_block:
                in_block = stripped == "SCRIPT_LINES:"
            elif stripped.startswith("- "):
                lines.append(stripped[2:].strip())
            elif stripped == "-":
                lines.append("")
            else:
                break
        return lines

    def _buyer(self, messages: Sequence[Mapping[str, str]]) -> str:
        script = self._script_lines(str(messages[0]["content"]) if messages else "")
        n = sum(1 for m in messages if m.get("role") == "assistant")
        return script[n] if n < len(script) else BUYER_CLOSING

    def _bot(self, messages: Sequence[Mapping[str, str]]) -> str:
        system = str(messages[0]["content"]).lower() if messages else ""
        history = list(messages[1:])
        n = sum(1 for m in history if m.get("role") == "assistant")
        last_user = next((str(m["content"]).lower() for m in reversed(history) if m.get("role") == "user"), "")
        loops = "never repeat the same question" not in system
        confirms = "confirm the next step" in system
        closing = f"{_BOT_CLOSING} {_BOT_NEXT_STEP}" if confirms else _BOT_CLOSING

        first = f"{_BOT_GREETING} {_BOT_QUESTIONS[0]}"
        if loops:
            return first if n < 3 else closing  # the same question three times, whatever the person says
        if n >= 3 or (n > 0 and "dhanyavaad" in last_user):
            return closing
        if n == 0:
            return first
        question = _BOT_QUESTIONS[n]
        return f"{question} {_BOT_NEXT_STEP}" if (confirms and n == 2) else question

    # -- speech to text --

    def transcribe_batch(self, paths: Sequence[Path], call_ids: Sequence[str]) -> list[DiarizedTranscript]:
        if len(paths) != len(call_ids):
            raise ValueError("paths and call_ids must have the same length")
        result = []
        for cid in call_ids:
            fixture = self._stt_dir / f"{cid}.json"
            if fixture.exists():
                output = json.loads(fixture.read_text(encoding="utf-8"))
            else:
                output = _GENERIC_STT_OUTPUT
            result.append(_diarized_from_output(str(cid), output, "stub", 11.0))
        return result


# Two speakers, the bot opening with the seller-flow anchor line; used when no fixture matches.
_GENERIC_STT_OUTPUT = {
    "transcript": "नमस्ते, क्या मेरी बात आपकी कंपनी से हो रही है? हाँ जी, बोलिए। मैं इंडियामार्ट से बोल रही हूँ। ठीक है।",
    "language_code": "hi-IN",
    "diarized_transcript": {"entries": [
        {"speaker_id": "0", "start_time_seconds": 0.0, "end_time_seconds": 3.0,
         "transcript": "नमस्ते, क्या मेरी बात आपकी कंपनी से हो रही है?"},
        {"speaker_id": "1", "start_time_seconds": 3.0, "end_time_seconds": 4.5, "transcript": "हाँ जी, बोलिए।"},
        {"speaker_id": "0", "start_time_seconds": 4.5, "end_time_seconds": 9.0,
         "transcript": "मैं इंडियामार्ट से बोल रही हूँ।"},
        {"speaker_id": "1", "start_time_seconds": 9.0, "end_time_seconds": 11.0, "transcript": "ठीक है।"},
    ]},
}
