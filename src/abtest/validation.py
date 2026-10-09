"""Validation proof pack (PLAN.md §2.11): A/A, power and naive-peeking runs on the real engine code.

CLI: python -m abtest.validation [EXP_YAML] [--runs N] [--seed S]   (make validate)

Each run plays a whole experiment on the fast path: per-lead outcomes are drawn straight from a
bootstrap pool of the historical data (vectorised numpy), look k analyses the first
round(t_k * N_arm) leads of each arm, and every statistic comes from the engine itself:
`analysis.analyze_look` for z, CIs and SRM, `boundaries.compute_boundaries` for the boundaries and
`rules.evaluate_look` for the decisions. Nothing statistical is reimplemented here, so a bug in the
engine shows up as a missed target in this report.

The pool unit is a LEAD-WINDOW, the simulator's bootstrap unit (contracts.Pool): one lead's calls
inside one non-overlapping `window.days` block. The pool is built by feeding the historical calls
through `metrics.lead_outcomes`, so the metric definitions are the engine's own.

Naive peeking is evaluated on the SAME looks with a fixed one-sided z at every look
(z_{1-alpha_win} to win, z_{1-alpha_harm} to stop for harm). Both paths are followed to their own
first decision, so the sequential stop never truncates the naive path and vice versa. The
guardrail RCI inside each LookResult always uses the sequential c_k.
"""

import argparse
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import binomtest, norm

from abtest import analysis, boundaries, config, data, metrics, power, rules
from abtest.contracts import (
    CONFIGS_DIR,
    DATA_DIR,
    REPORTS_DIR,
    Boundaries,
    ExperimentConfig,
    LeadOutcome,
    NMax,
    State,
)

DEFAULT_EXP = CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml"
DEFAULT_RUNS = 2000        # §2.11: 2,000 A/A runs and 2,000 power runs
DEFAULT_SEED = 20261009    # the only seed `make validate` uses unless --seed is given
CI_LEVEL = 0.95            # level of every binomial CI in the report
POWER_TOLERANCE = 0.05     # PLAN §4: power at the MDE within +/-5 percentage points of target
POOL_P_NOTE_REL = 0.10     # flag the report when the pool's p differs from baseline_p by > 10%
SIM_EPOCH = datetime(2026, 1, 1)  # constant clock for the fast path: outcomes are all mature

WIN_STATES = frozenset({State.PROMOTED, State.PENDING_APPROVAL})
OUTCOME_COLUMNS: tuple[str, ...] = tuple(LeadOutcome.model_fields)
_POOL_CALL_COLUMNS = ("call_id", "lead_id", "ts", "answered", "disposition")

# IndiaMART palette and a Calibri-style font stack (PLAN §2.10), for the PNG charts.
NAVY, GOLD, CRIMSON = "#003366", "#e8a517", "#C8102E"
GREY, SLATE = "#8c96a3", "#4a5568"
FONT = "Calibri, Carlito, Segoe UI, Helvetica, Arial, sans-serif"
STATE_COLOURS = {
    State.PROMOTED: NAVY, State.PENDING_APPROVAL: NAVY, State.STOPPED_HARM: CRIMSON,
    State.BLOCKED_GUARDRAIL: GOLD, State.INCONCLUSIVE: GREY, State.HALTED_SRM: SLATE,
}


# =========================================================================================
# Pool: historical calls -> per-lead-window outcomes, through the engine's own metrics
# =========================================================================================

@dataclass(frozen=True)
class PoolInfo:
    source: str
    n_calls: int
    n_blocks: int
    window_days: int
    n_units: int                  # lead-windows with at least one call
    n_mature: int                 # ... triggered and mature at the end of the data
    n_dropped_no_secondary: int   # mature units whose secondary is undefined (e.g. no duration)
    p: float                      # primary rate over the pool


