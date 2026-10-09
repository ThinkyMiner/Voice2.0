"""The platform API (ext/service.py, docs/loop_api.md) with fastapi's TestClient: a temp store, the
temp LOOP_DIR, invented calls, audits, root causes and fixes; the stubbed model for rubric tests."""

import time

import httpx
import pytest
from fastapi.testclient import TestClient
from test_loop import audit, cause, transcript, world  # noqa: F401 - world is a fixture

from abtest.contracts import BASE_PROMPT, State
from abtest.ext import improve, judge, listen, llm, loop, models, rubrics, service
from abtest.ext.models import CallAudit, Fix, RubricVerdict, save

SEND = httpx.Client.send  # before conftest's no-network patch: TestClient's transport is in-process


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(TestClient, "send", SEND)
    monkeypatch.setattr(loop, "DB_PATH", tmp_path / "abtest.db")
    monkeypatch.setattr(listen, "audited_calls", lambda: [{"call_id": "aud-1", "recording_url": "", "human": {
        "overall": "fatal", "grades": {"Quantity Probing Error": "fatal", "Looping Behavior": "pass"},
        "remarks": {"Quantity Probing Error": "invented remark: asked quantity four times"}}}])
    return TestClient(service.app, base_url="http://127.0.0.1:8600")


def _calls_on_disk():
    """Three invented calls: a fixture, a 'production' and an 'audited' one, the first two graded."""
    ts = [transcript("fx-1"), transcript("real-1", source="production"), transcript("aud-1", source="audited")]
    for t in ts:
        save(models.TRANSCRIPTS_DIR / f"{t.call_id}.json", t)
    save(models.AUDITS_DIR / "v1" / "fx-1.json", audit(ts[0], "pass"))
    save(models.AUDITS_DIR / "v1" / "real-1.json",
         audit(ts[1]).model_copy(update={"risk_flags": ("loop",), "summary": "invented: bot looped on quantity"}))
    return ts


def test_empty_platform_reads(client):
    o = client.get("/api/overview").json()
    assert o["production"]["version"] == "v1" and o["cycle"] is None and o["cycles"] == []
    assert o["simulated_traffic"] is True and o["kpis"]["calls_audited"] == 0 and o["kpis"]["fatal_rate"] is None
    assert o["kpis"]["experiments"] == 0 and o["kpis"]["per_week_gained"] == 0
    lp = client.get("/api/loop").json()
    assert lp == {"cycle": None, "next_action": loop.next_action(None), "busy": False, "log": lp["log"]}
    assert client.get("/api/experiments").json() == {"items": []}
    rubrics = client.get("/api/rubrics").json()["items"]
    assert rubrics and all(r["builtin"] for r in rubrics)
    s = client.get("/api/settings").json()
    assert s["bind"] == "127.0.0.1:8600" and s["models"]["judge"] == llm.model_for("judge")


def test_calls_list_filters_and_detail(client):
    _calls_on_disk()
    r = client.get("/api/calls").json()
    assert r["total"] == 3 and [i["call_id"] for i in r["items"]] == ["aud-1", "fx-1", "real-1"]
    real = next(i for i in r["items"] if i["call_id"] == "real-1")
    assert (real["overall"], real["risk_flags"], real["n_fatal"], real["human_overall"]) == ("fatal", ["loop"], 1, None)
    aud = next(i for i in r["items"] if i["call_id"] == "aud-1")
    assert aud["overall"] is None and aud["human_overall"] == "fatal"
    assert [i["call_id"] for i in client.get("/api/calls?source=production").json()["items"]] == ["real-1"]
    assert [i["call_id"] for i in client.get("/api/calls?overall=pass").json()["items"]] == ["fx-1"]
    assert [i["call_id"] for i in client.get("/api/calls?flag=loop").json()["items"]] == ["real-1"]
    assert [i["call_id"] for i in client.get("/api/calls?q=LOOPED").json()["items"]] == ["real-1"]
    assert client.get("/api/calls?limit=1").json()["total"] == 3
    assert client.get("/api/calls?limit=0").status_code == 422

    d = client.get("/api/calls/aud-1").json()
    assert d["transcript"]["source"] == "audited" and d["audit"] is None and d["audio_url"] is None
    assert d["human"]["grades"] == {"quantity_probing_error": "fatal", "looping_behavior": "pass"}
    assert d["human"]["remarks"] == {"quantity_probing_error": "invented remark: asked quantity four times"}
    assert d["rubrics"]["looping_behavior"]["category"]
    assert client.get("/api/calls/real-1").json()["audit"]["overall"] == "fatal"
    assert client.get("/api/calls/nope").status_code == 404
    assert client.get("/api/calls/..%2F..%2Fregistry").status_code == 404


