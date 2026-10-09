"""State machine, live allocation, approval flow and decision log (PLAN.md §2.4, §2.8, §2.9).

Every transition writes its log rows and the new state in one transaction (store.record), then
appends the same rows to the JSONL mirror. A state change's reason pairs the rule that fired
with what the move is worth per week at full rollout.

CLI: python -m abtest.decision <experiment_id> --actor NAME [--reject]   (make approve EXP= ACTOR=)
"""

import argparse
import math
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from abtest import contracts, power, store
from abtest.contracts import (DECISIONS_JSONL, LIVE_ALLOCATION, TRANSITIONS, Boundaries, Decision,
                               ExperimentConfig, ExperimentState, FrozenConfig, GuardrailLog,
                               LookResult, PrimaryLog, SecondaryStats, SrmLog, State, Verdict)

JSONL: Path | None = DECISIONS_JSONL  # the table's mirror; None turns it off (throwaway sweep runs)
DAY = timedelta(days=1)
_NO_LOOK = dict(look=None, info_fraction=None, n_A=None, n_B=None, primary=None, guardrail=None, srm=None)


def weekly_impact(lift_abs: float, n_matured: int, elapsed_days: float,
                  cfg: ExperimentConfig) -> float:
    """Extra primary successes per week if B took 100% of traffic: n_matured (both arms, so all
    traffic) arrived over elapsed_days - maturation_days days, floored at one day."""
    return lift_abs * n_matured / max(elapsed_days - cfg.maturation_days, 1) * 7


def _state(conn: sqlite3.Connection, experiment_id: str) -> ExperimentState:
    state = store.get_state(conn, experiment_id)
    if state is None:
        raise KeyError(f"experiment {experiment_id} is not in the store")
    return state


def _row(state: ExperimentState, ts_sim: datetime | None, kind: str, decision: str, reason: str,
         actor: str = "engine", **fields) -> Decision:
    return Decision(ts_wall=datetime.now(), ts_sim=ts_sim, experiment_id=state.experiment_id,
                    config_hash=state.config_hash, prompt_hash_A=state.prompt_hash["A"],
                    prompt_hash_B=state.prompt_hash["B"], kind=kind, decision=decision,
                    reason=reason, actor=actor, **fields)


def _commit(conn: sqlite3.Connection, state: ExperimentState, rows: list[Decision]) -> None:
    store.record(conn, state, rows)  # table first: a failed write never reaches the mirror
    if JSONL is not None:
        JSONL.parent.mkdir(parents=True, exist_ok=True)
        with JSONL.open("a", encoding="utf-8") as f:
            f.writelines(d.model_dump_json() + "\n" for d in rows)


def _label(cfg: ExperimentConfig) -> str:
    return " / ".join(cfg.primary.dispositions)


def _num(x: float, fmt: str) -> str:
    """Formatted with a true minus sign, as rules.py writes its reasons."""
    return format(x, fmt).replace("-", "−")


def _delta(d: float, s: SecondaryStats) -> str:
    return _num(d, "+.1%") if s.delta_type == "relative" else f"{_num(d * 100, '+.1f')} pp"


def _business(to: State, look: LookResult, bounds: Boundaries, cfg: ExperimentConfig,
              elapsed_days: float) -> str:
    """What a move out of RUNNING is worth per week at full rollout. The rules' reason already
    states the action and the evidence, so this adds only the business consequence."""
    p, s, label = look.primary, look.secondary, _label(cfg)

    def week(lift: float) -> str:
        return _num(weekly_impact(lift, look.n_A + look.n_B, elapsed_days, cfg), "+,.0f")

    w = week(p.lift_abs)
    if to == State.PROMOTED:
        return (f"Business impact: ≈ {w} {label} a week at full traffic "
                f"({_num(p.lift_abs * 1000, '+.0f')} per 1,000 triggered leads).")
    if to == State.PENDING_APPROVAL:
        return (f"Business impact: approving B is worth ≈ {w} {label} a week; B keeps its "
                f"{cfg.allocation.B:.0%} share until someone approves or rejects.")
    if to == State.STOPPED_HARM and p.z <= -bounds.harm[look.look - 1]:  # rule 2, primary side
        return f"Business impact: at full rollout B would have cost ≈ {w.lstrip('−+')} {label} a week."
    if to == State.STOPPED_HARM:  # rule 2, guardrail side
        return (f"Business impact: at full rollout B would move {s.name} by {_delta(s.delta, s)} for "
                f"≈ {w} {label} a week; the guardrail does not allow that trade.")
    if to == State.BLOCKED_GUARDRAIL:
        return (f"Business impact: B's ≈ {w} {label} a week comes with {s.name} {_delta(s.delta, s)}, "
                f"which the guardrail does not allow.")
    if to == State.INCONCLUSIVE:
        lo, hi = (week(x) for x in p.ci95)
        return (f"Business impact: B's effect is between {lo} and {hi} {label} a week (95% CI), "
                f"too small to call with this sample.")
    return "Business impact: no estimate; the split is broken, so fix routing and rerun."  # HALTED_SRM


