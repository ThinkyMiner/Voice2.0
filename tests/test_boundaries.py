"""Alpha-spending boundaries and crossing probabilities (PLAN.md §2.7).

The recursion is checked three independent ways: the gsDesign reference values, scipy's
multivariate-normal CDF (a different algorithm for the same joint distribution), and Monte Carlo.
The identity "cumulative crossing probability == spending function" is a consistency check on the
root-finder, not on the recursion, so it is never the only check.
"""

import math

import numpy as np
import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy.optimize import brentq
from scipy.stats import multivariate_normal, norm

from abtest import boundaries as bnd
from abtest.boundaries import (
    compute_boundaries,
    crossing_probability,
    monte_carlo_crossing,
    planned_info_fractions,
    spend_harm,
    spend_win,
)
from abtest.contracts import CONFIGS_DIR, Boundaries, ExperimentConfig
from abtest.power import inflation_factor

# gsDesign sfLDOF, 5 equally spaced looks, one-sided alpha 0.025 (PLAN.md §2.7), tolerance +/-0.01.
REFERENCE_WIN_5 = (4.877, 3.357, 2.680, 2.290, 2.031)
REFERENCE_TOL = 0.01

MC_PATHS = 1_000_000   # PLAN.md §2.7: at least 10^6 paths
MC_SEED = 20261009
MC_SE_MULT = 4.0

EQUAL_5 = tuple(k / 5 for k in range(1, 6))
EQUAL_7 = tuple(k / 7 for k in range(1, 8))
UNEQUAL = (0.2, 0.25, 0.7, 0.95, 1.0)
CLOSE = (0.3, 0.31, 0.32, 1.0)   # 1% apart: the kernel is narrow, the grid must follow
TIGHT = (0.2, 0.2001, 1.0)       # 1e-4 apart: look-2 kernel sd 0.01 vs sqrt(t1) = 0.45
OVERSHOOT = (0.4, 0.9, 1.15, 1.3)  # observed information past n_max: spending is capped at alpha
SCHEDULES = {"equal5": EQUAL_5, "equal7": EQUAL_7, "unequal": UNEQUAL, "close": CLOSE,
             "overshoot": OVERSHOOT}


@pytest.fixture(autouse=True)
def _fresh_cache():
    """Boundaries are memoised; tests that monkeypatch grid constants must not see stale results."""
    bnd._cached_bounds.cache_clear()
    yield
    bnd._cached_bounds.cache_clear()


@pytest.fixture(scope="module")
def cfg():
    path = CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml"
    return ExperimentConfig.model_validate(yaml.safe_load(open(path)))


def _mc_tol(p: float) -> float:
    """4 binomial standard errors, plus one path so a single hit on a ~1e-7 probability passes."""
    return MC_SE_MULT * math.sqrt(p * (1 - p) / MC_PATHS) + 1 / MC_PATHS


# --- spending functions ---------------------------------------------------------------------

@pytest.mark.parametrize("spend", [spend_win, spend_harm])
def test_spend_endpoints_and_monotone(spend):
    for alpha in (0.01, 0.025, 0.05):
        assert spend(0.0, alpha) == 0.0
        assert spend(1.0, alpha) == pytest.approx(alpha, rel=1e-12)
        vals = np.array([spend(float(t), alpha) for t in np.linspace(0.0, 1.0, 201)])
        assert (np.diff(vals) >= 0).all()  # win spends < 1e-300 at tiny t, which underflows to 0
        assert (np.diff(vals[20:]) > 0).all()  # strictly increasing from t = 0.1


@pytest.mark.parametrize("spend", [spend_win, spend_harm])
def test_spend_clips_t_above_one(spend):
    assert spend(1.7, 0.025) == spend(1.0, 0.025)
    assert spend(1.0000001, 0.05) == spend(1.0, 0.05)
    assert spend(1e9, 0.05) <= 0.05 * (1 + 1e-12)


