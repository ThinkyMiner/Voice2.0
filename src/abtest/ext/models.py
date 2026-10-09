"""Loop contracts: the models every ext module codes against. Orchestrator-owned; builders
never edit this file, they report proposed changes instead.

The loop, one cycle per production prompt version:
  Listen   recordings -> Transcript                       (ext/listen.py)
  Judge    Transcript x Rubric -> CallAudit; agreement with human QA   (ext/rubrics.py, ext/judge.py)
  Improve  CallAudits -> RootCause (ranked) -> Fix = Prompt B          (ext/improve.py)
  Gate     Fix -> GateResult (safety) and ReplayResult (simulated before/after)  (ext/improve.py)
  Prove    Prompt B on 10% through the base A/B engine (simulated traffic)       (ext/loop.py)
  Roll out PROMOTED -> registry production = B; the next cycle audits B's calls  (ext/loop.py)

Storage is plain JSON under LOOP_DIR (git-ignored: it holds real call content). Write with
`save`, which is atomic, so the web server and the loop can share files safely.
"""

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from abtest.contracts import DATA_DIR, ROOT

# --- paths -----------------------------------------------------------------------------

LOOP_DIR = Path(os.environ.get("ABTEST_LOOP_DIR", DATA_DIR / "derived" / "loop"))
RECORDINGS_DIR = LOOP_DIR / "recordings"    # <call_id>.mp3
TRANSCRIPTS_DIR = LOOP_DIR / "transcripts"  # <call_id>.json  (Transcript)
AUDITS_DIR = LOOP_DIR / "audits"            # <prompt_version>/<call_id>.json  (CallAudit)
LLM_CACHE_DIR = LOOP_DIR / "llm_cache"      # <sha256>.json, one per distinct request
PROMPTS_DIR = LOOP_DIR / "prompts"          # generated prompt versions: <version>.txt
CUSTOM_RUBRICS = LOOP_DIR / "rubrics_custom.json"   # every version of every user rubric
REGISTRY = LOOP_DIR / "registry.json"       # list[PromptVersion]
CYCLES = LOOP_DIR / "cycles.json"           # list[Cycle]
CALLS_CSV = DATA_DIR / "calls" / "squadstack_sample250.csv"      # production sample, 2026-10-06
AUDIT_CSV = DATA_DIR / "audit" / "audit_source_of_truth.csv"     # IndiaMART human QA audit
RUBRICS_DIR = ROOT / "configs" / "rubrics"  # built-in rubrics (committed)

# --- vocabulary ------------------------------------------------------------------------

Grade = Literal["pass", "non_fatal", "fatal", "na"]     # the QA sheet's Pass / Non-Fatal / Fatal / NA
Overall = Literal["pass", "non_fatal", "fatal"]
RootType = Literal["prompt", "asr", "knowledge", "flow", "platform"]
RiskFlag = Literal["wrong_requirement", "frustration", "loop", "low_confidence", "human_request"]
CallSource = Literal["production", "audited", "fixture", "replay"]
SEVERITY = {"pass": 0, "non_fatal": 1, "fatal": 2}     # na is ignored when taking the worst


def worst(grades) -> Overall:
    """The QA team's own rule: a call's Overall Quality is its worst graded parameter
    (checked on the audit sheet: 1,941 of 1,962 rows agree)."""
    graded = [g for g in grades if g in SEVERITY]
    return max(graded, key=SEVERITY.get, default="pass")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- Listen ----------------------------------------------------------------------------

class Turn(_Frozen):
    speaker: Literal["bot", "buyer"]
    text: str
    start_s: float | None = None
    end_s: float | None = None
    confidence: float | None = None


class Transcript(_Frozen):
    call_id: str
    source: CallSource
    turns: tuple[Turn, ...]
    recording_url: str | None = None
    vendor: str | None = None               # SQUADSTACK | SARVAM
    language: str | None = None             # hi | en | te | mixed
    duration_s: float | None = None
    prompt_version: str = "v1"              # registry version live on (or tested by) this call
    variant: Literal["A", "B"] | None = None
    provider: str = "unknown"               # "openrouter:<model>" | "sarvam" | "fixture" | "replay:<model>"
    meta: dict[str, str] = {}               # hotlead_type, bot, ...; never phone numbers


# --- Judge -----------------------------------------------------------------------------

class Rubric(_Frozen):
    id: str                                 # snake_case, stable across versions
    version: int = 1
    name: str
    category: str                           # Outcome | Quantity | Specs | Details | Conversation | Speech | Catalog | Compliance | Custom
    question: str                           # what the judge answers about the transcript
    guidance: str = ""                      # how to grade, in the auditors' terms
    fatal_when: str = ""
    non_fatal_when: str = ""
    applies_when: str = ""                  # precondition; the grade is na when it does not hold
    root_type: RootType = "prompt"
    weight: int = Field(3, ge=1, le=5)
    needs_audio: bool = False               # dead air, pronunciation: a transcript-only judge answers na
    qa_column: str | None = None            # the matching column in AUDIT_CSV, for agreement
    builtin: bool = True
    author: str = "IndiaMART QA matrix"
    active: bool = True
    examples_pass: tuple[str, ...] = ()
    examples_fail: tuple[str, ...] = ()


