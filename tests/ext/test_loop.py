"""The Vani Loop driver (ext/loop.py): registry, cycle stages, retries, the go-ahead stop, the CLI.

Listen, judge and improve are replaced at loop's module-level seams with invented calls, audits and
fixes. The prove stage runs the REAL base engine (runner, simulator, metrics ... decision) into a temp
store, on an invented buyer_logs pool (no data/ needed).
"""

import functools
import hashlib
import types

import pytest
import yaml

from abtest import decision, simulator, store
from abtest.contracts import BASE_PROMPT, ROOT, State
from abtest.ext import loop, models
from abtest.ext.models import (CallAudit, Fix, GateCheck, GateResult, PromptVersion, ReplayResult, RootCause,
                               RubricVerdict, Transcript, Turn, load, save)

REAL_CALLS, REAL_AUDITS = loop.calls, loop.audits  # before the world fixture replaces them
SIM = """true_effect: {primary_rel_lift: 0.5, secondary_change: 0.0}
reference: {from: null, to: null, bot_version: null}
volume: {daily_leads: 600, scale: 1.0}
fault: {drop_b_exposure_rate: 0.0}
"""


def _pool_csv(path):
    """An invented buyer_logs export with the columns data.load reads: 400 buyers over 5 days, every
    one answered once (a quarter after a missed call), half of them BL Enriched, none abruptly
    disconnected (so the guardrail clears once B has a few hundred leads)."""
    rows = ["fk_leap_call_records_id,fk_glusr_usr_id,entered_on,disposition_remarks,is_call_answered"]
    cid = 0
    for lead in range(400):
        calls = [("Not Answered", 0)] if lead % 4 == 0 else []
        calls.append(("BL Enriched", 1) if lead % 2 else ("Not Interested", 1))
        for k, (disp, answered) in enumerate(calls):
            cid += 1
            rows.append(f"{cid},{1000 + lead},{5 + lead % 5:02d}/10/26 {9 + lead % 9 + k:02d}:{lead * 7 % 60:02d},"
                        f"{disp},{answered}")
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def transcript(call_id, source="fixture", version="v1"):
    return Transcript(call_id=call_id, source=source, prompt_version=version, provider="fixture",
                      turns=(Turn(speaker="bot", text="Namaste, aapko kitni quantity chahiye?"),
                             Turn(speaker="buyer", text="Do sau piece chahiye.")))


def audit(t, overall="fatal"):
    v = RubricVerdict(rubric_id="quantity_probing", rubric_version=1, grade=overall, confidence=0.9,
                      reason="invented reason")
    return CallAudit(call_id=t.call_id, prompt_version=t.prompt_version, overall=overall, verdicts=(v,),
                     model="stub", rubric_set_hash="h")


