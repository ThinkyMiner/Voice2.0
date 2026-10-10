"""Loop contracts: the models and signatures every loop module codes against (PLAN_LOOP.md §L7).

Orchestrator-owned, like the base contracts.py. Subagents never edit this file; they report
proposed changes instead. Part 1 is the pydantic models; Part 2 is one stub class per loop module
listing the functions it must expose, with exact parameter names, order and defaults.
`tests/loop/test_loop_contracts.py` checks every built loop module against its stub.

Nothing here imports the engine path's internals or any simulator ground truth; the loop reaches
the base only through its public functions (PLAN_LOOP.md §L1.2).
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import sqlite3

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

from abtest.contracts import (CONFIGS_DIR, ROOT, Boundaries, ExperimentConfig, LookResult, Patch,
                              SecondaryStats, Verdict)

# ---------------------------------------------------------------------------------------
# Paths. Everything that can hold personal data lives under data/ (gitignored, TEAM.md §7).
# ---------------------------------------------------------------------------------------

LOOP_DATA_DIR = ROOT / "data" / "derived" / "loop"
CACHE_DIR = LOOP_DATA_DIR / "cache"            # model-call cache (PLAN_LOOP §L1.5)
RECORDINGS_DIR = ROOT / "data" / "squadstack" / "recordings"
LOOP_CONFIGS_DIR = ROOT / "configs_loop"     # outside configs/: the base tests treat configs/ as engine config
RUBRICS_DIR = LOOP_CONFIGS_DIR / "rubrics"
DEFAULT_LOOP_CONFIG = LOOP_CONFIGS_DIR / "default.yaml"
PRICES = LOOP_CONFIGS_DIR / "prices.yaml"
LOOP_REPORTS_DIR = ROOT / "reports" / "loop"   # tracked: aggregates only, never transcript text

Flow = Literal["buyer", "seller"]
Kind = Literal["yes_no", "score_1_5"]
Polarity = Literal["good", "bad"]
Role = Literal["bot", "buyer"]
Source = Literal["corpus", "live", "dogfood", "bot_test"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def sha256_json(obj: Any) -> str:
    """Hash of canonical JSON: sorted keys, compact separators, UTF-8 (the base freeze() convention)."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------
# Part 1: models
# ---------------------------------------------------------------------------------------

# --- rubric (§L2.2) ---------------------------------------------------------------------

class Criterion(_Frozen):
    id: str = Field(min_length=1)
    kind: Kind
    polarity: Polarity
    weight: float = Field(gt=0)        # every weight is positive; polarity carries the direction
    text: str = Field(min_length=1)
    hard: bool = False                 # the dogfood gate requires every call to pass a hard criterion


class JudgeSettings(_Frozen):
    model: str
    temperature: float = Field(default=0.0, ge=0)
    reasoning_effort: str = "low"
    max_tokens: int = Field(default=2048, gt=0)


class PassThresholds(_Frozen):
    bad: float = Field(default=0.5, gt=0, lt=1)    # a bad yes_no fails at p >= bad
    good: float = Field(default=0.5, gt=0, lt=1)   # a good criterion fails at g < good


class Rubric(_Frozen):
    id: str = Field(min_length=1)
    flow: Flow | Literal["any"] = "any"
    judge: JudgeSettings
    criteria: tuple[Criterion, ...] = Field(min_length=1)
    risk: tuple[str, ...] = ()
    aggregate: str = "weighted_mean"   # or a dotted path: fn(answers, rubric) -> float in [0, 1]
    pass_thresholds: PassThresholds = PassThresholds()

    @model_validator(mode="after")
    def _consistent(self):
        ids = [c.id for c in self.criteria]
        if len(ids) != len(set(ids)):
            raise ValueError(f"duplicate criterion ids in rubric {self.id}")
        if unknown := sorted(set(self.risk) - set(ids)):
            raise ValueError(f"risk names unknown criteria {unknown}")
        if bad := sorted(c for c in self.risk
                         if next(x for x in self.criteria if x.id == c).kind != "yes_no"):
            raise ValueError(f"risk criteria must be yes_no, got {bad}")
        return self


