"""analysis.analyze_look: per-look statistics -> LookResult (PLAN.md §2.7, §2.8 rule 1).

Frames are built here in the LeadOutcome layout; boundaries are made-up numbers, so nothing
depends on boundaries.py or on data/.
"""

import math
import time
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from scipy.stats import binomtest, chi2 as chi2_dist, chisquare, norm
from statsmodels.stats.proportion import confint_proportions_2indep, proportions_ztest

import abtest.analysis as analysis
from abtest import config
from abtest.analysis import CONFIDENCE, Z_CI, _srm_test, analyze_look
from abtest.contracts import CONFIGS_DIR, Boundaries, LeadOutcome, LookResult, SecondaryMetric, SrmResult
from abtest.power import detectable_lift, n_max

MEETING = config.load_experiment(CONFIGS_DIR / "experiments" / "vani-meeting-v1.yaml")  # continuous guardrail
BUYER = config.load_experiment(CONFIGS_DIR / "experiments" / "vani-buyer-qty-asks-v1.yaml")  # binary guardrail
TS = datetime(2026, 3, 10)
T0 = np.datetime64("2026-03-01T00:00:00", "ns")
BOUNDS = Boundaries(info_fractions=(0.3, 0.6, 1.0), win=(4.5, 3.2, 2.1), harm=(2.6, 2.4, 2.1))


def outcomes(variant, primary, secondary=None, mature=None, triggered=None) -> pd.DataFrame:
    """A LeadOutcome-layout frame. `mature` defaults to all True; `triggered` to `mature`
    (mature implies triggered). Untriggered rows get primary NA and secondary NaN."""
    variant = np.asarray(variant)
    n = len(variant)
    mature = np.ones(n, bool) if mature is None else np.asarray(mature, bool)
    triggered = mature.copy() if triggered is None else np.asarray(triggered, bool) | mature
    secondary = np.full(n, np.nan) if secondary is None else np.asarray(secondary, float)
    return pd.DataFrame({
        "lead_id": [f"L{i}" for i in range(n)],
        "variant": variant,
        "assigned_ts": np.full(n, T0),
        "triggered_ts": pd.Series(np.where(triggered, T0, np.datetime64("NaT", "ns")), dtype="datetime64[ns]"),
        "mature": mature,
        "primary": pd.Series(pd.arrays.IntegerArray(np.asarray(primary, "int64"), mask=~triggered)),
        "secondary": np.where(triggered, secondary, np.nan),
        "n_calls": np.ones(n, "int64"),
        "n_answered": triggered.astype("int64"),
    })


def two_arms(rng, n_a, n_b, p_a, p_b, sec_a=None, sec_b=None) -> pd.DataFrame:
    """All-mature frame. sec_a/sec_b: arrays, or None for a continuous gamma(2, 60) draw."""
    prim = np.concatenate([rng.random(n_a) < p_a, rng.random(n_b) < p_b]).astype(int)
    sa = rng.gamma(2, 60, n_a) if sec_a is None else sec_a
    sb = rng.gamma(2, 60, n_b) if sec_b is None else sec_b
    return outcomes(["A"] * n_a + ["B"] * n_b, prim, np.concatenate([sa, sb]))


def look(df, cfg=MEETING, k=1, bounds=BOUNDS, is_final=False) -> LookResult:
    return analyze_look(df, cfg, k, TS, bounds, is_final)


def with_primary(cfg, **update):
    return cfg.model_copy(update={"primary": cfg.primary.model_copy(update=update)})


def with_secondary(cfg, **update):
    return cfg.model_copy(update={"secondary": cfg.secondary.model_copy(update=update)})


# --- primary: z, CI, orientation ------------------------------------------------------------

@pytest.mark.parametrize("seed", range(8))
def test_z_and_p_agree_with_statsmodels_pooled_z_test(seed):
    rng = np.random.default_rng(seed)
    n_a, n_b = int(rng.integers(300, 4000)), int(rng.integers(100, 800))
    p_a = rng.uniform(0.05, 0.5)
    df = two_arms(rng, n_a, n_b, p_a, p_a * rng.uniform(0.7, 1.4))
    x_a, x_b = int(df.primary[:n_a].sum()), int(df.primary[n_a:].sum())
    z_sm, p_sm = proportions_ztest([x_b, x_a], [n_b, n_a], alternative="larger")  # pooled variance under H0
    r = look(df)
    assert r.primary.z == pytest.approx(z_sm, abs=1e-9)
    assert norm.sf(r.primary.z) == pytest.approx(p_sm, rel=1e-9)
    assert (r.n_A, r.n_B) == (n_a, n_b)
    assert r.primary.rate_A == x_a / n_a and r.primary.rate_B == x_b / n_b


