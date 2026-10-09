import math

import numpy as np
import pytest
import yaml
from scipy.stats import norm

from abtest import power as power_module
from abtest.boundaries import monte_carlo_crossing, planned_info_fractions, win_boundaries
from abtest.contracts import CONFIGS_DIR, ExperimentConfig
from abtest.power import detectable_lift, fixed_n_b, inflation_factor, n_max, preflight

CONFIGS = sorted((CONFIGS_DIR / "experiments").glob("*.yaml"))
MC_PATHS = 1_000_000   # PLAN.md §2.7: at least 10^6 paths
MC_SEED = 20261009


def _cfg(path):
    return ExperimentConfig.model_validate(yaml.safe_load(open(path)))


@pytest.fixture(params=CONFIGS, ids=lambda p: p.stem)
def cfg(request):
    return _cfg(request.param)


@pytest.fixture
def headline():
    return _cfg(CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml")


def test_textbook_anchor():
    # 0.5 vs 0.6, equal split, one-sided alpha 0.025, power 0.8: ~387-388 per group.
    assert fixed_n_b(0.5, 0.2, 0.5, 0.025, 0.8) == pytest.approx(387.3, abs=0.5)


def test_monte_carlo_power_matches(headline):
    c = headline
    share_b, p_a, mde = 0.1, c.baseline_p, 0.15
    p_b = p_a * (1 + mde)
    n_b = math.ceil(fixed_n_b(p_a, mde, share_b, c.alpha_win, c.power))
    n_a = math.ceil((1 - share_b) / share_b * n_b)
    rng = np.random.default_rng(20261009)
    reps = 400_000
    x_a, x_b = rng.binomial(n_a, p_a, reps), rng.binomial(n_b, p_b, reps)
    pooled = (x_a + x_b) / (n_a + n_b)
    z = (x_b / n_b - x_a / n_a) / np.sqrt(pooled * (1 - pooled) * (1 / n_a + 1 / n_b))
    assert (z > norm.ppf(1 - c.alpha_win)).mean() == pytest.approx(c.power, abs=0.015)


def test_decrease_mirrors_increase():
    # 0.5 -> 0.4 needs the same n as 0.5 -> 0.6 at an equal split (variance symmetric about 0.5).
    assert fixed_n_b(0.5, 0.2, 0.5, 0.025, 0.8, "decrease") == pytest.approx(
        fixed_n_b(0.5, 0.2, 0.5, 0.025, 0.8))
    # A decrease from p is an increase of the complement 1-p, at any split.
    p, m = 0.3, 0.2
    assert fixed_n_b(p, m, 0.1, 0.025, 0.8, "decrease") == pytest.approx(
        fixed_n_b(1 - p, p * m / (1 - p), 0.1, 0.025, 0.8, "increase"))


@pytest.mark.parametrize("p,m,better", [(0.9, 0.2, "increase"), (0.5, 1.0, "increase"),
                                        (0.3, 1.0, "decrease"), (0.3, 1.5, "decrease")])
def test_infeasible_p_b_raises(p, m, better):
    with pytest.raises(ValueError):
        fixed_n_b(p, m, 0.1, 0.025, 0.8, better)


def test_n_max_fields(cfg):
    nm = n_max(cfg)
    k = cfg.allocation.A / cfg.allocation.B
    assert nm.n_b_max == pytest.approx(nm.inflation_factor * nm.n_b_fixed)
    assert nm.n_a_max == pytest.approx(k * nm.n_b_max)
    assert nm.n_total_max == pytest.approx(nm.n_a_max + nm.n_b_max)


def _looks(c: ExperimentConfig, k: int) -> ExperimentConfig:
    return c.model_copy(update={"window": c.window.model_copy(update={"looks": k})})


def test_inflation_factor_is_one_for_a_single_look(cfg):
    # one look at t = 1 is the fixed design: boundary z_{1-alpha}, drift z_{1-alpha} + z_power
    assert inflation_factor(_looks(cfg, 1)) == pytest.approx(1.0, abs=1e-6)


def test_inflation_factor_grows_with_the_number_of_looks(cfg):
    r = [inflation_factor(_looks(cfg, k)) for k in (1, 2, 5, 7)]
    assert r[0] == pytest.approx(1.0, abs=1e-6)
    assert all(b > a for a, b in zip(r, r[1:])), r  # every extra look costs a little power


def test_inflation_factor_bounds_for_five_looks(cfg):
    """O'Brien-Fleming-type spending is cheap: R is a few percent, nowhere near Pocock's ~1.2.

    Rigorous bounds (no published table needed): stopping early never adds required information,
    so R > 1; and P(cross) >= P(Z_K >= b_K) = Phi(theta - b_K), so theta <= b_K + z_power and
    R <= ((b_K + z_power) / (z_{1-alpha} + z_power))^2, with b_K the final win boundary."""
    c = _looks(cfg, 5)
    r = inflation_factor(c)
    b_final = win_boundaries(planned_info_fractions(c), c.alpha_win)[-1]
    z_power, z_fixed = norm.ppf(c.power), norm.isf(c.alpha_win) + norm.ppf(c.power)
    upper = ((b_final + z_power) / z_fixed) ** 2
    assert 1.0 < r < upper < 1.06


def test_inflation_factor_depends_on_alpha_power_and_schedule(cfg):
    base = inflation_factor(cfg)
    assert inflation_factor(cfg.model_copy(update={"alpha_win": 0.05})) != base
    assert inflation_factor(cfg.model_copy(update={"power": 0.9})) != base
    # first look no earlier than t = 0.5: three looks fit between 0.5 and 1, not seven from 0.2
    assert inflation_factor(cfg.model_copy(update={"min_info_first_look": 0.5})) != base
    # harm, mde and allocation never enter R: the harm boundary is non-binding (§2.7)
    assert inflation_factor(cfg.model_copy(update={"alpha_harm": 0.01})) == base
    mde = cfg.primary.model_copy(update={"mde_rel": 0.3})
    assert inflation_factor(cfg.model_copy(update={"primary": mde})) == base


def test_inflation_factor_is_cached_across_configs(cfg):
    power_module._inflation.cache_clear()
    first = inflation_factor(cfg)
    assert power_module._inflation.cache_info().misses == 1
    assert inflation_factor(cfg.model_copy()) == first
    other = cfg.model_copy(update={"id": "another-experiment", "salt": "x"})  # same statistics
    assert inflation_factor(other) == first
    info = power_module._inflation.cache_info()
    assert (info.misses, info.hits) == (1, 2)


def test_power_at_the_inflated_drift_is_the_configured_power(cfg):
    """theta = (z_{1-alpha} + z_power) * sqrt(R) crosses the planned win boundaries with
    probability = power. Monte Carlo, independent of the recursion that produced R."""
    for k in (5, 7):
        c = _looks(cfg, k)
        planned = planned_info_fractions(c)
        theta = (norm.isf(c.alpha_win) + norm.ppf(c.power)) * math.sqrt(inflation_factor(c))
        mc = monte_carlo_crossing(win_boundaries(planned, c.alpha_win), planned, theta,
                                  MC_PATHS, MC_SEED)
        se = math.sqrt(c.power * (1 - c.power) / MC_PATHS)
        assert mc == pytest.approx(c.power, abs=4 * se)
        # and the fixed-design drift (R = 1) falls short: that is why n_max is inflated
        fixed = monte_carlo_crossing(win_boundaries(planned, c.alpha_win), planned,
                                     norm.isf(c.alpha_win) + norm.ppf(c.power), MC_PATHS, MC_SEED)
        assert fixed < c.power - 4 * se


def test_n_max_scales_the_fixed_design_by_the_computed_r(cfg):
    nm = n_max(cfg)
    assert nm.inflation_factor == inflation_factor(cfg) > 1.0
    assert nm.n_b_max > nm.n_b_fixed


@pytest.mark.parametrize("better", ["increase", "decrease"])
def test_detectable_lift_inverts_n_max(cfg, better):
    c = cfg.model_copy(update={"primary": cfg.primary.model_copy(update={"better": better})})
    assert detectable_lift(c, n_max(c).n_b_max) == pytest.approx(c.primary.mde_rel, rel=1e-6)
    assert detectable_lift(c, 1.0) == math.inf


def test_preflight(cfg):
    nm, share_b = n_max(cfg), cfg.allocation.B
    m, usable = cfg.maturation_days, cfg.window.days - cfg.maturation_days
    fit = nm.n_b_max / (share_b * usable)  # daily triggered leads that fill the window exactly

    exact = preflight(cfg, fit)
    assert exact.scale_to_fit_window == pytest.approx(1.0)
    assert exact.days_needed == pytest.approx(cfg.window.days)
    assert exact.min_detectable_lift_rel == pytest.approx(cfg.primary.mde_rel, rel=1e-6)

    low, high = preflight(cfg, fit / 4), preflight(cfg, fit * 4)
    assert low.underpowered and not high.underpowered
    assert low.days_needed == pytest.approx(nm.n_b_max / (share_b * fit / 4) + m)
    assert low.scale_to_fit_window == pytest.approx(4.0)
    assert low.min_detectable_lift_rel > cfg.primary.mde_rel > high.min_detectable_lift_rel
    assert low.n_max == nm and low.window_days == cfg.window.days


def test_preflight_rejects_window_inside_maturation(cfg):
    c = cfg.model_copy(update={"maturation_days": float(cfg.window.days)})
    with pytest.raises(ValueError):
        preflight(c, 100.0)


def test_preflight_cli_reports_how_r_was_computed(cfg, monkeypatch, capsys):
    from abtest import data, profile
    from abtest.contracts import VolumeStats

    path = next(p for p in CONFIGS if _cfg(p).id == cfg.id)
    monkeypatch.setattr(data, "load", lambda *a, **k: (None, None))  # no data/ on CI
    monkeypatch.setattr(profile, "volume_stats", lambda df, days: VolumeStats(
        window_days=days, daily_leads=500.0, daily_triggered_leads=300.0))
    power_module.main([str(path)])
    row = next(ln for ln in capsys.readouterr().out.splitlines() if "inflation factor R" in ln)
    assert f"{inflation_factor(cfg):.3f}" in row
    assert "7 looks at t = 0.20…1.00" in row and "placeholder" not in row
