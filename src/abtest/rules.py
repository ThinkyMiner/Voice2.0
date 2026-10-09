"""Decision rules (PLAN.md §2.8, reason sentences §2.9): one pure function, `evaluate_look`.

Inputs are a LookResult, the boundaries and the experiment config, nothing else: no I/O, no
clock, no randomness, and nothing from the simulator (§2.3 isolation test). Rules run in order
and the first match decides:

1. SRM on assigned or triggered leads        -> HALTED_SRM
2. z <= -harm, or the guardrail RCI beyond the margin -> STOPPED_HARM
3. z >= win and the guardrail cleared        -> PROMOTED (auto) / PENDING_APPROVAL (approval)
4. z >= win but the guardrail not cleared    -> CONTINUE, BLOCKED_GUARDRAIL at the final look
5. final look and nothing crossed            -> INCONCLUSIVE

NaN is "no crossing" throughout. Every comparison below is written so NaN makes it False (a
NaN z never crosses, a NaN RCI never proves harm and never clears the guardrail), and the NaN
tests in tests/test_rules.py pin that down. A boundary of +inf means "cannot cross".

Only a secondary with role == "guardrail" gates. A reported goal is never consulted: it can
neither trigger harm nor block a win (§2.3). The guardrail checks are small helpers over one
SecondaryStats so a later wave can apply them to a list of extra guardrails.
"""

import math

from abtest.contracts import (
    Boundaries,
    Direction,
    ExperimentConfig,
    LookResult,
    SecondaryStats,
    State,
    Verdict,
)

CONTINUE = "CONTINUE"
_MINUS = "−"  # PLAN.md §2.9 writes negatives with a true minus sign


# --- number formatting ----------------------------------------------------------------------

def _num(x: float, nd: int = 2, signed: bool = False) -> str:
    """Fixed-point with a true minus sign; NaN and inf are spelled out, never crash a reason."""
    if math.isnan(x):
        return "n/a"
    if math.isinf(x):
        return "+inf" if x > 0 else f"{_MINUS}inf"
    x = round(x, nd) + 0.0  # + 0.0 turns -0.0 into 0.0: never "−0.00"
    return f"{x:{'+' if signed else ''}.{nd}f}".replace("-", _MINUS)


def _pct(x: float, signed: bool = True) -> str:
    return _num(x * 100, 1, signed) + "%"


def _pp(x: float, signed: bool = True) -> str:
    return _num(x * 100, 2, signed) + " pp"


def _p_value(p: float) -> str:
    return "n/a" if math.isnan(p) else f"{p:.3g}"


def _between(z: float, h: float, w: float) -> str:
    """Where a z that crossed neither boundary sits. 3 decimals when z is within 0.005 of a
    boundary (more if still needed), so it never prints equal to the boundary it did not cross."""
    if math.isnan(z):
        return "z is not available, so no boundary was evaluated"
    nd = 3 if any(abs(z - b) < 0.005 for b in (-h, w)) else 2
    while nd < 6 and _num(z, nd) in (_num(-h, nd), _num(w, nd)):
        nd += 1
    return (f"z = {_num(z, nd)} is between the harm ({_num(-h, nd)}) and win ({_num(w, nd)}) "
            f"boundaries")


def _units(sec: SecondaryStats, x: float, signed: bool = True) -> str:
    """Relative deltas (continuous) as percent, absolute deltas (binary) as percentage points."""
    return _pct(x, signed) if sec.delta_type == "relative" else _pp(x, signed)


def _rci_text(sec: SecondaryStats) -> str:
    lo, hi = sec.rci
    return f"[{_units(sec, lo)}, {_units(sec, hi)}]"


# --- guardrail helpers (one SecondaryStats each) ---------------------------------------------

def _gates(sec: SecondaryStats, worse_if: Direction | None) -> bool:
    """True iff this secondary is a guardrail that can gate. A goal never does."""
    if sec.role != "guardrail":
        return False
    if worse_if is None or sec.margin is None:  # a guardrail without a direction/margin is a bug
        raise ValueError(f"guardrail {sec.name!r} needs worse_if and a margin to be evaluated")
    return True