def test_ci_uses_the_unpooled_se_and_z_the_pooled_one():
    rng = np.random.default_rng(1)
    n_a, n_b = 3000, 300
    df = two_arms(rng, n_a, n_b, 0.20, 0.30)  # unequal n and rates, so the two SEs differ
    r = look(df).primary
    p_a, p_b = r.rate_A, r.rate_B
    assert r.se_unpooled == pytest.approx(math.sqrt(p_a * (1 - p_a) / n_a + p_b * (1 - p_b) / n_b))
    p_bar = (p_a * n_a + p_b * n_b) / (n_a + n_b)
    assert r.se_pooled == pytest.approx(math.sqrt(p_bar * (1 - p_bar) * (1 / n_a + 1 / n_b)))
    assert r.se_pooled != pytest.approx(r.se_unpooled, rel=1e-3)
    assert r.z == pytest.approx(r.lift_abs / r.se_pooled)
    assert r.ci95 == pytest.approx((r.lift_abs - Z_CI * r.se_unpooled, r.lift_abs + Z_CI * r.se_unpooled))
    lo, hi = confint_proportions_2indep(int(df.primary[n_a:].sum()), n_b, int(df.primary[:n_a].sum()), n_a,
                                        method="wald", compare="diff", alpha=1 - CONFIDENCE)
    assert r.ci95 == pytest.approx((lo, hi))  # statsmodels' Wald interval is the unpooled one
    assert r.lift_abs == pytest.approx(p_b - p_a) and r.lift_rel == pytest.approx((p_b - p_a) / p_a)


def test_ci_level_is_the_named_constant():
    assert CONFIDENCE == 0.95
    assert Z_CI == pytest.approx(norm.ppf(0.975))


def test_z_orientation_flips_for_a_decrease_goal():
    df = two_arms(np.random.default_rng(3), 2000, 400, 0.30, 0.22)  # B is lower
    inc = look(df, with_primary(MEETING, better="increase")).primary
    dec = look(df, with_primary(MEETING, better="decrease")).primary
    assert inc.z < -1 and dec.z == pytest.approx(-inc.z)  # decrease: B lower is B better
    assert dec.lift_abs == inc.lift_abs and dec.ci95 == inc.ci95  # raw B - A scale, not oriented
    assert dec.se_pooled == inc.se_pooled


# --- secondary ------------------------------------------------------------------------------

def test_delta_method_se_matches_a_monte_carlo_sd_of_the_relative_change():
    rng = np.random.default_rng(20261009)
    n_a, n_b, reps = 600, 200, 6000
    xa = rng.gamma(2, 60, (reps, n_a))
    xb = rng.gamma(2, 63, (reps, n_b))  # B is 5% higher
    rel = xb.mean(1) / xa.mean(1) - 1
    prim = rng.integers(0, 2, n_a + n_b)
    got = [look(outcomes(["A"] * n_a + ["B"] * n_b, prim, np.concatenate([xa[i], xb[i]]))).secondary
           for i in range(60)]
    assert [s.delta for s in got] == pytest.approx(rel[:60])  # delta is the relative change itself
    assert float(np.mean([s.se for s in got])) == pytest.approx(rel.std(ddof=1), rel=0.05)


def test_continuous_secondary_follows_the_formulas():
    rng = np.random.default_rng(5)
    a, b = rng.gamma(2, 60, 500), rng.gamma(2, 70, 120)
    s = look(outcomes(["A"] * 500 + ["B"] * 120, rng.integers(0, 2, 620), np.concatenate([a, b]))).secondary
    ma, mb, va, vb = a.mean(), b.mean(), a.var(ddof=1), b.var(ddof=1)
    assert (s.mean_A, s.mean_B) == pytest.approx((ma, mb))
    assert s.delta == pytest.approx((mb - ma) / ma) and s.rel_change == s.delta
    assert s.delta_type == "relative"
    assert s.se == pytest.approx(math.sqrt(vb / (120 * ma**2) + mb**2 * va / (500 * ma**4)))
    assert s.ci95 == pytest.approx((s.delta - Z_CI * s.se, s.delta + Z_CI * s.se))
    assert (s.name, s.type, s.role, s.margin) == ("answered_duration_s", "continuous", "guardrail", 0.10)


