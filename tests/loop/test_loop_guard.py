"""loop/guard.py: the rubric_risk guardrail statistics and how it combines with the engine's verdict
(PLAN_LOOP §L9.3)."""

import itertools
import math
from datetime import datetime

import numpy as np
import pandas as pd
import pytest
from scipy.stats import norm

from abtest import config
from abtest.contracts import (Boundaries, LookResult, PrimaryStats, SecondaryStats, SrmResult, State,
                              Verdict)
from abtest.loop import guard

CFG = config.load_experiment("configs/experiments/vani-buyer-qty-asks-v1.yaml")
MARGIN = 0.05
WIN = (4.27, 3.38, 2.88, 2.57, 2.34, 2.19, 2.10)
BOUNDS = Boundaries(info_fractions=tuple(np.linspace(0.2, 1.0, 7)), win=WIN, harm=(2.1,) * 7)


def _outcomes(a, b, mature_a=None, mature_b=None):
    """metrics.lead_outcomes-shaped frame (only the columns guard reads) and its risk mapping."""
    rows, risk = [], {}
    for arm, vals, mature in (("A", a, mature_a), ("B", b, mature_b)):
        for i, v in enumerate(vals):
            lead = f"{arm}{i}"
            rows.append((lead, arm, True if mature is None else mature[i]))
            if v is not None:
                risk[lead] = v
    return pd.DataFrame(rows, columns=["lead_id", "variant", "mature"]), risk


def _look(k=3, is_final=False):
    srm = SrmResult(n_A=900, n_B=100, configured_share_B=0.1, observed_share_B=0.1, ci95=(0.08, 0.12),
                    chi2=0.0, p=1.0)
    primary = PrimaryStats(rate_A=0.66, rate_B=0.75, lift_abs=0.09, lift_rel=0.136, ci95=(0.01, 0.17),
                           se_pooled=0.04, se_unpooled=0.04, z=3.0, detectable_mde_rel=0.1)
    sec = SecondaryStats(name="abrupt_disconnect", type="binary", role="guardrail", mean_A=0.19, mean_B=0.19,
                         rel_change=0.0, delta=0.0, delta_type="absolute", se=0.02, ci95=(-0.04, 0.04),
                         rci=(-0.05, 0.05), margin=0.02)
    return LookResult(experiment_id="x", look=k, ts_sim=datetime(2026, 10, 12), is_final=is_final,
                      info_fraction=0.5, n_A=900, n_B=100, primary=primary, secondary=sec,
                      srm_assigned=srm, srm_triggered=srm)


def _rubric(lo, hi, look=3):
    mid = (lo + hi) / 2 if not (math.isnan(lo) or math.isnan(hi)) else math.nan
    return SecondaryStats(name="rubric_risk", type="continuous", role="guardrail", mean_A=0.2, mean_B=0.2 + mid,
                          rel_change=mid / 0.2, delta=mid, delta_type="absolute", se=0.01, ci95=(lo, hi),
                          rci=(lo, hi), margin=MARGIN)


def _verdict(decision, rule=3):
    return Verdict(decision=decision, rule=rule, reason=f"engine says {decision}")


# --- rubric_guardrail ---------------------------------------------------------------------------

def test_matches_a_hand_computation():
    a, b = [0.1, 0.2, 0.3, 0.4], [0.3, 0.5, 0.7]
    outcomes, risk = _outcomes(a, b)
    s = guard.rubric_guardrail(outcomes, risk, 3, BOUNDS, MARGIN)
    # hand: mean_A .25, var_A 0.05/3; mean_B .5, var_B 0.08/2; se = sqrt(var_A/4 + var_B/3)
    se = math.sqrt((0.05 / 3) / 4 + (0.08 / 2) / 3)
    z95 = norm.ppf(0.975)
    assert (s.name, s.type, s.role, s.delta_type, s.margin) == ("rubric_risk", "continuous", "guardrail",
                                                                 "absolute", MARGIN)
    assert s.mean_A == pytest.approx(0.25) and s.mean_B == pytest.approx(0.5)
    assert s.delta == pytest.approx(0.25)
    assert s.rel_change == pytest.approx(1.0)
    assert s.se == pytest.approx(se, rel=1e-12)
    assert s.ci95 == pytest.approx((0.25 - z95 * se, 0.25 + z95 * se))
    assert s.rci == pytest.approx((0.25 - WIN[2] * se, 0.25 + WIN[2] * se))      # look 3 -> win[2]