class JudgeAnswer(_Frozen):
    criterion_id: str
    value: float                       # p in [0, 1] for yes_no; s in [1, 5] for score_1_5
    probability: float | None = None   # the judge's stated confidence, if any (informational)
    provider: str


# --- transcripts (§L2.1-2.3) -------------------------------------------------------------

class DiarizedSegment(_Frozen):
    speaker_id: str
    start_s: float
    end_s: float
    text: str


class DiarizedTranscript(_Frozen):
    """What speech-to-text returns: speaker ids, not roles."""
    call_id: str
    language: str | None
    segments: tuple[DiarizedSegment, ...]
    stt_model: str
    audio_seconds: float


class Turn(_Frozen):
    role: Role
    start_s: float
    end_s: float
    text: str


class Transcript(_Frozen):
    """Role-labelled transcript: the only thing the judge, the simulator and the diagnosis read."""
    call_id: str
    source: Source
    language: str | None
    turns: tuple[Turn, ...]
    role_confidence: float = Field(ge=0, le=1)
    stt_model: str
    audio_seconds: float


class RubricScore(_Frozen):
    call_id: str
    rubric_hash: str
    judge_model: str
    answers: tuple[JudgeAnswer, ...]
    rubric_score: float = Field(ge=0, le=1)
    rubric_risk: float = Field(ge=0, le=1)
    failed: tuple[str, ...]            # criterion ids failing per pass_thresholds (labels for diagnosis/dogfood only)


# --- candidates, bot tests, generations (§L2.5-2.9) ---------------------------------------

class PersonaCard(_Frozen):
    card_id: str
    source_call_id: str
    requirement: str
    render_vars: dict[str, str]
    language: str
    tone: str
    irritation_point: str | None = None
    asked_for_human: bool = False
    script: tuple[str, ...] = ()       # buyer lines the stub simulator replays in order


class Candidate(_Frozen):
    candidate_id: str
    generation: int = Field(ge=1)
    patch: Patch
    patch_hash: str
    rationale: str
    targets: tuple[str, ...]
    prechecks: dict[str, bool] = {}
    gate: dict[str, float] | None = None
    bot_test_score: float | None = None
    bot_test_ci: tuple[float, float] | None = None
    status: Literal["proposed", "precheck_failed", "gate_blocked", "bot_tested", "picked",
                    "not_picked", "dogfood_failed", "live", "promoted", "not_promoted"] = "proposed"


GenState = Literal[
    "GEN_START", "OBSERVED", "AUDIT_PENDING", "DIAGNOSED", "PROPOSED", "GATED", "BOT_TESTED",
    "NO_CANDIDATE", "DOGFOOD", "DOGFOOD_FAILED", "DOGFOOD_REVIEW", "REJECTED_BY_APPROVER",
    "LIVE_AB", "PROMOTED", "STOPPED_HARM", "BLOCKED_GUARDRAIL", "INCONCLUSIVE", "HALTED_SRM",
    "REJECTED", "GEN_END",
]


class Generation(_Frozen):
    generation: int = Field(ge=1)
    loop_id: str
    state: GenState
    base_prompt_hash: str
    rubric_hash: str
    pick: str | None = None            # candidate_id
    experiment_id: str | None = None
    terminal: str | None = None
    spend_inr: float = 0.0
    started_ts: datetime
    updated_ts: datetime


class SpendRow(_Frozen):
    """Reserved before a paid call, settled after it (PLAN_LOOP §L2.9): retries never double-count."""
    spend_id: str                      # sha256(generation, step, cache key)
    generation: int
    step: str
    state: Literal["reserved", "settled"]
    estimate_inr: float = Field(ge=0)
    actual_inr: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


# --- loop config (§L7.3) -------------------------------------------------------------------

class LoopSection(_Frozen):
    id: str = Field(min_length=1)
    flow: Flow
    mode: Literal["sim", "live"]
    auto_after_generations: int = Field(ge=0)


class RoleSettings(_Frozen):
    reasoning_effort: str
    max_tokens: int = Field(gt=0)


class ProviderSection(_Frozen):
    name: Literal["sarvam", "stub"]
    rpm: int = Field(gt=0)
    concurrency: int = Field(gt=0)
    roles: dict[Literal["judge", "proposer", "buyer", "bot"], RoleSettings]


