"""Pre-flight power: fixed-design n_B, group-sequential n_B,max and days needed (PLAN.md §2.7).

n_B,max = R * fixed-design n_B, where R is computed from the win boundaries at the planned look
schedule (boundaries.py), never hard-coded.

CLI: python -m abtest.power <exp.yaml>   (make preflight EXP=...)
"""

import math
import sys
from functools import lru_cache

import numpy as np
from scipy.optimize import brentq
from scipy.special import ndtri
from scipy.stats import norm

from abtest.boundaries import crossing_probability, planned_info_fractions, win_boundaries
from abtest.contracts import Direction, ExperimentConfig, NMax, Preflight

_EDGE = float(np.sqrt(np.finfo(float).eps))  # numerical margin keeping brentq inside open limits


def _p_b(p: float, mde_rel: float, better: Direction) -> float:
    return p * (1 + mde_rel) if better == "increase" else p * (1 - mde_rel)


def fixed_n_b(p: float, mde_rel: float, share_b: float, alpha: float, power: float,
              better: Direction = "increase") -> float:
    """B-arm size for a one-sided two-proportion z-test: pooled variance under H0, unpooled
    under H1, no continuity correction. n_A = k * n_B with k = (1 - share_b) / share_b."""
    if not 0 < share_b < 1:
        raise ValueError(f"share_b must be in (0, 1), got {share_b}")
    p_a, p_b = p, _p_b(p, mde_rel, better)
    if not 0 < p_b < 1:
        raise ValueError(f"p_B = {p_b} is outside (0, 1) for p={p}, mde_rel={mde_rel}, {better}")
    k = (1 - share_b) / share_b
    p_bar = (k * p_a + p_b) / (k + 1)
    # ndtri is norm.ppf without scipy.stats overhead (~1 us vs ~100 us): analysis calls
    # detectable_lift at every look, and its root-find calls this dozens of times.
    h0 = ndtri(1 - alpha) * math.sqrt(p_bar * (1 - p_bar) * (1 + 1 / k))
    h1 = ndtri(power) * math.sqrt(p_a * (1 - p_a) / k + p_b * (1 - p_b))
    return (h0 + h1) ** 2 / (p_b - p_a) ** 2


@lru_cache(maxsize=None)
def _inflation(alpha_win: float, power: float, planned: tuple[float, ...]) -> float:
    """R = (theta / (z_{1-alpha} + z_power))^2, where theta is the drift at which the win
    boundaries at `planned` are crossed by the final look with probability `power` (§2.7).
    Cached: ExperimentConfig is not hashable, and detectable_lift calls _n_max dozens of times
    per root-find while the validation harness calls it thousands of times."""
    win = win_boundaries(planned, alpha_win)  # harm ignored: non-binding (§2.7)
    z_fixed = float(norm.isf(alpha_win) + norm.ppf(power))  # the fixed-design drift (R = 1)
    # Stopping early never adds power, so theta >= z_fixed; R <= 4 (theta <= 2 * z_fixed) is far
    # beyond any sane schedule and keeps the root bracketed (f(0) = alpha - power < 0).
    theta = brentq(lambda th: crossing_probability(win, planned, th) - power, 0.0, 2 * z_fixed)
    return (theta / z_fixed) ** 2


def inflation_factor(cfg: ExperimentConfig) -> float:
    """Group-sequential inflation R at the planned look schedule (`planned_info_fractions`)."""
    return _inflation(cfg.alpha_win, cfg.power, planned_info_fractions(cfg))


def _n_max(cfg: ExperimentConfig, mde_rel: float) -> NMax:
    share_b = cfg.allocation.B
    n_b_fixed = fixed_n_b(cfg.baseline_p, mde_rel, share_b, cfg.alpha_win, cfg.power,
                          cfg.primary.better)
    r = inflation_factor(cfg)
    n_b_max = r * n_b_fixed
    n_a_max = (1 - share_b) / share_b * n_b_max
    return NMax(n_b_fixed=n_b_fixed, inflation_factor=r, n_b_max=n_b_max, n_a_max=n_a_max,
                n_total_max=n_a_max + n_b_max)