def test_call_detail_shows_the_calibrated_grade_like_the_list(client):
    t = transcript("real-2", source="production")
    save(models.TRANSCRIPTS_DIR / "real-2.json", t)
    v = RubricVerdict(rubric_id="looping_behavior", rubric_version=1, grade="non_fatal", confidence=0.9, reason="r")
    save(models.AUDITS_DIR / "v1" / "real-2.json", CallAudit(call_id="real-2", prompt_version="v1", overall="non_fatal",
                                                             verdicts=(v,), model="m", rubric_set_hash="h"))
    save(judge.CALIBRATION, {"model": "m", "soft": {"looping_behavior": "pass"},
                             "rubrics": {"looping_behavior": rubrics.set_hash([rubrics.get("looping_behavior")])}})
    listed = next(i for i in client.get("/api/calls").json()["items"] if i["call_id"] == "real-2")
    assert listed["overall"] == client.get("/api/calls/real-2").json()["audit"]["overall"] == "pass"


def test_recordings_are_local_only(client):
    _calls_on_disk()
    assert client.get("/api/recordings/real-1").status_code == 404
    (models.RECORDINGS_DIR / "real-1.mp3").parent.mkdir(parents=True, exist_ok=True)
    (models.RECORDINGS_DIR / "real-1.mp3").write_bytes(b"ID3invented")
    r = client.get("/api/recordings/real-1")
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content == b"ID3invented"
    assert client.get("/api/calls/real-1").json()["audio_url"] == "/api/recordings/real-1"
    assert client.get("/api/recordings/.hidden").status_code == 404


def test_audit_issues_fixes_prompts(client):
    _calls_on_disk()
    a = client.get("/api/audit?version=v1").json()                           # the loop's corpus by default
    assert (a["source"], a["n_calls"], a["n_audited"]) == ("audited", 1, 0)
    assert client.get("/api/overview").json()["kpis"]["calls_total"] == 1
    a = client.get("/api/audit?version=v1&source=all").json()
    assert (a["n_calls"], a["n_audited"], a["split"]) == (3, 2, {"pass": 1, "non_fatal": 0, "fatal": 1})
    assert a["human_split"] == {"pass": 0, "non_fatal": 0, "fatal": 1}
    assert a["by_rubric"] == [{"rubric_id": "quantity_probing", "name": "quantity_probing", "category": None,
                               "root_type": None, "n_applicable": 2, "pass": 1, "non_fatal": 0, "fatal": 1, "na": 0}]
    assert a["risky"] == [{"call_id": "real-1", "flags": ["loop"], "overall": "fatal",
                           "summary": "invented: bot looped on quantity"}]
    assert client.get("/api/audit?version=v9").status_code == 404

    save(improve.ROOT_CAUSES_DIR / "v1.json", [cause(2), cause(1)])
    i = client.get("/api/issues?version=v1").json()
    assert [x["id"] for x in i["items"]] == ["rc-quantity_probing-1", "rc-quantity_probing-2"]
    assert i["formula"].startswith("priority = 100")

    fix = Fix(id="fx-0001", root_cause_id="rc-quantity_probing-1", base_version="v1", edits=(), rationale="invented",
              prompt_b_path="x.txt", prompt_b_sha256="0", diff="", model="stub")
    save(improve.FIXES_DIR / "fx-0001.json", fix)
    assert [f["id"] for f in client.get("/api/fixes").json()["items"]] == ["fx-0001"]
    one = client.get("/api/fixes/fx-0001").json()
    assert one["fix"]["id"] == "fx-0001" and one["root_cause"]["id"] == "rc-quantity_probing-1"
    assert client.get("/api/fixes/nope").status_code == 404

    p = client.get("/api/prompts").json()
    assert p["production"] == "v1" and [v["version"] for v in p["items"]] == ["v1"]
    t = client.get("/api/prompts/v1/text")
    assert t.headers["content-type"].startswith("text/plain") and t.text == BASE_PROMPT.read_text(encoding="utf-8")
    assert client.get("/api/prompts/v9/text").status_code == 404