class SttSection(_Frozen):
    model: str
    language_code: str
    max_jobs_in_flight: int = Field(gt=0)
    poll_interval_s: float = Field(ge=30)   # Starter tier allows 20 batch-STT requests/minute


class ObserveSection(_Frozen):
    window_days: int = Field(gt=0)
    min_duration_s: float = Field(ge=0)


class AuditSection(_Frozen):
    calls_per_arm: int = Field(gt=0)
    corpus_calls: int = Field(gt=0)
    min_rho: float = Field(gt=0, le=1)
    min_hard_agreement: float = Field(gt=0, le=1)


class DiagnoseSection(_Frozen):
    top_k: int = Field(gt=0)


class ProposeSection(_Frozen):
    candidates: int = Field(gt=0)
    max_candidates: int = Field(gt=0)
    max_patch_chars: int = Field(gt=0)
    modes: tuple[Literal["append", "replace_text", "replace_section"], ...]


class GateSection(_Frozen):
    block_threshold: float = Field(gt=0, lt=1)
    extra_questions: tuple[str, ...] = ()


class BotTestsSection(_Frozen):
    personas: int = Field(gt=0)
    max_turns: int = Field(gt=0)
    min_gap: float = Field(ge=0)
    bootstrap: int = Field(gt=0)
    seed: int


class DogfoodSection(_Frozen):
    calls: int = Field(gt=0)
    min_score: float = Field(ge=0, le=1)


class GuardrailSection(_Frozen):
    rubric_risk_margin: float = Field(gt=0, lt=1)


class IngestSection(_Frozen):
    score_timeout_h: float = Field(gt=0)


class LiveSection(_Frozen):
    surface: Literal["fake", "s1", "s2"]
    disposition_field: str


class SimSection(_Frozen):
    """Simulated traffic for the live A/B in sim mode (PLAN_LOOP §L9.3-4). Ground truth: only
    loop/live_ab.py and loop/sim_scores.py read it, never the engine path."""
    base_sim: str                      # SimConfig yaml (reference, volume, fault); true_effect is replaced
    effect_mode: Literal["from_bot_tests", "scenario_sequence"]
    lift_per_score_point: float        # injected relative lift per +1.0 of bot-test rubric_score delta
    max_abs_lift: float = Field(gt=0, lt=1)
    scenario_sequence: tuple[float, ...] = ()   # primary_rel_lift per generation, cycled
    risk_base: dict[str, float]        # per-call rubric_risk level by disposition; "default" required
    risk_noise_sd: float = Field(ge=0)
    seed: int


class Caps(_Frozen):
    max_share_b: float = Field(gt=0, le=0.5)
    max_live_calls_per_day: int = Field(ge=0)
    max_spend_inr: float = Field(ge=0)
    max_generations: int = Field(gt=0)


class LoopConfig(_Frozen):
    loop: LoopSection
    template: str                      # path relative to ROOT
    rubric: str                        # path relative to ROOT
    provider: ProviderSection
    stt: SttSection
    observe: ObserveSection
    audit: AuditSection
    diagnose: DiagnoseSection
    propose: ProposeSection
    gate: GateSection
    bot_tests: BotTestsSection
    dogfood: DogfoodSection
    guardrail: GuardrailSection
    ingest: IngestSection
    live: LiveSection
    sim: SimSection
    caps: Caps


class Prices(_Frozen):
    currency: Literal["INR"]
    llm: dict[str, dict[str, float]]
    stt: dict[str, dict[str, float]]
    free_credit_new_account: float = 0.0


class GateQuestion(_Frozen):
    id: str
    text: str
    blocking: bool = True


class BotTestResult(_Frozen):
    """run_bot_tests output. Scores are paired on the same cards: delta = candidate − base."""
    base_mean_score: float
    base_mean_risk: float
    candidates: tuple[Candidate, ...]  # with bot_test_score (= mean paired delta) and bot_test_ci set
    risk_delta: dict[str, float]       # candidate_id -> mean paired rubric_risk delta
    hard_fail: dict[str, bool]         # candidate_id -> any card failed a hard criterion
    pick: str | None
    reason: str                        # one readable sentence: why this pick, or why none


