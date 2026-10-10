"""The rubric guardrail between rules.evaluate_look and decision.apply_verdict (PLAN_LOOP.md §L9.3).

The base engine knows one guardrail (the template's). The loop adds a second one, `rubric_risk`,
without touching the engine: `rubric_guardrail` builds it as a SecondaryStats exactly like
analysis._secondary does for a continuous metric (mean per arm over mature leads, Welch SE, a 95% CI
and the repeated-look interval RCI = delta +/- c_k * se with c_k the win boundary at this look), and
`combine` applies the same rule order as rules.py to the pair (engine verdict, rubric stats):

  engine HALTED_SRM / STOPPED_HARM          kept as they are (the rubric adds nothing to a stop)
  rubric RCI entirely above the margin      STOPPED_HARM, rule 2 (harm on any guardrail stops)
  engine win, rubric not entirely below     CONTINUE, or BLOCKED_GUARDRAIL at the final look (rule 4)
  anything else                             the engine's verdict, unchanged

The rubric score never promotes anything: it can only stop a run or hold a win back. NaN is "no
harm and not cleared" (an empty arm or a single lead gives no interval), as in rules.py. The
formatting helpers below copy rules.py's conventions (true minus sign, percentage points) rather
than import its private ones.
"""

import math
from collections.abc import Mapping

import numpy as np
import pandas as pd
from scipy.stats import norm

from abtest.contracts import Boundaries, ExperimentConfig, LookResult, SecondaryStats, State, Verdict

NAME = "rubric_risk"
Z_CI = float(norm.ppf(0.975))
_MINUS = "−"
_NAN = math.nan


def _arm(values: np.ndarray) -> tuple[int, float, float]:
    """(n, mean, variance with ddof=1); NaN mean for an empty arm, NaN variance for one lead."""
    n = len(values)
    if n == 0:
        return 0, _NAN, _NAN
    mean = float(values.mean())
    d = values - mean
    return n, mean, (float(np.square(d).sum()) / (n - 1) if n > 1 else _NAN)


def _interval(center: float, se: float, c: float) -> tuple[float, float]:
    """analysis._interval: NaN if undefined, (-inf, inf) at an infinite boundary."""
    if math.isnan(center) or math.isnan(se):
        return _NAN, _NAN
    if math.isinf(c):
        return -math.inf, math.inf
    return center - c * se, center + c * se


def rubric_guardrail(outcomes: pd.DataFrame, risk_by_lead: Mapping[str, float], look: int,
                     bounds: Boundaries, margin: float) -> SecondaryStats:
    """The rubric_risk guardrail at this look: mature leads that have a risk value, B minus A."""
    if not 1 <= look <= len(bounds.win):
        raise ValueError(f"look must be in 1..{len(bounds.win)} (the boundaries given), got {look}")
    mature = outcomes[outcomes["mature"].to_numpy(dtype=bool)]
    risk = mature["lead_id"].astype(str).map(dict(risk_by_lead)).to_numpy(dtype=float, na_value=np.nan)
    arm = mature["variant"].to_numpy()
    have = ~np.isnan(risk)
    n_a, mean_a, var_a = _arm(risk[have & (arm == "A")])
    n_b, mean_b, var_b = _arm(risk[have & (arm == "B")])

    delta = mean_b - mean_a
    se = math.sqrt(var_a / n_a + var_b / n_b) if n_a and n_b else _NAN   # Welch; NaN propagates
    half = Z_CI * se
    return SecondaryStats(
        name=NAME, type="continuous", role="guardrail", mean_A=mean_a, mean_B=mean_b,
        rel_change=delta / mean_a if mean_a else _NAN, delta=delta, delta_type="absolute", se=se,
        ci95=(delta - half, delta + half), rci=_interval(delta, se, bounds.win[look - 1]),
        margin=margin)


# --- reasons ----------------------------------------------------------------------------------

def _num(x: float, signed: bool = True) -> str:
    if math.isnan(x):
        return "n/a"
    if math.isinf(x):
        return "+inf" if x > 0 else f"{_MINUS}inf"
    x = round(x * 100, 2) + 0.0                         # + 0.0 turns -0.0 into 0.0
    return f"{x:{'+' if signed else ''}.2f}".replace("-", _MINUS) + " pp"


def _rci(s: SecondaryStats) -> str:
    return f"[{_num(s.rci[0])}, {_num(s.rci[1])}]"


def _win_text(look: LookResult, bounds: Boundaries) -> str:
    z, w = look.primary.z, bounds.win[look.look - 1]
    return f"Win boundary crossed at look {look.look} (z = {z:.2f} ≥ {w:.2f})".replace("-", _MINUS)


def combine(base: Verdict, rubric: SecondaryStats, look: LookResult, bounds: Boundaries,
            cfg: ExperimentConfig) -> tuple[Verdict, bool]:
    """(verdict, rubric_decided): the verdict to apply, and whether the rubric guardrail produced it
    (True: hand apply_verdict a LookResult whose `secondary` is the rubric stats, so the decision row
    and its business sentence describe the guardrail that fired). `cfg` is part of the contract; the
    current rules need nothing from it."""
    decision = base.decision
    if decision in (State.HALTED_SRM, State.STOPPED_HARM):
        return base, False
    if rubric.margin is None:
        raise ValueError("the rubric guardrail needs a margin")
    k, margin = look.look, rubric.margin
    lo, hi = rubric.rci

    # Rule 2: harm on any guardrail stops, ahead of any win, blocked win or inconclusive end.
    if lo > margin:                                      # NaN compares False: no harm
        also = ("" if decision == "CONTINUE" else
                f" (the engine's own verdict was {decision}; a guardrail harm outranks it)")
        return Verdict(decision=State.STOPPED_HARM, rule=2, reason=(
            f"Rubric guardrail harm at look {k}: {NAME} RCI {_rci(rubric)} lies entirely above the "
            f"{_num(margin)} margin{also}; traffic returned to A.")), True

    # Rule 4: a win needs the rubric guardrail cleared too (RCI entirely below the margin).
    if decision in (State.PROMOTED, State.PENDING_APPROVAL) and not hi < margin:
        base_text = (f"{_win_text(look, bounds)} but the win waits for the rubric guardrail: "
                     f"{NAME} RCI {_rci(rubric)} is not entirely below the {_num(margin)} margin")
        if look.is_final:
            return Verdict(decision=State.BLOCKED_GUARDRAIL, rule=4, reason=(
                f"{base_text} at the final look; B is blocked and A stays.")), True
        return Verdict(decision="CONTINUE", rule=4, reason=(
            f"{base_text}; continuing until the rubric guardrail clears or the final look.")), False

    if look.is_final and decision == "CONTINUE":
        raise ValueError(f"look {k} is final but the engine verdict is CONTINUE; rules.evaluate_look "
                         "never returns that, so something upstream is wrong")
    return base, False
