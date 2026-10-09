import csv
import json
import os

import pytest

from abtest.ext import judge, listen, llm, models, rubrics
from abtest.ext.models import CallAudit, Rubric, RubricVerdict, Transcript, Turn

# Invented call: no real customer content anywhere in these tests.
T = Transcript(call_id="t-001", source="fixture", language="hi", duration_s=15.0, turns=(
    Turn(speaker="bot", text="Namaste, kya aapko steel almirah chahiye?", start_s=0.0, end_s=3.0),
    Turn(speaker="buyer", text="Haan ji, do almirah chahiye.", start_s=3.5, end_s=5.0),
    Turn(speaker="bot", text="Ji, aapko kitne piece chahiye?", start_s=11.0, end_s=13.0),
    Turn(speaker="buyer", text="Bola na, do.", start_s=13.5, end_s=14.5),
))
R = [
    Rubric(id="looping_behavior", name="Looping Behavior", category="Conversation",
           question="Did the bot repeat itself?", qa_column="Looping Behavior"),
    Rubric(id="quantity_accuracy_numeric", name="Quantity Accuracy (Numeric)", category="Quantity",
           question="Is the captured quantity right?", qa_column="Quantity Accuracy (Numeric)", version=3),
    Rubric(id="dead_air", name="Dead air", category="Speech", question="Long bot silences?",
           needs_audio=True, root_type="platform", qa_column="Dead air"),
    Rubric(id="polite_close", name="Polite close", category="Custom", question="Polite?", builtin=False),
]


def verdict(rid, grade="pass", evidence=()):
    return {"rubric_id": rid, "grade": grade, "confidence": 0.9, "reason": "Fine.", "evidence": list(evidence)}


def reply(grades: dict, evidence=None, **extra):
    evidence = evidence or {}
    return {"verdicts": [verdict(rid, g, evidence.get(rid, ())) for rid, g in grades.items()],
            "disposition": "BL Enriched", "risk_flags": [], "summary": "Buyer wants two almirahs.", **extra}


GOOD = {"looping_behavior": "non_fatal", "quantity_accuracy_numeric": "pass", "dead_air": "na", "polite_close": "pass"}


class Recorder:
    """A stub that answers from a list of replies (the last one repeats) and records each request."""

    def __init__(self, *replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, model, messages, schema):
        self.calls.append((model, messages, schema))
        return self.replies[min(len(self.calls), len(self.replies)) - 1]


def answer_all(model, messages, schema):
    """Grades whatever rubrics the schema asks for: pass, no evidence."""
    ids = schema["properties"]["verdicts"]["items"]["properties"]["rubric_id"]["enum"]
    return reply(dict.fromkeys(ids, "pass"))


def test_audit_call_grades_every_rubric_and_saves():
    stub = Recorder(reply(GOOD, evidence={"looping_behavior": [{"turn": 2, "quote": "kitne piece chahiye"}]},
                          risk_flags=["loop", "loop", "made_up"], summary="Buyer wants\n two almirahs."))
    llm.set_stub(stub)
    a = judge.audit_call(T, R, model="m/judge")
    assert [v.rubric_id for v in a.verdicts] == [r.id for r in R]
    assert a.overall == "non_fatal"
    assert {v.rubric_id: v.rubric_version for v in a.verdicts}["quantity_accuracy_numeric"] == 3
    assert a.verdicts[0].evidence[0].model_dump() == {"turn": 2, "quote": "kitne piece chahiye"}
    assert (a.disposition, a.risk_flags, a.summary) == ("BL Enriched", ("loop",), "Buyer wants two almirahs.")
    assert (a.model, a.rubric_set_hash, a.prompt_version) == ("m/judge", rubrics.set_hash(R), "v1")
    assert models.load(judge.audit_path("t-001"), CallAudit) == a
    model, messages, schema = stub.calls[0]
    system, user = messages[0]["content"][0]["text"], messages[1]["content"]
    assert all(f"[{r.id}]" in system for r in R) and "Audio parameter" in system
    assert "[1] BUYER 3.5s: Haan ji, do almirah chahiye." in user
    assert "[2] BOT 11.0s (bot silent 6.0s): Ji, aapko kitne piece chahiye?" in user
    assert schema["properties"]["verdicts"]["items"]["properties"]["rubric_id"]["enum"] == [r.id for r in R]