def unit_calls(calls: pd.DataFrame, cfg: ExperimentConfig) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(calls, pseudo-exposures) with one unit id f"{lead_id}|{block}" per lead-window.

    Blocks are non-overlapping `window.days` spans starting at midnight of the first day, with the
    partial trailing block dropped, exactly as profile.volume_stats (and so the simulator's
    Pool) counts them; if the data is shorter than one block the whole span is the one block.
    Each unit's pseudo-exposure is its first call, as variant "A" (the real exposure is the first
    dial), so lead_outcomes starts the unit's clock there.
    """
    window = cfg.window.days
    offset = (calls["ts"].dt.normalize() - calls["ts"].min().normalize()).dt.days
    span = int(offset.max()) + 1
    n_blocks, length = span // window, window
    if not n_blocks:
        n_blocks, length = 1, span
    block = offset // length
    keep = block < n_blocks

    cols = list(_POOL_CALL_COLUMNS)
    if cfg.secondary.type == "continuous" and cfg.secondary.column not in cols:
        cols.append(cfg.secondary.column)
    c = calls.loc[keep, cols].copy()
    c["lead_id"] = c["lead_id"].astype(str) + "|" + block[keep].astype(str)
    c = c.sort_values(["lead_id", "ts", "call_id"], kind="stable")
    first = c.drop_duplicates("lead_id")
    exposures = pd.DataFrame({"experiment_id": cfg.id, "lead_id": first["lead_id"].to_numpy(),
                              "variant": "A", "bucket": 0, "ts": first["ts"].to_numpy()})
    return c, exposures


def build_pool(calls: pd.DataFrame, cfg: ExperimentConfig,
               source: str = "synthetic") -> tuple[pd.DataFrame, PoolInfo]:
    """Pool of (primary, secondary) per mature triggered lead-window, from canonical calls."""
    c, exposures = unit_calls(calls, cfg)
    outcomes = metrics.lead_outcomes(c, exposures, cfg, c["ts"].max().to_pydatetime())
    mature = outcomes[outcomes["mature"]]
    secondary = mature["secondary"].to_numpy(dtype="float64")
    defined = ~np.isnan(secondary)
    if not defined.any():
        raise ValueError("the pool is empty: no triggered, mature, measurable lead-window")
    pool = pd.DataFrame({"primary": mature["primary"].to_numpy(dtype="int8")[defined],
                         "secondary": secondary[defined]})
    n_days = (c["ts"].dt.normalize().max() - c["ts"].dt.normalize().min()).days + 1
    info = PoolInfo(
        source=source, n_calls=len(c), n_blocks=max(n_days // cfg.window.days, 1),
        window_days=cfg.window.days, n_units=len(outcomes), n_mature=len(mature),
        n_dropped_no_secondary=int((~defined).sum()), p=float(pool["primary"].mean()))
    return pool, info


def load_pool(cfg: ExperimentConfig, data_dir: Path = DATA_DIR) -> tuple[pd.DataFrame, PoolInfo]:
    """The real pool for cfg.dataset. Raises FileNotFoundError, naming the path, if data is absent."""
    path = Path(data_dir) / data.FILES[cfg.dataset]
    if not path.exists():
        raise FileNotFoundError(
            f"validation draws from the real {cfg.dataset} data, which is not at {path}; "
            f"put the CSV there or set ABTEST_DATA_DIR")
    calls, _ = data.load(cfg.dataset, data_dir,
                         exclude_leads_with_calls_gt=cfg.exclude_leads_with_calls_gt)
    return build_pool(calls, cfg, source=path.name)


# =========================================================================================
# Fast path: nested looks over pre-drawn leads, analysed by the engine's own functions
# =========================================================================================

@dataclass(frozen=True)
class _Plan:
    """Everything that is the same in every run."""
    k: int
    n_a: int
    n_b: int
    take_a: tuple[int, ...]       # leads of arm A in the analysis at each look (nested prefixes)
    take_b: tuple[int, ...]
    planned: tuple[float, ...]
    bounds: Boundaries            # sequential, computed once: observed t_k == planned t_k
    naive: Boundaries             # fixed z at every look
    ts_sim: tuple[datetime, ...]
    consts: tuple[dict, ...]      # per look: the constant LeadOutcome columns
    n_max: NMax


def _plan(cfg: ExperimentConfig) -> _Plan:
    nm = power.n_max(cfg)
    n_a, n_b = math.ceil(nm.n_a_max), math.ceil(nm.n_b_max)
    planned = boundaries.planned_info_fractions(cfg)
    k = len(planned)
    take_a = tuple(int(np.rint(t * n_a)) for t in planned)
    take_b = tuple(int(np.rint(t * n_b)) for t in planned)
    bounds = boundaries.compute_boundaries(planned, cfg)
    naive = Boundaries(info_fractions=planned,
                       win=(float(norm.ppf(1 - cfg.alpha_win)),) * k,
                       harm=(float(norm.ppf(1 - cfg.alpha_harm)),) * k)
    ids_a = np.array([f"a{i}" for i in range(n_a)])
    ids_b = np.array([f"b{i}" for i in range(n_b)])
    consts = []
    for na, nb in zip(take_a, take_b):
        n = na + nb
        epoch = pd.array(np.full(n, np.datetime64(SIM_EPOCH, "ns")))
        # Ready-made pandas arrays: the DataFrame constructor then reuses them instead of
        # re-inferring dtypes for ~16k strings at every look of every run.
        consts.append({
            "lead_id": pd.array(np.concatenate([ids_a[:na], ids_b[:nb]]), dtype="str"),
            "variant": pd.array(np.concatenate([np.full(na, "A"), np.full(nb, "B")]), dtype="str"),
            "assigned_ts": epoch, "triggered_ts": epoch, "mature": np.ones(n, dtype=bool),
            "n_calls": np.ones(n, dtype="int64"), "n_answered": np.ones(n, dtype="int64"),
            "no_na": np.zeros(n, dtype=bool)})
    stamps = tuple(SIM_EPOCH + timedelta(days=cfg.window.days * t) for t in planned)
    return _Plan(k, n_a, n_b, take_a, take_b, planned, bounds, naive, stamps, tuple(consts), nm)


def _outcomes(plan: _Plan, look: int, prim_a, sec_a, prim_b, sec_b) -> pd.DataFrame:
    """LeadOutcome-shaped frame for one look: all rows triggered and mature, constant clocks."""
    na, nb = plan.take_a[look - 1], plan.take_b[look - 1]
    c = plan.consts[look - 1]
    primary = np.concatenate([prim_a[:na], prim_b[:nb]]).astype("int64")
    columns = {
        "lead_id": c["lead_id"], "variant": c["variant"], "assigned_ts": c["assigned_ts"],
        "triggered_ts": c["triggered_ts"], "mature": c["mature"],
        "primary": pd.arrays.IntegerArray(primary, c["no_na"]),
        "secondary": np.concatenate([sec_a[:na], sec_b[:nb]]),
        "n_calls": c["n_calls"], "n_answered": c["n_answered"]}
    assert tuple(columns) == OUTCOME_COLUMNS
    return pd.DataFrame(columns, copy=False)


def _inject(rng: np.random.Generator, prim: np.ndarray, sec: np.ndarray, pool_p: np.ndarray,
            pool_s: np.ndarray, p: float, rel_lift: float, better: str) -> None:
    """Lead-level effect injection into the B arm, in place (PLAN §2.5).

    The B primary rate moves by `rel_lift` in the direction that is better for `better` (a
    negative lift makes B worse). Rate up: each non-success becomes a success w.p.
    q = p*L/(1-p), so the rate goes from p to p(1+L) exactly. Rate down: each success loses
    it w.p. L. A flipped lead's secondary is redrawn from pool leads with its new primary value,
    mirroring the simulator, which resamples the changed call's duration and disposition.
    """
    rate_up = (rel_lift > 0) == (better == "increase")
    size = abs(rel_lift)
    if rate_up:
        q = p * size / (1 - p)
        if q > 1:
            raise ValueError(f"lift {size:.0%} is infeasible at p = {p:.4f} (q = {q:.2f} > 1)")
        flip, new = (prim == 0) & (rng.random(len(prim)) < q), 1
    else:
        if size > 1:
            raise ValueError(f"a relative decrease of {size:.0%} exceeds 100%")
        flip, new = (prim == 1) & (rng.random(len(prim)) < size), 0
    donors = np.flatnonzero(pool_p == new)
    if flip.any() and not len(donors):
        raise ValueError(f"the pool has no lead with primary = {new} to resample a secondary from")
    prim[flip] = new
    sec[flip] = pool_s[donors[rng.integers(0, len(donors), size=int(flip.sum()))]]


def _one_run(plan: _Plan, cfg: ExperimentConfig, pool_p: np.ndarray, pool_s: np.ndarray,
             rel_lift: float, run: int, seed: np.random.SeedSequence) -> dict:
    """One whole experiment: draw leads, inject the effect, then look at it K times."""
    p = float(pool_p.mean())
    rng = np.random.default_rng(seed)
    idx_a = rng.integers(0, len(pool_p), size=plan.n_a)
    idx_b = rng.integers(0, len(pool_p), size=plan.n_b)
    prim_a, sec_a = pool_p[idx_a], pool_s[idx_a]
    prim_b, sec_b = pool_p[idx_b], pool_s[idx_b]
    before = float(prim_b.mean())
    if rel_lift:
        _inject(rng, prim_b, sec_b, pool_p, pool_s, p, rel_lift, cfg.primary.better)
    after = float(prim_b.mean())

    seq = naive = None  # (decision, rule, look, reason) of each path's first non-CONTINUE verdict
    crossed_seq = crossed_naive = False
    z_path = [math.nan] * plan.k
    for k in range(1, plan.k + 1):
        look = analysis.analyze_look(_outcomes(plan, k, prim_a, sec_a, prim_b, sec_b), cfg, k,
                                     plan.ts_sim[k - 1], plan.bounds, k == plan.k)
        z_path[k - 1] = look.primary.z
        if seq is None:
            crossed_seq |= rules.primary_crossings(look, plan.bounds)[0]
            v = rules.evaluate_look(look, plan.bounds, cfg)
            if v.decision != rules.CONTINUE:
                seq = (str(v.decision), v.rule, k, v.reason)
        if naive is None:
            crossed_naive |= rules.primary_crossings(look, plan.naive)[0]
            v = rules.evaluate_look(look, plan.naive, cfg)
            if v.decision != rules.CONTINUE:
                naive = (str(v.decision), v.rule, k, v.reason)
        if seq is not None and naive is not None:
            break
    if seq is None or naive is None:  # rules 3-5 always decide at the final look
        raise RuntimeError(f"run {run}: no decision at the final look (rules.py broke §2.8 rule 4/5)")
    row = {"run": run, "decision": seq[0], "rule": seq[1], "stop_look": seq[2],
           "reason": seq[3], "naive_decision": naive[0], "naive_rule": naive[1],
           "naive_stop_look": naive[2], "crossed_primary_seq": crossed_seq,
           "crossed_primary_naive": crossed_naive, "rate_B_cf": before, "rate_B": after,
           "n_looks": sum(not math.isnan(z) for z in z_path)}
    row.update({f"z{k}": z for k, z in enumerate(z_path, 1)})
    return row


def simulate_runs(cfg: ExperimentConfig, pool: pd.DataFrame, n_runs: int,
                  seed: int | np.random.SeedSequence, rel_lift: float) -> pd.DataFrame:
    """One row per run: sequential and naive decisions, stop looks, primary crossings, z path.

    `pool` has columns primary (0/1) and secondary (float). `rel_lift` is the relative change of
    the B primary rate in B's favour (0 = A/A, negative = B worse). Run i draws from the i-th
    child of SeedSequence(seed), so run i is identical whatever n_runs is.
    """
    pool_p = pool["primary"].to_numpy(dtype="int8")
    pool_s = pool["secondary"].to_numpy(dtype="float64")
    root = seed if isinstance(seed, np.random.SeedSequence) else np.random.SeedSequence(seed)
    plan = _plan(cfg)
    rows = [_one_run(plan, cfg, pool_p, pool_s, rel_lift, i, child)
            for i, child in enumerate(root.spawn(n_runs))]
    return pd.DataFrame(rows)


# =========================================================================================
# Results, checks and the report
# =========================================================================================

@dataclass(frozen=True)
class ValidationResults:
    cfg: ExperimentConfig
    seed: int
    n_runs: int
    pool: PoolInfo
    plan: _Plan
    rel_lift: float
    aa: pd.DataFrame
    power: pd.DataFrame
    seconds: dict[str, float]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    observed: str
    criterion: str

    def line(self) -> str:
        status = "PASS" if self.passed else "FAIL"
        return f"{status}: {self.name}: {self.observed}; criterion: {self.criterion}"


def realised_lift(df: pd.DataFrame, better: str) -> float:
    """Relative lift of B's primary rate over its counterfactual, pooled over runs (the
    simulator's definition, rate_B / rate_B_counterfactual - 1), signed so > 0 means B better."""
    ratio = df["rate_B"].sum() / df["rate_B_cf"].sum() - 1
    return float(ratio if better == "increase" else -ratio)


