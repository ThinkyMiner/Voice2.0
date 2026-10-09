import math

import numpy as np
import pytest
import yaml
from scipy.stats import norm

from abtest.contracts import CONFIGS_DIR, ExperimentConfig
from abtest.power import detectable_lift, fixed_n_b, inflation_factor, n_max, preflight

CONFIGS = sorted((CONFIGS_DIR / "experiments").glob("*.yaml"))


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


def test_inflation_factor_placeholder(cfg):
    assert inflation_factor(cfg) == 1.0


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
