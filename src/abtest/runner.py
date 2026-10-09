"""Drive a scenario through the sim clock into the store (Phase 2: traffic only, no statistics).

CLI: python -m abtest.runner <scenario>   (make simulate SCENARIO=win)
"""

import sqlite3
import sys
import time
from datetime import timedelta

import pandas as pd

from abtest import config, simulator, store
from abtest.contracts import ExperimentState, Scenario, State


def run_scenario(scenario: Scenario, conn: sqlite3.Connection,
                 speed: float | None = None) -> ExperimentState:
    """Rerunning a scenario replaces its previous run in the store."""
    cfg = config.scenario_experiment(scenario)
    sim = config.load_sim(scenario.sim)  # ground truth: handed to the simulator only
    pool = simulator.build_pool(cfg, sim)

    store.reset_experiment(conn, cfg.id)
    state = store.create_experiment(conn, config.freeze(cfg), scenario.name, scenario.seed)
    # ponytail: Phase 2 flips DRAFT -> RUNNING directly; Phase 4's decision.start logs the transition.
    state = state.model_copy(update={"state": State.RUNNING, "started_ts_sim": pool.start,
                                     "updated_ts_sim": pool.start})
    store.put_state(conn, state)

    truth = []
    for day in range(cfg.window.days):
        out = simulator.simulate_day(pool, cfg, sim, state, day, scenario.seed)
        store.insert_df(conn, "exposures", out.exposures)
        store.insert_df(conn, "calls", out.calls)
        truth.append(out.truth)
        state = state.model_copy(update={"updated_ts_sim": pool.start + timedelta(days=day + 1)})
        store.put_state(conn, state)
        if speed:
            time.sleep(speed)

    t = simulator.truth_summary(pd.concat(truth, ignore_index=True), pool, cfg, sim,
                                scenario.name, scenario.seed)
    with conn:
        conn.execute("INSERT INTO sim_truth VALUES (?, ?, ?)", (cfg.id, scenario.name, t.model_dump_json()))
    simulator.write_truth_report(t)
    return state


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


def main(argv: list[str] | None = None) -> None:
    (name,) = argv if argv is not None else sys.argv[1:]
    sc = config.load_scenario(name)
    conn = store.connect()
    t0 = time.perf_counter()
    state = run_scenario(sc, conn)
    secs = time.perf_counter() - t0
    arms = store.read_df(conn, ARM_SQL, {"e": state.experiment_id})
    orphan = store.read_df(conn, ORPHAN_SQL, {"e": state.experiment_id}).iloc[0]
    print(f"Simulated {state.experiment_id} (scenario {sc.name}, seed {sc.seed}): "
          f"{state.started_ts_sim:%Y-%m-%d} to {state.updated_ts_sim:%Y-%m-%d}, {secs:.1f}s")
    print(arms.to_string(index=False))
    print(f"calls with no logged exposure (fault injection): {orphan.calls:,} calls, {orphan.leads:,} leads")
    print(f"ground truth -> reports/sim_truth_{sc.name}.md (the engine never reads it)")


if __name__ == "__main__":
    main()