def test_rci_uses_the_boundary_of_the_look():
    outcomes, risk = _outcomes([0.1, 0.2, 0.3], [0.2, 0.3, 0.5])
    s1 = guard.rubric_guardrail(outcomes, risk, 1, BOUNDS, MARGIN)
    s7 = guard.rubric_guardrail(outcomes, risk, 7, BOUNDS, MARGIN)
    assert s1.se == s7.se and s1.delta == s7.delta
    assert (s1.rci[1] - s1.rci[0]) / 2 == pytest.approx(WIN[0] * s1.se)
    assert (s7.rci[1] - s7.rci[0]) / 2 == pytest.approx(WIN[6] * s7.se)
    with pytest.raises(ValueError, match="look must be"):
        guard.rubric_guardrail(outcomes, risk, 8, BOUNDS, MARGIN)


def test_only_mature_leads_with_a_risk_value_count():
    outcomes, risk = _outcomes([0.1, 0.3, 0.9, None, 0.2], [0.2, 0.4, 0.99, None],
                               mature_a=[True, True, False, True, True], mature_b=[True, True, False, True])
    s = guard.rubric_guardrail(outcomes, risk, 2, BOUNDS, MARGIN)       # the 0.9 / 0.99 leads are immature
    assert s.mean_A == pytest.approx(0.2) and s.mean_B == pytest.approx(0.3)
    extra = guard.rubric_guardrail(outcomes, {**risk, "ghost": 0.7}, 2, BOUNDS, MARGIN)   # not in outcomes
    assert extra.mean_A == s.mean_A


def test_empty_arm_is_nan_everywhere():
    outcomes, risk = _outcomes([0.1, 0.2, 0.3], [])
    s = guard.rubric_guardrail(outcomes, risk, 3, BOUNDS, MARGIN)
    for x in (s.mean_B, s.rel_change, s.delta, s.se, *s.ci95, *s.rci):
        assert math.isnan(x)
    assert s.mean_A == pytest.approx(0.2)
    # an arm whose leads all lack a risk value is empty too
    outcomes, risk = _outcomes([0.1, 0.2], [None, None])
    assert math.isnan(guard.rubric_guardrail(outcomes, risk, 1, BOUNDS, MARGIN).delta)


def test_a_single_lead_arm_has_no_interval():
    outcomes, risk = _outcomes([0.1, 0.2, 0.3], [0.4])
    s = guard.rubric_guardrail(outcomes, risk, 3, BOUNDS, MARGIN)
    assert s.delta == pytest.approx(0.2)
    assert math.isnan(s.se) and all(math.isnan(x) for x in (*s.ci95, *s.rci))


def test_infinite_boundary_gives_an_unbounded_rci():
    bounds = Boundaries(info_fractions=(0.2, 0.6, 1.0), win=(math.inf, 2.5, 2.0), harm=(2.1, 2.1, 2.1))
    outcomes, risk = _outcomes([0.1, 0.2, 0.3], [0.2, 0.3, 0.4])
    s = guard.rubric_guardrail(outcomes, risk, 1, bounds, MARGIN)
    assert s.rci == (-math.inf, math.inf)
    assert all(math.isfinite(x) for x in s.ci95)


def test_no_variance_gives_a_point_interval():
    outcomes, risk = _outcomes([0.2, 0.2, 0.2], [0.3, 0.3])
    s = guard.rubric_guardrail(outcomes, risk, 3, BOUNDS, MARGIN)
    assert s.se == pytest.approx(0.0, abs=1e-12) and s.rci == pytest.approx((0.1, 0.1))


# --- combine ------------------------------------------------------------------------------------

HARM = (0.07, 0.12)          # RCI entirely above the 0.05 margin
CLEARED = (-0.02, 0.03)      # RCI entirely below it
UNCLEAR = (0.00, 0.08)       # straddles it
NAN = (math.nan, math.nan)


@pytest.mark.parametrize("decision", [State.HALTED_SRM, State.STOPPED_HARM])
@pytest.mark.parametrize("final", [False, True])
def test_srm_and_engine_harm_are_kept_even_with_rubric_harm(decision, final):
    base = _verdict(decision, rule=1 if decision == State.HALTED_SRM else 2)
    out, decided = guard.combine(base, _rubric(*HARM), _look(is_final=final), BOUNDS, CFG)
    assert out is base and decided is False


@pytest.mark.parametrize("decision", ["CONTINUE", State.PROMOTED, State.PENDING_APPROVAL])
def test_rubric_harm_stops_the_run_and_names_the_guardrail(decision):
    out, decided = guard.combine(_verdict(decision), _rubric(*HARM), _look(), BOUNDS, CFG)
    assert out.decision == State.STOPPED_HARM and out.rule == 2 and decided is True
    assert "rubric_risk" in out.reason and "look 3" in out.reason
    assert "+7.00 pp" in out.reason and "+12.00 pp" in out.reason and "+5.00 pp margin" in out.reason
    assert ("engine's own verdict" in out.reason) == (decision != "CONTINUE")


