"""S1: the Sarvam Voice Agents platform, Instant Outbound over REST (PLAN_LOOP.md §L2.4 S1, §L9 item 6).

No real call is placed in this build. The surface refuses (LiveCallsDisabled), already when it is
built and again before every call, unless all three hold: `live.surface` is "s1",
`caps.max_live_calls_per_day` is above zero, and `configs_loop/dogfood.yaml` exists (the ignored file that holds the
consenting team members' numbers). The dialer (not in this build) still owns the per-day count and
the pause flag; this is the adapter's own last line of defence.

The exact endpoint paths and field names are confirmed at L0 (PLAN_LOOP §L4), so the paths are
constructor parameters and every guessed name is marked TODO(L0). What the plan fixes is the data
flow: the create-call request carries webhook metadata {experiment_id, lead_id, call_id, variant,
prompt_hash}; the post-call webhook carries the status, duration, `interaction_id` and
`final_agent_variables` (whose `live.disposition_field` entry is the call's disposition, from the
platform and never from the rubric judge); the recording is fetched later by `interaction_id` from
the Analytics API. The S1 transcript is English (`en_text`) and is returned for storage only: scoring
always uses a fresh Saaras transcript (§L2.4).

`SARVAM_API_KEY` is read from the environment when a request is made, sent in a header and never
stored, logged or put in an error message. Phone numbers are validated but never echoed.
"""

import hashlib
import os
import re
from pathlib import Path

import httpx

from abtest.loop.contracts import LOOP_CONFIGS_DIR, LoopConfig
from abtest.loop.telephony.base import CallRef, CallRequest, CallResult, LiveCallsDisabled

SURFACE = "s1"
DOGFOOD_FILE = LOOP_CONFIGS_DIR / "dogfood.yaml"
DEFAULT_BASE_URL = "https://api.sarvam.ai"                                      # TODO(L0): confirm the host
CREATE_PATH = "/voice-agents/calls/outbound"                                     # TODO(L0): Instant Outbound create-call
ANALYTICS_PATH = "/voice-agents/interactions/{interaction_id}/recording"         # TODO(L0): Analytics recording fetch
KEY_HEADER = "api-subscription-key"
_PHONE = re.compile(r"\+?\d{8,15}")
# TODO(L0): the webhook's real status strings; unknown ones raise rather than guess.
_STATUS = {"connected": "connected", "answered": "connected", "completed": "connected",
           "no_answer": "no_answer", "noanswer": "no_answer", "unanswered": "no_answer",
           "busy": "busy", "failed": "failed", "error": "failed", "cancelled": "failed",
           "canceled": "failed", "in_progress": "in_progress", "ringing": "in_progress",
           "initiated": "in_progress", "queued": "in_progress"}
_ROLE = {"agent": "BOT", "user": "PERSON"}


class S1Error(RuntimeError):
    """A create-call or recording request failed. Carries the HTTP status only: a response body
    could echo a phone number."""


def _status(raw: object) -> str:
    key = re.sub(r"[\s\-]+", "_", str(raw).strip().lower())
    if key not in _STATUS:
        raise ValueError(f"unrecognised S1 call status {str(raw)[:40]!r}; add it to s1_sarvam._STATUS (TODO(L0))")
    return _STATUS[key]


def _metadata(payload: dict) -> dict:
    """Our webhook metadata as echoed back. TODO(L0): the key it arrives under."""
    meta = payload.get("metadata") or (payload.get("webhook_config") or {}).get("metadata") or {}
    return meta if isinstance(meta, dict) else {}


def _transcript(turns: object) -> str | None:
    lines = []
    for turn in turns if isinstance(turns, list) else []:
        text = turn.get("en_text") if isinstance(turn, dict) else None
        role = _ROLE.get(str(turn.get("role", "")).lower()) if isinstance(turn, dict) else None
        if text and role:
            lines.append(f"{role}: {text}")
    return "\n".join(lines) or None


