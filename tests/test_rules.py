"""rules.evaluate_look (PLAN.md §2.8): every rule, precedence, both guardrail directions, NaN, purity.

LookResult and Boundaries are built by hand, so nothing here depends on analysis.py or
boundaries.py: the rules are tested as a pure function of the numbers they are given.
"""

import builtins
import io
import math
import pathlib
import socket
from datetime import datetime

import pytest
import yaml

from abtest.contracts import (
    CONFIGS_DIR,
    Boundaries,
    ExperimentConfig,
    LookResult,
    PrimaryStats,
    SecondaryMetric,
    SecondaryStats,
    SrmResult,
    State,
)
from abtest.rules import evaluate_look, primary_crossings

NAN = float("nan")
INF = math.inf
MINUS = "−"

# Hand-picked 5-look boundaries (the OBF win values of PLAN §2.7; harm values as in the §2.9 example).
BOUNDS = Boundaries(info_fractions=(0.2, 0.4, 0.6, 0.8, 1.0),
                    win=(4.88, 3.36, 2.68, 2.29, 2.03),
                    harm=(2.50, 2.21, 2.10, 2.05, 2.00))


def _load(name: str) -> ExperimentConfig:
    return ExperimentConfig.model_validate(
        yaml.safe_load((CONFIGS_DIR / "experiments" / f"{name}.yaml").read_text()))


def _cfg(promotion="auto", worse_if="increase", **secondary) -> ExperimentConfig:
    """Headline config: continuous guardrail, worse if duration increases, margin_rel 0.10."""
    base = _load("vani-meeting-v1")
    sec = base.secondary.model_copy(update={"worse_if": worse_if, **secondary})
    return base.model_copy(update={"promotion": promotion, "secondary": sec})


def _srm(p=0.5, share=0.10) -> SrmResult:
    return SrmResult(n_A=900, n_B=100, configured_share_B=0.10, observed_share_B=share,
                     ci95=(share - 0.02, share + 0.02), chi2=0.0, p=p)


def _sec(rci=(-0.02, 0.05), role="guardrail", type="continuous", margin=0.10,
         c_k=BOUNDS.win[1]) -> SecondaryStats:
    """Stats whose RCI is delta +/- c_k se. ci95 = delta +/- 1.96 se is narrower, as at a real
    look, so a rule reading ci95 instead of the RCI fails the margin-edge cases below."""
    absolute = type == "binary"
    lo, hi = rci
    delta = (lo + hi) / 2 if not any(map(math.isnan, rci)) else 0.0
    se = (hi - lo) / (2 * c_k)
    return SecondaryStats(
        name="answered_duration_s", type=type, role=role, mean_A=100.0, mean_B=100.0 * (1 + delta),
        rel_change=delta, delta=delta, delta_type="absolute" if absolute else "relative",
        se=se, ci95=(delta - 1.96 * se, delta + 1.96 * se), rci=rci,
        margin=None if role == "goal" else margin)


def _look(k=2, z=0.5, rci=(-0.02, 0.05), is_final=False, srm_a=0.5, srm_t=0.5,
          mde=0.15, sec=None) -> LookResult:
    return LookResult(
        experiment_id="exp", look=k, ts_sim=datetime(2026, 7, 1), is_final=is_final,
        info_fraction=0.4, n_A=900, n_B=100,
        primary=PrimaryStats(rate_A=0.20, rate_B=0.21, lift_abs=0.01, lift_rel=0.05,
                             ci95=(-0.012, 0.032), se_pooled=0.04, se_unpooled=0.04, z=z,
                             detectable_mde_rel=mde),
        secondary=sec if sec is not None else _sec(rci, c_k=BOUNDS.win[k - 1]),
        srm_assigned=_srm(srm_a), srm_triggered=_srm(srm_t))


def _decide(look, cfg=None, bounds=BOUNDS):
    v = evaluate_look(look, bounds, cfg or _cfg())
    return v.decision, v.rule


# --- rule 1: SRM -----------------------------------------------------------------------------

@pytest.mark.parametrize("srm_a,srm_t", [(0.0004, 0.5), (0.5, 0.0004), (1e-9, 1e-9)])
def test_rule1_srm_on_assigned_or_triggered(srm_a, srm_t):
    assert _decide(_look(srm_a=srm_a, srm_t=srm_t)) == (State.HALTED_SRM, 1)


def test_srm_threshold_is_strict():
    thr = _cfg().srm_p_threshold
    assert _decide(_look(srm_a=thr, srm_t=thr)) == ("CONTINUE", 0)
    assert _decide(_look(srm_a=thr * 0.99)) == (State.HALTED_SRM, 1)


