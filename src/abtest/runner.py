"""Drive a scenario through the sim clock: simulate a day, look when the information fraction is
due, apply the decision, stop at a decided state (PLAN.md §2.5 Clock, §2.7, §2.8, Phase 4).

CLI:
  python -m abtest.runner <scenario> [--speed S]                  (make simulate / make live)
  python -m abtest.runner --sweep [--seeds N] [--workers W] [names...]   (make scenarios)

Engine path: metrics/analysis/boundaries/rules/decision only ever see the exposures and calls
read back from the store, aggregates of them, and the ExperimentConfig. The SimConfig, the Pool
and the sim truth stay on the simulator side.
"""

import argparse
import json
import math
import os
import sqlite3
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from datetime import timedelta

import pandas as pd

from abtest import config, contracts, decision, simulator, store
from abtest.contracts import (CONFIGS_DIR, REPORTS_DIR, Boundaries, ExperimentState, LookResult, Pool,
                              Scenario, ScenarioRun, State, Verdict)

# PLAN.md §3 Phase 4 scenario table: (expected state, latest allowed stop look or None).
EXPECTED: dict[str, tuple[State, int | None]] = {
    "win": (State.PROMOTED, None), "harm": (State.STOPPED_HARM, 3),
    "inconclusive": (State.INCONCLUSIVE, None), "guardrail_block": (State.BLOCKED_GUARDRAIL, None),
    "srm_fault": (State.HALTED_SRM, None), "approval": (State.PENDING_APPROVAL, None),
    "buyer_disconnect_stop": (State.STOPPED_HARM, None),
}
GRID_EPS = 1e-9  # float slack when placing t on the 1/K look grid (3/7 * 7 is 2.9999999999999996)
LOOK_FIELDS = ("stop_look", "info_fraction", "lift_rel", "lift_rel_ci95", "guardrail_delta",
               "guardrail_rci", "weekly_impact")


def _engine():
    """The Phase 3 stats chain. Imported at first use until phase-3 merges (then top-level)."""
    from abtest import analysis, boundaries, metrics, rules
    return metrics, analysis, boundaries, rules


def _pct(x: float) -> str:
    return f"{x:+.1%}"


def _pp(x: float) -> str:
    return f"{x * 100:+.1f} pp"


def _look_line(day: int, look: LookResult, bounds: Boundaries, verdict: Verdict) -> str:
    p = look.primary
    return (f"day {day:<2}  look {look.look}  t={look.info_fraction:.2f}  n_A={look.n_A:,} n_B={look.n_B:,}  "
            f"lift {_pct(p.lift_rel)} [{_pct(p.ci95[0] / p.rate_A)}, {_pct(p.ci95[1] / p.rate_A)}]  "
            f"z={p.z:+.2f} (win {bounds.win[-1]:.2f} / harm {-bounds.harm[-1]:.2f})  "
            f"-> {verdict.decision}: {verdict.reason}")


def _run(scenario: Scenario, conn: sqlite3.Connection, speed: float | None = None,
         quiet: bool = False, pool: Pool | None = None) -> ExperimentState:
    """quiet = sweep mode: no printing, no truth report file. `pool` reuses a built pool."""
    metrics, analysis, boundaries, rules = _engine()
    cfg = config.scenario_experiment(scenario)
    sim = config.load_sim(scenario.sim)  # ground truth: handed to the simulator only
    pool = pool if pool is not None else simulator.build_pool(cfg, sim)

    store.reset_experiment(conn, cfg.id)
    frozen = config.freeze(cfg)
    store.create_experiment(conn, frozen, scenario.name, scenario.seed)
    state = decision.start(conn, frozen, pool.start)

    K = cfg.window.looks
    next_tau = max(1 / K, cfg.min_info_first_look)
    truths = []
    for day in range(cfg.window.days):
        out = simulator.simulate_day(pool, cfg, sim, state, day, scenario.seed)
        store.insert_df(conn, "exposures", out.exposures)
        store.insert_df(conn, "calls", out.calls)
        truths.append(out.truth)  # truth report only; the engine never sees it

        look_ts = pool.start + timedelta(days=day + 1)
        exposures = store.read_df(conn, "SELECT * FROM exposures WHERE experiment_id = ?", (cfg.id,),
                                  parse_dates=["ts"])
        exposures = exposures[exposures.ts >= state.started_ts_sim]  # DRAFT-period exposures are out
        # The simulator writes each lead's later calls up front; the engine never sees the future.
        # ISO strings with microseconds (store._iso) compare in time order.
        calls = store.read_df(conn, "SELECT * FROM calls WHERE experiment_id = ? AND ts <= ?",
                              (cfg.id, look_ts.isoformat(timespec="microseconds")),
                              parse_dates=["ts"]).astype({"answered": bool})
        outcomes = metrics.lead_outcomes(calls, exposures, cfg, look_ts)
        t = int((outcomes.variant.eq("B") & outcomes.mature).sum()) / state.n_b_max

        is_final = t >= 1 or day == cfg.window.days - 1
        if is_final or t >= next_tau:
            # Lan-DeMets at the observed fractions; capped at 1 so total alpha is never exceeded.
            bounds = boundaries.compute_boundaries(state.info_fractions + (min(t, 1.0),), cfg)
            look = analysis.analyze_look(outcomes, cfg, state.looks_done + 1, look_ts, bounds, is_final)
            verdict = rules.evaluate_look(look, bounds, cfg)
            state = decision.apply_verdict(conn, state, look, bounds, verdict)
            next_tau = (math.floor(t * K + GRID_EPS) + 1) / K  # next grid point strictly above t
            if not quiet:
                print(_look_line(day + 1, look, bounds, verdict), flush=True)
        else:
            state = state.model_copy(update={"updated_ts_sim": look_ts})
            store.put_state(conn, state)
        if speed:
            time.sleep(speed)
        if state.state != State.RUNNING or is_final:
            break

    truth = simulator.truth_summary(pd.concat(truths, ignore_index=True), pool, cfg, sim,
                                    scenario.name, scenario.seed)
    with conn:
        conn.execute("INSERT INTO sim_truth VALUES (?, ?, ?)", (cfg.id, scenario.name, truth.model_dump_json()))
    if not quiet:
        simulator.write_truth_report(truth)
    return state