@pytest.mark.parametrize("decision", [State.INCONCLUSIVE, State.BLOCKED_GUARDRAIL])
def test_rubric_harm_outranks_an_inconclusive_or_blocked_end(decision):
    out, decided = guard.combine(_verdict(decision, rule=5), _rubric(*HARM), _look(is_final=True), BOUNDS, CFG)
    assert out.decision == State.STOPPED_HARM and decided is True


@pytest.mark.parametrize("decision", [State.PROMOTED, State.PENDING_APPROVAL])
def test_a_win_with_the_rubric_cleared_is_unchanged(decision):
    base = _verdict(decision)
    for final in (False, True):
        out, decided = guard.combine(base, _rubric(*CLEARED), _look(is_final=final), BOUNDS, CFG)
        assert out is base and decided is False


@pytest.mark.parametrize("decision", [State.PROMOTED, State.PENDING_APPROVAL])
@pytest.mark.parametrize("rci", [UNCLEAR, NAN, (0.01, 0.05)], ids=["straddles", "nan", "touches margin"])
def test_a_win_waits_for_the_rubric_guardrail(decision, rci):
    out, decided = guard.combine(_verdict(decision), _rubric(*rci), _look(), BOUNDS, CFG)
    assert out.decision == "CONTINUE" and out.rule == 4 and decided is False
    assert "waits for the rubric guardrail" in out.reason and "rubric_risk" in out.reason
    assert "+5.00 pp margin" in out.reason


@pytest.mark.parametrize("decision", [State.PROMOTED, State.PENDING_APPROVAL])
@pytest.mark.parametrize("rci", [UNCLEAR, NAN], ids=["straddles", "nan"])
def test_an_unclear_win_is_blocked_at_the_final_look(decision, rci):
    out, decided = guard.combine(_verdict(decision), _rubric(*rci), _look(k=7, is_final=True), BOUNDS, CFG)
    assert out.decision == State.BLOCKED_GUARDRAIL and out.rule == 4 and decided is True
    assert "rubric_risk" in out.reason and "final look" in out.reason


@pytest.mark.parametrize("decision", ["CONTINUE", State.INCONCLUSIVE, State.BLOCKED_GUARDRAIL])
@pytest.mark.parametrize("rci", [CLEARED, UNCLEAR, NAN])
def test_everything_else_is_unchanged(decision, rci):
    base = _verdict(decision, rule=0)
    out, decided = guard.combine(base, _rubric(*rci), _look(is_final=decision != "CONTINUE"), BOUNDS, CFG)
    assert out is base and decided is False


def test_never_continue_at_a_final_look():
    """Every verdict the engine can return at a final look, against every rubric outcome."""
    engine_final = [State.HALTED_SRM, State.STOPPED_HARM, State.PROMOTED, State.PENDING_APPROVAL,
                    State.BLOCKED_GUARDRAIL, State.INCONCLUSIVE]
    for decision, rci in itertools.product(engine_final, [HARM, CLEARED, UNCLEAR, NAN, (-math.inf, math.inf)]):
        out, _ = guard.combine(_verdict(decision), _rubric(*rci), _look(k=7, is_final=True), BOUNDS, CFG)
        assert out.decision != "CONTINUE", (decision, rci)
    with pytest.raises(ValueError, match="final"):
        guard.combine(_verdict("CONTINUE", rule=0), _rubric(*CLEARED), _look(k=7, is_final=True), BOUNDS, CFG)


def test_combine_needs_a_margin():
    bad = _rubric(*CLEARED).model_copy(update={"margin": None})
    with pytest.raises(ValueError, match="margin"):
        guard.combine(_verdict("CONTINUE", 0), bad, _look(), BOUNDS, CFG)


def test_reasons_read_cleanly_with_nan_and_negative_numbers():
    out, _ = guard.combine(_verdict(State.PROMOTED), _rubric(math.nan, math.nan), _look(), BOUNDS, CFG)
    assert "n/a" in out.reason and "nan" not in out.reason.lower().replace("n/a", "")
    out, _ = guard.combine(_verdict(State.PROMOTED), _rubric(-0.03, 0.09), _look(), BOUNDS, CFG)
    assert "−3.00 pp" in out.reason                                    # true minus sign, as rules.py writes