def test_bad_evidence_is_dropped_and_the_grade_kept():
    bad = [{"turn": 99, "quote": "Namaste"}, {"turn": 1, "quote": "teen almirah"}, {"turn": "x", "quote": "do"},
           {"quote": "no turn"}, {"turn": 3, "quote": "  'BOLA NA,  do.'"}]
    llm.set_stub(Recorder(reply({**GOOD, "looping_behavior": "fatal"}, evidence={"looping_behavior": bad})))
    v = judge.audit_call(T, R, model="m").verdicts[0]
    assert v.grade == "fatal"
    assert [(e.turn, e.quote) for e in v.evidence] == [(3, "Bola na, do")]   # stored as the turn's own text


@pytest.mark.parametrize("broken", [
    {k: g for k, g in GOOD.items() if k != "dead_air"},           # a rubric missing
    {**GOOD, "dead_air": "terrible"},                             # an unknown grade
])
def test_missing_rubric_or_unknown_grade_gets_one_retry(broken):
    stub = Recorder(reply(broken), reply(GOOD))
    llm.set_stub(stub)
    a = judge.audit_call(T, R, model="m")
    assert len(stub.calls) == 2 and a.overall == "non_fatal"
    retry = stub.calls[1][1]
    assert retry[-2]["role"] == "assistant" and "dead_air" in retry[-1]["content"]


def test_still_unusable_after_the_retry_raises_and_saves_nothing():
    stub = Recorder({"verdicts": "nope"})
    llm.set_stub(stub)
    with pytest.raises(judge.JudgeError):
        judge.audit_call(T, R, model="m")
    assert len(stub.calls) == 2 and not judge.audit_path("t-001").exists()


def test_extra_or_repeated_rubrics_are_ignored_and_spellings_normalised():
    data = reply({**GOOD, "looping_behavior": "Non-Fatal", "dead_air": "N/A", "not_a_rubric": "fatal"})
    data["verdicts"].append(verdict("looping_behavior", "non_fatal"))   # an exact repeat is harmless
    stub = Recorder(data)
    llm.set_stub(stub)
    a = judge.audit_call(T, R, model="m")
    assert len(stub.calls) == 1
    assert [v.grade for v in a.verdicts] == ["non_fatal", "pass", "na", "pass"]


def test_a_rubric_graded_twice_differently_gets_the_retry():
    data = reply(GOOD)
    data["verdicts"].append(verdict("looping_behavior", "fatal"))       # pass-or-fatal: never pick one
    stub = Recorder(data, reply({**GOOD, "looping_behavior": "fatal"}))
    llm.set_stub(stub)
    assert judge.audit_call(T, R, model="m").verdicts[0].grade == "fatal"
    assert len(stub.calls) == 2 and "graded twice" in stub.calls[1][1][-1]["content"]


@pytest.mark.parametrize("grades,overall", [
    (["na", "na", "na", "na"], "pass"),
    (["pass", "non_fatal", "na", "pass"], "non_fatal"),
    (["pass", "pass", "fatal", "pass"], "pass"),        # dead_air is audio: recorded na, never moves overall
    (["non_fatal", "fatal", "na", "pass"], "fatal"),
])
def test_overall_is_the_worst_grade_and_na_is_ignored(grades, overall):
    llm.set_stub(Recorder(reply(dict(zip(GOOD, grades)))))
    assert judge.audit_call(T, R, model="m").overall == overall