class Evidence(_Frozen):
    turn: int                               # index into Transcript.turns
    quote: str


class RubricVerdict(_Frozen):
    rubric_id: str
    rubric_version: int
    grade: Grade
    confidence: float = Field(ge=0, le=1)
    reason: str
    evidence: tuple[Evidence, ...] = ()


class CallAudit(_Frozen):
    call_id: str
    prompt_version: str
    overall: Overall                        # worst(verdict grades)
    verdicts: tuple[RubricVerdict, ...]
    disposition: str | None = None          # auto-disposition, e.g. "BL Approved", "BL Enriched", "BL Deleted"
    risk_flags: tuple[RiskFlag, ...] = ()
    summary: str = ""                       # one line: what happened on the call
    model: str
    rubric_set_hash: str
    cost_usd: float = 0.0


class AgreementRow(_Frozen):
    rubric_id: str
    n: int                                  # calls graded by both, na included as a class
    accuracy: float
    kappa: float | None
    fatal_recall: float | None              # of the human Fatal grades, the share the judge also called fatal
    confusion: dict[str, dict[str, int]]    # human grade -> judge grade -> count


class AgreementReport(_Frozen):
    n_calls: int
    overall_accuracy: float                 # Overall Quality, 3 classes
    overall_kappa: float | None
    fatal_vs_rest_accuracy: float           # PS01 headline: Fatal vs (Non-Fatal or Pass)
    rows: tuple[AgreementRow, ...]
    model: str
    held_out: bool                          # True when no call here was used to tune the rubrics


# --- Improve ---------------------------------------------------------------------------

class RootCause(_Frozen):
    id: str
    rubric_id: str
    name: str
    description: str
    type: RootType
    n_audited: int
    n_calls: int                            # calls affected
    n_fatal: int
    n_non_fatal: int
    frequency: float                        # n_calls / n_audited
    severity: float                         # (3*n_fatal + n_non_fatal) / (3*n_calls)
    priority: float                         # 100 * (3*n_fatal + n_non_fatal) / (3*n_audited)
    example_call_ids: tuple[str, ...]
    prompt_version: str


class Edit(_Frozen):
    mode: Literal["replace_text", "append"]
    find: str | None = None                 # replace_text: must occur exactly once in the base prompt
    replace: str | None = None
    text: str | None = None                 # append
    why: str


class GateCheck(_Frozen):
    id: str
    question: str
    probability: float
    blocking: bool
    threshold: float = 0.5


class GateResult(_Frozen):
    passed: bool
    checks: tuple[GateCheck, ...]
    model: str


class ReplayCase(_Frozen):
    call_id: str                            # the real call the scenario was built from
    grade_a: Grade
    grade_b: Grade


class ReplayResult(_Frozen):
    rubric_id: str
    n_cases: int
    fail_rate_a: float                      # the same scenarios simulated with Prompt A: the fair "before"
    fail_rate_b: float
    fail_rate_real: float                   # in the real calls the scenarios came from
    regressions: dict[str, float]           # other rubric id -> fail rate change, B minus A
    cases: tuple[ReplayCase, ...]
    seconds: float
    models: dict[str, str]                  # bot / buyer / judge


class Fix(_Frozen):
    id: str
    root_cause_id: str
    base_version: str
    edits: tuple[Edit, ...]
    rationale: str
    prompt_b_path: str                      # relative to ROOT
    prompt_b_sha256: str
    diff: str                               # unified diff, Prompt A -> Prompt B
    gate: GateResult | None = None
    replay: ReplayResult | None = None
    model: str


# --- Prove and roll out ----------------------------------------------------------------

class PromptVersion(_Frozen):
    version: str                            # v1, v2, ...
    path: str                               # relative to ROOT
    sha256: str
    parent: str | None = None
    fix_id: str | None = None
    status: Literal["production", "candidate", "retired", "rejected"]
    created_ts: str


Stage = Literal["listen", "judge", "improve", "gate", "replay", "prove", "approval", "rollout", "done", "blocked"]


class Cycle(_Frozen):
    n: int
    production_version: str                 # Prompt A of this cycle
    stage: Stage
    root_cause_id: str | None = None
    fix_id: str | None = None
    candidate_version: str | None = None    # Prompt B of this cycle
    experiment_id: str | None = None
    outcome: str | None = None              # engine terminal state, or GATE_BLOCKED / REPLAY_NO_GAIN
    notes: tuple[str, ...] = ()
    started_ts: str
    updated_ts: str


# --- JSON storage ----------------------------------------------------------------------

def save(path: Path, obj) -> None:
    """Atomic write of a model, a list of models, or plain JSON data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(obj, BaseModel):
        data = obj.model_dump(mode="json")
    elif isinstance(obj, list | tuple):
        data = [o.model_dump(mode="json") if isinstance(o, BaseModel) else o for o in obj]
    else:
        data = obj
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load(path: Path, model=None, default=None):
    """Read JSON; with `model`, validate a dict as that model or a list as a list of them."""
    if not path.exists():
        return default
    data = json.loads(path.read_text(encoding="utf-8"))
    if model is None:
        return data
    return [model.model_validate(d) for d in data] if isinstance(data, list) else model.model_validate(data)