def cause(k, version="v1", type_="prompt", priority=None):
    return RootCause(id=f"rc-quantity_probing-{k}", rubric_id="quantity_probing", name=f"Invented cause {k}",
                     description="invented", type=type_, n_audited=4, n_calls=2, n_fatal=1, n_non_fatal=1,
                     frequency=0.5, severity=0.67, priority=priority if priority is not None else 40.0 - k,
                     example_call_ids=("call-1",), prompt_version=version)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Fakes at every seam, a temp engine store, an invented pool and sim. Knobs: w.gate (results to
    return in order, then pass), w.replay (fail rates (a, b) in order, then a gain), w.calls[version]."""
    w = types.SimpleNamespace(calls={"v1": [transcript(f"call-{i}") for i in range(4)]}, causes={},
                              fixes={}, gate=[], replay=[], seen=[])
    _pool_csv(tmp_path / "Call_Logs_Hackathon.csv")
    (tmp_path / "sim.yaml").write_text(SIM, encoding="utf-8")
    monkeypatch.setenv("LOOP_DEMO_SIM", str(tmp_path / "sim.yaml"))
    monkeypatch.setattr(simulator, "build_pool", functools.partial(simulator.build_pool, data_dir=tmp_path))
    monkeypatch.setattr(simulator, "write_truth_report",
                        functools.partial(simulator.write_truth_report, out_dir=tmp_path))
    monkeypatch.setattr(decision, "JSONL", tmp_path / "decisions.jsonl")
    monkeypatch.setattr(loop, "DB_PATH", tmp_path / "abtest.db")

    def calls(name):
        w.seen.append(("calls", name))
        return w.calls.get(name, [])

    def judge(ts, allow_real):
        w.seen.append(("judge", len(ts), allow_real))
        return [audit(t) for t in ts]

    def rank(audits, name):
        w.seen.append(("rank", name))
        w.causes[name] = [cause(1, name), cause(2, name, "asr", 99.0), cause(3, name), cause(4, name)]
        return w.causes[name]

    def draft(rc, base, audits):
        text = (ROOT / base.path).read_text(encoding="utf-8") + f"\nInvented rule for {rc.id}.\n"
        path = models.PROMPTS_DIR / f"fx-{base.version}-{rc.id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        fix = Fix(id=path.stem, root_cause_id=rc.id, base_version=base.version, edits=(), rationale="invented",
                  prompt_b_path=str(path), prompt_b_sha256=hashlib.sha256(text.encode()).hexdigest(),
                  diff="", model="stub")
        w.fixes[fix.id] = fix
        return fix

    def gate(fix):
        passed = w.gate.pop(0) if w.gate else True
        return GateResult(passed=passed, model="stub", checks=(
            GateCheck(id="weakens_safety_rule", question="?", probability=0.1 if passed else 0.9, blocking=True),))

    def replay(fix, rc, audits, allow_real):
        a, b = w.replay.pop(0) if w.replay else (0.75, 0.25)
        return ReplayResult(rubric_id=rc.rubric_id, n_cases=4, fail_rate_a=a, fail_rate_b=b, fail_rate_real=1.0,
                            regressions={}, cases=(), seconds=0.1, models={"bot": "stub"})

    for name, fn in dict(calls=calls, _judge=judge, _rank=rank, _draft=draft, _gate=gate, _replay=replay,
                         audits=lambda name: [audit(t) for t in w.calls.get(name, [])],
                         _ranked=lambda name: w.causes.get(name, []), _fix=lambda fid: w.fixes[fid]).items():
        monkeypatch.setattr(loop, name, fn)
    return w


def _engine_state(eid):
    conn = store.connect(loop.DB_PATH)
    try:
        return store.get_state(conn, eid)
    finally:
        conn.close()


# --- registry -------------------------------------------------------------------------------

def test_registry_bootstraps_v1_on_first_use():
    assert not models.REGISTRY.exists()
    v1 = loop.production()
    assert (v1.version, v1.status, v1.parent, v1.path) == ("v1", "production", None, "prompts/base_buyer_vani.txt")
    assert v1.sha256 == hashlib.sha256(BASE_PROMPT.read_bytes()).hexdigest()
    assert load(models.REGISTRY, PromptVersion) == [v1] == loop.versions()


def test_registry_candidate_promote_reject(world):
    rc = cause(1)
    fix = loop._draft(rc, loop.production(), [])
    v2 = loop.add_candidate(fix)
    assert (v2.version, v2.status, v2.parent, v2.fix_id, v2.path) == ("v2", "candidate", "v1", fix.id, fix.prompt_b_path)
    assert loop.add_candidate(fix) == v2                       # idempotent per fix
    v3 = loop.add_candidate(loop._draft(cause(2), loop.production(), []))
    assert v3.version == "v3"

    assert loop.promote("v2").status == "production" and loop.promote("v2").status == "production"
    assert [v.status for v in loop.versions()] == ["retired", "production", "candidate"]
    assert loop.reject("v3").status == "rejected"
    with pytest.raises(ValueError, match="only a candidate"):
        loop.reject("v2")
    with pytest.raises(KeyError):
        loop.promote("v9")


def test_candidate_must_match_its_drafted_sha(world):
    fix = loop._draft(cause(1), loop.production(), [])
    (ROOT / fix.prompt_b_path).write_text("tampered", encoding="utf-8")
    with pytest.raises(ValueError, match="sha256"):
        loop.add_candidate(fix)


# --- one cycle end to end, with the real engine -------------------------------------------------

def test_cycle_runs_every_stage_then_approval_promotes_and_opens_cycle_2(world):
    c = loop.advance()
    assert (c.n, c.stage, c.outcome, c.experiment_id) == (1, "approval", "PENDING_APPROVAL", "vani-loop-c1")
    assert (c.root_cause_id, c.candidate_version) == ("rc-quantity_probing-1", "v2")  # top prompt cause, not asr
    assert [s[0] for s in world.seen if s[0] in ("judge", "rank")] == ["judge", "rank"]
    assert loop.next_action(c).startswith("waiting for approval: vani-loop-c1")
    assert _engine_state("vani-loop-c1").state == State.PENDING_APPROVAL  # a real engine run

    # the experiment: the buyer template, A = production, B = the candidate, a salt from the fix id
    cfg = yaml.safe_load((loop.EXPERIMENTS_DIR / "vani-loop-c1.yaml").read_text())
    assert cfg["id"] == "vani-loop-c1" and cfg["promotion"] == "approval"
    assert cfg["allocation"] == {"A": 0.9, "B": 0.1} and cfg["primary"]["name"] == "bl_success"
    assert cfg["secondary"]["name"] == "abrupt_disconnect"
    assert cfg["variants"] == {"A": {"base": "prompts/base_buyer_vani.txt"},
                               "B": {"patch": {"mode": "full", "path": loop.version("v2").path}}}
    assert cfg["salt"] == hashlib.sha256(c.fix_id.encode()).hexdigest()[:6]
    meta = load(loop.EXPERIMENTS_DIR / "vani-loop-c1.json")
    assert meta["assumed_effect"] == "+50% BL success for B (assumed; simulated traffic)"
    assert meta["scenario"]["seed"] == loop.SEED + 1 and meta["cycle"] == 1

    assert loop.advance() == c  # waiting: nothing re-runs
    assert sum(s[0] == "judge" for s in world.seen) == 1

    state = loop.approve("vani-loop-c1", "kartik", True)
    assert state.state == State.PROMOTED
    one, two = loop.cycles()
    assert (one.stage, one.outcome) == ("done", "PROMOTED")
    assert (two.n, two.stage, two.production_version) == (2, "listen", "v2")
    assert loop.production().version == "v2" and loop.version("v1").status == "retired"
    assert loop.current_cycle() == two


def test_rejected_experiment_keeps_production_and_next_cycle_skips_the_cause(world):
    loop.advance()
    loop.approve("vani-loop-c1", "kartik", False)
    one, two = loop.cycles()
    assert (one.stage, one.outcome) == ("done", "REJECTED") and loop.version("v2").status == "rejected"
    assert (two.production_version, two.stage) == ("v1", "listen") and loop.production().version == "v1"
    c = loop.advance(until="improve")
    assert c.root_cause_id == "rc-quantity_probing-3"  # 1 was tried by cycle 1; 2 is asr
    assert [s[0] for s in world.seen].count("rank") == 1  # the same ranking, so tried ids mean the same causes


def test_until_stops_after_that_stage_and_reruns_do_nothing(world):
    c = loop.advance(until="judge")
    assert c.stage == "improve"
    assert loop.advance(until="judge") == c  # already past judge
    assert [s[0] for s in world.seen].count("judge") == 1


# --- failed tries ------------------------------------------------------------------------------

def test_gate_blocked_rejects_the_candidate_and_tries_the_next_cause(world):
    world.gate = [False]
    c = loop.advance(until="replay")
    assert c.stage == "prove" and c.root_cause_id == "rc-quantity_probing-3" and c.candidate_version == "v3"
    assert c.outcome == "GATE_BLOCKED"
    assert loop.version("v2").status == "rejected" and loop.version("v3").status == "candidate"
    assert any(n.startswith("try 1: rc-quantity_probing-1 -> GATE_BLOCKED") for n in c.notes)


def test_replay_without_gain_rejects_and_tries_the_next_cause(world):
    world.replay = [(0.5, 0.5)]
    c = loop.advance(until="replay")
    assert c.stage == "gate" and c.outcome == "REPLAY_NO_GAIN" and c.root_cause_id == "rc-quantity_probing-3"
    assert loop.version("v2").status == "rejected"
    c = loop.advance()
    assert c.stage == "approval" and c.outcome == "PENDING_APPROVAL" and c.candidate_version == "v3"


def test_three_failed_tries_end_the_cycle_then_no_cause_is_left(world):
    world.gate = [False, False, False]
    c = loop.advance()
    one = loop.cycles()[0]
    assert (one.stage, one.outcome) == ("done", "GATE_BLOCKED")
    assert [v.status for v in loop.versions()] == ["production", "rejected", "rejected", "rejected"]
    assert (c.n, c.stage, c.production_version) == (2, "listen", "v1")
    c = loop.advance()
    assert c.stage == "blocked" and "no untried prompt or flow root cause" in c.notes[-1]


def test_invalid_fix_is_a_failed_try(world, monkeypatch):
    real_draft = loop._draft

    def draft(rc, base, audits):
        if rc.id.endswith("-1"):
            raise ValueError("find occurs 0 times")
        return real_draft(rc, base, audits)
    monkeypatch.setattr(loop, "_draft", draft)
    c = loop.advance(until="gate")
    assert c.outcome == "FIX_INVALID" and c.root_cause_id == "rc-quantity_probing-3"
    assert [v.version for v in loop.versions()] == ["v1"]  # the invalid try registered no candidate


# --- privacy: real transcripts wait for the go-ahead ----------------------------------------------

def test_real_transcripts_wait_for_the_go_ahead(world):
    world.calls["v1"] = [transcript(f"call-{i}", source="production") for i in range(3)]
    c = loop.advance()
    assert c.stage == "judge" and c.notes[-1] == loop.GO_AHEAD == loop.next_action(c)
    assert not any(s[0] == "judge" for s in world.seen)  # nothing was sent
    c = loop.advance(until="judge", allow_real=True)
    assert c.stage == "improve" and ("judge", 3, True) in world.seen
    assert loop.advance().notes[-1] == loop.GO_AHEAD  # improve sends quotes: it waits too


def test_no_transcripts_wait_at_listen(world):
    world.calls["v1"] = []
    c = loop.advance()
    assert c.stage == "listen" and c.notes[-1].startswith("waiting for transcripts of v1")
    assert loop.advance().notes == c.notes  # the same note is not repeated


# --- the real listen glue (production calls; a later version's replay transcripts) ---------------

def test_calls_and_audits_of_v1_are_the_corpus_and_of_v2_its_replay_b_transcripts(monkeypatch):
    from abtest.ext import improve
    save(models.TRANSCRIPTS_DIR / "p1.json", transcript("p1", source="production"))
    save(models.TRANSCRIPTS_DIR / "a1.json", transcript("a1", source="audited"))
    for t in (transcript("p1"), transcript("a1")):
        save(models.AUDITS_DIR / "v1" / f"{t.call_id}.json", audit(t))
    # Default corpus: the buyer-side calls the QA team audited (they match the buyer-side prompt).
    assert [t.call_id for t in loop.calls("v1")] == ["a1"]
    assert [a.call_id for a in loop.audits("v1")] == ["a1"]
    monkeypatch.setenv("LOOP_CORPUS", "production")
    assert [t.call_id for t in loop.calls("v1")] == ["p1"]
    assert [a.call_id for a in loop.audits("v1")] == ["p1"]
    monkeypatch.delenv("LOOP_CORPUS")

    rep = improve.REPLAY_DIR / "fx-1"
    save(rep / "p1-A.json", transcript("p1-A", source="replay", version="v1"))
    save(rep / "p1-B.json", b := transcript("p1-B", source="replay", version="fx-1"))
    save(rep / "p1-B.audit.json", audit(b, "pass"))
    v2 = PromptVersion(version="v2", path="x", sha256="0", parent="v1", fix_id="fx-1", status="production",
                       created_ts="")
    save(models.REGISTRY, [*loop.versions(), v2])
    assert [(t.call_id, t.source, t.prompt_version) for t in loop.calls("v2")] == [("p1-B", "replay", "v2")]
    assert [(a.call_id, a.overall, a.prompt_version) for a in loop.audits("v2")] == [("p1-B", "pass", "v2")]


def test_listen_on_a_later_version_labels_and_files_its_replay_transcripts(world):
    world.calls["v2"] = [transcript("call-1-B", source="replay", version="v2")]
    loop.advance()
    loop.approve("vani-loop-c1", "kartik", True)
    c = loop.advance(until="listen")
    assert c.stage == "judge" and "no live calls yet" in c.notes[-1] and "source=replay" in c.notes[-1]
    assert load(models.TRANSCRIPTS_DIR / "call-1-B.json", Transcript).source == "replay"


# --- concurrency and CLI ----------------------------------------------------------------------

def test_one_writer_at_a_time(world):
    with loop._exclusive():
        assert loop.busy()
        with pytest.raises(loop.Busy):
            loop.advance()
    assert not loop.busy()


def test_cli_advance_status_and_approve(world, capsys):
    assert loop.main(["advance", "--until", "judge"]) == 0
    out = capsys.readouterr().out
    assert "cycle 1 on v1: improve" in out and "next: advance: rank v1's root causes" in out
    assert loop.main(["advance"]) == 0
    assert loop.main(["approve", "vani-loop-c1", "--actor", "kartik"]) == 0
    out = capsys.readouterr().out
    assert "registry: v1 retired, v2 production" in out and "cycle 2 on v2: listen" in out
    assert loop.main(["approve", "vani-loop-c1", "--actor", "kartik"]) == 1  # not PENDING_APPROVAL any more
    assert "only PENDING_APPROVAL" in capsys.readouterr().err
    assert loop.main(["status"]) == 0


# --- recovery and integrity (the Codex review's findings) -------------------------------------------

def test_a_later_versions_judge_stage_keeps_its_fresh_grades(world, monkeypatch):
    """The judge files no audit of a replay call; the cycle keeps its own, so rubric edits count."""
    from abtest.ext import improve
    monkeypatch.setattr(loop, "calls", REAL_CALLS)
    monkeypatch.setattr(loop, "audits", REAL_AUDITS)
    rep = improve.REPLAY_DIR / "fx-1"
    save(rep / "r-B.json", b := transcript("r-B", source="replay", version="fx-1"))
    save(rep / "r-B.audit.json", audit(b, "pass"))  # graded when B was replayed
    save(models.REGISTRY, [*loop.versions(), PromptVersion(version="v2", path="x", sha256="0", parent="v1",
                                                           fix_id="fx-1", status="production", created_ts="")])
    assert [a.overall for a in loop.audits("v2")] == ["pass"]
    c = models.Cycle(n=1, production_version="v2", stage="judge", started_ts="t", updated_ts="t")
    c = loop._judge_stage(c, allow_real=False)  # the fake judge grades fatal: the current rubrics say so
    assert c.stage == "improve" and [a.overall for a in loop.audits("v2")] == ["fatal"]


def test_judging_nothing_waits_and_partial_grading_is_said(world, monkeypatch):
    monkeypatch.setattr(loop, "_judge", lambda ts, allow_real: [])
    c = loop.advance()
    assert c.stage == "judge" and c.notes[-1].startswith("waiting: none of the 4 calls could be graded")
    monkeypatch.setattr(loop, "_judge", lambda ts, allow_real: [audit(t) for t in ts[:3]])
    c = loop.advance(until="judge")
    assert c.stage == "improve" and "3 of 4 calls graded (1 could not be graded" in c.notes[-1]


def test_a_cycle_left_done_without_a_successor_opens_one(world):
    loop.advance(until="judge")
    one = loop.cycles()[0]
    save(models.CYCLES, [one.model_copy(update={"stage": "done"})])  # as if the process died right there
    c = loop.advance(until="listen")
    assert (c.n, c.stage, c.production_version) == (2, "judge", "v1")


def test_prove_resumed_after_its_run_keeps_the_human_decision(world):
    from abtest import decision as base_decision
    c = loop.advance()
    conn = store.connect(loop.DB_PATH)
    base_decision.approve(conn, c.experiment_id, "kartik", True)  # approved before the loop heard of it...
    conn.close()
    save(models.CYCLES, [c.model_copy(update={"stage": "prove"})])  # ...and the cycle never left prove
    c = loop.advance()
    assert _engine_state("vani-loop-c1").state == State.PROMOTED  # not reset by a rerun
    assert loop.production().version == "v2" and (c.n, c.stage) == (2, "listen")


def test_a_prompt_changed_after_review_is_never_proved_or_promoted(world):
    c = loop.advance(until="replay")
    path = ROOT / loop.version(c.candidate_version).path
    good = path.read_bytes()
    path.write_bytes(good + b"\nan unreviewed edit\n")
    with pytest.raises(ValueError, match="changed after it was registered"):
        loop.advance()
    path.write_bytes(good)
    c = loop.advance()
    assert c.stage == "approval"
    path.write_bytes(good + b"\nedited while waiting for approval\n")
    with pytest.raises(ValueError, match="changed after it was registered"):
        loop.approve(c.experiment_id, "kartik", True)
    assert loop.production().version == "v1" and loop.current_cycle().stage == "rollout"


def test_bootstrap_never_replaces_a_registry_written_meanwhile(monkeypatch):
    two = [PromptVersion(version=f"v{i}", path="x", sha256="0", status="production" if i == 1 else "candidate",
                         created_ts="") for i in (1, 2)]
    save(models.REGISTRY, two)
    reads = iter([[], two])  # the reader saw no registry, then the loop wrote two versions
    monkeypatch.setattr(loop, "load", lambda path, model=None, default=None: next(reads))
    assert loop.versions() == two
    assert load(models.REGISTRY, PromptVersion) == two