def n_max(cfg: ExperimentConfig) -> NMax:
    return _n_max(cfg, cfg.primary.mde_rel)


def detectable_lift(cfg: ExperimentConfig, n_b: float) -> float:
    """Relative MDE x with n_max(cfg at mde_rel=x).n_b_max == n_b; inf if no feasible x fits."""
    limit = 1 / cfg.baseline_p - 1 if cfg.primary.better == "increase" else 1.0
    f = lambda x: _n_max(cfg, x).n_b_max - n_b  # noqa: E731
    hi = limit * (1 - _EDGE)
    if f(hi) > 0:
        return math.inf
    return brentq(f, limit * _EDGE, hi)


def preflight(cfg: ExperimentConfig, daily_triggered_leads: float) -> Preflight:
    m, w = cfg.maturation_days, cfg.window.days
    usable = w - m
    if usable <= 0:
        raise ValueError(f"window ({w} d) must exceed maturation_days ({m} d)")
    b_per_day = cfg.allocation.B * daily_triggered_leads
    nm = n_max(cfg)
    # A lead triggered on day d matures at d + m, so matured B leads by day D = b_per_day*(D - m).
    days_needed = nm.n_b_max / b_per_day + m
    return Preflight(
        experiment_id=cfg.id, baseline_p=cfg.baseline_p, mde_rel=cfg.primary.mde_rel,
        share_b=cfg.allocation.B, n_max=nm, daily_triggered_leads=daily_triggered_leads,
        window_days=w, maturation_days=m, days_needed=days_needed, underpowered=days_needed > w,
        min_detectable_lift_rel=detectable_lift(cfg, b_per_day * usable),
        scale_to_fit_window=(days_needed - m) / usable,
    )


def main(argv: list[str] | None = None) -> None:
    from abtest import config, data, profile

    (path,) = argv if argv is not None else sys.argv[1:]
    cfg = config.load_experiment(path)
    df, _ = data.load(cfg.dataset, exclude_leads_with_calls_gt=cfg.exclude_leads_with_calls_gt)
    vs = profile.volume_stats(df, cfg.window.days)
    pf = preflight(cfg, vs.daily_triggered_leads)
    nm, c = pf.n_max, math.ceil
    planned = planned_info_fractions(cfg)
    p_b = _p_b(pf.baseline_p, pf.mde_rel, cfg.primary.better)
    lift = pf.min_detectable_lift_rel
    rows = [
        ("baseline p", f"{pf.baseline_p:<8.4f}(config)"),
        ("MDE (relative)", f"{pf.mde_rel:<8.1%}-> p_B {p_b:.4f}"),
        ("allocation", f"A {cfg.allocation.A:.0%} / B {pf.share_b:.0%}"),
        ("n_B fixed design", f"{c(nm.n_b_fixed):,}"),
        ("inflation factor R", f"{nm.inflation_factor:<8.3f}({cfg.window.looks} looks at t = "
                               f"{planned[0]:.2f}…{planned[-1]:.2f}, win boundaries, "
                               f"power {cfg.power:.0%})"),
        ("n_B,max", f"{c(nm.n_b_max):,}"),
        ("n_A,max / total n", f"{c(nm.n_a_max):,} / {c(nm.n_total_max):,}"),
        ("triggered leads/day", f"{pf.daily_triggered_leads:<8,.1f}"
                                f"(historical, {vs.window_days}-day blocks)"),
        ("days needed vs window", f"{pf.days_needed:.1f} vs {pf.window_days}   "
                                  + ("UNDERPOWERED" if pf.underpowered else "OK")),
        ("smallest lift detectable in window", "infeasible" if math.isinf(lift) else f"{lift:.1%}"),
        ("volume scale to fit window", f"{pf.scale_to_fit_window:.2f}x"),
    ]
    width = max(len(k) for k, _ in rows) + 2
    print(f"Preflight: {pf.experiment_id} (dataset {cfg.dataset})")
    for k, v in rows:
        print(f"  {k:<{width}}{v}")


if __name__ == "__main__":
    main()