def test_binary_guardrail_delta_is_absolute_with_the_unpooled_se():
    n_a, n_b, k_a, k_b = 200, 100, 30, 10
    sec = np.r_[np.ones(k_a), np.zeros(n_a - k_a), np.ones(k_b), np.zeros(n_b - k_b)]
    rng = np.random.default_rng(2)
    r = look(outcomes(["A"] * n_a + ["B"] * n_b, rng.integers(0, 2, n_a + n_b), sec), BUYER)
    s = r.secondary
    assert (s.mean_A, s.mean_B) == (0.15, 0.10)
    assert s.delta == pytest.approx(-0.05) and s.delta_type == "absolute"
    assert s.rel_change == pytest.approx(-0.05 / 0.15)  # always relative
    assert s.se == pytest.approx(math.sqrt(0.15 * 0.85 / n_a + 0.10 * 0.90 / n_b))
    lo, hi = confint_proportions_2indep(k_b, n_b, k_a, n_a, method="wald", compare="diff", alpha=1 - CONFIDENCE)
    assert s.ci95 == pytest.approx((lo, hi))
    assert (s.name, s.type, s.role, s.margin) == ("abrupt_disconnect", "binary", "guardrail", 0.02)


def test_rci_uses_the_win_boundary_of_this_look():
    df = two_arms(np.random.default_rng(4), 800, 200, 0.2, 0.2)
    for k, c in enumerate(BOUNDS.win, start=1):
        s = look(df, k=k).secondary
        assert s.rci == pytest.approx((s.delta - c * s.se, s.delta + c * s.se))
    # the RCI is wider than the plain CI exactly when c_k > z_{0.975}
    s = look(df, k=3).secondary
    assert BOUNDS.win[2] > Z_CI and s.rci[0] < s.ci95[0] and s.rci[1] > s.ci95[1]


def test_rci_is_unbounded_when_the_boundary_is_infinite():
    bounds = Boundaries(info_fractions=(0.2, 1.0), win=(math.inf, 2.0), harm=(math.inf, 2.0))
    df = two_arms(np.random.default_rng(4), 800, 200, 0.2, 0.2)
    assert look(df, k=1, bounds=bounds).secondary.rci == (-math.inf, math.inf)
    assert look(df, k=2, bounds=bounds).secondary.rci[0] > -math.inf
    flat = outcomes(["A"] * 20 + ["B"] * 10, [0, 1] * 15, np.full(30, 100.0))  # se == 0, and inf * 0 is NaN
    r = look(flat, k=1, bounds=bounds).secondary
    assert r.se == 0.0 and r.rci == (-math.inf, math.inf)


def test_goal_secondary_has_no_margin():
    goal = SecondaryMetric(name="dur", type="continuous", role="goal", better="increase")
    r = look(two_arms(np.random.default_rng(6), 300, 100, 0.2, 0.2), MEETING.model_copy(update={"secondary": goal}))
    assert r.secondary.margin is None and r.secondary.role == "goal" and r.secondary.name == "dur"


def test_mature_leads_without_a_secondary_value_stay_in_n_but_not_in_the_secondary():
    sec = np.array([10.0, 20.0, np.nan, 100.0, np.nan, 200.0])  # NaN: answered, no duration recorded
    df = outcomes(["A", "A", "A", "B", "B", "B"], [1, 0, 1, 0, 0, 1], sec)
    r = look(df)
    assert (r.n_A, r.n_B) == (3, 3)
    assert (r.secondary.mean_A, r.secondary.mean_B) == (15.0, 150.0)
    assert r.secondary.se == pytest.approx(math.sqrt(  # n = 2 per arm, not 3
        np.var([100, 200], ddof=1) / (2 * 15.0**2) + 150.0**2 * np.var([10, 20], ddof=1) / (2 * 15.0**4)))


# --- analysis set, information fraction, SRM ------------------------------------------------