def _guardrail_harm(sec: SecondaryStats, worse_if: Direction | None) -> bool:
    """RCI entirely beyond the margin on the worse side. NaN bounds compare False: no harm."""
    if not _gates(sec, worse_if):
        return False
    lo, hi = sec.rci
    return lo > sec.margin if worse_if == "increase" else hi < -sec.margin


def _guardrail_cleared(sec: SecondaryStats, worse_if: Direction | None) -> bool:
    """RCI entirely within the margin on the worse side (non-inferiority). A goal counts as
    cleared. NaN bounds compare False: not cleared, so a win is never promoted on missing data."""
    if not _gates(sec, worse_if):
        return True
    lo, hi = sec.rci
    return hi < sec.margin if worse_if == "increase" else lo > -sec.margin


def _margin_text(sec: SecondaryStats, worse_if: Direction | None) -> str:
    sign = 1.0 if worse_if == "increase" else -1.0
    return _units(sec, sign * sec.margin)


def _harm_text(sec: SecondaryStats, worse_if: Direction | None) -> str:
    side = "above" if worse_if == "increase" else "below"
    return (f"{sec.name} RCI {_rci_text(sec)} lies entirely {side} the "
            f"{_margin_text(sec, worse_if)} margin")


def _cleared_text(sec: SecondaryStats, worse_if: Direction | None) -> str:
    if not _gates(sec, worse_if):
        return f"secondary {sec.name} is a reported goal and does not gate"
    side = "below" if worse_if == "increase" else "above"
    return (f"guardrail {sec.name} cleared (RCI {_rci_text(sec)} stays {side} the "
            f"{_margin_text(sec, worse_if)} margin)")


def _not_cleared_text(sec: SecondaryStats, worse_if: Direction | None) -> str:
    side = "below" if worse_if == "increase" else "above"
    return (f"guardrail {sec.name} is not cleared (RCI {_rci_text(sec)} is not entirely {side} "
            f"the {_margin_text(sec, worse_if)} margin)")


# --- primary crossings ----------------------------------------------------------------------

def primary_crossings(look: LookResult, bounds: Boundaries) -> tuple[bool, bool]:
    """(crossed win, crossed harm) for the primary z alone, at this look. Rules 2-4 use this, and
    validation uses it for the "primary-only crossing" rates, so there is one definition.
    A NaN z or an infinite boundary never crosses."""
    k = _check_look(look, bounds)
    z, w, h = look.primary.z, bounds.win[k - 1], bounds.harm[k - 1]
    return (math.isfinite(w) and z >= w), (math.isfinite(h) and z <= -h)


def _check_look(look: LookResult, bounds: Boundaries) -> int:
    k = look.look
    if len(bounds.win) != len(bounds.harm):
        raise ValueError(f"win has {len(bounds.win)} boundaries but harm has {len(bounds.harm)}")
    if not 1 <= k <= len(bounds.win):
        raise ValueError(f"look {k} outside the {len(bounds.win)} boundaries given")
    return k


# --- evaluate_look --------------------------------------------------------------------------

