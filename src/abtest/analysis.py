"""Per-look test statistics: per-lead outcomes -> LookResult (PLAN.md §2.7, §2.8 rule 1, §2.9).

Only mature leads (maturation, §2.6) enter the arms; SRM is read off the assigned and the
triggered leads instead. The validation harness calls analyze_look ~30,000 times, so this works
on the column arrays with numpy and builds each pydantic model once. Degenerate data (an empty
arm, no variance, a zero baseline) never raises: the undefined statistic is NaN, which the rules
module reads as "no crossing".
"""

import math
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.stats import norm

from abtest.contracts import (
    Boundaries,
    ExperimentConfig,
    LeadOutcome,
    LookResult,
    PrimaryStats,
    SecondaryStats,
    SrmResult,
)
from abtest.power import detectable_lift, n_max

CONFIDENCE = 0.95  # two-sided level of every ci95
Z_CI = float(norm.ppf(1 - (1 - CONFIDENCE) / 2))  # z_{0.975}
_OUTCOME_COLUMNS = tuple(LeadOutcome.model_fields)
_NAN = math.nan


def _div(num: float, den: float) -> float:
    """num / den, NaN (never inf or an exception) when the denominator is zero."""
    return num / den if den else _NAN


def _arm(values: np.ndarray, rows: np.ndarray, binary: bool) -> tuple[int, float, float]:
    """(n, mean, variance) of values[rows]. Binary: the plug-in p(1-p) that the unpooled SE
    uses. Continuous: the per-lead sample variance (ddof=1), NaN for a single lead."""
    v = values[rows]
    n = len(v)
    if n == 0:
        return 0, _NAN, _NAN
    mean = float(v.mean())
    if binary:
        return n, mean, mean * (1 - mean)
    d = v - mean  # two-pass; no `d @ d`, whose BLAS threads spin on a busy machine
    return n, mean, (float(np.square(d).sum()) / (n - 1) if n > 1 else _NAN)


def _interval(center: float, se: float, c: float) -> tuple[float, float]:
    if math.isnan(center) or math.isnan(se):
        return _NAN, _NAN
    if math.isinf(c):  # a boundary at +inf (the first look may not be able to win): no information
        return -math.inf, math.inf
    return center - c * se, center + c * se


# TODO(phase-2 merge): replace with abtest.srm.srm_test
def _srm_test(n_a: int, n_b: int, share_b: float) -> SrmResult:
    """Chi-square goodness of fit (1 df) against the configured share; Wilson 95% CI.

    Same values as Lane A's scipy version (chisquare + binomtest(...).proportion_ci(method=
    "wilson"), pinned in tests/test_analysis.py), written in closed form because those scipy
    calls cost ~0.8 ms each and this runs twice per look, ~30,000 looks in the validation.
    """
    n = n_a + n_b
    if n == 0:  # no evidence either way
        return SrmResult(n_A=0, n_B=0, configured_share_B=share_b, observed_share_B=_NAN,
                         ci95=(0.0, 1.0), chi2=0.0, p=1.0)
    e_a, e_b = n * (1 - share_b), n * share_b
    chi2 = _div((n_a - e_a) ** 2, e_a) + _div((n_b - e_b) ** 2, e_b)
    p = math.erfc(math.sqrt(chi2 / 2))  # chi-square survival function at 1 df
    z2, observed = Z_CI**2, n_b / n
    denom = 2 * (n + z2)
    center = (2 * n_b + z2) / denom
    delta = Z_CI / denom * math.sqrt(4 * n * observed * (1 - observed) + z2)
    return SrmResult(n_A=n_a, n_B=n_b, configured_share_B=share_b, observed_share_B=observed,
                     ci95=(0.0 if n_b == 0 else center - delta,
                           1.0 if n_a == 0 else center + delta), chi2=chi2, p=p)