def test_immature_and_untriggered_leads_are_out_of_n_but_inside_srm():
    # A: 10 mature, 3 immature (triggered), 5 untriggered. B: 4 mature, 1 immature, 2 untriggered.
    variant = ["A"] * 18 + ["B"] * 7
    mature = [True] * 10 + [False] * 8 + [True] * 4 + [False] * 3
    triggered = [True] * 13 + [False] * 5 + [True] * 5 + [False] * 2
    primary = [1, 0] * 9 + [1, 1, 0, 1, 1, 0, 0]  # includes values on immature rows: ignored below
    r = look(outcomes(variant, primary, np.arange(25) + 1.0, mature, triggered))
    assert (r.n_A, r.n_B) == (10, 4)
    assert r.primary.rate_A == pytest.approx(np.mean(primary[:10])) and r.primary.rate_B == pytest.approx(np.mean(primary[18:22]))
    assert r.secondary.mean_A == pytest.approx(np.mean(np.arange(1, 11)))  # mature rows only
    assert (r.srm_assigned.n_A, r.srm_assigned.n_B) == (18, 7)
    assert (r.srm_triggered.n_A, r.srm_triggered.n_B) == (13, 5)  # triggered, mature or not
    assert r.srm_assigned.configured_share_B == r.srm_triggered.configured_share_B == MEETING.allocation.B


def test_info_fraction_is_matured_n_b_over_n_b_max():
    df = two_arms(np.random.default_rng(8), 900, 150, 0.2, 0.2)
    r = look(df)
    assert r.info_fraction == 150 / n_max(MEETING).n_b_max
    binary = outcomes(df.variant, df.primary, (df.secondary > 120).astype(float))  # BUYER's guardrail is binary
    assert look(binary, BUYER).info_fraction == 150 / n_max(BUYER).n_b_max


def test_detectable_mde_comes_from_power_and_is_none_when_infeasible_or_no_b(monkeypatch):
    df = two_arms(np.random.default_rng(8), 900, 150, 0.2, 0.2)
    assert look(df).primary.detectable_mde_rel == pytest.approx(detectable_lift(MEETING, 150))
    monkeypatch.setattr(analysis, "detectable_lift", lambda cfg, n_b: math.inf)
    assert look(df).primary.detectable_mde_rel is None
    monkeypatch.setattr(analysis, "detectable_lift", lambda cfg, n_b: pytest.fail("no B leads: nothing to detect"))
    assert look(two_arms(np.random.default_rng(8), 50, 0, 0.2, 0.2)).primary.detectable_mde_rel is None


def test_srm_helper_on_known_counts():
    even = _srm_test(900, 100, 0.1)
    assert (even.n_A, even.n_B, even.chi2, even.p, even.observed_share_B) == (900, 100, 0.0, 1.0, 0.1)
    off = _srm_test(800, 200, 0.1)
    chi2 = 100**2 / 900 + 100**2 / 100
    assert off.chi2 == pytest.approx(chi2) and off.p == pytest.approx(chi2_dist.sf(chi2, 1), rel=1e-9) and off.p < 1e-20
    assert off.ci95[0] < 0.2 < off.ci95[1] and off.observed_share_B == 0.2
    empty = _srm_test(0, 0, 0.1)
    assert (empty.p, empty.chi2, empty.ci95) == (1.0, 0.0, (0.0, 1.0)) and math.isnan(empty.observed_share_B)


def _lane_a_srm_test(n_a: int, n_b: int, share_b: float) -> SrmResult:
    """Lane A's srm.srm_test as handed over (scipy), the oracle for the closed-form helper."""
    n = n_a + n_b
    chi2, p = chisquare([n_a, n_b], f_exp=[n * (1 - share_b), n * share_b])
    ci = binomtest(n_b, n).proportion_ci(confidence_level=CONFIDENCE, method="wilson")
    return SrmResult(n_A=n_a, n_B=n_b, configured_share_B=share_b, observed_share_B=n_b / n,
                     ci95=(float(ci.low), float(ci.high)), chi2=float(chi2), p=float(p))


@pytest.mark.parametrize("share_b", [0.01, 0.1, 0.5, 0.9])
def test_srm_helper_equals_the_scipy_version(share_b):
    rng = np.random.default_rng(int(share_b * 100))
    counts = [(0, 7), (7, 0), (1, 0), (0, 1), (3, 3)]
    for n in (10, 100, 5000, 200_000):
        for skew in (0.5, 0.9, 1.0, 1.1, 2.0):
            b = int(min(n, rng.binomial(n, min(1.0, share_b * skew))))
            counts.append((n - b, b))
    for n_a, n_b in counts:
        mine, theirs = _srm_test(n_a, n_b, share_b), _lane_a_srm_test(n_a, n_b, share_b)
        assert mine.chi2 == pytest.approx(theirs.chi2, rel=1e-9, abs=1e-12)
        assert mine.p == pytest.approx(theirs.p, rel=1e-9, abs=1e-300)
        assert mine.observed_share_B == theirs.observed_share_B
        assert mine.ci95 == pytest.approx(theirs.ci95, abs=1e-12)