class LiveAbResult(_Frozen):
    experiment_id: str
    terminal: str                      # base State value reached
    looks: int
    lift_rel: float | None
    lift_ci95: tuple[float, float] | None   # on the absolute lift
    rubric_delta: float | None
    rubric_rci: tuple[float, float] | None
    decided_by: Literal["engine", "rubric_guardrail"]
    injected_lift: float               # simulated ground truth, reported, never used by the engine
    injected_risk_shift: float
    reason: str


# ---------------------------------------------------------------------------------------
# Part 2: module signatures. Implement each as a module-level function with the same
# parameter names, order and defaults. Bodies here are documentation only.
# ---------------------------------------------------------------------------------------

class Provider(Protocol):
    """loop/sarvam.py: `get_provider(cfg) -> Provider`; implementations `SarvamProvider`, `StubProvider`.
    Every call goes through the disk cache (CACHE_DIR) and, for paid calls, reserves then settles a
    SpendRow. Model calls happen only here (PLAN_LOOP §L1.3)."""
    name: str

    def complete_json(self, role: str, system: str, user: str, schema: dict) -> dict: ...
    def chat(self, role: str, messages: Sequence[Mapping[str, str]]) -> str: ...
    def transcribe_batch(self, paths: Sequence[Path], call_ids: Sequence[str]) -> list[DiarizedTranscript]: ...


class SarvamModule:  # src/abtest/loop/sarvam.py — L1 agent B
    @staticmethod
    def load_loop_config(path: str | Path = DEFAULT_LOOP_CONFIG) -> LoopConfig: ...

    @staticmethod
    def load_prices(path: str | Path = PRICES) -> Prices: ...

    @staticmethod
    def get_provider(cfg: LoopConfig, name: str | None = None) -> Provider:
        """`name` overrides cfg.provider.name; env LOOP_PROVIDER overrides both."""


class StoreLoopModule:  # src/abtest/loop/store_loop.py — L1 agent A (tables of PLAN_LOOP §L2.9, §L9)
    @staticmethod
    def connect_loop(path: str | Path | None = None) -> "sqlite3.Connection":
        """store.connect(path or DB_PATH) plus the loop tables (CREATE IF NOT EXISTS, loop_schema_version)."""

    @staticmethod
    def put_transcript(conn: "sqlite3.Connection", t: Transcript) -> None: ...

    @staticmethod
    def get_transcripts(conn: "sqlite3.Connection", source: str | None = None,
                        call_ids: Sequence[str] | None = None) -> list[Transcript]: ...

    @staticmethod
    def put_score(conn: "sqlite3.Connection", s: RubricScore, lead_id: str, prompt_hash: str | None = None) -> None: ...

    @staticmethod
    def get_scores(conn: "sqlite3.Connection", rubric_hash: str, prompt_hash: str | None = None) -> pd.DataFrame:
        """call_id, lead_id, prompt_hash, rubric_score, rubric_risk, failed, one column per criterion goodness."""

    @staticmethod
    def put_generation(conn: "sqlite3.Connection", g: Generation) -> None: ...

    @staticmethod
    def get_generations(conn: "sqlite3.Connection", loop_id: str) -> list[Generation]: ...

    @staticmethod
    def put_candidate(conn: "sqlite3.Connection", c: Candidate, loop_id: str) -> None: ...

    @staticmethod
    def get_candidates(conn: "sqlite3.Connection", loop_id: str, generation: int | None = None) -> list[Candidate]: ...

    @staticmethod
    def reserve_spend(conn: "sqlite3.Connection", row: SpendRow) -> bool:
        """Insert a reserved row; False if spend_id already exists (a retry: do not double-count)."""

    @staticmethod
    def settle_spend(conn: "sqlite3.Connection", spend_id: str, actual_inr: float,
                     input_tokens: int | None, output_tokens: int | None) -> None: ...

    @staticmethod
    def total_spend(conn: "sqlite3.Connection") -> float:
        """Settled actuals plus reserved estimates."""

    @staticmethod
    def put_guard_log(conn: "sqlite3.Connection", experiment_id: str, look: int, payload: dict) -> None: ...


class CorpusModule:  # src/abtest/loop/corpus.py — L1 agent A
    @staticmethod
    def load_corpus(path: str | Path) -> pd.DataFrame:
        """Columns: call_id, lead_id, ts, answered, disposition (None), duration_s, attempt_no (None),
        bot_version, then hotlead_type, recording_url and any other source column. Sorted by
        (lead_id, ts, call_id)."""