def rate_ci(k: int, n: int) -> tuple[float, float, float]:
    """(rate, lo, hi): Clopper-Pearson binomial CI at CI_LEVEL."""
    ci = binomtest(int(k), int(n)).proportion_ci(confidence_level=CI_LEVEL, method="exact")
    return k / n, float(ci.low), float(ci.high)


def _count(series: pd.Series, states=None) -> int:
    """Runs whose decision is in `states`, or (no states) runs where a boolean column is True."""
    return int(series.sum()) if states is None else int(series.isin(states).sum())


def _fmt_rate(k: int, n: int) -> str:
    r, lo, hi = rate_ci(k, n)
    return f"{r:.2%} ({k}/{n}; 95% CI {lo:.2%} to {hi:.2%})"


def rates(res: ValidationResults) -> dict[str, tuple[int, int]]:
    """Every headline (count, n) the report quotes."""
    aa, pw = res.aa, res.power
    return {
        "aa_false_win": (_count(aa.decision, WIN_STATES), len(aa)),
        "aa_naive_false_win": (_count(aa.naive_decision, WIN_STATES), len(aa)),
        "aa_false_harm": (_count(aa.decision, {State.STOPPED_HARM}), len(aa)),
        "aa_naive_false_harm": (_count(aa.naive_decision, {State.STOPPED_HARM}), len(aa)),
        "aa_cross_seq": (_count(aa.crossed_primary_seq), len(aa)),
        "aa_cross_naive": (_count(aa.crossed_primary_naive), len(aa)),
        "pw_win": (_count(pw.decision, WIN_STATES), len(pw)),
        "pw_naive_win": (_count(pw.naive_decision, WIN_STATES), len(pw)),
        "pw_cross_seq": (_count(pw.crossed_primary_seq), len(pw)),
        "pw_cross_naive": (_count(pw.crossed_primary_naive), len(pw)),
        "pw_harm": (_count(pw.decision, {State.STOPPED_HARM}), len(pw)),
    }


