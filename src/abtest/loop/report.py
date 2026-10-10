"""Rubric report for a scored corpus: aggregates only (PLAN_LOOP.md §L2.2, §L2.1 "Short calls", §L7.2).

This is the file that is allowed into git (reports/loop/), so it holds counts, rates and quantiles
and nothing else: no call id, no lead id, no name, no number, no transcript text. Rates are
lead-level (the lead is the unit, PLAN.md §2.6), with normal-approximation 95% CIs; a bot-version
group with fewer than 5 leads is reported without a CI and flagged, because a CI from so few leads
would promise more than the data holds. The output is deterministic: no timestamps.
"""

import re
from pathlib import Path

import numpy as np
import pandas as pd

from abtest.loop.contracts import LOOP_REPORTS_DIR, Rubric
from abtest.loop.diagnose import mean_ci95
from abtest.loop.rubric import rubric_hash

EARLY_HANGUP_S = 10.0      # observe.min_duration_s: a shorter answered call has no conversation to judge
MIN_LEADS_FOR_CI = 5
QUANTILES = (0.10, 0.25, 0.50, 0.75, 0.90)


def _pct(x: float) -> str:
    return f"{100 * x:.1f}%"


def _label(version: object) -> str:
    """A bot-version label safe inside a Markdown table cell."""
    return ("unknown" if pd.isna(version) else str(version)).replace("|", "/").replace("\n", " ")


def _cell(values: np.ndarray, fmt=_pct) -> str:
    """'mean (lo to hi)' for 5+ leads; 'mean (n=3, no CI)' below that; 'n/a' with no leads."""
    if not len(values):
        return "n/a"
    mean, lo, hi = mean_ci95(values)
    if len(values) < MIN_LEADS_FOR_CI:
        return f"{fmt(mean)} (n={len(values)}, no CI)"
    return f"{fmt(mean)} ({fmt(lo)} to {fmt(hi)})"


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return out


def _corpus_section(calls: pd.DataFrame, per_lead: pd.DataFrame) -> list[str]:
    n_calls = int(calls["call_id"].nunique()) if "call_id" in calls.columns else len(calls)
    answered = calls["answered"].fillna(False).astype(bool) if "answered" in calls.columns else pd.Series(True, index=calls.index)
    scored = int(per_lead["n_calls_scored"].sum()) if "n_calls_scored" in per_lead.columns else None
    rows = [["Calls in corpus", str(n_calls)], ["Calls answered", str(int(answered.sum()))],
            ["Calls scored", "n/a" if scored is None else str(scored)], ["Leads scored", str(len(per_lead))]]

    lines = ["## Corpus", "", *_table(["", "count"], rows), ""]
    if "duration_s" not in calls.columns:
        return lines + ["Early hang-up share: n/a (no duration_s column).", ""]
    dur = pd.to_numeric(calls["duration_s"], errors="coerce")
    known = answered & dur.notna()
    pool = calls.loc[known].copy()
    pool["_early"] = dur[known] < EARLY_HANGUP_S
    lines += [f"Early hang-up share: answered calls shorter than {EARLY_HANGUP_S:g} s, which are not scored "
              "(there is no conversation to judge).", ""]
    if pool.empty:
        return lines + ["No answered call has a duration.", ""]
    rows = [["all", str(len(pool)), str(int(pool["_early"].sum())), _pct(float(pool["_early"].mean()))]]
    if "bot_version" in pool.columns:
        for version, g in pool.groupby(pool["bot_version"].fillna("unknown").astype(str), sort=True):
            rows.append([_label(version), str(len(g)), str(int(g["_early"].sum())), _pct(float(g["_early"].mean()))])
    return lines + _table(["bot version", "answered calls", "early hang-ups", "share"], rows) + [""]


def _score_section(per_lead: pd.DataFrame) -> list[str]:
    lines = ["## Score distribution", "", "Per lead: the mean over the lead's scored calls. rubric_score is in [0, 1], higher is better.", ""]
    s = pd.to_numeric(per_lead["rubric_score"], errors="coerce").dropna().to_numpy()
    if len(s) == 0:
        return lines + ["No lead has a rubric_score.", ""]
    q = np.quantile(s, QUANTILES)
    rows = [["mean", f"{s.mean():.3f}"], ["min", f"{s.min():.3f}"]]
    rows += [[f"p{round(100 * p)}", f"{v:.3f}"] for p, v in zip(QUANTILES, q)]
    rows.append(["max", f"{s.max():.3f}"])
    return lines + _table(["statistic", "rubric_score"], rows) + [""]


