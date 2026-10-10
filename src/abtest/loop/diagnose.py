"""Rank the rubric's failures by weighted cost, per lead, and pull masked excerpts (PLAN_LOOP.md §L2.5, §L7.2).

Pure pandas, no model call. The lead is the unit (PLAN.md §2.6): a lead's failure on a criterion is
the mean over its scored calls of (1 - goodness), so a lead that was called five times does not
count five times. The failure rate is the mean over leads with a normal-approximation 95% CI.
Excerpts are the only transcript text this module touches, and they pass through pii.mask first.
"""

import math
from collections.abc import Sequence

import numpy as np
import pandas as pd
from scipy.stats import norm

from abtest.loop.contracts import Rubric, Transcript
from abtest.loop.scorer import render_transcript

Z95 = float(norm.ppf(0.975))  # two-sided 95%, the base's CONFIDENCE
OUT_COLUMNS = ["criterion_id", "failure_rate", "ci_low", "ci_high", "weight", "weighted_cost",
               "n_leads", "rank", "target"]


def mean_ci95(values: Sequence[float] | np.ndarray) -> tuple[float, float, float]:
    """(mean, ci_low, ci_high) of lead-level values in [0, 1]: normal approximation, CI clipped to
    [0, 1]. One lead has no spread to estimate, so its CI is the point itself. NaNs are ignored."""
    v = np.asarray(values, dtype=float)
    v = v[~np.isnan(v)]
    if len(v) == 0:
        raise ValueError("no values to average")
    mean = float(v.mean())
    if len(v) == 1:
        return mean, mean, mean
    half = Z95 * float(v.std(ddof=1)) / math.sqrt(len(v))
    return mean, max(0.0, mean - half), min(1.0, mean + half)


def _need_columns(frame: pd.DataFrame, rubric: Rubric, extra: Sequence[str] = ()) -> None:
    missing = [c for c in [*extra, *(c.id for c in rubric.criteria)] if c not in frame.columns]
    if missing:
        raise ValueError(f"scores frame has no column for {missing} (rubric {rubric.id})")


def diagnose(scores: pd.DataFrame, rubric: Rubric, top_k: int) -> pd.DataFrame:
    """One row per criterion, sorted by rank (1 = most costly).

    `scores` holds one row per scored call with lead_id and one goodness column per criterion id
    (store_loop.get_scores, or scorer.scores_frame merged with the calls' lead_id). Per lead,
    failure = 1 - mean goodness over its calls; failure_rate is the mean over leads, with a
    normal-approximation 95% CI clipped to [0, 1] (a single lead: the CI equals the point).
    weighted_cost = weight * failure_rate; rank is descending cost, ties broken by rubric order;
    target = rank <= top_k.
    """
    _need_columns(scores, rubric, extra=["lead_id"])
    if scores.empty:
        raise ValueError("no scored calls to diagnose")
    ids = [c.id for c in rubric.criteria]
    by_lead = scores.groupby("lead_id")[ids].mean()  # mean goodness over each lead's calls; NaN skipped

    rows = []
    for c in rubric.criteria:
        goodness_by_lead = by_lead[c.id].dropna()
        if goodness_by_lead.empty:
            raise ValueError(f"no scored lead has a value for criterion {c.id!r}")
        rate, lo, hi = mean_ci95(1.0 - goodness_by_lead.to_numpy())
        rate = min(1.0, max(0.0, rate))
        rows.append({"criterion_id": c.id, "failure_rate": rate, "ci_low": lo, "ci_high": hi,
                     "weight": c.weight, "weighted_cost": c.weight * rate, "n_leads": len(goodness_by_lead)})
    out = pd.DataFrame(rows)
    order = sorted(range(len(out)), key=lambda i: (-out.at[i, "weighted_cost"], i))
    out = out.iloc[order].reset_index(drop=True)
    out["rank"] = np.arange(1, len(out) + 1)
    out["target"] = out["rank"] <= top_k
    return out[OUT_COLUMNS]


def _mask(text: str) -> str:
    """abtest.loop.pii.mask, imported late: it is built by another agent, identity until it exists."""
    try:
        from abtest.loop.pii import mask
    except ImportError:
        return text
    return mask(text)


def _cut(text: str, max_chars: int) -> str:
    """Cut to at most max_chars, with a trailing '...' when there is room for one."""
    if len(text) <= max_chars:
        return text
    if max_chars < 8:
        return text[:max(0, max_chars)]
    return text[:max_chars - 3].rstrip() + "..."


def excerpts(transcripts: Sequence[Transcript], scores: pd.DataFrame, criterion_id: str, n: int = 3,
             max_chars: int = 600) -> list[str]:
    """Up to n masked excerpts from the calls that fail `criterion_id` worst (lowest goodness first,
    ties by call_id). A call with perfect goodness shows no failure, so it is never an excerpt;
    calls without a score row are ignored. Each excerpt is the transcript as BOT:/PERSON: lines,
    masked with pii.mask first and then cut to max_chars (so a cut can never expose half a number).
    """
    if criterion_id not in scores.columns or "call_id" not in scores.columns:
        raise ValueError(f"scores frame needs call_id and a {criterion_id!r} column")
    worst = (scores[["call_id", criterion_id]].dropna().groupby("call_id")[criterion_id].min()
             .loc[lambda s: s < 1.0].reset_index().sort_values([criterion_id, "call_id"]))
    by_id = {t.call_id: t for t in transcripts}
    out: list[str] = []
    for call_id in worst["call_id"]:
        if len(out) >= n:
            break
        t = by_id.get(call_id)
        if t is None or not (rendered := render_transcript(t)):
            continue
        out.append(_cut(_mask(rendered), max_chars))
    return out