class TranscribeModule:  # src/abtest/loop/transcribe.py — L1 agent A
    @staticmethod
    def download(calls: pd.DataFrame, out_dir: Path = RECORDINGS_DIR) -> pd.DataFrame:
        """Adds `audio_path` (None on failure) and `download_error`; skips files already present."""

    @staticmethod
    def transcribe(calls: pd.DataFrame, provider: Provider, rubric: "Rubric | None" = None,
                   min_duration_s: float = 10.0, out: Path = LOOP_DATA_DIR / "transcripts.jsonl") -> list[Transcript]:
        """Transcribes calls >= min_duration_s with a downloaded file, labels roles, appends to `out`
        (one Transcript JSON per line), resumes by skipping call_ids already in `out`."""


class RolesModule:  # src/abtest/loop/roles.py — L1 agent A
    @staticmethod
    def opening_lines(prompt_text: str, n: int = 5) -> list[str]:
        """Short lines the bot is told to open with, extracted from the flow's prompt."""

    @staticmethod
    def label_roles(diarized: DiarizedTranscript, opening_lines: Sequence[str],
                    source: Source = "corpus") -> Transcript: ...


class PiiModule:  # src/abtest/loop/pii.py — L1 agent A
    @staticmethod
    def mask(text: str, known_names: Sequence[str] = ()) -> str:
        """Regex masks (digit runs, spoken number words in Latin/Devanagari) plus known names."""


class RubricModule:  # src/abtest/loop/rubric.py — L1 agent B
    @staticmethod
    def load_rubric(path: str | Path) -> Rubric: ...

    @staticmethod
    def rubric_hash(rubric: Rubric) -> str: ...

    @staticmethod
    def goodness(criterion: Criterion, answer: JudgeAnswer) -> float: ...

    @staticmethod
    def score_from_answers(rubric: Rubric, answers: Mapping[str, JudgeAnswer]) -> tuple[float, float, tuple[str, ...]]:
        """(rubric_score, rubric_risk, failed criterion ids)."""


class ScorerModule:  # src/abtest/loop/scorer.py — L1 agent B
    @staticmethod
    def judge_call(transcript: Transcript, rubric: Rubric, provider: Provider) -> list[JudgeAnswer]: ...

    @staticmethod
    def score_calls(transcripts: Iterable[Transcript], rubric: Rubric, provider: Provider) -> list[RubricScore]: ...

    @staticmethod
    def scores_frame(scores: Sequence[RubricScore]) -> pd.DataFrame:
        """One row per call: call_id, rubric_score, rubric_risk, failed, and one column per criterion."""

    @staticmethod
    def per_lead(scores: pd.DataFrame, calls: pd.DataFrame) -> pd.DataFrame:
        """lead_id, bot_version, n_calls_scored, rubric_score, rubric_risk, and one column per
        criterion: the mean over the lead's scored calls (the lead is the unit, PLAN.md §2.6)."""


class AuditModule:  # src/abtest/loop/audit.py — L1 agent C
    @staticmethod
    def export_audit(scores: pd.DataFrame, calls: pd.DataFrame, n: int, seed: int, out: Path) -> Path:
        """Stratified by bot_version; writes the sheet the reviewer fills (gitignored path)."""

    @staticmethod
    def agreement(sheet: Path, rubric: Rubric, min_rho: float, min_hard_agreement: float) -> dict:
        """rho, rho_ci95, per-criterion agreement, role accuracy, n, trusted (bool)."""


class ReportModule:  # src/abtest/loop/report.py — L1 agent C
    @staticmethod
    def rubric_report(per_lead: pd.DataFrame, calls: pd.DataFrame, rubric: Rubric, corpus_name: str,
                      out_dir: Path = LOOP_REPORTS_DIR) -> Path:
        """Aggregates only: per-criterion failure rates with lead-level 95% CIs by bot_version and
        hotlead_type, early-hang-up rate, score distribution. No transcript text, no ids."""