def checks(res: ValidationResults) -> list[Check]:
    """The §2.11 / §4 targets. A rate may sit anywhere its CI does not exclude the target, so the
    pass rule for the two upper-limit checks is "CI lower bound <= target"; the power check is the
    PLAN's +/-5 pp on the point estimate."""
    cfg, r = res.cfg, rates(res)
    k, n = r["aa_false_win"]
    _, lo, _ = rate_ci(k, n)
    out = [Check("A/A false-win rate", lo <= cfg.alpha_win,
                 f"{_fmt_rate(k, n)} vs nominal {cfg.alpha_win:.2%}",
                 "95% CI contains the nominal rate or lies below it")]
    k, n = r["aa_naive_false_win"]
    _, lo, _ = rate_ci(k, n)
    out.append(Check("Naive peeking inflates the false-win rate", lo > cfg.alpha_win,
                     f"naive {_fmt_rate(k, n)} vs nominal {cfg.alpha_win:.2%}",
                     "95% CI lies above the nominal rate"))
    k, n = r["aa_false_harm"]
    _, lo, _ = rate_ci(k, n)
    out.append(Check("A/A false-harm-stop rate", lo <= cfg.alpha_harm,
                     f"{_fmt_rate(k, n)} vs limit {cfg.alpha_harm:.2%}",
                     "95% CI lower bound is at or below the limit"))
    k, n = r["pw_win"]
    rate, _, _ = rate_ci(k, n)
    out.append(Check("Power at the MDE", abs(rate - cfg.power) <= POWER_TOLERANCE,
                     f"{_fmt_rate(k, n)} vs target {cfg.power:.0%}",
                     f"within {POWER_TOLERANCE * 100:.0f} percentage points of the target"))
    return out