def test_spend_formulas():
    # §2.7 written out with scipy: win = 2 (1 - Phi(z_{1-a/2} / sqrt t)), harm = a ln(1 + (e-1) t)
    for t in (0.1, 0.37, 0.5, 0.9):
        assert spend_win(t, 0.025) == pytest.approx(
            2 * (1 - norm.cdf(norm.ppf(1 - 0.0125) / math.sqrt(t))), rel=1e-9)
        assert spend_harm(t, 0.05) == pytest.approx(0.05 * math.log(1 + (math.e - 1) * t), rel=1e-12)
    # O'Brien-Fleming type spends almost nothing early; Pocock type spends ln(1 + (e-1)/5) = 30%
    # of alpha by t = 0.2
    assert spend_win(0.2, 0.025) < 1e-6
    assert spend_harm(0.2, 0.05) / 0.05 == pytest.approx(math.log(1 + (math.e - 1) / 5))


@pytest.mark.parametrize("spend", [spend_win, spend_harm])
@pytest.mark.parametrize("t, alpha", [(-0.1, 0.025), (float("nan"), 0.025), (math.inf, 0.025),
                                      (0.5, 0.0), (0.5, 1.0), (0.5, -0.1)])
def test_spend_rejects_bad_arguments(spend, t, alpha):
    with pytest.raises(ValueError):
        spend(t, alpha)


# --- boundaries -----------------------------------------------------------------------------

def test_reference_check_five_equal_looks(cfg):
    assert cfg.alpha_win == 0.025
    win = compute_boundaries(EQUAL_5, cfg).win
    assert win == pytest.approx(REFERENCE_WIN_5, abs=REFERENCE_TOL)


def test_returns_plain_python_floats(cfg):
    b = compute_boundaries(EQUAL_5, cfg)
    assert isinstance(b, Boundaries)
    assert b.info_fractions == pytest.approx(EQUAL_5)
    for vals in (b.info_fractions, b.win, b.harm):
        assert isinstance(vals, tuple) and len(vals) == 5
        assert all(type(x) is float for x in vals)


def test_harm_boundaries_are_positive_and_pocock_like(cfg):
    b = compute_boundaries(EQUAL_7, cfg)
    assert all(h > 0 for h in b.harm)
    # Pocock-type: nearly flat, and far below the O'Brien-Fleming win boundary early on
    assert max(b.harm) - min(b.harm) < 0.25
    assert b.harm[0] < b.win[0]
    # the last look sits above the fixed-design 1.645: seven looks share the alpha budget
    assert norm.isf(cfg.alpha_harm) < b.harm[-1] < 2.2


@pytest.mark.parametrize("name", SCHEDULES)
def test_cumulative_crossing_equals_spending_under_null(cfg, name):
    """Win: P(cross by look k | theta = 0) = alpha_w(t_k); harm: same for the mirrored statistic."""
    t = SCHEDULES[name]
    b = compute_boundaries(t, cfg)
    for k in range(1, len(t) + 1):
        assert crossing_probability(b.win[:k], t[:k], 0.0) == pytest.approx(
            spend_win(t[k - 1], cfg.alpha_win), abs=1e-8)
        assert crossing_probability(b.harm[:k], t[:k], 0.0) == pytest.approx(
            spend_harm(t[k - 1], cfg.alpha_harm), abs=1e-8)


@given(t1=st.floats(0.05, 0.6), gaps=st.lists(st.floats(0.03, 0.4), min_size=0, max_size=5),
       alpha=st.sampled_from([0.01, 0.025, 0.05]))
@settings(max_examples=15, deadline=None)
def test_spending_identity_for_random_schedules(t1, gaps, alpha):
    t = tuple(float(x) for x in t1 + np.concatenate([[0.0], np.cumsum(gaps)]))
    win, harm = bnd.win_boundaries(t, alpha), bnd.harm_boundaries(t, alpha)
    assert crossing_probability(win, t, 0.0) == pytest.approx(spend_win(t[-1], alpha), abs=1e-8)
    assert crossing_probability(harm, t, 0.0) == pytest.approx(spend_harm(t[-1], alpha), abs=1e-8)