class DiagnoseModule:  # src/abtest/loop/diagnose.py — L2 agent B
    @staticmethod
    def diagnose(scores: pd.DataFrame, rubric: Rubric, top_k: int) -> pd.DataFrame:
        """`scores` = store_loop.get_scores(...) rows (lead_id per row; bot-test rows use the card id).
        Lead is the unit: per lead, failure = mean over its calls of (1 - goodness); then mean over
        leads with a normal-approximation 95% CI. Columns: criterion_id, failure_rate, ci_low, ci_high,
        weight, weighted_cost, n_leads, rank, target (rank <= top_k). Sorted by rank."""

    @staticmethod
    def excerpts(transcripts: Sequence[Transcript], scores: pd.DataFrame, criterion_id: str, n: int = 3,
                 max_chars: int = 600) -> list[str]:
        """Masked (pii.mask) BOT/PERSON turn excerpts from the n calls that fail the criterion worst."""


class RenderModule:  # src/abtest/loop/render.py — L2 agent C
    @staticmethod
    def template_variables(template: str) -> set[str]:
        """Names used by plain \ufeff name \ufeff interpolation markers and by Jinja tags."""

    @staticmethod
    def render(template: str, variables: Mapping[str, str]) -> str:
        """Interpolation markers -> values; Jinja tags evaluated with StrictUndefined;
        agent/tool markers (\ufeff@ name\ufeff) are left verbatim."""

    @staticmethod
    def apply_patch(template: str, patch: Patch) -> str:
        """The base config.py patch semantics on a string (append, replace_text exactly-once,
        replace_section); `full` is rejected."""

    @staticmethod
    def precheck(template: str, patch: Patch, fixture_vars: Sequence[Mapping[str, str]],
                 max_patch_chars: int) -> dict[str, bool]:
        """{applies, jinja_parses, renders, within_size}."""


class LedgerModule:  # src/abtest/loop/ledger.py — L2 agent C
    @staticmethod
    def patch_hash(patch: Patch) -> str: ...

    @staticmethod
    def tried_hashes(conn: "sqlite3.Connection", loop_id: str) -> set[str]: ...

    @staticmethod
    def ledger_summary(conn: "sqlite3.Connection", loop_id: str, max_items: int = 20) -> list[dict]:
        """Short records of past candidates for the proposer: generation, mode, targets, rationale,
        status, bot_test_score, live terminal state."""


class ProposerModule:  # src/abtest/loop/proposer.py — L2 agent C
    @staticmethod
    def propose(base_prompt: str, diagnosis: pd.DataFrame, excerpts: Mapping[str, Sequence[str]],
                rubric: Rubric, ledger: Sequence[Mapping], tried: set[str], k: int, generation: int,
                provider: Provider, cfg: LoopConfig) -> list[Candidate]:
        """k candidates with prechecks filled; candidates failing a precheck or already tried are
        returned with status precheck_failed (never silently dropped, so the ledger records them)."""


class GateModule:  # src/abtest/loop/gate.py — L2 agent C
    @staticmethod
    def gate_questions(cfg: LoopConfig) -> list[GateQuestion]:
        """§E4.2's blocking questions (alters_commitments, weakens_safety_rule, contradicts_instruction)
        plus cfg.gate.extra_questions; warnings raises_ask_limit, likely_longer_calls (non-blocking)."""

    @staticmethod
    def gate(base_prompt: str, candidate: Candidate, provider: Provider, cfg: LoopConfig) -> Candidate:
        """Fills candidate.gate; status gate_blocked if any blocking probability >= threshold."""


class BuyerSimModule:  # src/abtest/loop/buyer_sim.py — L2 agent C
    @staticmethod
    def load_seed_personas(path: str | Path) -> list[PersonaCard]: ...

    @staticmethod
    def converse(prompt_template: str, card: PersonaCard, provider: Provider, max_turns: int,
                 call_id: str) -> Transcript:
        """Bot turn: provider.chat('bot', [system=render(template, card.render_vars), ...history]).
        Person turn: provider.chat('buyer', [system=persona instructions, ...history]). The bot speaks
        first. Returns a Transcript with source='bot_test', role_confidence=1.0, stt_model='text'."""