def _primary(prim: np.ndarray, a: np.ndarray, b: np.ndarray, cfg: ExperimentConfig
             ) -> tuple[PrimaryStats, int, int]:
    n_a, rate_a, var_a = _arm(prim, a, binary=True)
    n_b, rate_b, var_b = _arm(prim, b, binary=True)
    if any(n and math.isnan(rate) for n, rate in ((n_a, rate_a), (n_b, rate_b))):
        raise ValueError("every mature lead needs a primary outcome (primary is NA)")
    lift_abs = rate_b - rate_a
    pooled = _div(rate_a * n_a + rate_b * n_b, n_a + n_b)  # NaN if an arm is empty
    inv_n = _div(1, n_a) + _div(1, n_b)
    se_pooled = math.sqrt(pooled * (1 - pooled) * inv_n)
    se_unpooled = math.sqrt(_div(var_a, n_a) + _div(var_b, n_b))
    sign = 1.0 if cfg.primary.better == "increase" else -1.0  # z > 0 always means B is better
    z = 0.0 if se_pooled == 0 else sign * lift_abs / se_pooled  # no variance: no evidence
    half = Z_CI * se_unpooled
    mde = detectable_lift(cfg, n_b) if n_b > 0 else None
    return PrimaryStats(
        rate_A=rate_a, rate_B=rate_b, lift_abs=lift_abs, lift_rel=_div(lift_abs, rate_a),
        ci95=(lift_abs - half, lift_abs + half), se_pooled=se_pooled, se_unpooled=se_unpooled,
        z=z, detectable_mde_rel=float(mde) if mde is not None and math.isfinite(mde) else None,
    ), n_a, n_b


def _secondary(sec: np.ndarray, a: np.ndarray, b: np.ndarray, cfg: ExperimentConfig,
               c_k: float) -> SecondaryStats:
    s = cfg.secondary
    binary = s.type == "binary"
    ok = ~np.isnan(sec)  # per-lead mean undefined (no non-null duration) -> not in the analysis
    n_a, mean_a, var_a = _arm(sec, a & ok, binary)
    n_b, mean_b, var_b = _arm(sec, b & ok, binary)
    diff = mean_b - mean_a
    rel_change = _div(diff, mean_a)
    if binary and any(m < 0 or m > 1 for m in (mean_a, mean_b)):  # NaN (empty arm) compares False
        raise ValueError("a binary secondary must be 0/1 per lead")
    if binary:  # absolute difference, unpooled SE
        delta, delta_type = diff, "absolute"
        se = math.sqrt(_div(var_a, n_a) + _div(var_b, n_b))
    else:  # relative change of means; delta-method variance of the ratio mean_B / mean_A
        delta, delta_type = rel_change, "relative"
        se = math.sqrt(_div(var_b, n_b * mean_a**2) + _div(mean_b**2 * var_a, n_a * mean_a**4))
    half = Z_CI * se
    return SecondaryStats(
        name=s.name, type=s.type, role=s.role, mean_A=mean_a, mean_B=mean_b,
        rel_change=rel_change, delta=delta, delta_type=delta_type, se=se,
        ci95=(delta - half, delta + half), rci=_interval(delta, se, c_k), margin=s.margin,
    )


def analyze_look(outcomes: pd.DataFrame, cfg: ExperimentConfig, look: int, ts_sim: datetime,
                 bounds: Boundaries, is_final: bool) -> LookResult:
    """Statistics of one look from metrics.lead_outcomes() output.

    n_A, n_B and every statistic use mature leads only; srm_assigned counts all rows and
    srm_triggered the rows with a trigger, whether mature yet or not (§2.8 rule 1).
    z is the pooled two-proportion z-statistic, oriented so z > 0 means B is better; the
    guardrail RCI is delta +/- c_k * se with c_k the win boundary at this look (§2.7).
    """
    if not 1 <= look <= len(bounds.win):
        raise ValueError(f"look must be in 1..{len(bounds.win)} (the boundaries given), got {look}")
    if missing := [c for c in _OUTCOME_COLUMNS if c not in outcomes.columns]:
        raise ValueError(f"outcomes is missing columns {missing}")

    arm = outcomes["variant"]
    is_a, is_b = arm.eq("A").to_numpy(dtype=bool), arm.eq("B").to_numpy(dtype=bool)
    triggered = outcomes["triggered_ts"].notna().to_numpy(dtype=bool)
    mature = outcomes["mature"].to_numpy(dtype=bool)
    a, b = mature & is_a, mature & is_b

    primary, n_a, n_b = _primary(
        outcomes["primary"].to_numpy(dtype="float64", na_value=np.nan), a, b, cfg)
    secondary = _secondary(
        outcomes["secondary"].to_numpy(dtype="float64", na_value=np.nan), a, b, cfg,
        bounds.win[look - 1])
    share_b = cfg.allocation.B
    return LookResult(
        experiment_id=cfg.id, look=look, ts_sim=ts_sim, is_final=is_final,
        info_fraction=n_b / n_max(cfg).n_b_max, n_A=n_a, n_B=n_b,
        primary=primary, secondary=secondary,
        srm_assigned=_srm_test(int(is_a.sum()), int(is_b.sum()), share_b),
        srm_triggered=_srm_test(int((triggered & is_a).sum()), int((triggered & is_b).sum()),
                                share_b),
    )