def test_a_later_version_shows_its_replay_calls_and_their_audits(client):
    rep = improve.REPLAY_DIR / "fx-1"
    save(rep / "r-B.json", b := transcript("r-B", source="replay", version="fx-1"))
    save(rep / "r-B.audit.json", audit(b, "pass"))
    d = client.get("/api/calls/r-B").json()
    assert (d["transcript"]["source"], d["transcript"]["prompt_version"], d["audit"]["overall"]) == \
        ("replay", "fx-1", "pass")
    save(models.REGISTRY, [*loop.versions(), models.PromptVersion(
        version="v2", path="x", sha256="0", parent="v1", fix_id="fx-1", status="production", created_ts="")])
    save(models.TRANSCRIPTS_DIR / "r-B.json", b.model_copy(update={"prompt_version": "v2"}))  # as listen files it
    assert client.get("/api/calls/r-B").json()["transcript"]["prompt_version"] == "v2"
    assert client.get("/api/calls?source=replay").json()["items"][0]["overall"] == "pass"
    a = client.get("/api/audit?version=v2").json()
    assert (a["n_calls"], a["n_audited"], a["human_split"], a["agreement"]) == (1, 1, None, None)


def test_rubric_studio_crud(client):
    body = {"id": "price_promise", "name": "Never promises a price", "category": "Compliance",
            "question": "Does the bot state or promise a price?", "actor": "Invented PM"}
    r = client.post("/api/rubrics", json=body)
    assert r.status_code == 201 and (r.json()["version"], r.json()["builtin"], r.json()["author"]) == (1, False, "Invented PM")
    assert client.post("/api/rubrics", json=body).status_code == 409
    assert client.post("/api/rubrics", json={**body, "id": "x2", "actor": ""}).status_code == 422
    assert client.post("/api/rubrics", json={**body, "id": "Bad Id"}).status_code == 422
    assert client.post("/api/rubrics", json={**body, "id": "w", "weight": 9}).status_code == 422

    r = client.put("/api/rubrics/price_promise", json={"guidance": "Any rupee figure counts.", "actor": "Invented QA"})
    assert r.status_code == 200 and r.json()["version"] == 2 and r.json()["question"] == body["question"]
    assert client.put("/api/rubrics/nope", json={"actor": "x"}).status_code == 404
    assert client.delete("/api/rubrics/price_promise").json()["active"] is False
    assert client.delete("/api/rubrics/nope").status_code == 404
    listed = {x["id"]: x for x in client.get("/api/rubrics").json()["items"]}
    assert listed["price_promise"]["active"] is False and listed["price_promise"]["version"] == 3
    assert [v["version"] for v in client.get("/api/rubrics/price_promise/versions").json()["items"]] == [1, 2, 3]
    assert client.get("/api/rubrics/nope/versions").status_code == 404


def test_rubric_test_refuses_real_calls_without_the_server_flag(client, monkeypatch):
    _calls_on_disk()

    def stub(model, messages, schema):
        ids = schema["properties"]["verdicts"]["items"]["properties"]["rubric_id"]["enum"]
        return {"verdicts": [{"rubric_id": i, "grade": "fatal", "confidence": 0.8, "reason": "invented",
                              "evidence": [{"turn": 0, "quote": "kitni quantity"}]} for i in ids]}
    llm.set_stub(stub)
    draft = {"name": "Asks quantity once", "question": "Does the bot ask the quantity at most once?"}
    v = client.post("/api/rubrics/test", json={"rubric": draft, "call_id": "fx-1"})
    assert v.status_code == 200 and v.json()["grade"] == "fatal" and v.json()["evidence"][0]["turn"] == 0
    assert client.post("/api/rubrics/test", json={"rubric": draft, "call_id": "real-1"}).status_code == 403
    assert client.post("/api/rubrics/test", json={"rubric": draft, "call_id": "aud-1"}).status_code == 403
    monkeypatch.setenv("LOOP_ALLOW_REAL", "1")
    assert client.post("/api/rubrics/test", json={"rubric": draft, "call_id": "real-1"}).status_code == 200
    assert client.post("/api/rubrics/test", json={"rubric": draft, "call_id": "nope"}).status_code == 404
    assert client.post("/api/rubrics/test", json={"call_id": "fx-1"}).status_code == 422
    assert client.post("/api/rubrics/test", json={"rubric": {"weight": 9}, "call_id": "fx-1"}).status_code == 422