class BotTestsModule:  # src/abtest/loop/bot_tests.py — L2 agent C
    @staticmethod
    def estimate_cost(n_cards: int, n_prompts: int, max_turns: int, prompt_tokens: int, prices: Prices) -> float: ...

    @staticmethod
    def run_bot_tests(base_prompt: str, candidates: Sequence[Candidate], cards: Sequence[PersonaCard],
                      rubric: Rubric, provider: Provider, cfg: LoopConfig, generation: int,
                      conn: "sqlite3.Connection | None" = None) -> BotTestResult:
        """Replays every card against the base and each eligible candidate; scores every transcript
        (scorer.score_calls); stores transcripts and scores when conn is given (prompt_hash set);
        paired deltas per card; paired bootstrap CI (cfg.bot_tests.bootstrap, seed); pick rule of
        PLAN_LOOP §L2.6 with the eligibility fix (§L9)."""


class SynthDataModule:  # src/abtest/loop/synthdata.py — L3 agent D
    @staticmethod
    def write_synthetic(out_dir: str | Path, seed: int = 20261010) -> dict[str, Path]:
        """Writes a synthetic `Call_Logs_Hackathon.csv` in the buyer source format that data.load
        reads (baseline per-triggered-lead BL success ≈ 0.667, abrupt disconnects ≈ 0.19 per lead,
        answer rate ≈ 0.76, 5 days, ~825 leads). Returns {"buyer_logs": path}."""


class SimScoresModule:  # src/abtest/loop/sim_scores.py — L3 agent D
    @staticmethod
    def score_sim_calls(calls: pd.DataFrame, variant_by_lead: Mapping[str, str], shift_b: float,
                        risk_base: Mapping[str, float], noise_sd: float, seed: int) -> pd.DataFrame:
        """call_id, rubric_risk for answered calls: base level by disposition + N(0, noise_sd), + shift_b
        in arm B, clipped to [0, 1]; deterministic per (seed, call_id)."""


class GuardModule:  # src/abtest/loop/guard.py — L3 agent D
    @staticmethod
    def rubric_guardrail(outcomes: pd.DataFrame, risk_by_lead: Mapping[str, float], look: int,
                         bounds: "Boundaries", margin: float) -> "SecondaryStats":
        """Mature leads with a risk value; delta = mean_B − mean_A (absolute), Welch SE, ci95, rci =
        delta ± bounds.win[look−1]·se; name 'rubric_risk', type continuous, role guardrail."""

    @staticmethod
    def combine(base: "Verdict", rubric: "SecondaryStats", look: "LookResult", bounds: "Boundaries",
                cfg: "ExperimentConfig") -> tuple["Verdict", bool]:
        """(verdict, rubric_decided) per PLAN_LOOP §L9.3."""


class LiveAbModule:  # src/abtest/loop/live_ab.py — L3 agent D
    @staticmethod
    def run_live_ab(conn: "sqlite3.Connection", cfg: LoopConfig, base_prompt_path: str, candidate: Candidate,
                    generation: int, injected_lift: float, injected_risk_shift: float, seed: int,
                    quiet: bool = True) -> LiveAbResult:
        """Simulated traffic through the real engine (mirrors runner._run), the rubric guardrail between
        rules.evaluate_look and decision.apply_verdict, guard rows in loop_guard_log."""


class ControllerModule:  # src/abtest/loop/controller.py — L4 agent E
    @staticmethod
    def run_generation(ctx: "LoopContext", generation: int) -> Generation: ...

    @staticmethod
    def run_loop(ctx: "LoopContext", generations: int) -> list[Generation]: ...


LOOP_MODULE_CONTRACTS: dict[str, type] = {
    "sarvam": SarvamModule, "store_loop": StoreLoopModule, "corpus": CorpusModule,
    "transcribe": TranscribeModule, "roles": RolesModule, "pii": PiiModule, "rubric": RubricModule,
    "scorer": ScorerModule, "audit": AuditModule, "report": ReportModule, "diagnose": DiagnoseModule,
    "render": RenderModule, "ledger": LedgerModule, "proposer": ProposerModule, "gate": GateModule,
    "buyer_sim": BuyerSimModule, "bot_tests": BotTestsModule, "synthdata": SynthDataModule,
    "sim_scores": SimScoresModule, "guard": GuardModule, "live_ab": LiveAbModule,
    "controller": ControllerModule,
}
