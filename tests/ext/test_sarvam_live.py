"""Live calls through Sarvam (ext/sarvam_live.py), offline: a fake Sarvam behind httpx.MockTransport, a temp
store, the temp LOOP_DIR, invented team numbers. No test reads the real .env or allowlist."""

import copy
import functools
import hashlib
import json
import types
from contextlib import closing
from datetime import datetime, timedelta

import httpx
import plotly.graph_objects as go
import pytest

from abtest import decision, store
from abtest.contracts import State
from abtest.ext import judge, llm, loop, models, sarvam_live as live
from abtest.ext.models import Transcript, load

SEND = httpx.Client.send  # before conftest's no-network patch; the mock transport below answers in-process
NOW = datetime(2026, 10, 10, 19, 0)  # IST, naive like the store
ENV = {"SARVAM_VA_API_KEY": "va-key", "SARVAM_API_KEY": "model-key", "SARVAM_ORG_ID": "org-1",
       "SARVAM_WORKSPACE_ID": "ws-1", "SARVAM_APP_ID": "Vani-1234", "SARVAM_VERSION_A": "1",
       "SARVAM_VERSION_B": "2", "SARVAM_CONNECTION_ID": "Sarvam-Vobiz-1", "SARVAM_AGENT_NUMBER": "+911100000000"}
TEAM = """# invented numbers
- {lead_id: team-01, phone: "+919990000001", name: Asha, product: Steel pipes, city: Noida}
- {lead_id: team-02, phone: "+919990000002", name: Ravi, product: Cotton bags, city: Pune}
- {lead_id: team-03, phone: "+919990000003", name: Meena, product: LED bulbs, city: Jaipur}
- {lead_id: team-04, phone: "+919990000004", name: Kabir, product: Office chairs, city: Delhi}
"""
OUTCOMES = {  # what Sarvam's attempts list says about each lead's call, besides the variables the POST sent
    "team-01": dict(connectivity_status="connected", interaction_id="20261010/abc:01", ended_by="agent",
                    duration_in_seconds=45.0, start_datetime="2026-10-10T13:31:00Z",
                    end_datetime="2026-10-10T13:31:45Z", language_name="Hindi",
                    agent_variables={"disposition": "BL Enriched"}),
    "team-02": dict(connectivity_status="no_answer", failure_reason="exotel: Phone number is registered under TRAI NDNC"),
    "team-03": dict(connectivity_status="connected", interaction_id="20261010/abc:03", ended_by="user",
                    duration_in_seconds=12.0, start_datetime="2026-10-10T13:29:58Z",  # their clock 2 s behind ours
                    end_datetime="2026-10-10T13:30:10Z", agent_variables={}),  # no disposition from the agent yet
    "team-04": dict(connectivity_status="connected", interaction_id="20261010/abc:04", end_datetime=None),  # talking
}
TRANSCRIPT = {"interaction_id": "x", "user_phone_number": "+919990000001", "transcript": [
    {"role": "agent", "content": "Namaste, main Vani bol rahi hoon. Aapko kitni quantity chahiye?"},
    {"role": "system", "content": "tool: update_variables"},
    {"role": "user", "content": "Do sau piece. Mera number 99900 00001 hai, call back karna."}]}