def test_a_dropped_b_exposure_shows_in_the_srm_not_in_the_analysis_set():
    df = two_arms(np.random.default_rng(9), 9000, 700, 0.2, 0.2)  # 10% configured, 7.2% observed
    assert look(df).srm_assigned.p < MEETING.srm_p_threshold


# --- degenerate data: never raises, NaN propagates ------------------------------------------

def _assert_plain(model) -> None:
    """Every float/int in the dumped result is a plain Python one, not a numpy scalar."""
    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (tuple, list)):
            for x in v:
                walk(x)
        else:
            assert not isinstance(v, np.generic), f"numpy scalar {type(v)}: {v!r}"
            if isinstance(v, (int, float)):
                assert type(v) in (int, float, bool), type(v)
    walk(model.model_dump())


def test_empty_arm_gives_nan_without_raising():
    df = two_arms(np.random.default_rng(10), 200, 0, 0.2, 0.2)  # no B leads at all
    r = look(df)
    p, s = r.primary, r.secondary
    assert (r.n_A, r.n_B, r.info_fraction) == (200, 0, 0.0)
    assert math.isnan(p.rate_B) and math.isnan(p.lift_abs) and math.isnan(p.lift_rel)
    assert math.isnan(p.z) and math.isnan(p.se_pooled) and math.isnan(p.se_unpooled)
    assert all(math.isnan(x) for x in p.ci95) and p.detectable_mde_rel is None
    assert math.isnan(s.mean_B) and math.isnan(s.delta) and math.isnan(s.se) and math.isnan(s.rci[0])
    assert r.srm_assigned.n_B == 0
    _assert_plain(r)
    only_b = look(two_arms(np.random.default_rng(10), 0, 50, 0.2, 0.2))
    assert math.isnan(only_b.primary.z) and only_b.n_A == 0


def test_no_rows_at_all():
    empty = outcomes([], [], [])
    r = look(empty)
    assert (r.n_A, r.n_B) == (0, 0) and math.isnan(r.primary.z) and math.isnan(r.secondary.se)
    assert r.srm_assigned.p == 1.0 and r.srm_assigned.n_A == 0
    _assert_plain(r)


def test_no_mature_leads_but_assigned_and_triggered_ones():
    df = outcomes(["A", "A", "B", "A", "B"], [1, 0, 1, 0, 0], np.ones(5), mature=[False] * 5,
                  triggered=[True, True, True, False, False])
    r = look(df)
    assert (r.n_A, r.n_B) == (0, 0) and math.isnan(r.primary.z)
    assert (r.srm_assigned.n_A, r.srm_assigned.n_B) == (3, 2)
    assert (r.srm_triggered.n_A, r.srm_triggered.n_B) == (2, 1)


@pytest.mark.parametrize("value", [0, 1])
def test_no_variance_gives_z_zero_not_nan(value):
    n = 40
    r = look(outcomes(["A"] * 30 + ["B"] * 10, [value] * 40, np.ones(n))).primary
    assert r.se_pooled == 0.0 and r.se_unpooled == 0.0
    assert r.z == 0.0 and r.lift_abs == 0.0 and r.ci95 == (0.0, 0.0)
    _assert_plain(look(outcomes(["A"] * 30 + ["B"] * 10, [value] * 40, np.ones(n))))


def test_zero_baseline_gives_nan_relative_quantities():
    zeros = np.zeros(20)
    r = look(outcomes(["A"] * 10 + ["B"] * 10, [0] * 10 + [1, 1, 0, 0, 0, 0, 0, 0, 0, 0], zeros), BUYER)
    assert r.primary.rate_A == 0.0 and r.primary.lift_abs == pytest.approx(0.2) and math.isnan(r.primary.lift_rel)
    assert math.isfinite(r.primary.z) and r.primary.z > 0
    s = r.secondary  # abrupt-disconnect rate is 0 in both arms
    assert s.mean_A == 0.0 and math.isnan(s.rel_change) and s.delta == 0.0 and s.se == 0.0
    c = look(outcomes(["A"] * 10 + ["B"] * 10, [0, 1] * 10, zeros))  # continuous mean_A == 0
    assert math.isnan(c.secondary.rel_change) and math.isnan(c.secondary.delta) and math.isnan(c.secondary.se)
    assert math.isfinite(c.primary.z)


def test_a_single_lead_arm_has_no_continuous_variance():
    r = look(outcomes(["A", "B", "B", "B"], [1, 0, 1, 1], [5.0, 3.0, 4.0, 5.0]))
    assert math.isnan(r.secondary.se) and math.isfinite(r.secondary.delta)
    assert math.isfinite(r.primary.z)