@pytest.mark.parametrize("bounds, t, theta", [
    ((3.0, 2.1), (0.4, 1.0), 0.0),
    ((3.0, 2.1), (0.4, 1.0), 2.5),
    ((2.8, 2.4, 2.0), (0.3, 0.6, 1.0), 1.0),
    ((2.8, 2.4, 2.0), (0.3, 0.6, 1.0), -1.0),
    ((4.0, 3.0, 2.5, 2.0), (0.2, 0.5, 0.8, 1.2), 2.0),
])
def test_matches_scipy_multivariate_normal(bounds, t, theta):
    """Independent route to the same joint law: Z ~ N(theta sqrt(t), corr = sqrt(t_j / t_k))."""
    t_arr = np.array(t)
    cov = np.minimum.outer(t_arr, t_arr) / np.sqrt(np.outer(t_arr, t_arr))
    p_none = multivariate_normal.cdf(np.array(bounds), mean=theta * np.sqrt(t_arr), cov=cov,
                                     abseps=1e-9, releps=1e-9, maxpts=2_000_000)
    assert crossing_probability(bounds, t, theta) == pytest.approx(1 - p_none, abs=1e-6)


def test_tight_spacing_matches_scipy_multivariate_normal(cfg):
    """Look 2 sits 1e-4 after look 1, so the look-1 grid must be sized for the narrow look-2 kernel.

    Reference: b2 solved from scipy's MVN CDF, P(Z1 < b1, Z2 >= b2) = alpha(t2) - alpha(t1), with
    corr = sqrt(t1 / t2); it agrees with an adaptive 1-D quadrature to < 1e-9 z. Tolerance 1e-6 z,
    as in test_quadrature_has_converged: sizing the grid for look 1 alone moves b2 by ~6e-3 z,
    which the spec's 1e-2 would miss. The 3-D CDF checks the final look against the total alpha.
    """
    t = np.array(TIGHT)
    b = compute_boundaries(TIGHT, cfg)
    rho = math.sqrt(t[0] / t[1])
    cov = np.minimum.outer(t, t) / np.sqrt(np.outer(t, t))
    for bounds, spend, alpha in ((b.win, spend_win, cfg.alpha_win),
                                 (b.harm, spend_harm, cfg.alpha_harm)):
        inc = spend(t[1], alpha) - spend(t[0], alpha)

        def gap(x, b1=bounds[0], inc=inc):  # P(Z1 < b1, Z2 >= x) - inc, as a CDF of (Z1, -Z2)
            return multivariate_normal.cdf([b1, -x], cov=[[1, -rho], [-rho, 1]], abseps=1e-15,
                                           releps=1e-10, maxpts=10_000_000) - inc

        b2_ref = brentq(gap, bounds[0] - 1, bounds[0] + 3, xtol=1e-12)
        assert bounds[1] == pytest.approx(b2_ref, abs=1e-6)
        p_none = multivariate_normal.cdf(np.array(bounds), cov=cov, abseps=1e-10, releps=1e-10,
                                         maxpts=5_000_000)
        assert 1 - p_none == pytest.approx(alpha, abs=1e-7)


def test_quadrature_has_converged(cfg, monkeypatch):
    """Finer grid and wider tails move no boundary by more than 1e-6 z (spec tolerance: 1e-2)."""
    coarse = compute_boundaries(EQUAL_7, cfg)
    monkeypatch.setattr(bnd, "GRID_R", 2 * bnd.GRID_R)
    monkeypatch.setattr(bnd, "TAIL_SD", bnd.TAIL_SD + 1)
    fine = compute_boundaries(EQUAL_7, cfg)
    assert coarse.win == pytest.approx(fine.win, abs=1e-6)
    assert coarse.harm == pytest.approx(fine.harm, abs=1e-6)


def test_blocked_kernel_evaluation_is_exact(cfg, monkeypatch):
    whole = compute_boundaries(EQUAL_7, cfg)
    monkeypatch.setattr(bnd, "KERNEL_BLOCK", 1_000)  # forces many row blocks
    blocked = compute_boundaries(EQUAL_7, cfg)
    assert blocked.win == pytest.approx(whole.win, abs=1e-12)
    assert blocked.harm == pytest.approx(whole.harm, abs=1e-12)


@pytest.mark.parametrize("name", SCHEDULES)
def test_prefix_property(cfg, name):
    t = SCHEDULES[name]
    full = compute_boundaries(t, cfg)
    for k in range(1, len(t)):
        part = compute_boundaries(t[:k], cfg)
        assert part.win == pytest.approx(full.win[:k], abs=1e-12)
        assert part.harm == pytest.approx(full.harm[:k], abs=1e-12)


def test_repeated_calls_are_memoised_and_independent(cfg):
    first = compute_boundaries(EQUAL_7, cfg)
    again = compute_boundaries(list(EQUAL_7), cfg)  # any sequence is normalised to the same key
    assert again == first and again is not first
    info = bnd._cached_bounds.cache_info()
    assert (info.misses, info.hits) == (2, 2)  # win and harm computed once, then reused


