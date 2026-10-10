"""The surface adapter interface (PLAN_LOOP.md §L2.4, §L9 item 6, contract §L7).

One rule for every surface: the loop places every call. A surface adapter receives a CallRequest
(`experiment_id, lead_id, variant, prompt_hash, rendered_prompt, phone`) and nothing else from any
caller, and answers with a CallRef; the call's outcome comes back as a CallResult. The dialer (not
in this build) checks the caps and the pause flag before it ever calls `start_call`, and verifies
the prompt hash before it renders; an adapter is the last line of defence, not the only one.

Phone numbers and rendered prompts are personal or large, so neither shows up in a model's repr
(and so not in a log line or a traceback).
"""

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

Variant = Literal["A", "B"]
# How a call ended: the S1 webhook vocabulary (connected / no_answer / busy / failed) plus
# "in_progress" for a call whose result has not arrived yet.
CallStatus = Literal["connected", "no_answer", "busy", "failed", "in_progress"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class LiveCallsDisabled(RuntimeError):
    """A surface refused to place a real call: the loop config or the dogfood file does not allow it."""


class CallRequest(_Frozen):
    experiment_id: str = Field(min_length=1)
    lead_id: str = Field(min_length=1)
    variant: Variant
    prompt_hash: str = Field(min_length=1)       # of the resolved prompt template the variant uses
    rendered_prompt: str = Field(repr=False)     # the prompt rendered for this lead (§L2.6 render)
    phone: str = Field(repr=False)


class CallRef(_Frozen):
    call_ref: str = Field(min_length=1)          # the id the result (or webhook) carries back
    surface: str                                 # "fake", "s1", ...


class CallResult(_Frozen):
    call_ref: str
    status: CallStatus
    duration_s: float | None = None
    disposition: str | None = None               # the platform's own end-of-call disposition, never the judge's
    recording_path: str | None = None            # a local file once fetched
    transcript_text: str | None = Field(default=None, repr=False)  # BOT:/PERSON: lines, never scored (§L2.4)


@runtime_checkable
class Surface(Protocol):
    """What the dialer needs from a telephony surface."""
    name: str

    def start_call(self, req: CallRequest) -> CallRef: ...

    def fetch_result(self, ref: CallRef) -> CallResult: ...