def test_audit_many_skips_calls_already_audited_with_the_same_rubrics_and_model():
    t2 = T.model_copy(update={"call_id": "t-002"})
    stub = Recorder(reply(GOOD))
    llm.set_stub(stub)
    first = judge.audit_many([T, t2], R, model="m", workers=2)
    assert [a.call_id for a in first] == ["t-001", "t-002"] and len(stub.calls) == 2
    assert judge.audit_many([T, t2], R, model="m") == first and len(stub.calls) == 2
    judge.audit_many([T], R, model="other")                             # another model: graded again
    edited = [R[0].model_copy(update={"guidance": "Stricter."}), *R[1:]]
    judge.audit_many([T], edited, model="m")                            # other rubric text: graded again
    assert len(stub.calls) == 4
    assert {a.call_id for a in judge.load_audits("v1")} == {"t-001", "t-002"}


def test_audit_many_reports_a_failing_call_and_keeps_going(capsys):
    def stub(model, messages, schema):
        if "Bola na" in messages[1]["content"] and "kursi" in messages[1]["content"]:
            raise RuntimeError("provider down")
        return reply(GOOD)
    t2 = T.model_copy(update={"call_id": "t-002", "turns": (*T.turns, Turn(speaker="buyer", text="kursi bhi"))})
    llm.set_stub(stub)
    assert [a.call_id for a in judge.audit_many([T, t2], R, model="m")] == ["t-001"]
    assert "t-002 failed: RuntimeError" in capsys.readouterr().err


def test_replay_never_reuses_a_saved_real_audit():
    stub = Recorder(reply(GOOD))
    llm.set_stub(stub)
    judge.audit_many([T], R, model="m")                                    # the real call, saved
    judge.audit_many([T.model_copy(update={"source": "replay"})], R, model="m")
    assert len(stub.calls) == 2


def test_a_call_transcribed_again_is_audited_again():
    stub = Recorder(reply(GOOD))
    llm.set_stub(stub)
    judge.audit_many([T], R, model="m")
    tp = models.TRANSCRIPTS_DIR / "t-001.json"
    models.save(tp, T)
    later = judge.audit_path("t-001").stat().st_mtime + 10
    os.utime(tp, (later, later))
    judge.audit_many([T], R, model="m")
    assert len(stub.calls) == 2


def test_replay_transcripts_are_graded_but_not_saved():
    llm.set_stub(Recorder(reply(GOOD)))
    judge.audit_call(T.model_copy(update={"source": "replay"}), R, model="m")
    assert not judge.audit_path("t-001").exists()


def test_unsafe_call_ids_are_refused():
    llm.set_stub(Recorder(reply(GOOD)))
    with pytest.raises(ValueError):
        judge.audit_call(T.model_copy(update={"call_id": "../escape"}), R, model="m")


def test_test_rubric_grades_one_rubric_and_saves_nothing():
    stub = Recorder({"verdicts": [verdict("polite_close", "fatal")]})
    llm.set_stub(stub)
    v = judge.test_rubric(R[3], T, model="m")
    assert isinstance(v, RubricVerdict) and (v.rubric_id, v.grade) == ("polite_close", "fatal")
    assert set(stub.calls[0][2]["properties"]) == {"verdicts"}
    assert not models.AUDITS_DIR.exists()
    llm.set_stub(Recorder({"verdicts": [verdict("draft", "pass")]}))
    assert judge.test_rubric({"name": "Draft", "question": "Polite?"}, T, model="m").rubric_id == "draft"


# --- agreement --------------------------------------------------------------------------

def audit(call_id, overall, grade):
    v = RubricVerdict(rubric_id="looping_behavior", rubric_version=1, grade=grade, confidence=1.0, reason="")
    return CallAudit(call_id=call_id, prompt_version="v1", overall=overall, verdicts=(v,), model="m",
                     rubric_set_hash="h")


HUMAN_LOOP = ["pass", "pass", "fatal", "fatal", "na"]
JUDGE_LOOP = ["pass", "fatal", "fatal", "fatal", "na"]
HUMAN_OVERALL = ["pass", "non_fatal", "fatal", "fatal", "pass"]
JUDGE_OVERALL = ["pass", "fatal", "fatal", "non_fatal", "pass"]
AUDITS = [audit(f"c{i}", o, g) for i, (o, g) in enumerate(zip(JUDGE_OVERALL, JUDGE_LOOP))] + [
    audit("not-in-human", "fatal", "fatal")]
