import sqlite3
from datetime import datetime

import pandas as pd
import pytest

from abtest import store
from abtest.contracts import ALL_A, Allocation, Decision, ExperimentConfig, FrozenConfig, State

CFG = ExperimentConfig.model_validate({
    "id": "t", "dataset": "bttc", "unit": "lead_id", "trigger": "first_answered_call",
    "salt": "s", "allocation": {"A": 0.9, "B": 0.1},
    "variants": {"A": {"base": "prompts/base_buyer_vani.txt"}, "B": {"base": "prompts/base_buyer_vani.txt"}},
    "window": {"days": 14, "looks": 7}, "maturation_days": 2, "baseline_p": 0.2,
    "primary": {"name": "m", "type": "binary", "dispositions": ["Meeting Fixed"], "better": "increase", "mde_rel": 0.15},
    "secondary": {"name": "d", "type": "continuous", "role": "guardrail", "worse_if": "increase", "margin_rel": 0.1},
    "alpha_win": 0.025, "alpha_harm": 0.05, "power": 0.8, "min_info_first_look": 0.2,
    "srm_p_threshold": 0.001, "promotion": "auto",
})
FROZEN = FrozenConfig(config=CFG, canonical_json=CFG.model_dump_json(), config_hash="h",
                      prompt_hash={"A": "a", "B": "b"})


@pytest.fixture
def conn():
    return store.connect(":memory:")


def test_state_round_trip_and_terminal_immutable(conn):
    s = store.create_experiment(conn, FROZEN, scenario="win", seed=1)
    assert store.get_state(conn, "t") == s
    done = s.model_copy(update={"state": State.PROMOTED, "allocation": Allocation(A=0.0, B=1.0),
                                "info_fractions": (0.3, 0.6), "updated_ts_sim": datetime(2026, 10, 1)})
    store.put_state(conn, done)
    got = store.get_state(conn, "t")
    assert got.state == State.PROMOTED and got.allocation.B == 1.0 and got.info_fractions == (0.3, 0.6)
    with pytest.raises(sqlite3.IntegrityError, match="terminal state is immutable"):
        store.put_state(conn, done.model_copy(update={"state": State.RUNNING}))
    assert store.get_config(conn, "t") == CFG


def test_decisions_append_only_and_config_frozen(conn):
    store.create_experiment(conn, FROZEN)
    with conn:
        conn.execute("INSERT INTO decisions (experiment_id, ts_wall, kind, decision, reason, actor, payload)"
                     " VALUES ('t', 'now', 'look', 'CONTINUE', 'r', 'engine', '{}')")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE decisions SET reason = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="frozen"):
        conn.execute("UPDATE experiments SET seed = 2")
    store.reset_experiment(conn, "t")
    assert store.get_state(conn, "t") is None


def _row(decision: str) -> Decision:
    return Decision(ts_wall=datetime(2026, 10, 9), ts_sim=datetime(2026, 10, 1), experiment_id="t",
                    config_hash="h", prompt_hash_A="a", prompt_hash_B="b", kind="state_change",
                    look=None, info_fraction=None, n_A=None, n_B=None, primary=None, guardrail=None,
                    srm=None, decision=decision, reason="r", actor="engine")


def test_draft_routes_all_to_a_and_record_is_atomic(conn):
    s = store.create_experiment(conn, FROZEN)
    assert s.state == State.DRAFT and s.allocation == ALL_A
    done = s.model_copy(update={"state": State.HALTED_SRM})
    store.record(conn, done, [_row("HALTED_SRM")])
    assert store.read_decisions(conn, "t") == [_row("HALTED_SRM")] and store.get_state(conn, "t") == done
    with pytest.raises(sqlite3.IntegrityError, match="terminal state is immutable"):
        store.record(conn, done.model_copy(update={"state": State.RUNNING}), [_row("RUNNING")])
    assert len(store.read_decisions(conn, "t")) == 1  # the failed state write rolled back its rows


def test_insert_df_types(conn):
    df = pd.DataFrame({"experiment_id": ["t", "t"], "lead_id": ["1", "2"], "variant": ["A", "B"],
                       "bucket": pd.Series([5, 9999], dtype="int64"),
                       "ts": pd.to_datetime(["2026-10-01 10:00", "2026-10-01 11:00"])})
    assert store.insert_df(conn, "exposures", df) == 2
    calls = pd.DataFrame({"experiment_id": ["t"], "call_id": ["c"], "lead_id": ["1"],
                          "ts": pd.to_datetime(["2026-10-01 10:00"]), "answered": [True],
                          "disposition": ["Meeting Fixed"], "duration_s": [float("nan")],
                          "attempt_no": pd.array([pd.NA], dtype="Int64")})
    store.insert_df(conn, "calls", calls)
    back = store.read_df(conn, "SELECT * FROM calls", parse_dates=["ts"])
    assert back.loc[0, "answered"] == 1 and pd.isna(back.loc[0, "duration_s"])
    assert back.loc[0, "ts"] == pd.Timestamp("2026-10-01 10:00")


def test_timestamps_round_trip_with_and_without_microseconds(conn):
    ts = pd.to_datetime(["2026-10-01 10:00:00", "2026-10-01 10:00:00.5"], format="ISO8601")
    df = pd.DataFrame({"experiment_id": "t", "lead_id": ["1", "2"], "variant": "A", "bucket": 1, "ts": ts})
    store.insert_df(conn, "exposures", df)
    back = store.read_df(conn, "SELECT ts FROM exposures ORDER BY lead_id", parse_dates=["ts"])
    assert list(back.ts) == list(ts)