def _failure_section(per_lead: pd.DataFrame, rubric: Rubric) -> list[str]:
    lines = ["## Criterion failure rates", "",
             "Failure rate = 1 - mean goodness: per lead over its scored calls, then the mean over leads, "
             "with a normal-approximation 95% CI. Probabilities and scores, not pass/fail labels. "
             f"Groups with fewer than {MIN_LEADS_FOR_CI} leads carry no CI.", "", "### All leads", ""]
    rows = []
    for c in rubric.criteria:
        rows.append([f"`{c.id}`", f"{c.weight:g}", "yes" if c.hard else "", _cell(1.0 - per_lead[c.id].dropna().to_numpy())])
    lines += _table(["criterion", "weight", "hard", "failure rate (95% CI)"], rows) + [""]

    if "bot_version" not in per_lead.columns:
        return lines
    versions = per_lead["bot_version"].fillna("unknown").astype(str)
    groups = sorted(versions.unique())
    lines += ["### By bot version", ""]
    header = ["criterion"] + [f"{_label(v)} (leads={int((versions == v).sum())})" for v in groups]
    rows = []
    for c in rubric.criteria:
        row = [f"`{c.id}`"]
        for v in groups:
            vals = per_lead.loc[versions == v, c.id].dropna().to_numpy()
            row.append(_cell(1.0 - vals) if len(vals) else "n/a")
        rows.append(row)
    return lines + _table(header, rows) + [""]


def _risk_section(per_lead: pd.DataFrame, rubric: Rubric) -> list[str]:
    lines = ["## Mean rubric_risk", "",
             f"rubric_risk is the probability that at least one of ({', '.join(f'`{c}`' for c in rubric.risk) or 'no criteria'}) "
             "fired, assuming independence; per lead it is the mean over the lead's scored calls.", ""]
    risk = pd.to_numeric(per_lead["rubric_risk"], errors="coerce")
    if risk.notna().sum() == 0:
        return lines + ["No lead has a rubric_risk.", ""]
    rows = [["all", str(int(risk.notna().sum())), _cell(risk.dropna().to_numpy(), lambda x: f"{x:.3f}")]]
    if "bot_version" in per_lead.columns:
        versions = per_lead["bot_version"].fillna("unknown").astype(str)
        for v in sorted(versions.unique()):
            vals = risk[versions == v].dropna().to_numpy()
            if len(vals):
                rows.append([_label(v), str(len(vals)), _cell(vals, lambda x: f"{x:.3f}")])
    return lines + _table(["bot version", "leads", "mean rubric_risk (95% CI)"], rows) + [""]


def rubric_report(per_lead: pd.DataFrame, calls: pd.DataFrame, rubric: Rubric, corpus_name: str,
                  out_dir: Path = LOOP_REPORTS_DIR) -> Path:
    """Write out_dir/rubric_<corpus_name>.md and return its path.

    `per_lead` is scorer.per_lead's output (lead_id, bot_version, n_calls_scored, rubric_score,
    rubric_risk, one goodness column per criterion); `calls` is the corpus frame (call_id, answered,
    duration_s, bot_version). Sections: corpus counts and the early-hang-up share, the score
    quantiles, per-criterion failure rate with a lead-level 95% CI overall and by bot_version, and
    mean rubric_risk. The report names no call, lead, person or phrase.
    """
    needed = ["rubric_score", "rubric_risk", *(c.id for c in rubric.criteria)]
    if missing := [k for k in needed if k not in per_lead.columns]:
        raise ValueError(f"per_lead frame has no column for {missing} (rubric {rubric.id})")

    lines = [f"# Rubric report: {corpus_name}", "",
             f"Rubric `{rubric.id}` (hash `{rubric_hash(rubric)[:12]}`). Aggregates only: no call or lead ids, "
             "names, numbers or transcript text.", "",
             *_corpus_section(calls, per_lead), *_score_section(per_lead),
             *_failure_section(per_lead, rubric), *_risk_section(per_lead, rubric)]

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"rubric_{re.sub(r'[^A-Za-z0-9._-]', '_', corpus_name)}.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path