def test_srm_reason_names_both_p_values_and_the_threshold():
    reason = evaluate_look(_look(srm_a=0.0004, srm_t=0.31), BOUNDS, _cfg()).reason
    assert "0.0004" in reason and "0.31" in reason and "0.001" in reason
    assert "assigned" in reason and "untrusted" in reason


def test_srm_beats_harm_and_win():
    assert _decide(_look(z=-9.0, srm_a=1e-6)) == (State.HALTED_SRM, 1)
    assert _decide(_look(z=9.0, srm_t=1e-6)) == (State.HALTED_SRM, 1)
    assert _decide(_look(z=0.0, rci=(0.5, 0.9), srm_a=1e-6)) == (State.HALTED_SRM, 1)


# --- rule 2: harm ----------------------------------------------------------------------------

def test_rule2_primary_harm_and_the_plan_example_sentence():
    v = evaluate_look(_look(k=2, z=-3.10), BOUNDS, _cfg())
    assert (v.decision, v.rule) == (State.STOPPED_HARM, 2)
    assert v.reason == (f"Harm boundary crossed at look 2 (z = {MINUS}3.10 ≤ {MINUS}2.21); "
                        "traffic returned to A.")


def test_harm_boundary_equality_crosses_and_just_inside_does_not():
    h = BOUNDS.harm[1]
    assert _decide(_look(k=2, z=-h)) == (State.STOPPED_HARM, 2)
    assert _decide(_look(k=2, z=-h + 1e-9)) == ("CONTINUE", 0)


def test_win_boundary_equality_crosses_and_just_inside_does_not():
    w = BOUNDS.win[1]
    assert _decide(_look(k=2, z=w)) == (State.PROMOTED, 3)
    assert _decide(_look(k=2, z=w - 1e-9)) == ("CONTINUE", 0)


def test_boundaries_are_indexed_by_look():
    # z = 2.5 wins at look 4 (2.29) and 5 (2.03) but not at look 3 (2.68).
    assert _decide(_look(k=3, z=2.5))[0] == "CONTINUE"
    assert _decide(_look(k=4, z=2.5))[0] == State.PROMOTED
    assert _decide(_look(k=5, z=2.5, is_final=True))[0] == State.PROMOTED
    # z = -2.15 is harm at look 3 (2.10) but not at look 2 (2.21).
    assert _decide(_look(k=2, z=-2.15))[0] == "CONTINUE"
    assert _decide(_look(k=3, z=-2.15))[0] == State.STOPPED_HARM


@pytest.mark.parametrize("worse_if,rci,harm", [
    ("increase", (0.11, 0.20), True),
    ("increase", (0.10, 0.20), False),    # lower bound exactly at the margin: not "entirely beyond"
    ("increase", (-0.30, -0.20), False),  # B much better (shorter): never harm
    ("decrease", (-0.30, -0.11), True),
    ("decrease", (-0.30, -0.10), False),
    ("decrease", (0.20, 0.30), False),
])
def test_guardrail_harm_both_directions(worse_if, rci, harm):
    got = _decide(_look(z=0.3, rci=rci), _cfg(worse_if=worse_if))
    assert got == ((State.STOPPED_HARM, 2) if harm else ("CONTINUE", 0))


def test_guardrail_harm_reason_has_rci_margin_and_units():
    reason = evaluate_look(_look(rci=(0.112, 0.198)), BOUNDS, _cfg()).reason
    assert "Guardrail harm at look 2" in reason
    assert "[+11.2%, +19.8%]" in reason and "+10.0%" in reason and "above" in reason
    assert reason.endswith("traffic returned to A.")
    dec = evaluate_look(_look(rci=(-0.30, -0.112)), BOUNDS, _cfg(worse_if="decrease")).reason
    assert f"[{MINUS}30.0%, {MINUS}11.2%]" in dec and f"{MINUS}10.0%" in dec and "below" in dec


def test_primary_and_guardrail_harm_together_are_both_reported():
    v = evaluate_look(_look(z=-3.0, rci=(0.2, 0.3)), BOUNDS, _cfg())
    assert (v.decision, v.rule) == (State.STOPPED_HARM, 2)
    assert f"z = {MINUS}3.00" in v.reason and "guardrail answered_duration_s" in v.reason


def test_harm_beats_win_via_the_guardrail():
    # The primary crossed the win boundary, but the guardrail RCI is entirely beyond the margin.
    assert _decide(_look(k=3, z=5.0, rci=(0.15, 0.30))) == (State.STOPPED_HARM, 2)