def test_boundaries_are_non_binding(cfg):
    """Win ignores harm and vice versa (§2.7): changing one alpha leaves the other unchanged."""
    base = compute_boundaries(EQUAL_5, cfg)
    other = compute_boundaries(EQUAL_5, cfg.model_copy(update={"alpha_win": 0.05, "alpha_harm": 0.01}))
    assert compute_boundaries(EQUAL_5, cfg.model_copy(update={"alpha_harm": 0.01})).win == base.win
    assert compute_boundaries(EQUAL_5, cfg.model_copy(update={"alpha_win": 0.05})).harm == base.harm
    assert other.win != base.win and other.harm != base.harm


@pytest.mark.parametrize("alpha_win, alpha_harm", [(0.025, 0.05), (0.01, 0.1)])
def test_single_look_is_the_fixed_design_critical_value(cfg, alpha_win, alpha_harm):
    c = cfg.model_copy(update={"alpha_win": alpha_win, "alpha_harm": alpha_harm})
    b = compute_boundaries((1.0,), c)
    assert b.win == (pytest.approx(norm.isf(alpha_win), abs=1e-12),)
    assert b.harm == (pytest.approx(norm.isf(alpha_harm), abs=1e-12),)
    # a single look before the end spends only alpha(t): the critical value is higher
    early = compute_boundaries((0.5,), c)
    assert early.win[0] == pytest.approx(norm.isf(spend_win(0.5, alpha_win)), abs=1e-12)
    assert early.win[0] > b.win[0] and early.harm[0] > b.harm[0]


def test_zero_spending_increment_gives_infinite_boundary(cfg):
    b = compute_boundaries((1.0, 1.2), cfg)
    assert b.win[0] == pytest.approx(norm.isf(cfg.alpha_win), abs=1e-12)
    assert b.harm[0] == pytest.approx(norm.isf(cfg.alpha_harm), abs=1e-12)
    assert b.win[1] == math.inf and b.harm[1] == math.inf
    assert compute_boundaries((0.5, 1.0, 1.5), cfg).win[2:] == (math.inf,)
    # an infinite boundary never crosses, so it adds nothing to the crossing probability
    assert crossing_probability(b.win, (1.0, 1.2), 1.0) == pytest.approx(
        crossing_probability(b.win[:1], (1.0,), 1.0), abs=1e-12)


def test_information_overshoot(cfg):
    """t > 1: spending is capped at alpha, so the look that crosses t = 1 spends the rest, every
    later look has no alpha left (+inf), and the total type I error never exceeds alpha."""
    b = compute_boundaries(OVERSHOOT, cfg)  # looks at 0.4, 0.9, 1.15, 1.3
    assert all(math.isfinite(x) for x in b.win[:3] + b.harm[:3])
    assert b.win[3] == math.inf and b.harm[3] == math.inf
    assert crossing_probability(b.win, OVERSHOOT, 0.0) == pytest.approx(cfg.alpha_win, abs=1e-8)
    assert crossing_probability(b.harm, OVERSHOOT, 0.0) == pytest.approx(cfg.alpha_harm, abs=1e-8)
    # a look after the budget is exhausted cannot cross; the look that exhausts it can
    spent = compute_boundaries((0.6, 1.4, 1.6), cfg)
    assert math.isfinite(spent.win[1]) and spent.win[2] == math.inf
    assert crossing_probability(spent.win, (0.6, 1.4, 1.6), 0.0) == pytest.approx(
        cfg.alpha_win, abs=1e-8)


BAD_FRACTIONS = [[], [0.0, 0.5], [-0.1, 0.5], [0.5, 0.5], [0.6, 0.4], [0.5, float("nan")],
                 [0.5, math.inf], [float("nan")]]


@pytest.mark.parametrize("bad", BAD_FRACTIONS)
def test_bad_info_fractions_raise(cfg, bad):
    with pytest.raises(ValueError):
        compute_boundaries(bad, cfg)
    with pytest.raises(ValueError):
        crossing_probability([2.0] * len(bad), bad, 0.0)
    with pytest.raises(ValueError):
        monte_carlo_crossing([2.0] * len(bad), bad, 0.0, 10, 1)


