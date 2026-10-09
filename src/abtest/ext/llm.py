"""The only model client (OpenRouter). Every call is cached on disk by the sha256 of its request,
so reruns are free and deterministic; spend is tracked across processes and capped.

PLAN_EXTENSIONS §E1.6 scope: only ext/ modules call this, never the engine path.
Tests never touch the network: `set_stub(fn)` (or LLM_PROVIDER=stub) answers every call locally.
"""

import base64
import fcntl
import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from abtest.contracts import ROOT
from abtest.ext.models import LLM_CACHE_DIR, LOOP_DIR

URL = "https://openrouter.ai/api/v1/chat/completions"
SPEND = LOOP_DIR / "llm_spend.json"
MODELS = {  # role -> default model; override with LLM_MODEL_<ROLE>
    "transcribe": "google/gemini-3-flash-preview",  # listen pilot: Latin script first time, best text, cheapest
    "judge": "anthropic/claude-sonnet-5.5",
    "judge_fast": "anthropic/claude-haiku-5.5",
    "fixer": "anthropic/claude-opus-5.5",
    "gate": "anthropic/claude-sonnet-5.5",
    "replay": "anthropic/claude-haiku-5.5",
}

_stub: Callable[[str, list[dict], dict | None], Any] | None = None
_lock = threading.Lock()


class BudgetExceeded(RuntimeError):
    pass


class LLMResult(BaseModel):
    text: str
    data: Any = None          # parsed JSON when a schema was given
    model: str
    cost_usd: float = 0.0
    cached: bool = False


def model_for(role: str) -> str:
    return os.environ.get(f"LLM_MODEL_{role.upper()}", MODELS[role])


def set_stub(fn: Callable[[str, list[dict], dict | None], Any] | None) -> None:
    """fn(model, messages, schema) -> str (text) or dict/list (JSON data). None restores the network."""
    global _stub
    _stub = fn


def audio_part(path: Path) -> dict:
    fmt = path.suffix.lstrip(".").lower() or "mp3"
    return {"type": "input_audio",
            "input_audio": {"data": base64.b64encode(path.read_bytes()).decode(), "format": fmt}}


def _env_key() -> str:
    for name in ("OPENROUTER_API_KEY", "OPENROUTER"):
        if os.environ.get(name):
            return os.environ[name]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("OPENROUTER_API_KEY", "OPENROUTER") and v.strip():
                return v.strip().strip("'\"")
    raise RuntimeError("no OpenRouter key: set OPENROUTER_API_KEY (or OPENROUTER) in .env")


def spent_usd() -> float:
    return (json.loads(SPEND.read_text()) if SPEND.exists() else {}).get("total_usd", 0.0)


def _add_spend(cost: float) -> float:
    SPEND.parent.mkdir(parents=True, exist_ok=True)
    with open(SPEND, "a+") as f:  # flock: several processes may call the model at once
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        raw = f.read()
        d = json.loads(raw) if raw.strip() else {"total_usd": 0.0, "calls": 0}
        d["total_usd"] = round(d["total_usd"] + cost, 6)
        d["calls"] += 1
        f.seek(0)
        f.truncate()
        f.write(json.dumps(d))
        return d["total_usd"]


def _parse_json(text: str):
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = min((i for i in (text.find("{"), text.find("[")) if i >= 0), default=-1)
        end = max(text.rfind("}"), text.rfind("]"))
        if start < 0 or end <= start:
            raise
        return json.loads(text[start:end + 1])


def chat(model: str, messages: list[dict], *, schema: dict | None = None, schema_name: str = "result",
         max_tokens: int = 4000, temperature: float = 0.0, cache: bool = True,
         timeout: float = 300.0) -> LLMResult:
    """One chat completion. With `schema`, the reply must be JSON matching it (`.data`)."""
    if _stub is not None or os.environ.get("LLM_PROVIDER") == "stub":
        if _stub is None:
            raise RuntimeError("LLM_PROVIDER=stub but no stub is set")
        out = _stub(model, messages, schema)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return LLMResult(text=text, data=None if isinstance(out, str) else out, model=model)

    body = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    if schema is not None:
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": schema_name, "strict": True, "schema": schema}}
    key = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    path = LLM_CACHE_DIR / f"{key}.json"
    if cache and path.exists():
        hit = json.loads(path.read_text(encoding="utf-8"))
        return LLMResult(**{**hit, "cost_usd": 0.0, "cached": True})

    budget = float(os.environ.get("LLM_BUDGET_USD", "40"))
    if spent_usd() >= budget:
        raise BudgetExceeded(f"LLM spend {spent_usd():.2f} USD reached the cap {budget:.2f} (LLM_BUDGET_USD)")

    headers = {"Authorization": f"Bearer {_env_key()}", "Content-Type": "application/json",
               "X-Title": "Vani Loop"}
    delay = 2.0
    for attempt in range(5):
        try:
            r = httpx.post(URL, json=body, headers=headers, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 4:
                time.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            d = r.json()
            if "error" in d:
                raise RuntimeError(f"OpenRouter error: {d['error']}")
            text = d["choices"][0]["message"].get("content") or ""
            break
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == 4:
                raise
            time.sleep(delay)
            delay *= 2
    cost = float((d.get("usage") or {}).get("cost") or 0.0)
    with _lock:
        _add_spend(cost)
    data = _parse_json(text) if schema is not None else None
    result = LLMResult(text=text, data=data, model=d.get("model", model), cost_usd=cost)
    if cache:
        LLM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(result.model_dump_json(), encoding="utf-8")
        os.replace(tmp, path)
    return result