def test_binary_guardrail_uses_absolute_units_in_the_reason():
    cfg = _load("vani-buyer-qty-asks-v1")
    sec = _sec(rci=(0.031, 0.062), type="binary", margin=cfg.secondary.margin)
    v = evaluate_look(_look(sec=sec), BOUNDS, cfg)
    assert (v.decision, v.rule) == (State.STOPPED_HARM, 2)
    assert "[+3.10 pp, +6.20 pp]" in v.reason and "+2.00 pp" in v.reason


def test_guardrail_reads_the_rci_not_the_plain_ci():
    # Look 2, c_k = 3.36: the RCI is much wider than ci95, and only the RCI may decide.
    straddles = _sec(rci=(-0.06, 0.12))
    assert straddles.ci95 == pytest.approx((-0.0225, 0.0825), abs=1e-4)  # inside the +10% margin
    assert _decide(_look(k=2, z=0.5, sec=straddles)) == ("CONTINUE", 0)
    assert _decide(_look(k=2, z=3.5, sec=straddles)) == ("CONTINUE", 4)  # a win, but not cleared
    beyond = _sec(rci=(0.06, 0.30))
    assert beyond.ci95 == pytest.approx((0.11, 0.25), abs=1e-4)  # entirely beyond the margin
    assert _decide(_look(k=2, z=0.5, sec=beyond)) == ("CONTINUE", 0)  # the RCI straddles: no harm


# --- rule 3: promotion -----------------------------------------------------------------------

@pytest.mark.parametrize("worse_if,rci,cleared", [
    ("increase", (-0.05, 0.099), True),
    ("increase", (-0.05, 0.10), False),   # upper bound exactly at the margin: not "within"
    ("increase", (-0.05, 0.20), False),
    ("increase", (-0.60, -0.30), True),   # far better than A
    ("decrease", (-0.099, 0.20), True),
    ("decrease", (-0.10, 0.20), False),
    ("decrease", (-0.30, 0.05), False),
])
def test_guardrail_clearing_both_directions(worse_if, rci, cleared):
    cfg = _cfg(worse_if=worse_if)
    got = _decide(_look(k=4, z=3.0, rci=rci), cfg)
    assert got == ((State.PROMOTED, 3) if cleared else ("CONTINUE", 4))


def test_auto_promotes_approval_waits():
    assert _decide(_look(k=4, z=3.0), _cfg("auto")) == (State.PROMOTED, 3)
    assert _decide(_look(k=4, z=3.0), _cfg("approval")) == (State.PENDING_APPROVAL, 3)


def test_promotion_reason_has_z_boundary_rci_and_margin():
    auto = evaluate_look(_look(k=4, z=3.014, rci=(-0.021, 0.063)), BOUNDS, _cfg("auto")).reason
    assert "look 4" in auto and "z = 3.01" in auto and "2.29" in auto
    assert f"[{MINUS}2.1%, +6.3%]" in auto and "+10.0%" in auto and "cleared" in auto
    assert "promoted" in auto and "approval" not in auto
    appr = evaluate_look(_look(k=4, z=3.014), BOUNDS, _cfg("approval")).reason
    assert "awaits approval" in appr and "promoted" not in appr


def test_win_with_a_goal_secondary_ignores_the_goal():
    goal = SecondaryMetric(name="answered_duration_s", type="continuous", role="goal", better="decrease")
    cfg = _cfg().model_copy(update={"secondary": goal})
    # A goal whose RCI sits miles beyond any margin neither blocks the win nor stops the test.
    sec = _sec(rci=(0.5, 0.9), role="goal")
    v = evaluate_look(_look(k=4, z=3.0, sec=sec), BOUNDS, cfg)
    assert (v.decision, v.rule) == (State.PROMOTED, 3)
    assert "goal" in v.reason and "does not gate" in v.reason


def test_goal_secondary_never_triggers_harm():
    sec = _sec(rci=(0.5, 0.9), role="goal")
    assert _decide(_look(z=0.2, sec=sec)) == ("CONTINUE", 0)           # config says guardrail
    assert _decide(_look(z=0.2, sec=sec, is_final=True, k=5)) == (State.INCONCLUSIVE, 5)


def test_a_guardrail_that_cannot_be_evaluated_is_a_loud_error():
    cfg = _cfg().model_copy(update={"secondary": SecondaryMetric(
        name="d", type="continuous", role="goal", better="increase")})
    with pytest.raises(ValueError, match="worse_if"):
        evaluate_look(_look(k=4, z=3.0), BOUNDS, cfg)  # stats say guardrail, config says goal