class FakeSarvam:
    """Instant Outbound and the analytics API in memory. A POST checks the lead's exposure is already logged;
    an attempt in the list carries the agent variables its POST sent, plus the agent's outputs."""

    def __init__(self):
        self.requests, self.placed, self.sent, self.fail = [], {}, {}, {}
        self.outcomes = copy.deepcopy(OUTCOMES)
        self.transcript = lambda request: httpx.Response(200, json=TRANSCRIPT)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path.endswith("/outbounds"):
            sent = json.loads(request.content)["app_config"]["agent_variables"]
            with closing(store.connect(loop.DB_PATH)) as conn:
                assert conn.execute("SELECT COUNT(*) FROM exposures WHERE lead_id = ?",
                                    (sent["lead_id"],)).fetchone()[0], \
                    "the exposure must be logged before the call is placed"
            if sent["lead_id"] in self.fail:
                return self.fail[sent["lead_id"]](request)
            aid = f"0000000{len(self.placed) + 1}-aaaa-bbbb-cccc-dddddddddddd"
            self.placed[aid], self.sent[aid] = sent["lead_id"], sent
            return httpx.Response(200, json={"attempt_id": aid})
        if path.endswith("/attempts"):
            ours = [{"attempt_id": aid, **(o := self.outcomes[lead]),
                     "agent_variables": {**self.sent[aid], **o.get("agent_variables", {})}}
                    for aid, lead in self.placed.items()]
            items = [{"attempt_id": "someone-elses-attempt", "connectivity_status": "connected"}, *ours, *ours[:1]]
            q = request.url.params
            page = items[int(q["offset"]):int(q["offset"]) + int(q["limit"])]
            return httpx.Response(200, json={"items": page, "total": len(items), "limit": int(q["limit"]),
                                             "offset": int(q["offset"])})
        if "/transcripts/" in path:
            return self.transcript(request)
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture
def w(tmp_path, monkeypatch):
    for name in ENV:
        monkeypatch.setenv(name, ENV[name])
    monkeypatch.setattr(live, "ENV_FILE", tmp_path / "no.env")  # never the repo's .env
    (tmp_path / "team_phones.yaml").write_text(TEAM, encoding="utf-8")
    monkeypatch.setattr(live, "ALLOWLIST", tmp_path / "team_phones.yaml")
    for v, text in (("A", "Vani demo prompt. Ask the quantity at most quantity = 3 times.\n"),
                    ("B", "Vani demo prompt. Ask the quantity at most quantity = 2 times.\n")):
        (tmp_path / f"vani_demo_{v}.txt").write_text(text, encoding="utf-8")
    monkeypatch.setattr(live, "PROMPTS", {v: str(tmp_path / f"vani_demo_{v}.txt") for v in "AB"})
    monkeypatch.setattr(live, "PAGE", 2)  # the attempts list comes back over several pages
    monkeypatch.setattr(loop, "DB_PATH", tmp_path / "abtest.db")
    monkeypatch.setattr(decision, "JSONL", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(go.Figure, "write_image", lambda *a, **k: None)
    ns = types.SimpleNamespace(now=NOW, fake=FakeSarvam(), tmp=tmp_path)
    monkeypatch.setattr(live, "now_ist", lambda: ns.now)
    real_client = live.client

    def client(s):
        c = real_client(s, transport=httpx.MockTransport(ns.fake))  # ns.fake as it is when the run starts
        c.send = functools.partial(SEND, c)  # this client only: its transport is the fake
        return c
    monkeypatch.setattr(live, "client", client)
    ns.conn = lambda: closing(store.connect(loop.DB_PATH))
    return ns


def _rows(w, sql, *args):
    with w.conn() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def _posted(requests):
    return [json.loads(r.content)["app_config"]["agent_variables"]["lead_id"] for r in requests if r.method == "POST"]


def _started(w, split="0.5"):
    assert live.main(["start", "--split", split]) == 0


def test_start_freezes_a_live_experiment_and_its_agent(w, capsys, monkeypatch):
    _started(w)
    out = capsys.readouterr().out
    assert "DEMO SPLIT: 50/50" in out and "n_B,max" in out and "agent Vani-1234: A = version 1" in out
    with w.conn() as conn:
        s, cfg = store.get_state(conn, "vani-live-c1"), store.get_config(conn, "vani-live-c1")
    assert s.state == State.RUNNING and s.allocation.B == 0.5 and s.started_ts_sim == NOW
    assert cfg.maturation_days == 0 and cfg.salt == hashlib.sha256(b"vani-live-c1").hexdigest()[:6]
    assert cfg.primary.dispositions == ("BL Enriched", "BL Approved") and cfg.promotion == "approval"
    assert s.prompt_hash["A"] != s.prompt_hash["B"]
    assert json.loads(_rows(w, "SELECT agent FROM live_experiments")[0]["agent"]) == {k: ENV[k] for k in live.FROZEN}
    head = (live.LIVE_DIR / "vani-live-c1.yaml").read_text(encoding="utf-8")
    assert "a DEMO split" in head and "maturation_days 0" in head
    for argv, why in ((["start"], "already exists"), (["start", "--id", "c2"], "vani-live-")):
        with pytest.raises(SystemExit, match=why):
            live.main(argv)
    monkeypatch.delenv("SARVAM_APP_ID")
    with pytest.raises(SystemExit, match="missing SARVAM_APP_ID"):  # it freezes which agent it calls
        live.main(["start", "--id", "vani-live-c2"])
    monkeypatch.setenv("SARVAM_APP_ID", "Vani-1234")
    (w.tmp / "vani_demo_B.txt").unlink()
    with pytest.raises(SystemExit, match="cannot start vani-live-c2: variant B: cannot read prompt"):
        live.main(["start", "--id", "vani-live-c2"])


def test_call_refusals_come_before_anything_is_dialled(w, monkeypatch):
    _started(w)
    with pytest.raises(SystemExit, match="--yes-place-real-calls"):
        live.main(["call"])
    for argv in (["call", "--yes"], ["call", "--yes-place"], ["call", "--y"]):  # no prefix counts as the flag
        with pytest.raises(SystemExit):
            live.main(argv)
    with pytest.raises(SystemExit, match="a live experiment id is vani-live-"):  # never a simulated experiment
        live.main(["call", "--yes-place-real-calls", "--id", "vani-loop-c1"])
    for hhmm in ((21, 0), (8, 59)):
        w.now = NOW.replace(hour=hhmm[0], minute=hhmm[1])
        with pytest.raises(SystemExit, match="only from 09:00 to 21:00 IST"):
            live.main(["call", "--yes-place-real-calls"])
    w.now = NOW
    monkeypatch.delenv("SARVAM_CONNECTION_ID")
    with pytest.raises(SystemExit, match="missing SARVAM_CONNECTION_ID"):
        live.main(["call", "--yes-place-real-calls"])
    monkeypatch.setenv("SARVAM_CONNECTION_ID", "Sarvam-Vobiz-1")
    monkeypatch.setenv("SARVAM_VERSION_B", "1")
    with pytest.raises(SystemExit, match="two different committed versions"):
        live.main(["call", "--yes-place-real-calls"])
    monkeypatch.setenv("SARVAM_VERSION_B", "3")  # not the version the experiment froze for B
    with pytest.raises(SystemExit, match="SARVAM_VERSION_B changed since vani-live-c1 started"):
        live.main(["call", "--yes-place-real-calls"])
    monkeypatch.setenv("SARVAM_VERSION_B", "2")
    for team, why in ((TEAM.replace('"+919990000003"', "+919990000003"), "team-03: phone must be an E.164 string"),
                      (TEAM.replace("lead_id: team-02", "lead_id: 919990000002"), "entry 2: lead_id .* not a phone"),
                      (TEAM.replace('"+919990000001",', '"+919990000001"'), r"not valid YAML \(line 2, column")):
        live.ALLOWLIST.write_text(team, encoding="utf-8")
        with pytest.raises(SystemExit, match=why) as e:
            live.main(["call", "--yes-place-real-calls"])
        assert "999000000" not in str(e.value)  # YAML's own message would quote the broken line, number and all
    live.ALLOWLIST.write_text(TEAM.replace("+919990000004", "+919990000001"), encoding="utf-8")
    with pytest.raises(SystemExit, match="the same phone appears twice"):
        live.main(["call", "--yes-place-real-calls"])
    live.ALLOWLIST.unlink()
    with pytest.raises(SystemExit, match="no allowlist"):
        live.main(["call", "--yes-place-real-calls"])
    assert w.fake.requests == [] and _rows(w, "SELECT * FROM exposures") == []
    assert _rows(w, "SELECT * FROM live_attempts") == []


def test_call_refuses_when_a_prompt_changed_after_start(w):
    _started(w)
    (w.tmp / "vani_demo_B.txt").write_text("Vani demo prompt, edited after the start.\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="changed after it started"):
        live.main(["call", "--yes-place-real-calls"])
    assert w.fake.requests == [] and _rows(w, "SELECT * FROM exposures") == []


def test_call_routes_each_lead_and_places_it_with_its_arms_version(w, capsys):
    _started(w)
    assert live.main(["call", "--yes-place-real-calls", "--max", "3"]) == 0
    assert live.main(["call", "--yes-place-real-calls"]) == 0  # the fourth lead only
    assert live.main(["call", "--yes-place-real-calls"]) == 0  # nobody left: no call
    posts = [r for r in w.fake.requests if r.method == "POST"]
    exposures = {r["lead_id"]: r for r in _rows(w, "SELECT * FROM exposures")}
    assert len(posts) == 4 and len(exposures) == 4 and {e["variant"] for e in exposures.values()} == {"A", "B"}
    team = {x["lead_id"]: x for x in live.allowlist()}
    for r in posts:
        assert r.url.path == "/api/outbounds/v1/orgs/org-1/workspaces/ws-1/outbounds"
        assert r.headers["X-API-Key"] == "va-key"  # the Voice Agents key wins over SARVAM_API_KEY
        body = json.loads(r.content)
        lead = team[body["app_config"]["agent_variables"]["lead_id"]]
        v = exposures[lead["lead_id"]]["variant"]
        assert body == {"app_config": {
            "app_id": "Vani-1234", "app_version": {"A": 1, "B": 2}[v],
            "connection_config": {"connection_id": "Sarvam-Vobiz-1", "agent_phone_number": ENV["SARVAM_AGENT_NUMBER"]},
            "agent_variables": {"lead_id": lead["lead_id"], "ab_variant": v, "buyer_name": lead["name"],
                                "product_name": lead["product"], "city": lead["city"]}},
            "user_config": {"user_phone_number": lead["phone"]}}
    assert {e["ts"] for e in exposures.values()} == {"2026-10-10T19:00:00.000000"}  # IST, the store's format
    with w.conn() as conn:
        hashes = store.get_state(conn, "vani-live-c1").prompt_hash
        recs = live._attempts(conn, "vani-live-c1")
    assert [(r["lead_id"], r["variant"], r["prompt_hash"], r["ts"], r["status"]) for r in recs] == [
        (x, exposures[x]["variant"], hashes[exposures[x]["variant"]], "2026-10-10T19:00:00.000000", "placed")
        for x in team]
    out = capsys.readouterr().out
    assert "+91******0001" in out and "9990000001" not in out


def test_a_failed_post_stops_the_run_and_only_a_definite_refusal_is_tried_again(w, capsys):
    _started(w)

    def timeout(request):
        raise httpx.ReadTimeout("no reply", request=request)

    def offline(request):
        raise httpx.ConnectError("no route to host", request=request)
    w.fake.fail = {"team-01": timeout}
    with pytest.raises(SystemExit, match="ReadTimeout; it may have rung, so it is never dialled again"):
        live.main(["call", "--yes-place-real-calls"])  # it stops: an outage costs one lead, not the allowlist
    w.fake.fail = {"team-02": lambda r: httpx.Response(503, text="busy")}
    with pytest.raises(SystemExit, match="HTTP 503: busy; it may have rung"):
        live.main(["call", "--yes-place-real-calls"])
    echo = {"detail": [{"msg": "bad", "input": "+919990000003"}]}
    w.fake.fail = {"team-03": lambda r: httpx.Response(422, json=echo), "team-04": offline}
    with pytest.raises(SystemExit, match="ConnectError, no call was placed"):
        live.main(["call", "--yes-place-real-calls"])
    out = capsys.readouterr().out
    assert "refused, HTTP 422" in out and "9990000003" not in out  # Sarvam's 422 echoes the number back
    w.fake.fail = {}
    live.main(["call", "--yes-place-real-calls"])  # team-03 and team-04 only: neither request reached a phone
    assert _posted(w.fake.requests) == ["team-01", "team-02", "team-03", "team-04", "team-03", "team-04"]
    tries = _rows(w, "SELECT lead_id, try_no, status, note FROM live_attempts ORDER BY rowid")
    assert [tuple(r.values()) for r in tries] == [
        ("team-01", 1, "unknown", "ReadTimeout"), ("team-02", 1, "unknown", "HTTP 503"),
        ("team-03", 1, "refused", "HTTP 422"), ("team-04", 1, "refused", "ConnectError"),
        ("team-03", 2, "placed", None), ("team-04", 2, "placed", None)]  # every try stays on record
    capsys.readouterr()
    live.main(["status"])
    out = capsys.readouterr().out
    assert "may have rung, no attempt id" in out and "team-01 (ReadTimeout), team-02 (HTTP 503)" in out
    assert "refused by Sarvam" not in out  # team-03 and team-04 went through on their second try


def test_a_crash_during_the_post_never_rings_a_lead_twice(w, capsys):
    """Sarvam takes team-01's call, then the run dies (Ctrl-C) before the answer is read. The claim was
    committed before the POST, so no later run dials team-01 again, and fetch finds its call by lead and arm."""
    _started(w)
    fake = w.fake

    def crash(request):
        fake(request)
        raise KeyboardInterrupt
    w.fake = crash
    with pytest.raises(KeyboardInterrupt):
        live.main(["call", "--yes-place-real-calls"])
    w.fake = fake
    live.main(["call", "--yes-place-real-calls"])
    live.main(["call", "--yes-place-real-calls"])
    assert _posted(fake.requests) == ["team-01", "team-02", "team-03", "team-04"]
    live.main(["status"])
    assert "team-01 (the run stopped during the POST)" in capsys.readouterr().out
    w.now = NOW + timedelta(minutes=10)
    live.main(["fetch"])
    aid = next(a for a, lead in fake.placed.items() if lead == "team-01")
    assert "team-01: its POST got no answer, but Sarvam lists its call" in capsys.readouterr().out
    assert _rows(w, "SELECT status, attempt_id FROM live_attempts WHERE lead_id = 'team-01'") == [
        {"status": "placed", "attempt_id": aid}]
    assert _rows(w, "SELECT call_id, disposition FROM calls WHERE lead_id = 'team-01'") == [
        {"call_id": aid, "disposition": "BL Enriched"}]


def test_two_call_runs_at_once_dial_each_lead_once(w, capsys):
    _started(w)
    fake, second = w.fake, []

    def meanwhile(request):  # a second call run starts while the first run's first POST is in flight
        if not second:
            second.append(request)
            live.main(["call", "--yes-place-real-calls"])
        return fake(request)
    w.fake = meanwhile
    live.main(["call", "--yes-place-real-calls"])
    assert sorted(_posted(fake.requests)) == ["team-01", "team-02", "team-03", "team-04"]  # each once
    assert capsys.readouterr().out.count("another call run claimed it first; skipped") == 3


def test_a_run_stops_at_the_end_of_calling_hours(w):
    _started(w)
    w.now, fake = NOW.replace(hour=20, minute=59), w.fake

    def slow(request):  # this POST takes the clock past 21:00
        w.now = NOW.replace(hour=21)
        return fake(request)
    w.fake = slow
    with pytest.raises(SystemExit, match=r"only from 09:00 to 21:00 IST \(stopped; 1 placed this run\)"):
        live.main(["call", "--yes-place-real-calls"])
    assert len(_posted(fake.requests)) == 1 and len(_rows(w, "SELECT * FROM exposures")) == 1


def test_fetch_brings_back_outcomes_in_ist_and_transcripts_once(w, capsys):
    _started(w)
    live.main(["call", "--yes-place-real-calls"])
    w.now = NOW + timedelta(minutes=10)
    assert live.main(["fetch"]) == 0
    calls = {r["lead_id"]: r for r in _rows(w, "SELECT * FROM calls")}
    assert {k: (r["ts"], r["answered"], r["disposition"]) for k, r in calls.items()} == {
        "team-01": ("2026-10-10T19:01:00.000000", 1, "BL Enriched"),
        "team-02": ("2026-10-10T19:00:00.000000", 0, "Not Connected"),          # no start: the exposure time
        "team-03": ("2026-10-10T19:00:00.000000", 1, "Abruptly Disconnected Call"),  # clamped to the exposure
    }  # team-04 is still talking
    first = {r["lead_id"]: r["ts"] for r in _rows(w, "SELECT lead_id, MIN(ts) AS ts FROM exposures GROUP BY lead_id")}
    assert all(c["ts"] >= first[k] for k, c in calls.items())  # metrics would drop a call before its exposure
    q = next(r for r in w.fake.requests if r.url.path.endswith("/attempts")).url.params
    assert (q["start_datetime"], q["end_datetime"]) == ("2026-10-10T13:20:00Z", "2026-10-10T13:50:00Z")  # UTC
    assert any(r.url.raw_path.endswith(b"/api/analytics/v1/org-1/ws-1/Vani-1234/transcripts/20261010/abc%3A01")
               for r in w.fake.requests)
    saved = sorted(models.TRANSCRIPTS_DIR.glob("*.json"))
    assert len(saved) == 2
    out = capsys.readouterr().out
    assert "TRAI NDNC" in out and "ended by user" in out and "team-04 (B): connected, not ended yet" in out
    assert "4 calls placed, 4 listed by Sarvam; 3 outcomes recorded now, 1 provisional (waiting for the agent's " \
        "disposition), 1 still ringing or talking, 2 transcripts saved" in out

    before = (_rows(w, "SELECT * FROM calls ORDER BY call_id"), [(p, p.stat().st_mtime_ns) for p in saved])
    n_requests = len(w.fake.requests)
    assert live.main(["fetch"]) == 0
    assert (_rows(w, "SELECT * FROM calls ORDER BY call_id"), [(p, p.stat().st_mtime_ns) for p in saved]) == before
    assert not any("/transcripts/" in r.url.path for r in w.fake.requests[n_requests:])  # none fetched again
    assert "0 outcomes recorded now" in capsys.readouterr().out


def test_a_late_disposition_corrects_its_provisional_outcome(w, capsys):
    _started(w)
    live.main(["call", "--yes-place-real-calls"])
    w.now = NOW + timedelta(minutes=10)
    live.main(["fetch"])
    assert "Abruptly Disconnected Call, provisional: no disposition from the agent yet" in capsys.readouterr().out
    w.fake.outcomes["team-03"]["agent_variables"] = {"disposition": "BL Enriched"}  # the agent's extraction lands
    w.now = NOW + timedelta(minutes=15)
    live.main(["fetch"])
    assert "team-03 (B): BL Enriched now (was Abruptly Disconnected Call)" in capsys.readouterr().out
    assert _rows(w, "SELECT disposition FROM calls WHERE lead_id = 'team-03'") == [{"disposition": "BL Enriched"}]
    assert {r["lead_id"]: r["outcome"] for r in _rows(w, "SELECT lead_id, outcome FROM live_attempts")} == {
        "team-01": "final", "team-02": "final", "team-03": "final", "team-04": None}  # team-04 still talks


def test_a_live_transcript_is_real_content_with_no_phone_number(w):
    _started(w)
    live.main(["call", "--yes-place-real-calls"])
    w.now = NOW + timedelta(minutes=10)
    live.main(["fetch"])
    aid = next(a for a, lead in w.fake.placed.items() if lead == "team-01")
    t = load(models.TRANSCRIPTS_DIR / f"{aid}.json", Transcript)
    assert (t.source, t.vendor, t.provider, t.language, t.duration_s) == ("live", "SARVAM", "sarvam", "hi", 45.0)
    exposure = _rows(w, "SELECT variant FROM exposures WHERE lead_id = 'team-01'")[0]["variant"]
    assert t.variant == exposure and t.prompt_version == "live" and t.meta["lead_id"] == "team-01"
    assert t.meta["agent_version"] == {"A": "1", "B": "2"}[exposure]
    assert [(x.speaker, x.text) for x in t.turns] == [
        ("bot", "Namaste, main Vani bol rahi hoon. Aapko kitni quantity chahiye?"),
        ("buyer", "Do sau piece. Mera number [number] hai, call back karna.")]
    assert models.is_real(t)  # the judge sends it to the model provider only on the user's go-ahead
    raw = (live.RAW_DIR / f"{aid}.json").read_text(encoding="utf-8")
    assert "user_phone_number" not in raw and "[number]" in raw
    # The allowlist is the only place a number lives: not in any file the loop wrote, nor in the store.
    files = [p for p in models.LOOP_DIR.rglob("*") if p.is_file()]
    with w.conn() as conn:
        dump = "\n".join(conn.iterdump())
    for digits in ("9990000001", "99900 00001", "9990000002", "9990000003", "9990000004"):
        assert digits not in dump and not any(digits in p.read_text(encoding="utf-8") for p in files), digits


def test_a_transcript_comes_with_its_id_sent_either_way_and_as_plain_text(w):
    _started(w)

    def encoded_only(request):  # a Sarvam that wants the id's "/" as %2F, and answers in plain text
        if b"%2F" not in request.url.raw_path:
            return httpx.Response(404, json={"detail": "Not Found"})
        return httpx.Response(200, text="Agent: Namaste ji\nUser: Haan, mera number 99900 00001 hai\n")
    w.fake.transcript = encoded_only
    live.main(["call", "--yes-place-real-calls"])
    w.now = NOW + timedelta(minutes=10)
    live.main(["fetch"])
    aid = next(a for a, lead in w.fake.placed.items() if lead == "team-01")
    t = load(models.TRANSCRIPTS_DIR / f"{aid}.json", Transcript)
    assert [(x.speaker, x.text) for x in t.turns] == [("bot", "Namaste ji"),
                                                      ("buyer", "Haan, mera number [number] hai")]
    assert "99900" not in (live.RAW_DIR / f"{aid}.json").read_text(encoding="utf-8")


def test_no_phone_number_survives_scrubbing_or_an_error_message():
    raw = {"+919990000001": {"to": 919990000001, "n": 919990000001.0, "secs": 45.0, "at": "2026-10-10T13:31:00Z"},
           "user_phone_number": "+919990000001", "text": "call 99900 00001", "id": "20261010/abc:01"}
    assert live._scrub(raw) == {"[number]": {"to": "[number]", "n": "[number]", "secs": 45.0,
                                             "at": "2026-10-10T13:31:00Z"},
                                "text": "call [number]", "id": "20261010/abc:01"}
    echoed = httpx.Response(422, text="x" * 293 + '"+919990000001" is not valid')  # the number crosses the cut
    assert "9990" not in live._text(echoed)


def test_the_judge_grades_live_calls_on_the_go_ahead(w):
    _started(w)
    live.main(["call", "--yes-place-real-calls"])
    w.now = NOW + timedelta(minutes=10)
    live.main(["fetch"])

    def stub(model, messages, schema):
        ids = schema["properties"]["verdicts"]["items"]["properties"]["rubric_id"]["enum"]
        return {"verdicts": [{"rubric_id": i, "grade": "pass", "confidence": 0.9, "reason": "invented",
                              "evidence": [{"turn": 0, "quote": "kitni quantity"}]} for i in ids],
                "disposition": "BL Enriched", "risk_flags": [], "summary": "invented"}
    llm.set_stub(stub)
    assert judge.main(["audit", "--source", "live"]) == 2  # real call content: refused without the flag
    with pytest.raises(SystemExit):  # nor with a prefix of it
        judge.main(["audit", "--source", "live", "--yes"])
    assert judge.main(["audit", "--source", "live", "--yes-send-real-transcripts"]) == 0
    assert sorted(p.stem for p in (models.AUDITS_DIR / "live").glob("*.json")) == sorted(
        a for a, lead in w.fake.placed.items() if lead in ("team-01", "team-03"))


LABELS = ("BL Enriched", "BL Approved", "Abruptly Disconnected Call")  # the buyer template's


@pytest.mark.parametrize("item,label", [
    (dict(connectivity_status="connected", agent_variables={"disposition": "BL Approved"}), "BL Approved"),
    (dict(connectivity_status="Connected", agent_variables={"disposition": " bl enriched "}), "BL Enriched"),
    (dict(connectivity_status="connected", ended_by="user", duration_in_seconds=8.0,
          agent_variables={"disposition": "BL Enriched"}), "BL Enriched"),  # a success is a success
    (dict(connectivity_status="connected", ended_by="user", duration_in_seconds=10.0,
          agent_variables={"disposition": "No Enrichment Scope"}), "Abruptly Disconnected Call"),
    (dict(connectivity_status="connected", ended_by="user", duration_in_seconds=10.0), "Abruptly Disconnected Call"),
    (dict(connectivity_status="connected", ended_by="customer_hangup", duration_in_seconds=9.0),
     "Abruptly Disconnected Call"),
    (dict(connectivity_status="connected", ended_by="agent", duration_in_seconds=10.0), "No Enrichment Scope"),
    (dict(connectivity_status="connected", ended_by="caller", duration_in_seconds=9.0), "No Enrichment Scope"),
    (dict(connectivity_status="connected", ended_by="user", duration_in_seconds=30.0,
          agent_variables={"disposition": "BL Generated"}), "No Enrichment Scope"),
    (dict(connectivity_status="busy"), "Not Connected"),
    (dict(connectivity_status="failed", failure_reason="exotel: Phone number is registered under TRAI NDNC",
          agent_variables={"disposition": "BL Enriched"}), "Not Connected"),
])
def test_disposition_mapping(item, label):
    placed = {"experiment_id": "vani-live-c1", "lead_id": "team-01", "ts": "2026-10-10T19:00:00.000000"}
    row, _ = live.call_row({"attempt_id": "a-1", "end_datetime": "2026-10-10T13:35:00Z", **item}, placed, LABELS,
                           datetime(2026, 10, 10, 19, 10))
    assert (row["disposition"], row["answered"]) == (label, int(item["connectivity_status"].lower() == "connected"))


def test_an_attempt_is_recorded_once_it_ended_and_final_once_the_agent_has_spoken():
    placed = {"experiment_id": "e", "lead_id": "l", "ts": "2026-10-10T19:00:00.000000"}
    ended = dict(attempt_id="a", connectivity_status="connected", end_datetime="2026-10-10T13:35:00Z")  # 19:05 IST
    at = lambda m: datetime(2026, 10, 10, 19, 5) + timedelta(minutes=m)  # noqa: E731
    row = lambda x, m: live.call_row(x, placed, LABELS, at(m))  # noqa: E731
    assert row({"attempt_id": "a"}, 9) is None  # ringing: no status yet
    talking = {**ended, "end_datetime": None, "start_datetime": "2026-10-10T13:34:00Z", "duration_in_seconds": 60.0}
    assert row(talking, 9) is None  # no end time: still talking, whatever the duration says
    for unknown in ("in_progress", "ringing", "initiated"):  # statuses Sarvam does not document wait...
        assert row({"attempt_id": "a", "connectivity_status": unknown}, 9) is None
    late = row({"attempt_id": "a", "connectivity_status": "initiated", "failure_reason": "exotel: x"}, 9)
    assert late[0]["disposition"] == "Not Connected" and late[1]  # ...until they come with a reason or an end
    first, final = row(ended, 1)
    assert (first["disposition"], final) == ("No Enrichment Scope", False)  # provisional: the agent may still speak
    assert row(ended, 30) == (first, True)  # no disposition 30 min after the end: the fallback is final
    labelled = {**ended, "agent_variables": {"disposition": "BL Generated"}}
    assert row(labelled, 0)[1]  # the agent has spoken: nothing to wait for
    for stamp in ("2026-10-10T13:36:00Z", "2026-10-10T13:36:00", "2026-10-10T19:06:00+05:30"):
        assert row({**labelled, "start_datetime": stamp}, 9)[0]["ts"] == datetime(2026, 10, 10, 19, 6)
    assert row({**labelled, "start_datetime": "2026-10-10T19:06:00"}, 9)[0]["ts"] == at(9)  # IST read as UTC: now


def test_transcript_turns_are_found_in_any_shape():
    flat = [{"speaker": "Agent", "text": "Namaste"}, {"speaker": "customer", "text": "Haan"}]
    assert [t.speaker for t in live._turns(flat)] == ["bot", "buyer"]
    nested = {"data": {"meta": {"n": 2}, "messages": [{"role": "assistant", "en_text": "Hello"},
                                                      {"role": "tool", "content": "x"}]}}
    assert [(t.speaker, t.text) for t in live._turns(nested)] == [("bot", "Hello")]
    text = {"summary": "short", "transcript_text": "Agent: Namaste ji\nUser: Pipe chahiye\n\nnot a turn"}
    assert [(t.speaker, t.text) for t in live._turns(text)] == [("bot", "Namaste ji"), ("buyer", "Pipe chahiye")]
    assert live._turns({}) == () and live._turns({"transcript": []}) == ()


def test_look_follows_the_plan_and_writes_the_split_report(w, capsys):
    _started(w)
    live.main(["call", "--yes-place-real-calls"])
    w.now = NOW + timedelta(minutes=10)
    live.main(["fetch"])
    capsys.readouterr()
    assert live.main(["look"]) == 0
    out = capsys.readouterr().out
    assert "4 leads assigned, 2 answered" in out and "verdict: no look due" in out  # 2 matured B leads, t << 0.2
    assert "Assigned leads (A 1 / B 3)" in out and (live.LIVE_DIR / "split_vani-live-c1.md").exists()  # git-ignored
    with w.conn() as conn:
        s = store.get_state(conn, "vani-live-c1")
        assert (s.state, s.looks_done, s.updated_ts_sim) == (State.RUNNING, 0, w.now)
    w.now = NOW + timedelta(days=15)  # past the 14-day window: the final look must decide...
    live.main(["look"])
    assert "a look is due, but it waits for 1 provisional outcome" in capsys.readouterr().out  # ...on final outcomes
    with w.conn() as conn:
        assert store.get_state(conn, "vani-live-c1").looks_done == 0
    live.main(["fetch"])
    assert "no disposition from the agent in 30 min: Abruptly Disconnected Call is final" in capsys.readouterr().out
    live.main(["look"])
    with w.conn() as conn:
        s, ds = store.get_state(conn, "vani-live-c1"), store.read_decisions(conn, "vani-live-c1")
    assert s.state not in (State.RUNNING, State.DRAFT) and s.looks_done == 1 and ds[-1].kind == "state_change"
    assert f"look 1 of 7: {s.state.value} -> {s.state.value}" in capsys.readouterr().out
    live.main(["status"])
    out = capsys.readouterr().out
    assert f"vani-live-c1: {s.state.value}" in out and "B: assigned 3, called 3, outcomes back 2, connected 2" in out
