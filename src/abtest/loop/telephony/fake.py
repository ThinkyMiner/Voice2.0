"""Canned, deterministic calls for tests and sim mode (PLAN_LOOP.md §L9 item 6). No network, no clock.

The same request in the same order always yields the same ref and the same result: both are drawn
from a hash of (experiment, lead, variant, prompt hash, attempt number), where the attempt number
counts earlier calls with the same four values, so a retried lead gets a new ref. Pass `results`
(lead id -> CallResult) to script the outcome for a lead instead; its call_ref is filled in.
"""

import hashlib
from collections.abc import Mapping

from abtest.loop.telephony.base import CallRef, CallRequest, CallResult

SURFACE = "fake"
DISPOSITIONS = ("BL Enriched", "BL Enriched", "BL Approved", "Abruptly Disconnected Call", "Queue Drop")
TRANSCRIPT = ("BOT: Namaste, main IndiaMART se virtual assistant bol raha hoon. Aap kaunsa product "
              "dhoondh rahe hain?\nPERSON: Mujhe 500 piece chahiye, Pune ke liye.\n"
              "BOT: Theek hai, aapki requirement note kar li hai. Seller aapse sampark karega.\n"
              "PERSON: Theek hai, dhanyavaad.")


def _draw(key: str) -> tuple[str, float, float]:
    """(hex digest, u1, u2): two independent uniforms in [0, 1) from one hash."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return digest, int(digest[:8], 16) / 16**8, int(digest[8:16], 16) / 16**8


class FakeSurface:
    name = SURFACE

    def __init__(self, results: Mapping[str, CallResult] | None = None, recording_path: str | None = None):
        self._scripted = dict(results or {})   # by lead id: a ref is only known once the call starts
        self._recording_path = recording_path
        self._attempts: dict[str, int] = {}
        self._results: dict[str, CallResult] = {}
        self.requests: list[CallRequest] = []   # what the dialer sent, in order, for assertions

    def start_call(self, req: CallRequest) -> CallRef:
        key = "|".join((req.experiment_id, req.lead_id, req.variant, req.prompt_hash))
        n = self._attempts[key] = self._attempts.get(key, 0) + 1
        digest, u1, u2 = _draw(f"{key}|{n}")
        ref = CallRef(call_ref=f"fake-{digest[:12]}", surface=SURFACE)
        self.requests.append(req)
        self._results[ref.call_ref] = self._canned(req.lead_id, ref.call_ref, u1, u2)
        return ref

    def _canned(self, lead_id: str, call_ref: str, u1: float, u2: float) -> CallResult:
        if lead_id in self._scripted:
            return self._scripted[lead_id].model_copy(update={"call_ref": call_ref})
        if u1 >= 0.8:   # 80% connect; the rest split between nobody home, busy and a failed dial
            status = "no_answer" if u1 < 0.9 else "busy" if u1 < 0.95 else "failed"
            return CallResult(call_ref=call_ref, status=status)
        return CallResult(call_ref=call_ref, status="connected", duration_s=round(20 + u2 * 160, 1),
                          disposition=DISPOSITIONS[int(u2 * 1e6) % len(DISPOSITIONS)],
                          recording_path=self._recording_path, transcript_text=TRANSCRIPT)

    def fetch_result(self, ref: CallRef) -> CallResult:
        try:
            return self._results[ref.call_ref]
        except KeyError:
            raise KeyError(f"unknown fake call {ref.call_ref!r}: start_call it first") from None