# --- rule 4: win but guardrail not cleared ---------------------------------------------------

def test_rule4_continue_before_the_final_look_block_at_the_final_look():
    not_cleared = (-0.02, 0.14)
    mid = evaluate_look(_look(k=4, z=3.0, rci=not_cleared), BOUNDS, _cfg())
    assert (mid.decision, mid.rule) == ("CONTINUE", 4)
    assert "not cleared" in mid.reason and "+14.0%" in mid.reason and "+10.0%" in mid.reason
    fin = evaluate_look(_look(k=5, z=2.5, rci=not_cleared, is_final=True), BOUNDS, _cfg())
    assert (fin.decision, fin.rule) == (State.BLOCKED_GUARDRAIL, 4)
    assert "final look" in fin.reason and "A stays" in fin.reason


# --- rule 5: inconclusive --------------------------------------------------------------------

def test_rule5_only_at_the_final_look():
    assert _decide(_look(k=4, z=1.0)) == ("CONTINUE", 0)
    assert _decide(_look(k=5, z=1.0, is_final=True)) == (State.INCONCLUSIVE, 5)


def test_rule5_says_no_boundary_crossed_at_this_look():
    # An earlier look may have crossed the win boundary with the guardrail not cleared (rule 4).
    reason = evaluate_look(_look(k=5, z=1.0, is_final=True), BOUNDS, _cfg()).reason
    assert reason.startswith("Final look 5 reached with no boundary crossed at this look: z = 1.00 "
                             f"is between the harm ({MINUS}2.00) and win (2.03) boundaries; ")


def test_inconclusive_reason_reports_the_lift_ci_and_the_detectable_mde():
    v = evaluate_look(_look(k=5, z=1.2, is_final=True, mde=0.1834), BOUNDS, _cfg())
    assert f"[{MINUS}1.20 pp" not in v.reason  # CI is written "A to B", not as a bracket pair
    assert f"{MINUS}1.20 pp to +3.20 pp" in v.reason   # ci95 (-0.012, 0.032) on lift_abs
    assert "+5.0%" in v.reason                          # observed relative lift
    assert "18.3%" in v.reason and "could have detected" in v.reason
    assert "A stays" in v.reason


def test_inconclusive_without_a_detectable_mde_still_reads_well():
    for mde in (None, NAN, INF):
        v = evaluate_look(_look(k=5, z=1.2, is_final=True, mde=mde), BOUNDS, _cfg())
        assert v.decision == State.INCONCLUSIVE and "not estimable" in v.reason


# --- continue --------------------------------------------------------------------------------

def test_continue_says_where_z_sits_between_the_boundaries():
    v = evaluate_look(_look(k=2, z=0.80), BOUNDS, _cfg())
    assert (v.decision, v.rule) == ("CONTINUE", 0)
    assert v.reason.startswith("Look 2 of 7:")
    assert f"z = 0.80" in v.reason and f"{MINUS}2.21" in v.reason and "3.36" in v.reason


def test_a_z_next_to_a_boundary_never_prints_equal_to_it():
    close = Boundaries(info_fractions=(0.5, 1.0), win=(3.0, 2.0451), harm=(2.2049, 2.0))
    mid = evaluate_look(_look(k=1, z=-2.2001), close, _cfg()).reason  # 2 decimals: -2.20 both
    assert f"z = {MINUS}2.200 is between the harm ({MINUS}2.205) and win (3.000)" in mid
    fin = evaluate_look(_look(k=2, z=2.0449, is_final=True), close, _cfg()).reason  # 3: 2.045 both
    assert f"z = 2.0449 is between the harm ({MINUS}2.0000) and win (2.0451)" in fin


def test_minus_zero_prints_as_zero():
    reason = evaluate_look(_look(k=2, z=-0.001), BOUNDS, _cfg()).reason
    assert "z = 0.00 is between" in reason and f"{MINUS}0.00" not in reason


# --- NaN and infinite boundaries -------------------------------------------------------------

def test_nan_z_never_crosses():
    assert _decide(_look(k=2, z=NAN)) == ("CONTINUE", 0)
    assert _decide(_look(k=5, z=NAN, is_final=True)) == (State.INCONCLUSIVE, 5)
    assert primary_crossings(_look(z=NAN), BOUNDS) == (False, False)
    nan_z = "z is not available, so no boundary was evaluated"
    assert nan_z in evaluate_look(_look(z=NAN), BOUNDS, _cfg()).reason
    assert nan_z in evaluate_look(_look(k=5, z=NAN, is_final=True), BOUNDS, _cfg()).reason