HUMAN = {f"c{i}": {"overall": o, "grades": {"Looping Behavior": g}}
         for i, (o, g) in enumerate(zip(HUMAN_OVERALL, HUMAN_LOOP))}


def test_agreement_matches_hand_computed_kappa():
    rep = judge.agreement(AUDITS, HUMAN, R, held_out=True)
    (row,) = rep.rows                       # only rubrics with a qa_column and pairs get a row
    # Looping: observed 4/5 = 0.8; human marginals pass 2, fatal 2, na 1; judge pass 1, fatal 3, na 1;
    # chance = (2*1 + 2*3 + 1*1) / 25 = 0.36; kappa = (0.8 - 0.36) / (1 - 0.36) = 0.6875.
    assert (row.rubric_id, row.n, row.accuracy, row.fatal_recall) == ("looping_behavior", 5, 0.8, 1.0)
    assert row.kappa == pytest.approx(0.6875)
    assert row.confusion["pass"] == {"pass": 1, "non_fatal": 0, "fatal": 1, "na": 0}
    assert row.confusion["fatal"]["fatal"] == 2 and row.confusion["na"]["na"] == 1
    # Overall, like for like: both sides are the worst grade over the shared text-observable QA
    # parameters (looping and quantity here; dead_air is audio, polite_close has no QA column). Human
    # pass, pass, fatal, fatal, pass vs judge pass, fatal, fatal, fatal, pass: 4 of 5 agree;
    # chance = (3*2 + 2*3) / 25 = 0.48; kappa = (0.8 - 0.48) / 0.52 = 0.6154.
    assert (rep.n_calls, rep.overall_accuracy, rep.model, rep.held_out) == (5, 0.8, "m", True)
    assert rep.overall_kappa == pytest.approx(0.32 / 0.52)
    assert rep.fatal_vs_rest_accuracy == pytest.approx(0.8)   # fatal flags agree on calls 0, 2, 3 and 4


def test_kappa_is_none_when_undefined():
    rep = judge.agreement([audit("c0", "pass", "pass")], {"c0": {"overall": "pass", "grades": {"Looping Behavior": "pass"}}},
                          R, held_out=False)
    assert (rep.overall_accuracy, rep.overall_kappa, rep.rows[0].kappa, rep.rows[0].fatal_recall) == (1.0, None, None, None)
    with pytest.raises(ValueError):
        judge.agreement(AUDITS, {}, R, held_out=True)


def test_agreement_report_has_aggregate_numbers_only(tmp_path):
    md = judge.write_agreement_report(judge.agreement(AUDITS, HUMAN, R, held_out=True), tmp_path / "a.md").read_text()
    assert "80.0%" in md and "0.69" in md and "0.62" in md and "Looping Behavior" in md
    assert not any(c in md for c in ("c0", "c1", "not-in-human"))


def write_sheet(path):
    """An invented QA sheet in the real one's shape: messy header spacing, escaped module column."""
    header = ["Vendor", "fk\\_gl\\_module\\_id", "Call  Recording", "Overall Quality", "Looping\nBehavior",
              "Product Change/ No Product Accuracy"]
    rows = [
        ["SQUADSTACK", "INBOUND", "https://bucket.s3.amazonaws.com/ABC123.mp3", "Non-Fatal", "Pass",
         "Non-Fatal- Product Change Case"],
        ["SQUADSTACK", "INBOUND", "https://bucket.s3.amazonaws.com/XYZ789.mp3", "Fatal", "Fatal", "Pass- No Product Case"],
        ["SARVAM", "INBOUND", "not downloadable", "Pass", "Pass", "NA"],
        ["SQUADSTACK", "OUTBOUND", "https://bucket.s3.amazonaws.com/OUT1.mp3", "Pass", "Pass", "NA"],
    ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f).writerows([header, *rows])


