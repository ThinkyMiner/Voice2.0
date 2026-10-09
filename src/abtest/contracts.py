"""Shared contracts: the interfaces every module codes against.

Orchestrator-owned. Subagents never edit this file; they report proposed changes instead.

Part 1 is the pydantic models passed between modules. Part 2 is one stub class per module,
listing the functions that module must expose with exact parameter names, order and defaults.
`tests/test_contracts.py` checks every built module against its stub.

Freeze status: the models and the Config/Data/Profile/Power stubs freeze at tag phase-1.
Stubs for later modules are PROVISIONAL until their phase starts; each class says which phase.
"""

import math
import os
from dataclasses import dataclass
import sqlite3
from collections.abc import Sequence
from datetime import date, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("ABTEST_DATA_DIR", ROOT / "data"))  # read-only, always
CONFIGS_DIR = ROOT / "configs"
SIMS_DIR = CONFIGS_DIR / "sims"  # simulator ground truth: the engine path never reads this
REPORTS_DIR = ROOT / "reports"
LOGS_DIR = ROOT / "logs"
DECISIONS_JSONL = LOGS_DIR / "decisions.jsonl"
DB_PATH = Path(os.environ.get("ABTEST_DB", ROOT / "abtest.db"))
BASE_PROMPT = ROOT / "prompts" / "base_buyer_vani.txt"

# ---------------------------------------------------------------------------------------
# Part 1: models
# ---------------------------------------------------------------------------------------

Variant = Literal["A", "B"]
Dataset = Literal["bttc", "buyer_logs"]
Direction = Literal["increase", "decrease"]