def run_scenario(scenario: Scenario, conn: sqlite3.Connection,
                 speed: float | None = None) -> ExperimentState:
    """Rerunning a scenario replaces its previous run in the store."""
    return _run(scenario, conn, speed)


def summarize(conn: sqlite3.Connection, scenario: Scenario) -> ScenarioRun:
    cfg = config.scenario_experiment(scenario)
    state = store.get_state(conn, cfg.id)
    looks = [d for d in store.read_decisions(conn, cfg.id) if d.kind == "look"]
    stats = dict.fromkeys(LOOK_FIELDS)
    if looks:
        d = looks[-1]
        p, g = d.primary, d.guardrail
        elapsed = (d.ts_sim - state.started_ts_sim) / timedelta(days=1)
        stats.update(stop_look=d.look, info_fraction=d.info_fraction, lift_rel=p.lift_rel,
                     lift_rel_ci95=(p.ci95[0] / p.rate_A, p.ci95[1] / p.rate_A),
                     guardrail_delta=g.delta, guardrail_rci=g.rci,
                     weekly_impact=decision.weekly_impact(p.lift_abs, d.n_A + d.n_B, elapsed, cfg))
    expected, bound = EXPECTED[scenario.name]
    hit = state.state == expected and (bound is None or (bool(looks) and stats["stop_look"] <= bound))
    return ScenarioRun(scenario=scenario.name, seed=scenario.seed, experiment_id=cfg.id,
                       state=state.state, expected=expected, hit=hit,
                       sim_days=(state.updated_ts_sim - state.started_ts_sim).days, **stats)


# --- sweep --------------------------------------------------------------------------------

# build_pool reads the whole CSV (~1 s for bttc). ponytail: one pool (~50 MB for bttc) cached per
# process, since sweep jobs arrive scenario by scenario; key on what the pool depends on if that changes.
_POOLS: dict[tuple, Pool] = {}


def _no_jsonl() -> None:
    decision.JSONL = None


def _sweep_one(job: tuple[str, int]) -> ScenarioRun:
    name, seed = job
    sc = config.load_scenario(name).model_copy(update={"seed": seed})
    key = (sc.experiment, sc.sim, json.dumps(sc.overrides, sort_keys=True))
    if key not in _POOLS:
        _POOLS.clear()
        _POOLS[key] = simulator.build_pool(config.scenario_experiment(sc), config.load_sim(sc.sim))
    conn = store.connect(":memory:")
    _run(sc, conn, quiet=True, pool=_POOLS[key])
    return summarize(conn, sc)


def sweep(names: Sequence[str], n_seeds: int, workers: int | None = None) -> list[ScenarioRun]:
    jobs = [(name, seed) for name in names for seed in range(1, n_seeds + 1)]
    if workers == 1:
        saved = decision.JSONL
        try:
            _no_jsonl()
            runs = [_sweep_one(j) for j in jobs]
        finally:
            decision.JSONL = saved
    else:
        with ProcessPoolExecutor(workers or os.cpu_count(), initializer=_no_jsonl) as ex:
            runs = list(ex.map(_sweep_one, jobs))
    return sorted(runs, key=lambda r: (r.scenario, r.seed))


def _ci(x: float, ci: tuple[float, float], fmt) -> str:
    return f"{fmt(x)} ({fmt(ci[0])}, {fmt(ci[1])})"