def test_human_labels_come_from_the_squadstack_inbound_rows(tmp_path, monkeypatch):
    write_sheet(tmp_path / "sheet.csv")
    monkeypatch.setattr(listen, "AUDIT_CSV", tmp_path / "sheet.csv")
    labels = judge.human_labels()
    assert set(labels) == {"ABC123", "XYZ789"}
    assert labels["ABC123"]["overall"] == "non_fatal" and labels["XYZ789"]["overall"] == "fatal"
    assert labels["ABC123"]["grades"]["Product Change/ No Product Accuracy"] == "non_fatal"
    assert labels["XYZ789"]["grades"]["Looping Behavior"] == "fatal"
    assert "remarks" not in labels["ABC123"]


# --- CLI ----------------------------------------------------------------------------------

def test_cli_refuses_to_send_real_transcripts_without_the_flag(capsys):
    llm.set_stub(lambda *a: pytest.fail("the model must not be called"))
    assert judge.main(["audit", "--source", "audited"]) == 2
    assert "refusing" in capsys.readouterr().err


def test_cli_audit_then_agreement(tmp_path, monkeypatch):
    models.save(models.TRANSCRIPTS_DIR / "ABC123.json", T.model_copy(update={"call_id": "ABC123", "source": "audited"}))
    models.save(models.TRANSCRIPTS_DIR / "P1.json", T.model_copy(update={"call_id": "P1", "source": "production"}))
    llm.set_stub(answer_all)
    assert judge.main(["audit", "--source", "audited", "--model", "m", "--yes-send-real-transcripts"]) == 0
    assert judge.audit_path("ABC123").exists() and not judge.audit_path("P1").exists()
    stale = models.load(judge.audit_path("ABC123"), CallAudit).model_copy(
        update={"call_id": "XYZ789", "rubric_set_hash": "an older rubric set"})
    models.save(judge.audit_path("XYZ789"), stale)                         # left out: another rubric set
    write_sheet(tmp_path / "sheet.csv")
    monkeypatch.setattr(listen, "AUDIT_CSV", tmp_path / "sheet.csv")
    monkeypatch.setattr(judge, "REPORTS_DIR", tmp_path)
    assert judge.main(["agreement", "--model", "m"]) == 0
    rep = models.load(judge.AGREEMENT_JSON, models.AgreementReport)
    assert (rep.n_calls, rep.overall_accuracy, rep.held_out) == (1, 0.0, True)   # judge pass vs human non_fatal
    assert (tmp_path / "judge_agreement.md").exists()
    assert json.loads(judge.AGREEMENT_JSON.read_text())["model"] == "m"


def test_calibration_remaps_only_minor_flags_and_never_fatal():
    def a(cid, grade):
        return CallAudit(call_id=cid, prompt_version="v1", overall=grade, model="m", rubric_set_hash="h",
                         verdicts=(RubricVerdict(rubric_id="looping_behavior", rubric_version=1, grade=grade,
                                                 confidence=0.9, reason="r"),))
    # The auditors call 2 of the judge's 3 minor looping flags "pass": learn non_fatal -> pass.
    tune = [a("t1", "non_fatal"), a("t2", "non_fatal"), a("t3", "non_fatal"), a("t4", "fatal")]
    human = {"t1": {"grades": {"Looping Behavior": "pass"}}, "t2": {"grades": {"Looping Behavior": "pass"}},
             "t3": {"grades": {"Looping Behavior": "non_fatal"}}, "t4": {"grades": {"Looping Behavior": "pass"}}}
    soft = judge.calibrate(tune, human, R)
    assert soft == {"looping_behavior": "pass"}
    assert judge.apply_calibration(a("x", "non_fatal"), soft).overall == "pass"
    assert judge.apply_calibration(a("y", "fatal"), soft).overall == "fatal"      # fatal is never softened
    # A tie keeps the judge's grade.
    assert judge.calibrate([a("t1", "non_fatal"), a("t3", "non_fatal")], human, R) == {}