def evaluate_look(look: LookResult, bounds: Boundaries, cfg: ExperimentConfig) -> Verdict:
    k = _check_look(look, bounds)
    z, w, h = look.primary.z, bounds.win[k - 1], bounds.harm[k - 1]
    sec, worse_if = look.secondary, cfg.secondary.worse_if
    crossed_win, crossed_harm = primary_crossings(look, bounds)
    thr = cfg.srm_p_threshold

    # Rule 1: SRM. Both checks run; either one failing makes every number untrustworthy.
    p_a, p_t = look.srm_assigned.p, look.srm_triggered.p
    if p_a < thr or p_t < thr:
        failed = " and ".join(n for n, p in (("assigned", p_a), ("triggered", p_t)) if p < thr)
        return Verdict(decision=State.HALTED_SRM, rule=1, reason=(
            f"Sample ratio mismatch at look {k} ({failed}): assigned-lead p = {_p_value(p_a)}, "
            f"triggered-lead p = {_p_value(p_t)}, threshold {_p_value(thr)} (observed B share "
            f"{_pct(look.srm_assigned.observed_share_B, False)} assigned, "
            f"{_pct(look.srm_triggered.observed_share_B, False)} triggered, configured "
            f"{_pct(look.srm_assigned.configured_share_B, False)}); results are untrusted, "
            f"so the experiment is halted."))

    # Rule 2: harm, from the primary boundary or the guardrail RCI.
    guard_harm = _guardrail_harm(sec, worse_if)
    if crossed_harm or guard_harm:
        primary = f"(z = {_num(z)} ≤ {_MINUS}{_num(h)})"
        if crossed_harm and guard_harm:
            what = (f"Harm boundary crossed at look {k} {primary} and guardrail "
                    f"{_harm_text(sec, worse_if)}")
        elif crossed_harm:
            what = f"Harm boundary crossed at look {k} {primary}"
        else:
            what = f"Guardrail harm at look {k}: {_harm_text(sec, worse_if)}"
        return Verdict(decision=State.STOPPED_HARM, rule=2, reason=f"{what}; traffic returned to A.")

    guard_cleared = _guardrail_cleared(sec, worse_if)

    # Rule 3: win with the guardrail cleared.
    if crossed_win and guard_cleared:
        where = (f"Win boundary crossed at look {k} (z = {_num(z)} ≥ {_num(w)}) and "
                 f"{_cleared_text(sec, worse_if)}")
        if cfg.promotion == "auto":
            return Verdict(decision=State.PROMOTED, rule=3,
                           reason=f"{where}; B promoted to 100% of traffic.")
        return Verdict(decision=State.PENDING_APPROVAL, rule=3,
                       reason=f"{where}; B awaits approval before promotion.")

    # Rule 4: win without a cleared guardrail. Keep collecting data; block at the final look.
    if crossed_win:
        base = (f"Win boundary crossed at look {k} (z = {_num(z)} ≥ {_num(w)}) but "
                f"{_not_cleared_text(sec, worse_if)}")
        if look.is_final:
            return Verdict(decision=State.BLOCKED_GUARDRAIL, rule=4,
                           reason=f"{base} at the final look; B is blocked and A stays.")
        return Verdict(decision=CONTINUE, rule=4,
                       reason=f"{base}; continuing until the guardrail clears or the final look.")

    # Rule 5: the final look with nothing crossed.
    if look.is_final:
        pr = look.primary
        mde = ("n/a (not estimable)" if pr.detectable_mde_rel is None
               or not math.isfinite(pr.detectable_mde_rel) else _pct(pr.detectable_mde_rel, False))
        lo, hi = pr.ci95
        rel_ci = (f", {_pct(lo / pr.rate_A)} to {_pct(hi / pr.rate_A)} relative"
                  if pr.rate_A > 0 else "")
        # "at this look": a win may have crossed earlier with the guardrail not yet cleared.
        return Verdict(decision=State.INCONCLUSIVE, rule=5, reason=(
            f"Final look {k} reached with no boundary crossed at this look: {_between(z, h, w)}; "
            f"observed lift {_pct(pr.lift_rel)} relative ({_pp(pr.lift_abs)}; 95% CI {_pp(lo)} "
            f"to {_pp(hi)}{rel_ci}), and this sample could have detected a lift of about {mde} "
            f"relative; A stays."))

    # No rule matched: keep going.
    guard = (f"; guardrail {sec.name} RCI {_rci_text(sec)} has not crossed the "
             f"{_margin_text(sec, worse_if)} margin" if _gates(sec, worse_if) else "")
    return Verdict(decision=CONTINUE, rule=0, reason=(
        f"Look {k} of {cfg.window.looks}: {_between(z, h, w)}; SRM p-values pass (assigned "
        f"{_p_value(p_a)}, triggered {_p_value(p_t)}){guard}; continuing."))