def scenario_table(pinned: list[ScenarioRun], runs: list[ScenarioRun], n_seeds: int) -> str:
    """Markdown table: the pinned-seed run's statistics, plus the sweep's hit rate."""
    lines = ["| scenario | terminal state | stop look | lift estimate (95% CI) | guardrail Δ (RCI) "
             f"| ≈ Δ successes/week at 100% | hit rate over {n_seeds} seeds |",
             "|---|---|---|---|---|---:|---:|"]
    for r in pinned:
        fmt = _pp if config.scenario_experiment(config.load_scenario(r.scenario)).secondary.type == "binary" else _pct
        state = r.state.value if r.hit else f"**{r.state.value} (MISS: expected {r.expected.value})**"
        hits = sum(x.hit for x in runs if x.scenario == r.scenario)
        if r.stop_look is None:
            cols = ["-", "-", "-", "-"]
        else:
            cols = [f"{r.stop_look} (day {r.sim_days})", _ci(r.lift_rel, r.lift_rel_ci95, _pct),
                    _ci(r.guardrail_delta, r.guardrail_rci, fmt), f"{r.weekly_impact:+,.0f}"]
        lines.append(f"| {r.scenario} (seed {r.seed}) | {state} | " + " | ".join(cols) + f" | {hits}/{n_seeds} |")
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------------------

ARM_SQL = """
WITH lv AS (SELECT lead_id, MIN(variant) AS variant FROM exposures
            WHERE experiment_id = :e GROUP BY lead_id)
SELECT lv.variant AS arm, COUNT(DISTINCT lv.lead_id) AS leads,
       COUNT(DISTINCT CASE WHEN c.answered = 1 THEN lv.lead_id END) AS triggered,
       COUNT(c.call_id) AS calls, COALESCE(SUM(c.answered), 0) AS answered_calls
FROM lv LEFT JOIN calls c ON c.experiment_id = :e AND c.lead_id = lv.lead_id
GROUP BY lv.variant ORDER BY lv.variant
"""
ORPHAN_SQL = """
SELECT COUNT(*) AS calls, COUNT(DISTINCT lead_id) AS leads FROM calls
WHERE experiment_id = :e AND lead_id NOT IN (SELECT lead_id FROM exposures WHERE experiment_id = :e)
"""


def _simulate(name: str, speed: float | None) -> None:
    sc = config.load_scenario(name)
    cfg = config.scenario_experiment(sc)
    conn = store.connect(contracts.DB_PATH)
    t0 = time.perf_counter()
    state = run_scenario(sc, conn, speed)
    secs = time.perf_counter() - t0
    arms = store.read_df(conn, ARM_SQL, {"e": state.experiment_id})
    orphan = store.read_df(conn, ORPHAN_SQL, {"e": state.experiment_id}).iloc[0]
    run = summarize(conn, sc)
    last = store.read_decisions(conn, state.experiment_id)[-1]
    print(f"\nSimulated {state.experiment_id} (scenario {sc.name}, seed {sc.seed}): "
          f"{state.started_ts_sim:%Y-%m-%d} to {state.updated_ts_sim:%Y-%m-%d}, {secs:.1f}s")
    print(arms.to_string(index=False))
    print(f"calls with no logged exposure (fault injection): {orphan.calls:,} calls, {orphan.leads:,} leads")
    print(f"final state {state.state.value} after {run.sim_days} sim days: {last.reason}")
    if run.weekly_impact is not None:
        print(f"engine estimate: ≈ {run.weekly_impact:+,.0f} '{' / '.join(cfg.primary.dispositions)}' "
              "a week if B served 100% of traffic")
    print(f"ground truth -> {REPORTS_DIR / f'sim_truth_{sc.name}.md'} (the engine never reads it)")


def _sweep_cli(names: list[str], n_seeds: int, workers: int | None) -> None:
    names = names or sorted(p.stem for p in (CONFIGS_DIR / "scenarios").glob("*.yaml"))
    t0 = time.perf_counter()
    runs = sweep(names, n_seeds, workers)
    print(f"swept {len(names)} scenarios x {n_seeds} seeds in {time.perf_counter() - t0:.0f}s; "
          "rerunning each pinned demo seed into the main store")
    conn = store.connect(contracts.DB_PATH)
    pinned = []
    for name in names:
        sc = config.load_scenario(name)
        print(f"\n== {name} (pinned seed {sc.seed})")
        run_scenario(sc, conn)
        pinned.append(summarize(conn, sc))
    md = "\n".join([f"# Scenario results ({n_seeds} seeds per scenario)", "",
                    "Statistics are from the pinned demo seed's run; the hit rate is over seeds "
                    f"1..{n_seeds}. Δ successes/week is the engine's estimate if B served 100% of "
                    "traffic.", "", scenario_table(pinned, runs, n_seeds), ""])
    path = REPORTS_DIR / "scenarios.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(md, encoding="utf-8")
    print(f"\n{md}\nreport -> {path}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m abtest.runner", description=__doc__.split("\n\n")[0])
    ap.add_argument("names", nargs="*", help="scenario name (one), or names to sweep (default: all)")
    ap.add_argument("--speed", type=float, help="live mode: wall-clock seconds per sim day")
    ap.add_argument("--sweep", action="store_true", help="run every scenario across seeds 1..N")
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--workers", type=int, help="processes for --sweep (default: CPU count)")
    a = ap.parse_args(argv)
    if a.sweep:
        _sweep_cli(a.names, a.seeds, a.workers)
    elif len(a.names) == 1:
        _simulate(a.names[0], a.speed)
    else:
        ap.error("give exactly one scenario, or --sweep")


if __name__ == "__main__":
    main()