class S1Surface:
    name = SURFACE

    def __init__(self, cfg: LoopConfig, base_url: str = DEFAULT_BASE_URL, create_path: str = CREATE_PATH,
                 analytics_path: str = ANALYTICS_PATH, *, client: httpx.Client | None = None,
                 dogfood_path: Path = DOGFOOD_FILE):
        self._cfg = cfg
        self._base_url = base_url.rstrip("/")
        self._create_path = create_path
        self._analytics_path = analytics_path
        self._client = client or httpx.Client(timeout=30.0)
        self._dogfood_path = Path(dogfood_path)
        self._attempts: dict[str, int] = {}
        self._results: dict[str, CallResult] = {}
        self._interaction_ids: dict[str, str] = {}
        self._require_enabled()

    # --- guard ---------------------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if self._cfg.live.surface != SURFACE:
            raise LiveCallsDisabled(f"live.surface is {self._cfg.live.surface!r}, not 's1': no real call is placed")
        if self._cfg.caps.max_live_calls_per_day <= 0:
            raise LiveCallsDisabled("caps.max_live_calls_per_day is 0: set it (after L0) before any real call")
        if not self._dogfood_path.exists():
            raise LiveCallsDisabled("the dogfood file is missing: configs_loop/dogfood.yaml lists the consenting "
                                    "call takers and must exist before any real call")

    # --- placing a call ------------------------------------------------------------------------

    def build_request(self, req: CallRequest, call_id: str) -> dict:
        """The Instant Outbound create-call body. TODO(L0): confirm field names, and whether the
        prompt is set per call or needs one committed agent version per variant."""
        return {
            "to_number": req.phone,                   # TODO(L0)
            "prompt": req.rendered_prompt,            # TODO(L0)
            "webhook_config": {"metadata": {          # TODO(L0): confirm where metadata goes
                "experiment_id": req.experiment_id, "lead_id": req.lead_id, "call_id": call_id,
                "variant": req.variant, "prompt_hash": req.prompt_hash}},
        }

    def _headers(self) -> dict[str, str]:
        key = os.environ.get("SARVAM_API_KEY")
        if not key:
            raise RuntimeError("SARVAM_API_KEY is not set (put it in .env or the environment)")
        return {KEY_HEADER: key}

    def start_call(self, req: CallRequest) -> CallRef:
        self._require_enabled()
        if not _PHONE.fullmatch(req.phone):
            raise ValueError("phone must be 8-15 digits with an optional leading +")
        key = "|".join((req.experiment_id, req.lead_id, req.variant, req.prompt_hash))
        n = self._attempts[key] = self._attempts.get(key, 0) + 1
        call_id = "s1-" + hashlib.sha256(f"{key}|{n}".encode("utf-8")).hexdigest()[:16]
        resp = self._client.post(self._base_url + self._create_path, json=self.build_request(req, call_id),
                                 headers=self._headers())
        if resp.status_code >= 400:
            raise S1Error(f"S1 create-call failed with HTTP {resp.status_code}")
        return CallRef(call_ref=call_id, surface=SURFACE)

    # --- results -------------------------------------------------------------------------------

    def parse_webhook(self, payload: dict) -> CallResult:
        """A post-call webhook payload -> CallResult. Remembers the result and its `interaction_id`
        so `fetch_result` and `fetch_recording` can answer for the call later."""
        call_ref = _metadata(payload).get("call_id") or payload.get("attempt_id")
        if not call_ref:
            raise ValueError("webhook payload carries neither metadata.call_id nor attempt_id")
        raw_duration = payload.get("duration")
        duration = None if raw_duration is None else float(raw_duration)
        if duration is not None and duration < 0:
            raise ValueError("webhook duration is negative")
        variables = payload.get("final_agent_variables") or {}
        disposition = variables.get(self._cfg.live.disposition_field) if isinstance(variables, dict) else None
        result = CallResult(call_ref=str(call_ref), status=_status(payload.get("status")), duration_s=duration,
                            disposition=str(disposition) if disposition not in (None, "") else None,
                            transcript_text=_transcript(payload.get("interaction_transcript")))
        self._results[result.call_ref] = result
        if payload.get("interaction_id"):
            self._interaction_ids[result.call_ref] = str(payload["interaction_id"])
        return result

    def fetch_result(self, ref: CallRef) -> CallResult:
        """The webhook's result for this call, or status "in_progress" until one has been parsed."""
        return self._results.get(ref.call_ref) or CallResult(call_ref=ref.call_ref, status="in_progress")

    def fetch_recording(self, ref: CallRef, out_dir: Path) -> Path | None:
        """Download the call's recording by `interaction_id` (Analytics API) into `out_dir`; None if
        no webhook has told us the interaction id yet. Updates the stored result's recording_path."""
        interaction_id = self._interaction_ids.get(ref.call_ref)
        if interaction_id is None:
            return None
        resp = self._client.get(self._base_url + self._analytics_path.format(interaction_id=interaction_id),
                                headers=self._headers())
        if resp.status_code >= 400:
            raise S1Error(f"S1 recording fetch failed with HTTP {resp.status_code}")
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{re.sub(r'[^A-Za-z0-9_.-]', '_', ref.call_ref)}.mp3"
        path.write_bytes(resp.content)
        if ref.call_ref in self._results:
            self._results[ref.call_ref] = self._results[ref.call_ref].model_copy(
                update={"recording_path": str(path)})
        return path