def test_bad_bounds_and_paths_raise():
    with pytest.raises(ValueError):
        crossing_probability([2.0], [0.5, 1.0], 0.0)
    with pytest.raises(ValueError):
        crossing_probability([2.0, float("nan")], [0.5, 1.0], 0.0)
    with pytest.raises(ValueError):
        monte_carlo_crossing([2.0], [0.5, 1.0], 0.0, 10, 1)
    with pytest.raises(ValueError):
        monte_carlo_crossing([2.0], [1.0], 0.0, 0, 1)


# --- crossing_probability -------------------------------------------------------------------

def test_crossing_probability_single_look_closed_form():
    for b, t, theta in [(1.96, 1.0, 0.0), (2.5, 0.4, 1.7), (-0.3, 0.7, 0.2)]:
        assert crossing_probability([b], [t], theta) == pytest.approx(
            norm.sf(b - theta * math.sqrt(t)), abs=1e-14)


def test_crossing_probability_increases_with_drift(cfg):
    b = compute_boundaries(EQUAL_7, cfg)
    ps = [crossing_probability(b.win, EQUAL_7, th) for th in np.linspace(-2, 8, 41)]
    assert (np.diff(ps) > 0).all()
    assert ps[0] < cfg.alpha_win / 100 and ps[-1] > 1 - 1e-6
    assert crossing_probability(b.win, EQUAL_7, 0.0) == pytest.approx(cfg.alpha_win, abs=1e-8)


def test_crossing_probability_is_at_least_the_final_look_alone(cfg):
    """P(any crossing) >= P(Z_K >= b_K): the bound behind the R upper bound in test_power."""
    b = compute_boundaries(EQUAL_5, cfg)
    for theta in (0.5, 2.0, 3.0):
        assert crossing_probability(b.win, EQUAL_5, theta) >= norm.sf(b.win[-1] - theta) - 1e-12


def test_crossing_probability_all_infinite_bounds_is_zero():
    assert crossing_probability([math.inf] * 3, [0.3, 0.6, 1.0], 2.0) == 0.0


def test_large_drift_empties_the_continuation_region():
    # theta so large that the walk is past the boundary before the second look
    assert crossing_probability([0.5, 2.0], [0.2, 1.0], 40.0) == pytest.approx(1.0, abs=1e-12)


# --- Monte Carlo ----------------------------------------------------------------------------

def test_monte_carlo_is_deterministic_and_seeded(cfg):
    b = compute_boundaries(EQUAL_5, cfg)
    one = monte_carlo_crossing(b.harm, EQUAL_5, 0.0, 100_000, 1)
    assert one == monte_carlo_crossing(b.harm, EQUAL_5, 0.0, 100_000, 1)
    assert one != monte_carlo_crossing(b.harm, EQUAL_5, 0.0, 100_000, 2)


def test_monte_carlo_chunking_does_not_change_the_answer(cfg, monkeypatch):
    b = compute_boundaries(EQUAL_5, cfg)
    n = 2 * bnd.MC_CHUNK + 12_345   # not a multiple of the chunk size
    p = monte_carlo_crossing(b.harm, EQUAL_5, 0.0, n, MC_SEED)
    assert p == pytest.approx(crossing_probability(b.harm, EQUAL_5, 0.0), abs=_mc_tol(0.05))
    monkeypatch.setattr(bnd, "MC_CHUNK", 7_777)  # same law, different chunks: still agrees
    assert monte_carlo_crossing(b.harm, EQUAL_5, 0.0, n, MC_SEED) == pytest.approx(p, abs=2 * _mc_tol(0.05))


@pytest.mark.parametrize("schedule", [EQUAL_5, UNEQUAL])
def test_monte_carlo_matches_spending_at_every_look(cfg, schedule):
    """Cumulative probabilities: MC cannot pin the earliest win boundary (z ~ 4.9) by itself."""
    b = compute_boundaries(schedule, cfg)
    for k in range(1, len(schedule) + 1):
        for bounds, target in ((b.win, spend_win(schedule[k - 1], cfg.alpha_win)),
                               (b.harm, spend_harm(schedule[k - 1], cfg.alpha_harm))):
            mc = monte_carlo_crossing(bounds[:k], schedule[:k], 0.0, MC_PATHS, MC_SEED)
            assert mc == pytest.approx(target, abs=_mc_tol(target))