def start(conn: sqlite3.Connection, frozen: FrozenConfig, ts_sim: datetime) -> ExperimentState:
    cfg = frozen.config
    state = _state(conn, cfg.id)
    if state.state != State.DRAFT:
        raise ValueError(f"{cfg.id} is {state.state}; only a DRAFT experiment can start")
    if state.config_hash != frozen.config_hash:
        raise ValueError(f"{cfg.id}: config_hash {frozen.config_hash} does not match the stored "
                         f"{state.config_hash}; the config changed after it was created")
    n_b_max = power.n_max(cfg).n_b_max
    out = state.model_copy(update={"state": State.RUNNING, "allocation": cfg.allocation, "n_b_max": n_b_max,
                                   "started_ts_sim": ts_sim, "updated_ts_sim": ts_sim})
    mde = cfg.primary.mde_rel if cfg.primary.better == "increase" else -cfg.primary.mde_rel
    reason = (f"Experiment started: B gets {cfg.allocation.B:.0%} of traffic; the test ends by "
              f"n_B,max = {math.ceil(n_b_max):,} matured triggered B leads ({cfg.power:.0%} power for a "
              f"{mde:+.0%} relative lift in {_label(cfg)}).")
    _commit(conn, out, [_row(out, ts_sim, "state_change", State.RUNNING.value, reason,
                             from_state=State.DRAFT, to_state=State.RUNNING, **_NO_LOOK)])
    return out


def apply_verdict(conn: sqlite3.Connection, state: ExperimentState, look: LookResult,
                  bounds: Boundaries, verdict: Verdict) -> ExperimentState:
    k, new = look.look, verdict.decision
    if state.state != State.RUNNING:
        raise ValueError(f"{state.experiment_id} is {state.state}; looks apply only while RUNNING")
    if look.experiment_id != state.experiment_id:
        raise ValueError(f"look is for {look.experiment_id}, not {state.experiment_id}")
    if k != state.looks_done + 1:
        raise ValueError(f"{state.experiment_id}: expected look {state.looks_done + 1}, got look {k}")
    if len(bounds.win) < k:
        raise ValueError(f"boundaries cover {len(bounds.win)} looks, but this is look {k}")
    if new != "CONTINUE" and new not in TRANSITIONS[State.RUNNING]:
        raise ValueError(f"a look cannot move RUNNING to {new}")
    if look.is_final and new == "CONTINUE":
        raise ValueError(f"look {k} is final; the verdict must end the experiment, not CONTINUE")

    cfg = store.get_config(conn, state.experiment_id)
    to = State.RUNNING if new == "CONTINUE" else new
    reason = verdict.reason
    if to != State.RUNNING:
        reason += " " + _business(to, look, bounds, cfg, (look.ts_sim - state.started_ts_sim) / DAY)
    a, t = look.srm_assigned, look.srm_triggered
    fields = dict(
        look=k, info_fraction=look.info_fraction, n_A=look.n_A, n_B=look.n_B,
        primary=PrimaryLog(**look.primary.model_dump(include=set(PrimaryLog.model_fields)),
                           win_boundary=bounds.win[k - 1], harm_boundary=-bounds.harm[k - 1]),
        guardrail=GuardrailLog(**look.secondary.model_dump(include=set(GuardrailLog.model_fields))),
        srm=SrmLog(configured_share_B=a.configured_share_B, observed_share_B=a.observed_share_B, p=a.p,
                   observed_share_B_triggered=t.observed_share_B, p_triggered=t.p),
    )
    rows = [_row(state, look.ts_sim, "look", str(new), reason, **fields)]
    if to != State.RUNNING:
        rows.append(_row(state, look.ts_sim, "state_change", to.value, reason,
                         from_state=State.RUNNING, to_state=to, **fields))
    out = state.model_copy(update={"state": to, "looks_done": k, "info_fractions": bounds.info_fractions,
                                   "updated_ts_sim": look.ts_sim,
                                   "allocation": LIVE_ALLOCATION[to] or cfg.allocation})
    _commit(conn, out, rows)
    return out


def approve(conn: sqlite3.Connection, experiment_id: str, actor: str, approved: bool) -> ExperimentState:
    state = _state(conn, experiment_id)
    if state.state != State.PENDING_APPROVAL:
        raise ValueError(f"{experiment_id} is {state.state}; only PENDING_APPROVAL can be approved or rejected")
    if not actor.strip():
        raise ValueError("an approval needs the approver's name (actor)")
    to = State.PROMOTED if approved else State.REJECTED
    if approved:
        cfg = store.get_config(conn, experiment_id)
        d = next(d for d in reversed(store.read_decisions(conn, experiment_id)) if d.kind == "look")
        w = weekly_impact(d.primary.lift_abs, d.n_A + d.n_B, (d.ts_sim - state.started_ts_sim) / DAY, cfg)
        reason = f"Approved by {actor}: B now gets 100% of traffic, worth ≈ {_num(w, '+,.0f')} {_label(cfg)} a week."
    else:
        reason = f"Rejected by {actor}: all traffic back to A."
    out = state.model_copy(update={"state": to, "allocation": LIVE_ALLOCATION[to]})
    _commit(conn, out, [_row(state, state.updated_ts_sim, "state_change", to.value, reason, actor,
                             from_state=State.PENDING_APPROVAL, to_state=to, **_NO_LOOK)])
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m abtest.decision",
                                 description="Approve (or --reject) an experiment waiting in PENDING_APPROVAL.")
    ap.add_argument("experiment_id")
    ap.add_argument("--actor", required=True, help="who decides; recorded on the decision log row")
    ap.add_argument("--reject", action="store_true")
    args = ap.parse_args(argv)
    conn = store.connect(contracts.DB_PATH)
    try:
        state = approve(conn, args.experiment_id, args.actor, not args.reject)
    except (KeyError, ValueError) as e:
        print(f"error: {e.args[0]}", file=sys.stderr)
        sys.exit(1)
    print(f"{state.experiment_id}: {State.PENDING_APPROVAL} -> {state.state} by {args.actor}")
    print(store.read_decisions(conn, state.experiment_id)[-1].reason)


if __name__ == "__main__":
    main()