def test_nan_rci_neither_harms_nor_clears():
    # A win on a NaN guardrail is not promoted (not cleared), and a NaN guardrail never stops B.
    assert _decide(_look(k=4, z=3.0, rci=(NAN, NAN))) == ("CONTINUE", 4)
    assert _decide(_look(k=5, z=2.5, rci=(NAN, NAN), is_final=True)) == (State.BLOCKED_GUARDRAIL, 4)
    assert _decide(_look(k=4, z=0.0, rci=(NAN, NAN))) == ("CONTINUE", 0)
    for direction in ("increase", "decrease"):
        assert _decide(_look(z=0.0, rci=(NAN, NAN)), _cfg(worse_if=direction)) == ("CONTINUE", 0)


def test_one_nan_rci_bound_only_matters_for_the_side_that_is_checked():
    # worse_if=increase looks at the upper bound to clear and the lower bound to flag harm.
    assert _decide(_look(k=4, z=3.0, rci=(NAN, 0.05))) == (State.PROMOTED, 3)
    assert _decide(_look(k=4, z=3.0, rci=(-0.05, NAN))) == ("CONTINUE", 4)
    assert _decide(_look(z=0.0, rci=(NAN, 0.50))) == ("CONTINUE", 0)


def test_nan_z_with_a_harmful_guardrail_still_stops():
    assert _decide(_look(z=NAN, rci=(0.2, 0.3))) == (State.STOPPED_HARM, 2)


def test_infinite_boundaries_cannot_be_crossed():
    inf_bounds = Boundaries(info_fractions=(0.5, 1.0), win=(INF, 2.0), harm=(INF, 2.0))
    assert _decide(_look(k=1, z=INF), bounds=inf_bounds) == ("CONTINUE", 0)
    assert _decide(_look(k=1, z=-INF), bounds=inf_bounds) == ("CONTINUE", 0)
    assert _decide(_look(k=2, z=2.0, is_final=True), bounds=inf_bounds) == (State.PROMOTED, 3)


# --- input validation, precedence summary, purity --------------------------------------------

@pytest.mark.parametrize("k", [0, 6, -1])
def test_look_outside_the_boundaries_is_rejected(k):
    with pytest.raises(ValueError, match="look"):
        evaluate_look(_look(k=1).model_copy(update={"look": k}), BOUNDS, _cfg())


def test_mismatched_boundary_lengths_are_rejected():
    bad = Boundaries(info_fractions=(0.5, 1.0), win=(3.0, 2.0), harm=(2.0,))
    with pytest.raises(ValueError):
        evaluate_look(_look(k=1), bad, _cfg())


def test_bounds_may_be_longer_than_the_look():
    # The runner may pass boundaries for all planned looks while only look 1 is being evaluated.
    assert _decide(_look(k=1, z=0.0)) == ("CONTINUE", 0)


def test_precedence_table():
    cfg = _cfg()
    cases = [  # (look kwargs, expected) -- each row adds a lower-priority trigger to the row above
        (dict(z=-9, rci=(0.2, 0.3), srm_a=1e-9), State.HALTED_SRM),
        (dict(z=-9, rci=(0.2, 0.3)), State.STOPPED_HARM),
        (dict(z=9, rci=(0.2, 0.3)), State.STOPPED_HARM),
        (dict(z=9, rci=(0.01, 0.14)), "CONTINUE"),
        (dict(z=9, rci=(-0.01, 0.04)), State.PROMOTED),
    ]
    for kw, expected in cases:
        assert evaluate_look(_look(k=3, **kw), BOUNDS, cfg).decision == expected, kw


def test_pure_same_input_same_output():
    look, cfg = _look(k=2, z=-3.10), _cfg()
    first = evaluate_look(look, BOUNDS, cfg)
    assert all(evaluate_look(look, BOUNDS, cfg) == first for _ in range(3))


def test_pure_no_file_or_network_access(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("evaluate_look must not touch files or the network")

    look, cfg = _look(k=4, z=3.0), _cfg()
    expected = evaluate_look(look, BOUNDS, cfg)  # warm any lazy imports before sabotage
    monkeypatch.setattr(builtins, "open", boom)
    monkeypatch.setattr(io, "open", boom)
    for name in ("open", "read_text", "read_bytes", "write_text", "write_bytes"):
        monkeypatch.setattr(pathlib.Path, name, boom)
    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    for case in (look, _look(z=-9.0), _look(srm_a=1e-9), _look(k=5, z=0.0, is_final=True)):
        evaluate_look(case, BOUNDS, cfg)
    assert evaluate_look(look, BOUNDS, cfg) == expected
