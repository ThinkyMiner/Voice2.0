"""Shared dashboard helpers (PLAN.md §2.10): read-only store access, palette, banner, business text.

The dashboard only reads the store, each read on a short-lived read-only connection opened at
call time from contracts.DB_PATH (so a test can point it elsewhere). The approval write in app.py
is the one exception.
"""

import functools
import html
import math
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st
from scipy.stats import binomtest

from abtest import config, contracts, decision, metrics, srm, store
from abtest.contracts import Decision, ExperimentConfig, ExperimentState, SimTruth, State

NAVY, GOLD, CRIMSON = "#003366", "#e8a517", "#C8102E"
GREEN, GREY, SLATE, INK, MUTED = "#1e7b4f", "#7b8594", "#4a5568", "#1a2433", "#5a6473"
FONT = "Calibri, Carlito, 'Segoe UI', Helvetica, Arial, sans-serif"
DAY = timedelta(days=1)
BADGE = {  # state -> (background, text colour)
    State.DRAFT: (GREY, "white"), State.RUNNING: (NAVY, "white"),
    State.PENDING_APPROVAL: (GOLD, NAVY), State.PROMOTED: (GREEN, "white"),
    State.REJECTED: (GREY, "white"), State.STOPPED_HARM: (CRIMSON, "white"),
    State.BLOCKED_GUARDRAIL: ("#b45f06", "white"), State.INCONCLUSIVE: (GREY, "white"),
    State.HALTED_SRM: (SLATE, "white"),
}

CSS = f"""<style>
.ab-banner {{background:#fff6e0; border-left:5px solid {GOLD}; color:{NAVY}; padding:.6rem 1rem;
  border-radius:4px; font-size:.95rem; margin-bottom:.4rem}}
.ab-badge {{display:inline-block; padding:.15rem .6rem; border-radius:999px; font-size:.78rem;
  font-weight:700; letter-spacing:.03em; white-space:nowrap}}
.ab-wrap {{overflow-x:auto}}
div.ab-wrap table.ab {{width:100%; border-collapse:collapse; font-size:.92rem; color:{INK}; border:none; margin:0}}
div.ab-wrap table.ab th {{text-align:left; color:{MUTED}; font-weight:600; border:none; border-bottom:2px solid {NAVY};
  padding:.45rem .6rem; background:none; white-space:nowrap}}
div.ab-wrap table.ab tr.ab-sel td {{background:#fff6e0}}
div.ab-wrap table.ab td {{border:none; border-bottom:1px solid #e3e7ee; padding:.5rem .6rem; vertical-align:top}}
div.ab-wrap table.ab tr.ab-change td {{background:#f4f6fa}}
div.ab-wrap table.ab tr.ab-stats td {{border-bottom:none; padding-bottom:.1rem}}
div.ab-wrap table.ab tr.ab-why td {{color:{MUTED}; font-size:.85rem; padding-top:.1rem}}
div.ab-wrap table.ab a {{color:{NAVY}; font-weight:700; text-decoration:none}}
.ab-sub {{color:{MUTED}; font-size:.8rem}}
.ab-num {{white-space:nowrap; font-variant-numeric:tabular-nums}}
.ab-head {{border-left:6px solid var(--c); background:#f7f9fc; padding:.9rem 1.2rem; border-radius:6px;
  margin:.3rem 0 .8rem}}
.ab-head .big {{font-size:1.45rem; font-weight:700; color:{NAVY}; line-height:1.3}}
.ab-head .why {{color:{MUTED}; font-size:.92rem; margin-top:.35rem}}
.ab-kpis {{display:flex; gap:.7rem; flex-wrap:wrap; margin:.2rem 0 .6rem}}
.ab-kpi {{flex:1 1 180px; border:1px solid #e1e5ec; border-radius:6px; padding:.6rem .8rem; background:white}}
.ab-kpi .l {{color:{MUTED}; font-size:.78rem; text-transform:uppercase; letter-spacing:.04em}}
.ab-kpi .v {{color:{NAVY}; font-size:1.35rem; font-weight:700; white-space:nowrap}}
.ab-kpi .s {{color:{MUTED}; font-size:.82rem}}
.ab-hash {{font-family:ui-monospace, Menlo, Consolas, monospace; font-size:.8rem; word-break:break-all}}
.ab-ok {{color:{GREEN}; font-weight:700}} .ab-bad {{color:{CRIMSON}; font-weight:700}}
</style>"""


def style() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def esc(x) -> str:
    return html.escape(str(x))


def badge(state: State) -> str:
    bg, fg = BADGE.get(state, (GREY, "white"))
    return (f'<span class="ab-badge" style="background:{bg};color:{fg}" title="{state.value}">'
            f'{state.value.replace("_", " ")}</span>')


