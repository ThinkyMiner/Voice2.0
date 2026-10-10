"""The loop's live A/B on simulated traffic through the real engine (PLAN_LOOP.md §L9.3-4, contract §L7).

`run_live_ab` is runner._run (PLAN.md §2.5-2.8, Phase 4) copied step for step, with three loop
additions and no base file touched:

  * the buyer template is rebuilt per generation (variant A = the production prompt, B = the
    candidate's patch on it, the B share capped by `caps.max_share_b`, promotion auto) and run on a
    synthetic buyer dataset (loop/synthdata.py), because the real call logs are not on every machine;
  * at every look each answered call gets a simulated `rubric_risk` (loop/sim_scores.py), averaged
    per lead, and guard.rubric_guardrail / guard.combine put the rubric between rules.evaluate_look
    and decision.apply_verdict. When the rubric decided, apply_verdict gets the LookResult with the
    rubric stats as its `secondary`, so the decision row and its business sentence describe the
    guardrail that fired;
  * both guardrails and both verdicts of every look are logged in loop_guard_log (store_loop).

The injected lift and risk shift are simulated ground truth: they go to the simulator and to
sim_scores only, are reported in LiveAbResult, and the engine never reads them. The template
guardrail's own true change (disconnects) is the one the lift implies by itself, with no direct
effect on top (see _secondary_change for why it is not 0). Everything this produces is simulated
traffic; nothing here says anything about real business impact. Unlike runner._run it never writes
reports/sim_truth_*.md (that directory is tracked); the sim_truth row in the store is written as usual.
"""

import math
import sqlite3
from datetime import timedelta
from pathlib import Path

import pandas as pd
import yaml

from abtest import config, decision, simulator, store
from abtest.contracts import (ROOT, Boundaries, ExperimentConfig, LookResult, Scenario, SecondaryStats,
                              State, Verdict)
from abtest.loop import guard, sim_scores, synthdata
from abtest.loop.contracts import LOOP_DATA_DIR, Candidate, LiveAbResult, LoopConfig


def _engine():
    """The stats chain, behind one seam as in runner._engine."""
    from abtest import analysis, boundaries, metrics, rules
    return metrics, analysis, boundaries, rules


def _ref(path: Path) -> str:
    """Scenario paths are relative to ROOT; a path outside it (a test's temp dir) stays absolute."""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _synthetic_dir(cfg: LoopConfig) -> Path:
    """LOOP_DATA_DIR/synthetic, written once and reused while its seed and generator version match."""
    out = LOOP_DATA_DIR / "synthetic"
    if not synthdata.is_current(out, cfg.sim.seed):
        synthdata.write_synthetic(out, cfg.sim.seed)
    return out


def _secondary_change(pool, exp_cfg: ExperimentConfig, lift: float) -> float:
    """The template guardrail's TOTAL true change for an injected primary lift: only what the lift
    itself implies, no direct effect on top (secondary_change is the total, simulator.simulate_day).

    Not 0: a total of 0 asks the simulator to hold disconnects flat while a win converts failed
    leads, which adds disconnects to B by hand and leaves the template guardrail unable to clear at
    n_B,max; a negative lift cannot be injected at all with 0 (its disconnects must rise, and the
    simulator has no removal path). The tiny epsilon keeps the direct rise at 0 + 1e-9 instead of
    -1e-17 after rounding."""
    if lift == 0:
        return 0.0
    return float(simulator._from_primary(pool, exp_cfg, lift, simulator._q(pool, lift))) + 1e-9