def _md_table(header: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(lines)


def _stop_table(df: pd.DataFrame, k: int, naive: bool = False) -> str:
    dec, look = ("naive_decision", "naive_stop_look") if naive else ("decision", "stop_look")
    tab = pd.crosstab(df[dec], df[look]).reindex(columns=range(1, k + 1), fill_value=0)
    tab = tab.reindex([s for s in State if s in tab.index]).fillna(0).astype(int)
    rows = [[state, *tab.loc[state].tolist(), int(tab.loc[state].sum()),
             f"{tab.loc[state].sum() / len(df):.1%}"] for state in tab.index]
    return _md_table(["terminal state", *[f"look {i}" for i in range(1, k + 1)], "runs", "share"], rows)


# --- charts ---------------------------------------------------------------------------------

def _layout(fig, title: str, width: int, height: int):
    fig.update_layout(
        title=dict(text=title, x=0.01, font=dict(size=18, color=NAVY)),
        font=dict(family=FONT, size=14, color="#1a202c"), width=width, height=height,
        paper_bgcolor="white", plot_bgcolor="white", margin=dict(l=70, r=30, t=70, b=70))
    fig.update_yaxes(gridcolor="#e6e9ee", zeroline=False, linecolor="#c7ccd4")
    fig.update_xaxes(linecolor="#c7ccd4")


def _false_win_chart(res: ValidationResults):
    import plotly.graph_objects as go

    r, alpha = rates(res), res.cfg.alpha_win
    keys = ("aa_false_win", "aa_naive_false_win")
    names = ("Sequential (alpha spending)", "Naive peeking (fixed z at every look)")
    vals = [rate_ci(*r[key]) for key in keys]
    # The value goes under the name: a label on the bar would collide with the CI whisker.
    labels = [f"{n}<br><b>{v[0]:.2%}</b> (95% CI {v[1]:.2%} to {v[2]:.2%})"
              for n, v in zip(names, vals)]
    fig = go.Figure(go.Bar(
        x=labels, y=[v[0] for v in vals], marker=dict(color=[NAVY, CRIMSON]), width=0.42,
        error_y=dict(type="data", symmetric=False, color="#1a202c", thickness=1.5,
                     array=[v[2] - v[0] for v in vals], arrayminus=[v[0] - v[1] for v in vals]),
        hovertemplate="%{y:.2%}<extra></extra>"))
    fig.add_hline(y=alpha, line=dict(color=GOLD, width=2, dash="dash"),
                  annotation_text=f"nominal {alpha:.1%}", annotation_position="top left",
                  annotation_font=dict(color=SLATE))
    _layout(fig, f"A/A false-win rate over {len(res.aa):,} runs", 820, 500)
    fig.update_layout(showlegend=False, margin=dict(l=70, r=30, t=70, b=100))
    fig.update_yaxes(tickformat=".0%", rangemode="tozero", title_text="runs declaring a win")
    return fig


def _stop_look_chart(res: ValidationResults):
    """Stacked bars of where runs stopped. INCONCLUSIVE (the bulk of A/A runs, always at the final
    look) is left off so the early stops stay visible; its share goes in each panel's subtitle."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    k = res.plan.k
    panels = [("A/A (no effect)", res.aa), (f"True lift = MDE ({res.rel_lift:+.0%})", res.power)]
    titles = [f"{t}: {(df.decision == State.INCONCLUSIVE).mean():.1%} INCONCLUSIVE (not drawn)"
              for t, df in panels]
    fig = make_subplots(rows=1, cols=2, subplot_titles=titles, horizontal_spacing=0.09)
    groups = [("PROMOTED / PENDING_APPROVAL", WIN_STATES, NAVY),
              (str(State.STOPPED_HARM), {State.STOPPED_HARM}, CRIMSON),
              (str(State.BLOCKED_GUARDRAIL), {State.BLOCKED_GUARDRAIL}, GOLD),
              (str(State.HALTED_SRM), {State.HALTED_SRM}, SLATE)]
    shown = set()
    for col, (_, df) in enumerate(panels, 1):
        for name, states, colour in groups:
            looks = df.loc[df.decision.isin(states), "stop_look"]
            if looks.empty:
                continue
            share = looks.value_counts().reindex(range(1, k + 1), fill_value=0) / len(df)
            fig.add_trace(go.Bar(
                x=share.index.tolist(), y=share.tolist(), name=name,
                marker=dict(color=colour, line=dict(color="white", width=2)),
                legendgroup=name, showlegend=name not in shown,
                hovertemplate=f"{name}<br>look %{{x}}: %{{y:.2%}} of runs<extra></extra>"),
                row=1, col=col)
            shown.add(name)
    _layout(fig, "Where runs stopped (share of all runs, by look)", 1100, 520)
    fig.update_layout(barmode="stack", legend=dict(orientation="h", y=-0.22, x=0))
    fig.update_xaxes(title_text="look", dtick=1)
    fig.update_yaxes(tickformat=".1%", rangemode="tozero")
    fig.update_yaxes(title_text="share of all runs", col=1)
    fig.update_annotations(font=dict(size=14, color=SLATE))
    return fig


def write_charts(res: ValidationResults, out_dir: Path) -> tuple[list[Path], str | None]:
    """Write the PNGs beside the report. Returns (paths, error); a failed export (kaleido needs a
    Chrome it cannot find, say) is reported, never raised: the report is still written."""
    figs = {"validation_false_win.png": _false_win_chart, "validation_stop_looks.png": _stop_look_chart}
    paths: list[Path] = []
    try:
        for name, build in figs.items():
            path = out_dir / name
            build(res).write_image(path, scale=2)
            paths.append(path)
    except Exception as e:  # noqa: BLE001 - kaleido raises assorted types when Chrome is missing
        return paths, f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else e}"
    return paths, None


# --- markdown report ------------------------------------------------------------------------

def render_report(res: ValidationResults, charts: list[Path], chart_error: str | None) -> str:
    cfg, plan, pool, r = res.cfg, res.plan, res.pool, rates(res)
    aa, pw = res.aa, res.power
    results = checks(res)
    try:
        cfg_hash = config.freeze(cfg).config_hash[:12]
    except (OSError, ValueError):  # prompt files unreadable: the hash is a label, not a gate
        cfg_hash = "n/a"
    rel_pool = abs(pool.p - cfg.baseline_p) / cfg.baseline_p

    L = []
    L.append(f"# Validation proof pack: {cfg.id}\n")
    L.append(f"Generated by `make validate` (`python -m abtest.validation`). Seed {res.seed}, "
             f"{res.n_runs:,} A/A runs and {res.n_runs:,} power runs. Total runtime "
             f"{res.seconds['total']:.1f} s (A/A {res.seconds['aa']:.1f} s, power "
             f"{res.seconds['power']:.1f} s).\n")
    L.append("## Verdict\n")
    L += [f"- {c.line()}" for c in results]
    L.append("")

    L.append("## Setup\n")
    L.append(_md_table(["item", "value"], [
        ["experiment id / config hash", f"`{cfg.id}` / `{cfg_hash}`"],
        ["dataset / unit / trigger", f"{cfg.dataset} / {cfg.unit} / {cfg.trigger}"],
        ["allocation", f"A {cfg.allocation.A:.0%} / B {cfg.allocation.B:.0%}"],
        ["window / looks / maturation",
         f"{cfg.window.days} d / {cfg.window.looks} / {cfg.maturation_days:g} d"],
        ["primary", f"`{cfg.primary.name}`, better = {cfg.primary.better}, "
                    f"MDE {cfg.primary.mde_rel:.1%} relative"],
        ["secondary", f"`{cfg.secondary.name}` ({cfg.secondary.type}), role = {cfg.secondary.role}"
                      + (f", worse if {cfg.secondary.worse_if}, margin {cfg.secondary.margin:g}"
                         if cfg.secondary.role == "guardrail" else "")],
        ["alpha_win / alpha_harm / power",
         f"{cfg.alpha_win:g} / {cfg.alpha_harm:g} / {cfg.power:g}"],
        ["min info at first look / SRM threshold",
         f"{cfg.min_info_first_look:g} / {cfg.srm_p_threshold:g}"],
        ["promotion mode", cfg.promotion],
        ["baseline p (config)", f"{cfg.baseline_p:.4f}"],
        ["n_B,max / n_A,max (leads per run)", f"{plan.n_b:,} / {plan.n_a:,} (fixed design "
         f"{plan.n_max.n_b_fixed:,.0f}, inflation R = {plan.n_max.inflation_factor:.3f})"],
    ]))
    L.append("")

    L.append("## Bootstrap pool\n")
    L.append(f"Pool unit: one lead-window, i.e. one lead's calls inside one non-overlapping "
             f"{pool.window_days}-day block ({pool.n_blocks} blocks), the simulator's bootstrap unit. "
             f"Outcomes come from `metrics.lead_outcomes` over the historical calls (source: "
             f"{pool.source}), keeping mature triggered units.\n")
    L.append(_md_table(["item", "value"], [
        ["calls used", f"{pool.n_calls:,}"],
        ["lead-windows", f"{pool.n_units:,}"],
        ["triggered and mature (the pool)", f"{pool.n_mature - pool.n_dropped_no_secondary:,}"
         + (f" ({pool.n_dropped_no_secondary:,} more dropped: secondary undefined)"
            if pool.n_dropped_no_secondary else "")],
        ["pool p (primary rate)", f"{pool.p:.4f}"],
        ["baseline p in config", f"{cfg.baseline_p:.4f}"],
    ]))
    if rel_pool > POOL_P_NOTE_REL:
        L.append(f"\nNOTE: the pool rate differs from the config's baseline p by {rel_pool:.0%}. "
                 f"The pool counts calls only inside a {pool.window_days}-day block while baseline p "
                 f"is measured at a fixed follow-up horizon, so the power runs test a slightly "
                 f"different problem from the one n_max was sized for.")
    L.append("")

    L.append("## Planned looks and boundaries\n")
    L.append("Boundaries are computed once at the planned information fractions (the fast path "
             "realises them exactly). The naive columns are a fixed one-sided z at every look.\n")
    L.append(_md_table(
        ["look", "t_k", "n_B", "n_A", "win z", "harm z", "naive win z", "naive harm z"],
        [[k + 1, f"{t:.3f}", f"{plan.take_b[k]:,}", f"{plan.take_a[k]:,}",
          f"{plan.bounds.win[k]:.3f}", f"{plan.bounds.harm[k]:.3f}",
          f"{plan.naive.win[k]:.3f}", f"{plan.naive.harm[k]:.3f}"]
         for k, t in enumerate(plan.planned)]))
    L.append("")

    L.append("## A/A runs (zero true effect)\n")
    f = lambda key: _fmt_rate(*r[key])  # noqa: E731
    inflation = r["aa_cross_naive"][0] / max(r["aa_cross_seq"][0], 1)
    rows = [
        ["false win, sequential (PROMOTED or PENDING_APPROVAL)", f("aa_false_win"),
         f"{cfg.alpha_win:.1%} (nominal)"],
        ["false win, naive peeking", f("aa_naive_false_win"), "much higher"],
        ["false-harm stop, sequential (STOPPED_HARM)", f("aa_false_harm"), f"<= {cfg.alpha_harm:.0%}"],
        ["false-harm stop, naive peeking", f("aa_naive_false_harm"), "higher"],
        ["primary-only win crossing, sequential", f("aa_cross_seq"), f"<= {cfg.alpha_win:.1%}"],
        ["primary-only win crossing, naive", f("aa_cross_naive"),
         f"much higher ({inflation:.1f}x sequential)"],
    ]
    L.append(_md_table(["quantity", "observed", "target"], rows))
    L.append("\nPrimary-only crossing means z reached the win boundary at some look the run "
             "actually got to (up to its own first decision), ignoring the guardrail and SRM. A win "
             "additionally needs the guardrail RCI cleared. The RCI inside each LookResult uses the "
             "sequential c_k, also in the naive evaluation.\n")

    L.append(f"## Power runs (true lift = MDE = {res.rel_lift:.1%} relative)\n")
    gap = (r["pw_cross_seq"][0] - r["pw_win"][0]) / r["pw_win"][1]
    rows = [
        ["win call, sequential (PROMOTED or PENDING_APPROVAL)", f("pw_win"),
         f"{cfg.power:.0%} +/- {POWER_TOLERANCE * 100:.0f} pp"],
        ["win call, naive peeking", f("pw_naive_win"), "not a target"],
        ["primary-only win crossing, sequential", f("pw_cross_seq"), "upper bound on the row above"],
        ["primary-only win crossing, naive", f("pw_cross_naive"), "not a target"],
        ["primary crossed but not promoted (guardrail not cleared)", f"{gap:.2%}",
         "0 if the guardrail never binds"],
        ["harm stop, sequential", f("pw_harm"), "should be rare"],
        ["realised lift injected into B (pooled over runs)",
         f"{realised_lift(pw, cfg.primary.better):.2%}", f"{res.rel_lift:.2%} (injected)"],
    ]
    L.append(_md_table(["quantity", "observed", "target"], rows))
    L.append("\nA gap between the win call and the primary-only crossing is the guardrail "
             "refusing to clear (rule 4); a gap between the crossing rate and the target power is "
             "the planned power not being met by the engine's boundaries.\n")

    L.append("## Stop look by terminal state (sequential)\n")
    L.append("A/A:\n")
    L.append(_stop_table(aa, plan.k))
    L.append("\nPower runs:\n")
    L.append(_stop_table(pw, plan.k))
    L.append("\nNaive peeking, A/A:\n")
    L.append(_stop_table(aa, plan.k, naive=True))
    L.append("")

    L.append("## Charts\n")
    if charts:
        L += [f"![{p.stem}]({p.name})\n" for p in charts]
    if chart_error:
        L.append(f"NOTE: PNG export failed, so no charts were written: {chart_error}. "
                 f"(plotly's kaleido needs a Chrome or Chromium it can find.)\n")
    L.append("## Notes\n")
    L.append("- Every statistic and decision comes from `analysis.analyze_look`, "
             "`boundaries.compute_boundaries` and `rules.evaluate_look`; this module only draws "
             "leads, injects the effect and counts outcomes.\n"
             "- Effect injection follows PLAN §2.5 at lead level; a flipped lead's secondary is "
             "redrawn from pool leads with its new primary value.\n"
             "- Runs stop at the first non-CONTINUE verdict, as the runner will. The naive path is "
             "followed separately to its own first decision on the same looks.")
    return "\n".join(L) + "\n"


def write_report(res: ValidationResults, out_dir: Path = REPORTS_DIR) -> Path:
    """Write out_dir/validation.md plus the PNG charts beside it."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    charts, chart_error = write_charts(res, out_dir)
    path = out_dir / "validation.md"
    path.write_text(render_report(res, charts, chart_error), encoding="utf-8")
    return path


# =========================================================================================
# Entry points
# =========================================================================================

def validate(cfg: ExperimentConfig, pool: pd.DataFrame, info: PoolInfo, n_runs: int,
             seed: int) -> ValidationResults:
    """A/A and power runs on `pool`. A/A and power draw from independent seed streams."""
    ss_aa, ss_power = np.random.SeedSequence(seed).spawn(2)
    plan = _plan(cfg)
    t0 = time.perf_counter()
    aa = simulate_runs(cfg, pool, n_runs, ss_aa, 0.0)
    t1 = time.perf_counter()
    pw = simulate_runs(cfg, pool, n_runs, ss_power, cfg.primary.mde_rel)
    t2 = time.perf_counter()
    return ValidationResults(cfg=cfg, seed=seed, n_runs=n_runs, pool=info, plan=plan,
                             rel_lift=cfg.primary.mde_rel, aa=aa, power=pw,
                             seconds={"aa": t1 - t0, "power": t2 - t1, "total": t2 - t0})


def run_validation(cfg: ExperimentConfig, n_runs: int, seed: int,
                   out_dir: Path = REPORTS_DIR) -> Path:
    """Build the real pool, run A/A and power, write validation.md (+ PNGs); return its path."""
    pool, info = load_pool(cfg)
    return write_report(validate(cfg, pool, info, n_runs, seed), out_dir)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m abtest.validation",
                                 description=__doc__.split("\n")[0])
    ap.add_argument("exp", nargs="?", default=str(DEFAULT_EXP), help="experiment YAML")
    ap.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="runs for each of A/A and power")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out-dir", type=Path, default=REPORTS_DIR)
    args = ap.parse_args(argv)

    cfg = config.load_experiment(args.exp)
    try:
        pool, info = load_pool(cfg)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    res = validate(cfg, pool, info, args.runs, args.seed)
    path = write_report(res, args.out_dir)

    print(f"Validation: {cfg.id}  seed {args.seed}  {args.runs:,} A/A + {args.runs:,} power runs  "
          f"pool {len(pool):,} lead-windows (p = {info.p:.4f}, config baseline {cfg.baseline_p:.4f})")
    results = checks(res)
    for c in results:
        print(f"  {c.line()}")
    print(f"runtime {res.seconds['total']:.1f} s; report: {path}")
    return 0 if all(c.passed for c in results) else 1


if __name__ == "__main__":
    sys.exit(main())