def test_experiments_and_approval_continue_the_loop(world, client):  # noqa: F811
    loop.advance()
    items = client.get("/api/experiments").json()["items"]
    assert [(e["experiment_id"], e["state"], e["cycle"], e["simulated"]) for e in items] == \
        [("vani-loop-c1", "PENDING_APPROVAL", 1, True)]
    assert items[0]["assumed_effect"] == "+50% BL success for B (assumed; simulated traffic)"
    assert items[0]["allocation"] == {"A": 0.9, "B": 0.1} and items[0]["n_looks"] == 7

    e = client.get("/api/experiments/vani-loop-c1").json()
    assert e["config"]["id"] == "vani-loop-c1" and len(e["looks"]) == e["looks_done"] >= 1
    last = e["looks"][-1]
    assert last["z"] >= last["win_bound"] and last["harm_bound"] < 0 and last["ci95"][0] < last["lift_rel"] < last["ci95"][1]
    assert last["n_b"] > 0 and last["decision"] == "PENDING_APPROVAL"
    assert e["decisions"][-1]["to_state"] == "PENDING_APPROVAL"
    assert e["impact"]["per_week"] > 0 and e["impact"]["unit"] == "BL Enriched / BL Approved"
    assert client.get("/api/experiments/nope").status_code == 404

    url = "/api/experiments/vani-loop-c1/decision"
    assert client.post(url, json={"actor": " ", "approved": True}).status_code == 422
    assert client.post(url, json={"approved": True}).status_code == 422
    with loop._exclusive():
        assert client.post(url, json={"actor": "kartik", "approved": True}).status_code == 409  # busy
    r = client.post(url, json={"actor": "kartik", "approved": True})
    assert r.status_code == 200 and r.json()["state"] == State.PROMOTED.value
    assert (r.json()["cycle"]["n"], r.json()["cycle"]["stage"]) == (2, "listen")
    assert client.post(url, json={"actor": "kartik", "approved": True}).status_code == 409  # not pending now
    assert client.post("/api/experiments/nope/decision", json={"actor": "k", "approved": True}).status_code == 404

    o = client.get("/api/overview").json()
    assert o["production"]["version"] == "v2" and [c["n"] for c in o["cycles"]] == [2, 1]
    assert o["kpis"]["experiments"] == 1 and o["kpis"]["per_week_gained"] > 0


def _idle(client) -> dict:
    """GET /api/loop once the background advance is done (never leave it running past a test)."""
    for _ in range(500):
        if not (lp := client.get("/api/loop").json())["busy"]:
            return lp
        time.sleep(0.02)
    raise AssertionError("the background advance did not finish")


def test_advance_runs_in_the_background_one_at_a_time(world, client):  # noqa: F811
    assert client.post("/api/loop/advance", json={"until": "nope"}).status_code == 422
    with loop._exclusive():
        assert client.post("/api/loop/advance", json={}).status_code == 409
    assert client.post("/api/loop/advance", json={"until": "judge"}).status_code == 202
    lp = _idle(client)
    assert lp["cycle"]["stage"] == "improve"
    assert any("advance finished: cycle 1 at improve" in line for line in lp["log"])
    assert client.post("/api/loop/advance").status_code == 202  # no body: until the cycle waits or ends
    assert _idle(client)["cycle"]["stage"] == "approval"


def test_base_route_and_api_win_over_the_static_site(client):
    assert client.get("/assign").status_code == 422          # the base app's route, not a static 404
    assert client.get("/api/overview", headers={"host": "rebound.example"}).status_code == 400  # DNS rebinding
    assert client.get("/api/nope").status_code == 404
    if (service.WEB_DIR / "index.html").exists():
        assert client.get("/").headers["content-type"].startswith("text/html")


def test_non_finite_numbers_become_null():
    assert service._finite({"a": [float("nan"), 1.0], "b": float("inf"), "c": "NaN"}) == \
        {"a": [None, 1.0], "b": None, "c": "NaN"}