def test_monte_carlo_agrees_with_recursion_under_null_and_power_drift(cfg):
    t = EQUAL_7
    b = compute_boundaries(t, cfg)
    z = norm.isf(cfg.alpha_win) + norm.ppf(cfg.power)
    for theta in (0.0, z * math.sqrt(inflation_factor(cfg)), z, 0.5 * z):
        for bounds in (b.win, b.harm):
            exact = crossing_probability(bounds, t, theta)
            mc = monte_carlo_crossing(bounds, t, theta, MC_PATHS, MC_SEED)
            assert mc == pytest.approx(exact, abs=_mc_tol(exact)), (theta, bounds[0])


# --- planned_info_fractions -----------------------------------------------------------------

def _looks(cfg: ExperimentConfig, k: int, min_first: float | None = None) -> ExperimentConfig:
    update = {"window": cfg.window.model_copy(update={"looks": k})}
    if min_first is not None:
        update["min_info_first_look"] = min_first
    return cfg.model_copy(update=update)


def test_planned_info_fractions_examples(cfg):
    assert cfg.min_info_first_look == 0.2
    assert planned_info_fractions(_looks(cfg, 5)) == pytest.approx((0.2, 0.4, 0.6, 0.8, 1.0))
    seven = planned_info_fractions(_looks(cfg, 7))
    assert seven == pytest.approx((0.2, 1 / 3, 0.2 + 2 * (0.8 / 6), 0.6, 0.2 + 4 * (0.8 / 6),
                                   0.2 + 5 * (0.8 / 6), 1.0))
    assert seven[1] == pytest.approx(0.3333, abs=1e-4)
    assert planned_info_fractions(_looks(cfg, 1)) == (1.0,)
    assert planned_info_fractions(_looks(cfg, 2)) == pytest.approx((0.5, 1.0))
    # t1 = max(min_info_first_look, 1/K): the larger floor wins, or 1/K when it is larger
    assert planned_info_fractions(_looks(cfg, 3, 0.5)) == pytest.approx((0.5, 0.75, 1.0))
    assert planned_info_fractions(_looks(cfg, 10, 0.05)) == pytest.approx(
        tuple(np.linspace(0.1, 1.0, 10)))


@pytest.mark.parametrize("k", [1, 2, 5, 7, 12])
def test_planned_info_fractions_are_valid_schedules(cfg, k):
    t = planned_info_fractions(_looks(cfg, k))
    assert len(t) == k and t[-1] == 1.0 and all(type(x) is float for x in t)
    assert t[0] >= cfg.min_info_first_look or k == 1
    assert all(b > a for a, b in zip(t, t[1:]))
    compute_boundaries(t, cfg)  # accepted by the validator


# --- CLI ------------------------------------------------------------------------------------

def test_cli_passes_and_prints_every_section(monkeypatch, capsys):
    monkeypatch.setattr(bnd, "MC_PATHS", 100_000)  # the 10^6-path run is covered above
    assert bnd.main([]) == 0
    out = capsys.readouterr().out
    assert "5 equally spaced looks" in out and "7 equally spaced looks" in out
    assert "Reference check" in out and "PASS (max |diff|" in out
    assert "Monte Carlo cross-check" in out and "FAIL" not in out
    assert "inflation factor R = 1.03" in out  # 7 looks from t = 0.2 (vani-meeting-v1)


def test_cli_exits_non_zero_when_a_check_fails(monkeypatch, capsys):
    monkeypatch.setattr(bnd, "MC_PATHS", 100_000)
    monkeypatch.setattr(bnd, "REFERENCE_WIN_5", (4.0, 3.0, 2.5, 2.2, 2.0))
    assert bnd.main([]) == 1
    assert "FAIL (max |diff|" in capsys.readouterr().out


def test_cli_skips_the_reference_for_other_alphas(monkeypatch, capsys, tmp_path):
    text = (CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml").read_text()
    other = tmp_path / "exp.yaml"
    other.write_text(text.replace("alpha_win: 0.025", "alpha_win: 0.05"))
    monkeypatch.setattr(bnd, "MC_PATHS", 100_000)
    assert bnd.main([str(other)]) == 0
    assert "skipped: alpha_win is 0.05" in capsys.readouterr().out
