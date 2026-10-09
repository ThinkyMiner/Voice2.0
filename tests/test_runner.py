import pytest

from abtest import config, runner, store
from abtest.contracts import DATA_DIR, State


@pytest.mark.data
@pytest.mark.skipif(not (DATA_DIR / "Call_Logs_Hackathon.csv").exists(), reason="data not present")
def test_run_scenario_fills_store_deterministically(tmp_path):
    sc = config.load_scenario("win").model_copy(update={"seed": 7})
    runs = []
    for i in range(2):
        conn = store.connect(tmp_path / f"run{i}.db")
        state = runner.run_scenario(sc, conn)
        runs.append([store.read_df(conn, f"SELECT * FROM {t} ORDER BY 1, 2, 3", ())
                     for t in ("exposures", "calls")])
        assert state.state == State.RUNNING
        assert conn.execute("SELECT COUNT(*) FROM sim_truth").fetchone()[0] == 1
    (exp, calls), (exp2, calls2) = runs
    assert exp.equals(exp2) and calls.equals(calls2)  # same seed -> identical store
    share_b = exp.drop_duplicates("lead_id").variant.eq("B").mean()
    assert 0.08 < share_b < 0.12 and len(calls) > len(exp)