def table(header: list[str], rows: list, widths: list[str] | None = None) -> str:
    """An HTML table. Header and cells are HTML already (escape text with esc()); a row is a list
    of cells, or a (css class, cells) pair; a cell can be a (colspan, html) pair."""
    def row(r) -> str:
        cls, cells = r if isinstance(r, tuple) else ("", r)
        tds = "".join(f'<td colspan="{x[0]}">{x[1]}</td>' if isinstance(x, tuple) else f"<td>{x}</td>"
                      for x in cells)
        return f'<tr class="{cls}">{tds}</tr>'

    cols = "".join(f'<col style="width:{w}">' for w in widths or [])
    head = "".join(f"<th>{h}</th>" for h in header)
    return (f'<div class="ab-wrap"><table class="ab"><colgroup>{cols}</colgroup><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(row(r) for r in rows)}</tbody></table></div>')


def html_block(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


# --- store access (read-only) ------------------------------------------------------------

@dataclass(frozen=True)
class Exp:
    state: ExperimentState
    cfg: ExperimentConfig
    scenario: str | None
    seed: int | None
    created: str            # created_ts_wall: a scenario rerun replaces the experiment
    version: str            # experiment_state.updated_ts_wall: changes on every write
    decisions: list[Decision]
    truth: SimTruth | None  # simulator ground truth; written when the run ends

    @property
    def id(self) -> str:
        return self.state.experiment_id

    @property
    def looks(self) -> list[Decision]:
        return [d for d in self.decisions if d.kind == "look"]

    @property
    def last(self) -> Decision | None:
        looks = self.looks
        return looks[-1] if looks else None


def connect(path: str | Path | None = None) -> sqlite3.Connection | None:
    """A read-only connection holding one snapshot (WAL), or None if the store file is missing."""
    path = Path(path or contracts.DB_PATH)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")  # every read below sees the same snapshot while the runner writes
    return conn


def load_experiments() -> list[Exp]:
    """Every experiment in the store, most recently updated first; [] if missing or empty."""
    conn = connect()
    if conn is None:
        return []
    with closing(conn):
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sim_truth'").fetchone() is None:
            return []  # an empty file: no schema yet
        rows = conn.execute(
            "SELECT experiment_id, e.scenario, e.seed, e.created_ts_wall, s.updated_ts_wall, t.payload "
            "FROM experiments e JOIN experiment_state s USING (experiment_id) "
            "LEFT JOIN sim_truth t USING (experiment_id) ORDER BY updated_ts_wall DESC").fetchall()
        return [Exp(store.get_state(conn, r[0]), store.get_config(conn, r[0]), r[1], r[2], r[3], r[4],
                    store.read_decisions(conn, r[0]),
                    SimTruth.model_validate_json(r[5]) if r[5] else None) for r in rows]


@st.cache_data(max_entries=32, show_spinner=False)
def split_summary(path: str, experiment_id: str, version: str):
    """srm.split_summary, cached per store write (`version`)."""
    with closing(connect(path)) as conn:
        return srm.split_summary(conn, experiment_id)


@st.cache_data(max_entries=32, show_spinner=False)
def outcomes(path: str, experiment_id: str, created: str, ts: datetime) -> pd.DataFrame:
    """metrics.lead_outcomes at `ts`, over the exposures and calls the runner hands the engine
    (exposures from the start, calls up to ts). Fixed once the sim clock has passed ts."""
    with closing(connect(path)) as conn:
        cfg = store.get_config(conn, experiment_id)
        start = store.get_state(conn, experiment_id).started_ts_sim
        exposures = store.read_df(conn, "SELECT * FROM exposures WHERE experiment_id = ? AND ts >= ?",
                                  (experiment_id, start.isoformat(timespec="microseconds")))
        calls = store.read_df(conn, "SELECT * FROM calls WHERE experiment_id = ? AND ts <= ?",
                              (experiment_id, ts.isoformat(timespec="microseconds")))
    return metrics.lead_outcomes(calls.astype({"answered": bool}), exposures, cfg, ts)


@functools.lru_cache
def scale(scenario: str | None) -> float | None:
    """The scenario's simulated volume multiple (configs/scenarios -> configs/sims); None if unknown."""
    if not scenario:
        return None
    try:
        return config.load_sim(config.load_scenario(scenario).sim).volume.scale
    except (OSError, ValueError):
        return None


def banner(pairs) -> None:
    """PLAN §2.10 banner for (dataset, scale) pairs: one experiment's, or every one in the store."""
    parts = sorted({f"{d}, volume ×{s:g}" if s else d for d, s in pairs})
    source = f" from {'; '.join(parts)}" if parts else ""
    html_block(f'<div class="ab-banner"><b>Simulated traffic{esc(source)}</b>: outcomes generated by '
               f'replay with injected effects.</div>')


# --- formatting ---------------------------------------------------------------------------

def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return math.nan, math.nan
    ci = binomtest(k, n).proportion_ci(confidence_level=0.95, method="wilson")
    return float(ci.low), float(ci.high)


def pval(p: float) -> str:
    return f"{p:.3f}" if p >= 0.001 else f"{p:.1e}"


def delta(x: float, cfg: ExperimentConfig) -> str:
    """A secondary delta in its units: relative (continuous) or percentage points (binary)."""
    return f"{x:+.1%}" if cfg.secondary.type == "continuous" else f"{x * 100:+.1f} pp"


def primary_label(cfg: ExperimentConfig) -> str:
    return " / ".join(cfg.primary.dispositions)


def secondary_label(cfg: ExperimentConfig) -> str:
    s = cfg.secondary
    if s.type == "binary":
        return " / ".join(s.dispositions)
    return "Answered-call duration" if s.column == "duration_s" else s.name


def shorten(reason: str, limit: int = 130) -> str:
    text = reason.split("; ")[0].split(". ")[0].rstrip(".")
    return text if len(text) <= limit else text[:limit - 1] + "…"


def impact(e: Exp) -> tuple[tuple[str, str], str]:
    """((value, note) for the list, detail headline): what the result means for the business,
    per week at 100% traffic, from decision.weekly_impact at the latest look."""
    s, cfg, d = e.state, e.cfg, e.last
    label = primary_label(cfg)
    if d is None:
        if s.state == State.RUNNING:
            return (("–", "first look pending"),
                    f"Running: collecting data. The first look comes once {cfg.min_info_first_look:.0%} "
                    f"of n_B,max ({s.n_b_max:,.0f} matured B leads) is in.")
        return ("–", "not started"), "Not started yet."
    days = (d.ts_sim - s.started_ts_sim) / DAY
    n = d.n_A + d.n_B

    def week(lift: float) -> float:
        return decision.weekly_impact(lift, n, days, cfg)

    w, day, actor = week(d.primary.lift_abs), f"day {days:.0f}", e.decisions[-1].actor
    g, sec, sec_label = d.guardrail, cfg.secondary, secondary_label(cfg)
    match s.state:
        case State.PROMOTED:
            how = "" if actor == "engine" else f" (approved by {actor})"
            return ((f"{w:+,.0f} / week", f"{label} gained"),
                    f"B promoted on {day}{how}: ≈ {w:+,.0f} {label} a week at full rollout.")
        case State.PENDING_APPROVAL:
            return ((f"{w:+,.0f} / week", f"{label}, if approved"),
                    f"B won on {day}: approving it is worth ≈ {w:+,.0f} {label} a week at full rollout.")
        case State.REJECTED:
            return ((f"{w:+,.0f} / week", f"{label} forgone (rejected)"),
                    f"B rejected by {actor}: A keeps 100% of traffic (B's estimate was ≈ {w:+,.0f} "
                    f"{label} a week).")
        case State.STOPPED_HARM if d.primary.z <= d.primary.harm_boundary:
            return ((f"≈ {abs(w):,.0f} / week", f"{label} protected"),
                    f"B stopped on {day}, protecting ≈ {abs(w):,.0f} {label} a week at full rollout.")
        case State.STOPPED_HARM if sec.type == "binary":
            more = week(g.delta)
            return ((f"≈ {more:,.0f} / week", f"{sec_label} avoided"),
                    f"B stopped on {day}: it raised {sec_label} by {delta(g.delta, cfg)}, "
                    f"≈ {more:,.0f} more a week at full rollout. All traffic is back on A.")
        case State.STOPPED_HARM:
            return ((delta(g.delta, cfg), f"{sec_label} rise avoided"),
                    f"B stopped on {day}: it moved {sec_label} by {delta(g.delta, cfg)}, "
                    f"beyond the {delta(sec.margin, cfg)} guardrail. All traffic is back on A.")
        case State.BLOCKED_GUARDRAIL:
            return ((f"{w:+,.0f} / week", f"{label} held back by the guardrail"),
                    f"B held back on {day}: its ≈ {w:+,.0f} {label} a week comes with "
                    f"{sec_label} {delta(g.delta, cfg)}, which does not clear the guardrail. A stays.")
        case State.INCONCLUSIVE:
            lo, hi = (week(x) for x in d.primary.ci95)
            return ((f"{lo:+,.0f} to {hi:+,.0f} / week", f"{label}, 95% CI: too small to call"),
                    f"No winner after {days:.0f} days: B's effect is between {lo:+,.0f} and {hi:+,.0f} "
                    f"{label} a week (95% CI), too small to call. A stays.")
        case State.HALTED_SRM:
            share = min((d.srm.p, d.srm.observed_share_B), (d.srm.p_triggered, d.srm.observed_share_B_triggered))[1]
            return (("no estimate", "traffic split broken"),
                    f"Halted on {day}: B got {share:.2%} of leads vs {d.srm.configured_share_B:.0%} "
                    f"configured, so no result can be trusted. All traffic is back on A.")
    return ((f"{w:+,.0f} / week", f"{label}, estimate so far"),
            f"Running, look {d.look} of {cfg.window.looks}: B's current estimate is ≈ {w:+,.0f} {label} "
            f"a week at full rollout, not yet significant.")

