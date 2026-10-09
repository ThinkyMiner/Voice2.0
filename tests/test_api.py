"""GET /assign (PLAN.md §2.4) against a temp-file store."""

from fastapi.testclient import TestClient

from abtest import api, config, store
from abtest.contracts import State

CFG_PATH = "configs/experiments/vani-meeting-v1.yaml"


def test_assign_endpoint(tmp_path, monkeypatch):
    db = tmp_path / "api.db"
    monkeypatch.setattr(api, "DB_PATH", db)
    cfg = config.load_experiment(CFG_PATH)
    conn = store.connect(db)
    s = store.create_experiment(conn, config.freeze(cfg))
    store.put_state(conn, s.model_copy(update={"state": State.RUNNING}))
    client = TestClient(api.app)

    got = [client.get("/assign", params={"experiment_id": cfg.id, "lead_id": "lead-7"}) for _ in range(2)]
    assert [r.status_code for r in got] == [200, 200]
    first, second = (r.json() for r in got)
    assert first == second
    assert first == {"variant": first["variant"], "state": "RUNNING", "config_hash": s.config_hash,
                     "prompt_hash": s.prompt_hash[first["variant"]]}

    assert client.get("/assign", params={"experiment_id": "nope", "lead_id": "lead-7"}).status_code == 404
    assert client.get("/assign", params={"experiment_id": cfg.id, "lead_id": ""}).status_code == 422
    rows = conn.execute("SELECT experiment_id, lead_id, variant FROM exposures").fetchall()
    assert [tuple(r) for r in rows] == [(cfg.id, "lead-7", first["variant"])] * 2
    conn.close()