def _sim_config(cfg: LoopConfig, generation: int, injected_lift: float, secondary_change: float) -> str:
    """Write the generation's SimConfig (the base sim with the injected true effect) and return its
    path as a Scenario reference."""
    raw = yaml.safe_load((ROOT / cfg.sim.base_sim).read_text(encoding="utf-8"))
    raw["true_effect"] = {"primary_rel_lift": injected_lift, "secondary_change": secondary_change}
    path = LOOP_DATA_DIR / "sims" / f"{cfg.loop.id}-g{generation}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ("# Simulated ground truth for one loop generation (PLAN_LOOP §L9.4): read only by "
              "loop/live_ab.py,\n# never by the engine path.\n")
    path.write_text(header + yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return _ref(path)


def _variants(base_prompt_path: str, candidate: Candidate) -> dict:
    patch = candidate.patch.model_dump(mode="json")
    b = {"patch": patch} if patch["mode"] == "full" else {"base": base_prompt_path, "patch": patch}
    return {"A": {"base": base_prompt_path}, "B": b}


def _json_safe(x):
    """Strict JSON: NaN and +/-inf as the strings the base decision log uses (contracts._LogRow)."""
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return "NaN" if math.isnan(x) else ("Infinity" if x > 0 else "-Infinity")
    if isinstance(x, dict):
        return {k: _json_safe(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_json_safe(v) for v in x]
    return x


def _risk_by_lead(calls: pd.DataFrame, outcomes: pd.DataFrame, cfg: LoopConfig, shift: float,
                  seed: int) -> dict[str, float]:
    """Mean simulated rubric_risk per lead over its answered calls so far."""
    variant_by_lead = dict(zip(outcomes["lead_id"], outcomes["variant"], strict=True))
    scores = sim_scores.score_sim_calls(calls, variant_by_lead, shift, cfg.sim.risk_base,
                                        cfg.sim.risk_noise_sd, seed)
    owner = calls.set_index("call_id")["lead_id"]
    per_lead = scores["rubric_risk"].groupby(scores["call_id"].map(owner).to_numpy()).mean()
    return per_lead.to_dict()


def _num(x: float | None) -> float | None:
    return None if x is None or math.isnan(x) else float(x)


def _pair(t: tuple[float, float]) -> tuple[float, float] | None:
    return None if any(math.isnan(v) for v in t) else (float(t[0]), float(t[1]))


def _look_line(day: int, look: LookResult, bounds: Boundaries, base: Verdict, final: Verdict,
               rubric: SecondaryStats, rubric_decided: bool) -> str:
    p = look.primary
    return (f"day {day:<2}  look {look.look}  t={look.info_fraction:.2f}  n_A={look.n_A:,} n_B={look.n_B:,}  "
            f"lift {p.lift_rel:+.1%} (z={p.z:+.2f}, win {bounds.win[-1]:.2f})  rubric_risk "
            f"{rubric.delta * 100:+.2f} pp  engine {base.decision} -> {final.decision}"
            f"{' by the rubric guardrail' if rubric_decided else ''}: {final.reason}")


def run_live_ab(conn: sqlite3.Connection, cfg: LoopConfig, base_prompt_path: str, candidate: Candidate,
                generation: int, injected_lift: float, injected_risk_shift: float, seed: int,
                quiet: bool = True) -> LiveAbResult:
    """One simulated live A/B of `candidate` against the prompt at `base_prompt_path`.

    Reruns of the same (loop id, generation) replace the previous run in `conn`, as a scenario
    rerun does."""
    from abtest.loop import store_loop  # another module's tables; imported here, as the loop's seam to it

    metrics, analysis, boundaries, rules = _engine()
    data_dir = _synthetic_dir(cfg)
    share = cfg.caps.max_share_b
    overrides = {"variants": _variants(base_prompt_path, candidate), "promotion": "auto",
                 "allocation": {"A": 1 - share, "B": share}}
    name = f"{cfg.loop.id}-g{generation}"
    # The pool does not depend on the true effect, so build it first (from a no-effect SimConfig)
    # and size the guardrail's total change from it.
    probe = Scenario(name=name, experiment=cfg.template, sim=_sim_config(cfg, generation, 0.0, 0.0),
                     seed=seed, overrides=overrides)
    exp_cfg: ExperimentConfig = config.scenario_experiment(probe)
    pool = simulator.build_pool(exp_cfg, config.load_sim(probe.sim), data_dir=data_dir)
    scenario = probe.model_copy(update={"sim": _sim_config(
        cfg, generation, injected_lift, _secondary_change(pool, exp_cfg, injected_lift))})
    sim = config.load_sim(scenario.sim)  # ground truth: handed to the simulator only
    margin = cfg.guardrail.rubric_risk_margin

    # --- runner._run, step for step ---------------------------------------------------------
    store.reset_experiment(conn, exp_cfg.id)
    with conn:  # the guard log belongs to this run too: a rerun that ends sooner must not keep old looks
        conn.execute("DELETE FROM loop_guard_log WHERE experiment_id = ?", (exp_cfg.id,))
    frozen = config.freeze(exp_cfg)
    store.create_experiment(conn, frozen, scenario.name, scenario.seed)
    state = decision.start(conn, frozen, pool.start)

    planned = boundaries.planned_info_fractions(exp_cfg)
    next_tau = planned[0]
    truths = []
    last_look: LookResult | None = None
    last_rubric = None
    decided_by = "engine"
    for day in range(exp_cfg.window.days):
        out = simulator.simulate_day(pool, exp_cfg, sim, state, day, scenario.seed)
        store.insert_df(conn, "exposures", out.exposures)
        store.insert_df(conn, "calls", out.calls)
        truths.append(out.truth)  # truth report only; the engine never sees it

        look_ts = pool.start + timedelta(days=day + 1)
        exposures = store.read_df(conn, "SELECT * FROM exposures WHERE experiment_id = ?", (exp_cfg.id,),
                                  parse_dates=["ts"])
        exposures = exposures[exposures.ts >= state.started_ts_sim]  # DRAFT-period exposures are out
        # The simulator writes each lead's later calls up front; the engine never sees the future.
        calls = store.read_df(conn, "SELECT * FROM calls WHERE experiment_id = ? AND ts <= ?",
                              (exp_cfg.id, look_ts.isoformat(timespec="microseconds")),
                              parse_dates=["ts"]).astype({"answered": bool})
        outcomes = metrics.lead_outcomes(calls, exposures, exp_cfg, look_ts)
        t = int((outcomes.variant.eq("B") & outcomes.mature).sum()) / state.n_b_max

        is_final = t >= 1 or day == exp_cfg.window.days - 1
        if is_final or t >= next_tau:
            fractions = state.info_fractions + (_look_fraction(t, state.info_fractions),)
            bounds = boundaries.compute_boundaries(fractions, exp_cfg)
            look = analysis.analyze_look(outcomes, exp_cfg, state.looks_done + 1, look_ts, bounds, is_final)
            verdict = rules.evaluate_look(look, bounds, exp_cfg)

            # --- the loop's addition: the rubric guardrail between the rules and the decision ---
            risk = _risk_by_lead(calls, outcomes, cfg, injected_risk_shift, seed)
            rubric = guard.rubric_guardrail(outcomes, risk, look.look, bounds, margin)
            final, rubric_decided = guard.combine(verdict, rubric, look, bounds, exp_cfg)
            store_loop.put_guard_log(conn, exp_cfg.id, look.look, _json_safe({
                "base_verdict": verdict.model_dump(mode="json"),
                "final_verdict": final.model_dump(mode="json"),
                "rubric_decided": rubric_decided,
                "template_guardrail": look.secondary.model_dump(mode="json"),
                "rubric_guardrail": rubric.model_dump(mode="json")}))
            applied = look.model_copy(update={"secondary": rubric}) if rubric_decided else look
            state = decision.apply_verdict(conn, state, applied, bounds, final)
            last_look, last_rubric, decided_by = look, rubric, "rubric_guardrail" if rubric_decided else "engine"
            next_tau = min((p for p in planned if p > t), default=math.inf)  # skip points already passed
            if not quiet:
                print(_look_line(day + 1, look, bounds, verdict, final, rubric, rubric_decided), flush=True)
        else:
            state = state.model_copy(update={"updated_ts_sim": look_ts})
            store.put_state(conn, state)
        if state.state != State.RUNNING or is_final:
            break

    truth = simulator.truth_summary(pd.concat(truths, ignore_index=True), pool, exp_cfg, sim,
                                    scenario.name, scenario.seed)
    with conn:
        conn.execute("INSERT INTO sim_truth VALUES (?, ?, ?)", (exp_cfg.id, scenario.name, truth.model_dump_json()))

    reason = store.read_decisions(conn, exp_cfg.id)[-1].reason
    p = last_look.primary if last_look is not None else None
    return LiveAbResult(
        experiment_id=exp_cfg.id, terminal=state.state.value, looks=state.looks_done,
        lift_rel=_num(p.lift_rel) if p else None, lift_ci95=_pair(p.ci95) if p else None,
        rubric_delta=_num(last_rubric.delta) if last_rubric else None,
        rubric_rci=_pair(last_rubric.rci) if last_rubric else None,
        decided_by=decided_by, injected_lift=injected_lift, injected_risk_shift=injected_risk_shift,
        reason=reason)


def _look_fraction(t: float, done: tuple[float, ...]) -> float:
    """runner._look_fraction: the fraction a look is computed at. Boundaries need strictly increasing
    fractions > 0, so a forced final look with no newly matured B leads moves a hair past the last
    one: that spends no new alpha, so its boundary is effectively uncrossable."""
    return max(t, done[-1] * (1 + 1e-9) if done else 1e-9)