@settings(max_examples=150)
@given(rows=st.lists(st.tuples(st.sampled_from("AB"), st.booleans(), st.booleans(), st.integers(0, 1),
                               st.one_of(st.none(), st.floats(0, 1e4))), max_size=40),
       cfg=st.sampled_from([MEETING, BUYER]), better=st.sampled_from(["increase", "decrease"]),
       k=st.integers(1, 3))
def test_arbitrary_small_frames_never_raise_and_always_validate(rows, cfg, better, k):
    variant, mature, extra, primary, secondary = (list(c) for c in zip(*rows)) if rows else ([], [], [], [], [])
    sec = [np.nan if v is None else v for v in secondary]
    if cfg is BUYER:  # binary guardrail: 0/1 values
        sec = [np.nan if np.isnan(v) else float(v > 5000) for v in sec]
    r = look(outcomes(variant, primary, sec, mature, extra), with_primary(cfg, better=better), k=k)
    _assert_plain(r)
    LookResult.model_validate(r.model_dump())
    assert r.n_A == sum(v == "A" and m for v, m in zip(variant, mature))
    if r.n_A and r.n_B:
        assert math.isfinite(r.primary.z)  # either a real z or the no-variance 0.0


# --- shape, validation, speed ---------------------------------------------------------------

def test_result_is_a_valid_lookresult_with_plain_floats():
    df = two_arms(np.random.default_rng(11), 1800, 200, 0.2, 0.25)
    r = analyze_look(df, MEETING, 2, TS, BOUNDS, True)
    assert (r.experiment_id, r.look, r.ts_sim, r.is_final) == ("vani-meeting-v1", 2, TS, True)
    assert LookResult.model_validate_json(r.model_dump_json()) == r
    _assert_plain(r)
    assert type(r.n_A) is int and type(r.n_B) is int


def test_look_must_index_the_boundaries():
    df = two_arms(np.random.default_rng(12), 100, 20, 0.2, 0.2)
    for bad in (0, -1, len(BOUNDS.win) + 1):
        with pytest.raises(ValueError, match="look"):
            look(df, k=bad)
    look(df, k=len(BOUNDS.win))


def test_missing_outcome_columns_raise():
    df = two_arms(np.random.default_rng(12), 100, 20, 0.2, 0.2)
    for col in LeadOutcome.model_fields:
        with pytest.raises(ValueError, match=col):
            look(df.drop(columns=col))


def test_a_mature_lead_without_a_primary_outcome_is_a_caller_bug():
    df = two_arms(np.random.default_rng(12), 100, 20, 0.2, 0.2)
    df["primary"] = pd.array([pd.NA] + [1] * 119, dtype="Int64")
    with pytest.raises(ValueError, match="primary"):
        look(df)


def test_the_input_frame_is_not_modified():
    df = two_arms(np.random.default_rng(13), 300, 60, 0.2, 0.2)
    before = df.copy(deep=True)
    look(df)
    pd.testing.assert_frame_equal(df, before)


def _cpu(fn, n: int, repeats: int = 5) -> float:
    """Best-of-`repeats` mean CPU seconds per call (CPU time, so a busy machine does not count)."""
    best = math.inf
    for _ in range(repeats):
        t = time.process_time()
        for _ in range(n):
            fn()
        best = min(best, (time.process_time() - t) / n)
    return best


def test_20k_rows_are_fast_enough_for_the_validation_harness(monkeypatch):
    n = 20_000
    rng = np.random.default_rng(14)
    df = outcomes(np.r_[["A"] * 18_000, ["B"] * 2_000], rng.random(n) < 0.2, rng.gamma(2, 60, n),
                  mature=rng.random(n) < 0.9)  # some leads not mature (and untriggered), as in a real look
    full = _cpu(lambda: look(df), 30)
    monkeypatch.setattr(analysis, "detectable_lift", lambda cfg, n_b: 0.1)
    own = _cpu(lambda: look(df), 100)  # this module's share: array work, SRM and model building
    print(f"\nanalyze_look on {n:,} rows: {full * 1e3:.2f} ms with power.detectable_lift, {own * 1e3:.2f} ms without")
    assert own < 5e-3, f"{own * 1e3:.1f} ms per call, excluding detectable_lift"  # ~1.5 ms idle; headroom for a busy machine
    assert full < 25e-3, f"{full * 1e3:.1f} ms per call"