class State(StrEnum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    PROMOTED = "PROMOTED"
    REJECTED = "REJECTED"
    STOPPED_HARM = "STOPPED_HARM"
    BLOCKED_GUARDRAIL = "BLOCKED_GUARDRAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    HALTED_SRM = "HALTED_SRM"


TERMINAL = frozenset(
    {State.PROMOTED, State.REJECTED, State.STOPPED_HARM, State.BLOCKED_GUARDRAIL,
     State.INCONCLUSIVE, State.HALTED_SRM}
)

# §2.8 state machine. Terminal states have no outgoing edges.
TRANSITIONS: dict[State, frozenset[State]] = {
    State.DRAFT: frozenset({State.RUNNING}),
    State.RUNNING: frozenset({State.PROMOTED, State.PENDING_APPROVAL, State.STOPPED_HARM,
                              State.BLOCKED_GUARDRAIL, State.INCONCLUSIVE, State.HALTED_SRM}),
    State.PENDING_APPROVAL: frozenset({State.PROMOTED, State.REJECTED}),
    **{s: frozenset() for s in TERMINAL},
}


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --- experiment config (§2.3): the only config the engine reads --------------------------

class Allocation(_Frozen):
    A: float = Field(ge=0, le=1)
    B: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _sums_to_one(self):
        if not math.isclose(self.A + self.B, 1.0, abs_tol=1e-9):
            raise ValueError(f"allocation must sum to 1, got {self.A + self.B}")
        return self


class AppendPatch(_Frozen):
    mode: Literal["append"]
    text: str


class ReplaceSectionPatch(_Frozen):
    """Replace the text strictly between `start` and `end`; both markers are kept and each must
    occur exactly once in the base, with `start` before `end`."""
    mode: Literal["replace_section"]
    start: str
    end: str
    text: str


class ReplaceTextPatch(_Frozen):
    """`find` must occur exactly once in the base prompt, else config validation fails."""
    mode: Literal["replace_text"]
    find: str
    replace: str


class FullPatch(_Frozen):
    """The variant is a complete prompt of its own: exactly one of `path` or `text`."""
    mode: Literal["full"]
    path: str | None = None
    text: str | None = None


Patch = Annotated[AppendPatch | ReplaceSectionPatch | ReplaceTextPatch | FullPatch,
                  Field(discriminator="mode")]


class VariantSpec(_Frozen):
    base: str | None = None  # path relative to ROOT
    patch: Patch | None = None


class Window(_Frozen):
    days: int = Field(gt=0)
    looks: int = Field(gt=0)


class PrimaryMetric(_Frozen):
    name: str
    type: Literal["binary"]
    dispositions: tuple[str, ...] = Field(min_length=1)
    better: Direction
    mde_rel: float = Field(gt=0)


class SecondaryMetric(_Frozen):
    """Guardrail (role=guardrail: worse_if + margin) or reported goal (role=goal: better).

    Continuous: per-lead mean of `column` over answered calls; delta is relative (mB-mA)/mA,
    so the margin is `margin_rel`. Binary: per-lead 0/1 over `dispositions`; delta is
    absolute pB-pA, so the margin is `margin_abs`. A goal never gates a decision.
    """
    name: str
    type: Literal["binary", "continuous"]
    role: Literal["guardrail", "goal"]
    dispositions: tuple[str, ...] | None = None
    column: str = "duration_s"
    worse_if: Direction | None = None
    better: Direction | None = None
    margin_rel: float | None = Field(default=None, gt=0)
    margin_abs: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _consistent(self):
        if self.type == "binary" and not self.dispositions:
            raise ValueError("binary secondary needs dispositions")
        if self.role == "guardrail":
            want, other = (("margin_rel", "margin_abs") if self.type == "continuous"
                           else ("margin_abs", "margin_rel"))
            if self.worse_if is None or getattr(self, want) is None or getattr(self, other) is not None:
                raise ValueError(f"{self.type} guardrail needs worse_if and {want} (and not {other})")
        elif self.better is None or self.margin_rel is not None or self.margin_abs is not None:
            raise ValueError("goal secondary needs `better` and no margin")
        return self

    @property
    def margin(self) -> float | None:
        """Positive margin magnitude in the units of delta."""
        return self.margin_rel if self.margin_rel is not None else self.margin_abs


class ExperimentConfig(_Frozen):
    id: str = Field(min_length=1)
    dataset: Dataset
    unit: Literal["lead_id"]
    trigger: Literal["first_answered_call"]
    salt: str = Field(min_length=1)
    allocation: Allocation
    variants: dict[Variant, VariantSpec]
    window: Window
    maturation_days: float = Field(ge=0)
    # Per-triggered-lead primary rate from reports/data_profile_<dataset>.md. Drives n_max.
    baseline_p: float = Field(gt=0, lt=1)
    primary: PrimaryMetric
    secondary: SecondaryMetric
    alpha_win: float = Field(gt=0, lt=0.5)   # one-sided, O'Brien-Fleming-type spending
    alpha_harm: float = Field(gt=0, lt=0.5)  # one-sided, Pocock-type spending
    power: float = Field(gt=0, lt=1)
    min_info_first_look: float = Field(ge=0, lt=1)
    srm_p_threshold: float = Field(gt=0, lt=1)
    promotion: Literal["auto", "approval"]
    exclude_leads_with_calls_gt: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _both_variants(self):
        if set(self.variants) != {"A", "B"}:
            raise ValueError("variants must define exactly A and B")
        return self


class FrozenConfig(_Frozen):
    """What DRAFT -> RUNNING stores. canonical_json = json.dumps(config.model_dump(mode="json"),
    sort_keys=True, separators=(",", ":"), ensure_ascii=False); config_hash = SHA-256 of its
    UTF-8 bytes; prompt_hash[v] = SHA-256 of the resolved prompt text of variant v."""
    config: ExperimentConfig
    canonical_json: str
    config_hash: str
    prompt_hash: dict[Variant, str]


# --- sim config (§2.3): ground truth, read only by the simulator -----------------------

class TrueEffect(_Frozen):
    primary_rel_lift: float = Field(gt=-1)
    secondary_change: float = 0.0  # relative for continuous, absolute for binary


class Reference(_Frozen):
    """Bootstrap pool. None means unbounded / no filter."""
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    bot_version: tuple[str, ...] | None = None


class Volume(_Frozen):
    daily_leads: float = Field(gt=0)  # from the Phase 1 profile
    scale: float = Field(gt=0)


class Fault(_Frozen):
    drop_b_exposure_rate: float = Field(default=0.0, ge=0, lt=1)


class SimConfig(_Frozen):
    true_effect: TrueEffect
    reference: Reference
    volume: Volume
    fault: Fault = Fault()


class Scenario(_Frozen):
    """configs/scenarios/<name>.yaml. Paths are relative to ROOT. `overrides` are top-level
    experiment-config keys applied before validation; the resolved experiment id is
    "<experiment id>-<scenario name>" so scenario runs never collide in the store."""
    name: str
    experiment: str
    sim: str
    seed: int
    overrides: dict[str, Any] = {}


# --- data -----------------------------------------------------------------------------

class CallRecord(_Frozen):
    """One canonical call row (§2.5). Adapters return a DataFrame with these columns first,
    then any source columns passed through unchanged. Fields a dataset lacks are null."""
    call_id: str
    lead_id: str
    ts: datetime
    answered: bool
    disposition: str
    duration_s: float | None
    attempt_no: int | None
    bot_version: str | None


CANONICAL_COLUMNS: tuple[str, ...] = tuple(CallRecord.model_fields)


class AdapterReport(_Frozen):
    dataset: Dataset
    source_file: str
    rows_read: int
    rows_out: int
    dropped: dict[str, int]          # reason -> rows dropped
    inferred: dict[str, int]         # "<field>: <rule>" -> rows whose value was inferred
    excluded_leads: dict[str, int]   # lead_id -> call count, for exclude_leads_with_calls_gt
    notes: list[str] = []


class VolumeStats(_Frozen):
    """Mean over non-overlapping `window_days` blocks (partial trailing block dropped) of the
    distinct leads dialed in the block (daily_leads) or answered in it (daily_triggered_leads),
    divided by window_days. If the data spans fewer than window_days days, the whole span is
    one block, divided by its day count."""
    window_days: int
    daily_leads: float
    daily_triggered_leads: float


class DataProfile(_Frozen):
    dataset: Dataset
    adapter: AdapterReport
    rows: int
    leads: int
    date_from: datetime
    date_to: datetime
    triggered_leads: int
    # Success rate per triggered lead, counting calls up to baseline_horizon_days after the
    # trigger: window.days, or the longest horizon with full follow-up if the data is shorter.
    baseline_p: float
    baseline_horizon_days: int
    baseline_p_by_horizon_days: dict[int, float]  # same rate at other follow-up horizons
    volume: VolumeStats
    markdown: str


class NMax(_Frozen):
    n_b_fixed: float
    inflation_factor: float
    n_b_max: float
    n_a_max: float
    n_total_max: float


class Preflight(_Frozen):
    experiment_id: str
    baseline_p: float
    mde_rel: float
    share_b: float
    n_max: NMax
    daily_triggered_leads: float
    window_days: int
    maturation_days: float
    days_needed: float
    underpowered: bool
    min_detectable_lift_rel: float   # within the window at the configured split
    scale_to_fit_window: float       # volume multiple at which days_needed == window_days


# --- routing ----------------------------------------------------------------------------

class Exposure(_Frozen):
    experiment_id: str
    lead_id: str
    variant: Variant
    bucket: int
    ts: datetime


class Assignment(_Frozen):
    variant: Variant
    bucket: int
    state: State
    config_hash: str
    prompt_hash: str  # of the assigned variant


class ExperimentState(_Frozen):
    """Live state. `allocation` is the LIVE allocation the router reads: the configured one
    while RUNNING, B=1 after PROMOTED, A=1 after STOPPED_HARM."""
    experiment_id: str
    state: State
    allocation: Allocation
    salt: str
    config_hash: str
    prompt_hash: dict[Variant, str]
    looks_done: int = 0
    info_fractions: tuple[float, ...] = ()  # observed t_k of every look taken so far
    n_b_max: float | None = None
    started_ts_sim: datetime | None = None
    updated_ts_sim: datetime | None = None


# --- analysis (§2.6-2.8) ----------------------------------------------------------------

class LeadOutcome(_Frozen):
    """One row of metrics.lead_outcomes(). The maturity clock starts at the trigger (first
    answered call): mature iff triggered_ts + maturation_days <= look_ts."""
    lead_id: str
    variant: Variant
    assigned_ts: datetime
    triggered_ts: datetime | None
    mature: bool
    primary: int | None       # 0/1, None if not triggered
    secondary: float | None   # per-lead mean (continuous) or 0/1 (binary); None if undefined
    n_calls: int
    n_answered: int


class SrmResult(_Frozen):
    n_A: int
    n_B: int
    configured_share_B: float
    observed_share_B: float
    ci95: tuple[float, float]  # on observed_share_B
    chi2: float
    p: float


class PrimaryStats(_Frozen):
    rate_A: float
    rate_B: float
    lift_abs: float               # rate_B - rate_A
    lift_rel: float               # lift_abs / rate_A
    ci95: tuple[float, float]     # on lift_abs, unpooled SE
    se_pooled: float
    se_unpooled: float
    z: float                      # pooled; oriented so z > 0 means B is better per `better`
    detectable_mde_rel: float | None  # relative lift this sample could detect (rule 5)


class SecondaryStats(_Frozen):
    name: str
    type: Literal["binary", "continuous"]
    role: Literal["guardrail", "goal"]
    mean_A: float
    mean_B: float
    rel_change: float             # (mean_B - mean_A) / mean_A, always relative
    delta: float                  # the tested quantity: relative (continuous) or absolute (binary)
    delta_type: Literal["relative", "absolute"]
    se: float                     # of delta (delta method for continuous, unpooled for binary)
    ci95: tuple[float, float]     # delta +/- 1.96 se
    rci: tuple[float, float]      # delta +/- c_k se, c_k = win boundary at this look
    margin: float | None          # positive magnitude; None for a goal


class LookResult(_Frozen):
    experiment_id: str
    look: int                     # 1-based
    ts_sim: datetime
    is_final: bool
    info_fraction: float          # matured n_B / n_b_max
    n_A: int                      # matured triggered leads
    n_B: int
    primary: PrimaryStats
    secondary: SecondaryStats
    srm_assigned: SrmResult
    srm_triggered: SrmResult


class Boundaries(_Frozen):
    """z-scale boundaries for looks 1..k at the observed information fractions. Win: cross
    if z >= win[k-1]. Harm: cross if z <= -harm[k-1] (harm values are positive)."""
    info_fractions: tuple[float, ...]
    win: tuple[float, ...]
    harm: tuple[float, ...]


class Verdict(_Frozen):
    decision: State | Literal["CONTINUE"]
    rule: int  # §2.8 rule number that matched, 0 if none (CONTINUE)
    reason: str


# --- simulation and split evidence (§2.5, Phase 2) ---------------------------------------

@dataclass(frozen=True, eq=False)
class Pool:
    """Bootstrap pool, built once per run; only simulator functions read its frames.

    The pool unit is a LEAD-WINDOW: one lead's calls inside one non-overlapping
    `window.days` block of the reference period (blocks as in VolumeStats), with timing
    relative to that lead's first call in the block. This matches how `volume.daily_leads`
    is measured (distinct leads dialed per block / window days)."""
    calls: pd.DataFrame   # one row per pool call; simulator-defined columns (incl. pool lead key, offset)
    leads: pd.DataFrame   # one row per pool lead-window; simulator-defined columns
    start: datetime       # sim day 0 = midnight after the reference period's last day
    p: float              # primary success rate per triggered pool lead (the §2.5 injection p)
    secondary_base: float  # binary: per-triggered-lead rate; continuous: mean per-lead mean


@dataclass(frozen=True, eq=False)
class SimDay:
    """What one simulated day produces. `exposures` and `calls` have exactly the store columns
    of their tables and are inserted as is; dropped B exposures (fault injection) are absent
    from `exposures` but their calls are still in `calls`.

    truth: one row per new lead (both arms) with columns lead_id, variant, exposure_logged,
    triggered, primary_cf, primary, secondary_cf, secondary, injected_primary,
    injected_secondary. *_cf = historical (counterfactual) outcome; the others = realised.
    Outcomes use every generated call (calls after the window end are never generated)."""
    exposures: pd.DataFrame
    calls: pd.DataFrame
    truth: pd.DataFrame


class SimTruth(_Frozen):
    """Ground truth for one run. Stored in the sim_truth table and reports/sim_truth_<scenario>.md.
    The engine never reads it. Rates are over triggered leads."""
    experiment_id: str
    scenario: str
    seed: int
    injected_primary_rel_lift: float
    injected_secondary_change: float
    secondary_change_type: Literal["relative", "absolute"]
    pool_leads: int
    pool_p: float
    leads: dict[Variant, int]
    triggered: dict[Variant, int]
    exposures_dropped: int
    rate_A: float
    rate_B_counterfactual: float
    rate_B: float
    realised_primary_rel_lift: float          # rate_B / rate_B_counterfactual - 1
    secondary_A: float
    secondary_B_counterfactual: float
    secondary_B: float
    realised_secondary_change: float          # relative or absolute, per secondary_change_type


class DaySplit(_Frozen):
    date: date
    n_A: int
    n_B: int
    share_B: float


class SplitSummary(_Frozen):
    """Split evidence (§2.4, §2.8 rule 1). Leads are counted once, by their first exposure."""
    experiment_id: str
    configured_share_B: float
    assigned: SrmResult
    triggered: SrmResult      # assigned leads with >= 1 answered call in the store
    per_day: list[DaySplit]   # by first-exposure date
    leads_in_both_arms: int
    rehash_checked: int       # exposures re-bucketed with router.bucket
    rehash_mismatches: int    # stored bucket or variant disagrees with a fresh re-hash
    rehash_pass: bool


# --- decision log (§2.9) ------------------------------------------------------------------

class PrimaryLog(_Frozen):
    rate_A: float
    rate_B: float
    lift_abs: float
    lift_rel: float
    ci95: tuple[float, float]
    z: float
    win_boundary: float
    harm_boundary: float


class GuardrailLog(_Frozen):
    mean_A: float
    mean_B: float
    rel_change: float
    delta: float
    rci: tuple[float, float]
    margin: float | None


class SrmLog(_Frozen):
    configured_share_B: float
    observed_share_B: float           # assigned leads
    p: float
    observed_share_B_triggered: float
    p_triggered: float


class Decision(_Frozen):
    """One append-only log row: kind="look" per look (including CONTINUE), kind="state_change"
    per transition. Look-only fields are None on state-change rows that no look produced
    (for example an approval)."""
    ts_wall: datetime
    ts_sim: datetime | None
    experiment_id: str
    config_hash: str
    prompt_hash_A: str
    prompt_hash_B: str
    kind: Literal["look", "state_change"]
    look: int | None
    info_fraction: float | None
    n_A: int | None
    n_B: int | None
    primary: PrimaryLog | None
    guardrail: GuardrailLog | None
    srm: SrmLog | None
    decision: str            # Verdict.decision value, or the new state on a state change
    reason: str              # a readable sentence
    actor: str               # "engine" or the approver's name
    from_state: State | None = None
    to_state: State | None = None


# ---------------------------------------------------------------------------------------
# Part 2: module signatures. Implement each as a module-level function with the same
# parameter names, order and defaults. Bodies here are documentation only.
# ---------------------------------------------------------------------------------------

class Config:  # src/abtest/config.py — frozen at phase-1
    @staticmethod
    def load_experiment(path: str | Path) -> ExperimentConfig:
        """Read YAML, validate, and check both variant prompts resolve (paths exist under
        ROOT; replace_text `find` occurs exactly once; replace_section markers valid)."""

    @staticmethod
    def load_sim(path: str | Path) -> SimConfig: ...

    @staticmethod
    def load_scenario(name_or_path: str | Path) -> Scenario:
        """"win" resolves to configs/scenarios/win.yaml; a path is used as is."""

    @staticmethod
    def scenario_experiment(scenario: Scenario) -> ExperimentConfig:
        """The scenario's experiment config with `overrides` applied, validated as in
        load_experiment, with id "<id>-<scenario.name>"."""

    @staticmethod
    def resolve_prompts(cfg: ExperimentConfig) -> dict[Variant, str]: ...

    @staticmethod
    def freeze(cfg: ExperimentConfig) -> FrozenConfig: ...


class Data:  # src/abtest/data.py — frozen at phase-1
    @staticmethod
    def load(dataset: Dataset, data_dir: Path = DATA_DIR,
             exclude_leads_with_calls_gt: int | None = None) -> tuple[pd.DataFrame, AdapterReport]:
        """Canonical DataFrame sorted by (lead_id, ts, call_id). dtypes: call_id/lead_id str,
        ts datetime64 (naive, source local time), answered bool, disposition str,
        duration_s float64 (NaN if absent), attempt_no Int64, bot_version str (NA if absent)."""


class Profile:  # src/abtest/profile.py — frozen at phase-1. CLI: python -m abtest.profile
    @staticmethod
    def build_profile(cfg: ExperimentConfig, data_dir: Path = DATA_DIR) -> DataProfile: ...

    @staticmethod
    def write_profile(profile: DataProfile, path: Path | None = None) -> Path:
        """Default path: REPORTS_DIR / f"data_profile_{dataset}.md"."""

    @staticmethod
    def volume_stats(df: pd.DataFrame, window_days: int) -> VolumeStats: ...


class Power:  # src/abtest/power.py — frozen at phase-1. CLI: python -m abtest.power <exp.yaml>
    @staticmethod
    def fixed_n_b(p: float, mde_rel: float, share_b: float, alpha: float, power: float,
                  better: Direction = "increase") -> float:
        """Fixed-design B-arm size for a one-sided two-proportion z-test (see Phase 1 brief)."""

    @staticmethod
    def inflation_factor(cfg: ExperimentConfig) -> float:
        """Group-sequential inflation R. Returns 1.0 until Phase 3 computes it from boundaries."""

    @staticmethod
    def n_max(cfg: ExperimentConfig) -> NMax: ...

    @staticmethod
    def detectable_lift(cfg: ExperimentConfig, n_b: float) -> float:
        """Smallest relative lift whose n_max(...).n_b_max equals n_b (inverse of n_max)."""

    @staticmethod
    def preflight(cfg: ExperimentConfig, daily_triggered_leads: float) -> Preflight: ...


class Router:  # src/abtest/router.py — frozen at phase-2
    @staticmethod
    def bucket(salt: str, lead_id: str) -> int:
        """int(sha256(f"{salt}:{lead_id}").hexdigest()[:15], 16) % 10000"""

    @staticmethod
    def variant_for(bucket: int, allocation: Allocation) -> Variant:
        """B if bucket < allocation.B * 10000, else A."""

    @staticmethod
    def route(state: ExperimentState, lead_id: str) -> Assignment:
        """Pure: bucket with state.salt, split with state.allocation (the live allocation)."""

    @staticmethod
    def assign(conn: sqlite3.Connection, experiment_id: str, lead_id: str,
               ts: datetime) -> Assignment:
        """Load state from the store, route, append the Exposure to the exposures table."""


# src/abtest/api.py — frozen at phase-2: `app = FastAPI()` with
# GET /assign?experiment_id=&lead_id= -> {variant, state, config_hash, prompt_hash}
# (calls router.assign with the wall clock; 404 for an unknown experiment).


class Simulator:  # src/abtest/simulator.py — frozen at phase-2
    @staticmethod
    def build_pool(cfg: ExperimentConfig, sim: SimConfig, data_dir: Path = DATA_DIR) -> Pool: ...

    @staticmethod
    def simulate_day(pool: Pool, cfg: ExperimentConfig, sim: SimConfig, state: ExperimentState,
                     day: int, seed: int) -> SimDay:
        """Leads entering on sim day `day` (0-based), routed with router.route(state, ...).
        All randomness comes from np.random.default_rng((seed, day)), so any day can be
        regenerated alone; lead ids are f"sim-{seed}-{n}" with n global across days."""

    @staticmethod
    def truth_summary(truth: pd.DataFrame, pool: Pool, cfg: ExperimentConfig, sim: SimConfig,
                      scenario: str, seed: int) -> SimTruth:
        """`truth` = every SimDay.truth of the run, concatenated."""

    @staticmethod
    def write_truth_report(truth: SimTruth, out_dir: Path = REPORTS_DIR) -> Path:
        """Writes out_dir / f"sim_truth_{truth.scenario}.md"."""


class Srm:  # src/abtest/srm.py — frozen at phase-2. CLI: python -m abtest.srm <scenario>
    @staticmethod
    def srm_test(n_a: int, n_b: int, share_b: float) -> SrmResult:
        """Chi-square goodness of fit (1 df) against the configured share; Wilson 95% CI."""

    @staticmethod
    def split_summary(conn: sqlite3.Connection, experiment_id: str) -> SplitSummary: ...

    @staticmethod
    def split_report(conn: sqlite3.Connection, experiment_id: str,
                     out_dir: Path = REPORTS_DIR) -> Path:
        """Writes out_dir / f"split_{scenario or experiment_id}.md" and a PNG beside it."""


class BoundariesModule:  # src/abtest/boundaries.py — PROVISIONAL until Phase 3
    @staticmethod
    def spend_win(t: float, alpha: float) -> float: ...

    @staticmethod
    def spend_harm(t: float, alpha: float) -> float: ...

    @staticmethod
    def compute_boundaries(info_fractions: Sequence[float], cfg: ExperimentConfig) -> Boundaries: ...

    @staticmethod
    def crossing_probability(bounds: Sequence[float], info_fractions: Sequence[float],
                             drift: float) -> float: ...

    @staticmethod
    def monte_carlo_crossing(bounds: Sequence[float], info_fractions: Sequence[float],
                             drift: float, n_paths: int, seed: int) -> float: ...


class Metrics:  # src/abtest/metrics.py — PROVISIONAL until Phase 3
    @staticmethod
    def lead_outcomes(calls: pd.DataFrame, exposures: pd.DataFrame, cfg: ExperimentConfig,
                      look_ts: datetime) -> pd.DataFrame:
        """One LeadOutcome row per assigned lead, using only calls with ts <= look_ts."""


class Analysis:  # src/abtest/analysis.py — PROVISIONAL until Phase 3
    @staticmethod
    def analyze_look(outcomes: pd.DataFrame, cfg: ExperimentConfig, look: int,
                     ts_sim: datetime, bounds: Boundaries, is_final: bool) -> LookResult: ...


class Rules:  # src/abtest/rules.py — PROVISIONAL until Phase 3
    @staticmethod
    def evaluate_look(look: LookResult, bounds: Boundaries, cfg: ExperimentConfig) -> Verdict: ...


class DecisionModule:  # src/abtest/decision.py — PROVISIONAL until Phase 4
    @staticmethod
    def start(conn: sqlite3.Connection, frozen: FrozenConfig, ts_sim: datetime) -> ExperimentState: ...

    @staticmethod
    def apply_verdict(conn: sqlite3.Connection, state: ExperimentState, look: LookResult,
                      bounds: Boundaries, verdict: Verdict) -> ExperimentState: ...

    @staticmethod
    def approve(conn: sqlite3.Connection, experiment_id: str, actor: str,
                approved: bool) -> ExperimentState: ...


class Runner:  # src/abtest/runner.py — orchestrator stub in Phase 2, PROVISIONAL until Phase 4
    @staticmethod
    def run_scenario(scenario: Scenario, conn: sqlite3.Connection,
                     speed: float | None = None) -> ExperimentState: ...


class Validation:  # src/abtest/validation.py — PROVISIONAL until Phase 3
    @staticmethod
    def run_validation(cfg: ExperimentConfig, n_runs: int, seed: int,
                       out_dir: Path = REPORTS_DIR) -> Path: ...


MODULE_CONTRACTS: dict[str, type] = {
    "config": Config, "data": Data, "profile": Profile, "power": Power, "router": Router,
    "simulator": Simulator, "srm": Srm, "boundaries": BoundariesModule, "metrics": Metrics,
    "analysis": Analysis, "rules": Rules, "decision": DecisionModule, "runner": Runner,
    "validation": Validation,
}
