"""Improve: score math, grouping, edit validation, gate rule, replay. Invented calls only, stubbed model."""

import hashlib
import json

import pytest

from abtest.contracts import ROOT
from abtest.ext import improve, llm
from abtest.ext.models import (
    LOOP_DIR,
    REGISTRY,
    TRANSCRIPTS_DIR,
    CallAudit,
    Evidence,
    Fix,
    GateResult,
    PromptVersion,
    RootCause,
    Rubric,
    RubricVerdict,
    Transcript,
    Turn,
    load,
    save,
    worst,
)

PROMPT_A = ("You are Vani, a virtual assistant from IndiaMART; never claim that you are a Human.\n"
            "Ask the quantity at most 3 times.\n"
            "Ask the quantity with a unit.\n")
RUBRICS = [Rubric(id="qty", name="Quantity Probing Error", category="Quantity",
                  question="Did the bot re-ask the quantity after the buyer gave it?"),
           Rubric(id="spec", name="Spec1 Accuracy", category="Specs", question="Is spec 1 right?")]


def _sys_text(messages) -> str:
    c = messages[0]["content"]
    return c if isinstance(c, str) else "".join(p["text"] for p in c)


def _audit(cid: str, **grades) -> CallAudit:
    verdicts = tuple(RubricVerdict(rubric_id=r, rubric_version=1, grade=g, confidence=0.9,
                                   reason=f"{r} on {cid}: buyer said 500 piece twice",
                                   evidence=(Evidence(turn=2, quote="kitne piece chahiye 500"),))
                     for r, g in grades.items())
    return CallAudit(call_id=cid, prompt_version="v1", overall=worst(grades.values()), verdicts=verdicts,
                     model="stub-judge", rubric_set_hash="h")


def _transcript(cid: str, source="fixture") -> Transcript:
    lines = [("bot", "Namaste, main Vani, IndiaMART se. Aap PP bags ke liye call kar rahe the?"),
             ("buyer", "haan ji, PP bags chahiye"), ("bot", "Kitne piece chahiye?"),
             ("buyer", "500 piece"), ("bot", "Kitne piece chahiye aapko?"), ("buyer", "bola na 500 piece")]
    return Transcript(call_id=cid, source=source, turns=tuple(Turn(speaker=s, text=t) for s, t in lines),
                      language="mixed", provider="fixture")


def _base() -> PromptVersion:
    path = LOOP_DIR / "prompt_a.txt"
    path.write_text(PROMPT_A, encoding="utf-8")
    pv = PromptVersion(version="v1", path=str(path), status="production", created_ts="2026-10-10",
                       sha256=hashlib.sha256(PROMPT_A.encode()).hexdigest())
    save(REGISTRY, [pv])
    return pv


def _rc(ids=("c1", "c2", "c3"), n_audited=10) -> RootCause:
    return RootCause(id="rc-qty-1", rubric_id="qty", name="Re-asks quantity", description="d", type="prompt",
                     n_audited=n_audited, n_calls=len(ids), n_fatal=len(ids), n_non_fatal=0, frequency=0.3,
                     severity=1.0, priority=10.0, example_call_ids=tuple(ids), prompt_version="v1")


def _fix_answer(*edits, rationale="Stop re-asking a quantity the buyer already gave."):
    return {"edits": [{"mode": "replace_text", "find": "", "replace": "", "text": "", "why": "w", **e}
                      for e in edits], "rationale": rationale}


# --- root causes -------------------------------------------------------------------------

def test_scores_ids_and_order_follow_the_documented_formulas():
    audits = [_audit("c1", qty="fatal", spec="pass"), _audit("c2", qty="fatal", spec="pass"),
              _audit("c3", qty="non_fatal", spec="pass"), _audit("c4", qty="non_fatal", spec="na"),
              _audit("c5", qty="non_fatal", spec="pass"), _audit("c6", qty="pass", spec="fatal")]
    audits += [_audit(f"p{i}", qty="pass", spec="pass") for i in range(4)]       # 10 audited

    def stub(model, messages, schema):
        if "Rubric qty" in messages[1]["content"]:
            return {"causes": [{"name": "Re-asks given quantity", "description": "d1", "type": "prompt",
                                "type_reason": "", "call_ids": ["c1", "c3"]},
                               {"name": "Unit never asked", "description": "d2", "type": "prompt",
                                "type_reason": "", "call_ids": ["c5", "c2", "c4"]}]}
        return {"causes": [{"name": "Wrong spec", "description": "d3", "type": "knowledge",
                            "type_reason": "catalog lacks the spec", "call_ids": ["c6"]}]}
    llm.set_stub(stub)
    rcs = improve.root_causes(audits, RUBRICS, "v1", model="m")
    assert [rc.id for rc in rcs] == ["rc-qty-1", "rc-qty-2", "rc-spec-1"]
    unit, reask, spec = rcs
    assert (unit.name, unit.n_audited, unit.n_calls, unit.n_fatal, unit.n_non_fatal) == \
        ("Unit never asked", 10, 3, 1, 2)
    assert unit.frequency == pytest.approx(3 / 10)
    assert unit.severity == pytest.approx((3 * 1 + 2) / (3 * 3))
    assert unit.priority == pytest.approx(100 * 5 / 30)
    assert unit.example_call_ids == ("c2", "c4", "c5")                           # fatal first, then by id
    assert reask.priority == pytest.approx(100 * 4 / 30) and reask.severity == pytest.approx(4 / 6)
    assert (spec.priority, spec.severity, spec.type) == (pytest.approx(10.0), 1.0, "knowledge")
    assert "catalog lacks the spec" in spec.description                         # a refined type says why
    assert improve.load_root_causes("v1") == rcs


def test_grouping_keeps_every_failing_call_exactly_once_and_masks_digits():
    audits = [_audit(c, qty="fatal") for c in ("c1", "c2", "c3", "c4", "c5")]
    seen = []

    def stub(model, messages, schema):
        seen.append(messages[1]["content"])
        def g(name, ids):
            return {"name": name, "description": "", "type": "prompt", "type_reason": "", "call_ids": ids}
        return {"causes": [g("A", ["c1", "zz", "c1"]), g("B", ["c1", "c2"]), g("C", []), g("D", ["c3"]),
                           g("E", ["c4"])]}                     # empty C skipped; E is a 4th cause: dropped
    llm.set_stub(stub)
    rcs = improve.root_causes(audits, RUBRICS[:1], model="m")
    members = {rc.name: rc.example_call_ids for rc in rcs}
    assert members == {"A": ("c1",), "B": ("c2",), "D": ("c3",), "Other Quantity Probing Error failures": ("c4", "c5")}
    assert sum(rc.n_calls for rc in rcs) == 5
    assert "500" not in seen[0] and "###" in seen[0]


def test_no_failures_means_no_model_call():
    llm.set_stub(lambda *a: pytest.fail("no failing calls, nothing to group"))
    assert improve.root_causes([_audit("c1", qty="pass")], RUBRICS) == []
    with pytest.raises(ValueError, match="bad prompt version"):
        improve.load_root_causes("../../etc")


# --- draft_fix -----------------------------------------------------------------------------

def test_fix_applies_edits_in_order_and_writes_prompt_b_and_diff():
    base, rc = _base(), _rc()
    save(TRANSCRIPTS_DIR / "c1.json", _transcript("c1"))
    prompts = []

    def stub(model, messages, schema):
        prompts.append(messages)
        return _fix_answer({"find": "Ask the quantity at most 3 times.",
                            "replace": "Ask the quantity at most 3 times; never re-ask one the buyer gave."},
                           {"mode": "append", "text": "If the buyer already said the quantity, confirm it once."})
    llm.set_stub(stub)
    fix = improve.draft_fix(rc, base, [_audit("c1", qty="fatal")], model="anthropic/fixer")
    b = (ROOT / fix.prompt_b_path).read_text(encoding="utf-8")
    assert b == PROMPT_A.replace("3 times.", "3 times; never re-ask one the buyer gave.") + \
        "If the buyer already said the quantity, confirm it once.\n"
    assert fix.prompt_b_sha256 == hashlib.sha256(b.encode()).hexdigest()
    assert fix.id == "fx-" + hashlib.sha256((rc.id + base.sha256).encode()).hexdigest()[:12]
    assert "-Ask the quantity at most 3 times.\n" in fix.diff and "+If the buyer already said" in fix.diff
    assert improve.load_fixes() == [fix] and fix.gate is None
    system, user = prompts[0][0]["content"][0], prompts[0][1]["content"]
    assert system["cache_control"] == {"type": "ephemeral"} and PROMPT_A in system["text"]
    assert ">> bot: Kitne piece chahiye?\n   buyer: ### piece" in user and "500" not in user  # masked


@pytest.mark.parametrize("bad", ["Ask the quantity", "not in the prompt"])   # occurs 2 times, 0 times
def test_a_find_that_is_not_unique_gets_one_retry_then_fails(bad):
    base = _base()
    calls = []

    def stub(model, messages, schema):
        calls.append(messages)
        return _fix_answer({"find": bad, "replace": "x"})
    llm.set_stub(stub)
    with pytest.raises(ValueError, match="need exactly 1"):
        improve.draft_fix(_rc(), base, [])
    assert len(calls) == 2 and bad in calls[1][-1]["content"]                   # the retry names the find
    assert not improve.load_fixes()


def test_the_retry_can_repair_the_edit_and_history_changes_the_id():
    base = _base()
    answers = iter([_fix_answer({"find": "nowhere", "replace": "x"}),
                    _fix_answer({"find": "Ask the quantity with a unit.", "replace": "Ask with a unit."})])
    llm.set_stub(lambda *a: next(answers))
    first = improve.draft_fix(_rc(), base, [])
    assert first.edits[0].find == "Ask the quantity with a unit."
    llm.set_stub(lambda *a: _fix_answer({"mode": "append", "text": "Never re-ask."}))
    second = improve.draft_fix(_rc(), base, [], history=[(first, "REPLAY_NO_GAIN")])
    assert second.id != first.id and len(improve.load_fixes()) == 2


def test_fix_refuses_a_base_whose_text_no_longer_matches_its_sha():
    base = _base().model_copy(update={"sha256": "0" * 64})
    llm.set_stub(lambda *a: pytest.fail("must not call the model"))
    with pytest.raises(ValueError, match="sha256"):
        improve.draft_fix(_rc(), base, [])


# --- gate ----------------------------------------------------------------------------------

def _drafted(edit=None) -> Fix:
    base = _base()
    edit = edit or {"find": "Ask the quantity with a unit.", "replace": "Ask with a unit."}
    llm.set_stub(lambda *a: _fix_answer(edit))
    return improve.draft_fix(_rc(), base, [])


@pytest.mark.parametrize("answer,passed", [
    ({"alters_commitments": 0.1, "weakens_safety_rule": 0.2, "contradicts_instruction": 0.49,
      "raises_ask_limit": 0.9, "likely_longer_calls": 0.9}, True),               # warnings never block
    ({"alters_commitments": 0.1, "weakens_safety_rule": 0.5, "contradicts_instruction": 0.1,
      "raises_ask_limit": 0.0, "likely_longer_calls": 0.0}, False),              # p >= 0.5 blocks
    ({"alters_commitments": 0.1, "contradicts_instruction": 0.1}, False),       # unanswered: fail closed
    ({"alters_commitments": -1, "weakens_safety_rule": False, "contradicts_instruction": float("nan"),
      "raises_ask_limit": 0.0, "likely_longer_calls": 0.0}, False),              # malformed: fail closed
])
def test_gate_blocks_when_any_blocking_check_reaches_the_threshold(answer, passed):
    fix = _drafted()
    seen = []

    def stub(model, messages, schema):
        seen.append(messages[1]["content"])
        return {k: {"reason": "r", "probability": p} for k, p in answer.items()}
    llm.set_stub(stub)
    res = improve.gate(fix, model="m")
    assert res.passed is passed
    assert {c.id: c.blocking for c in res.checks} == {cid: b for cid, b, _ in improve.GATE_CHECKS}
    assert PROMPT_A in seen[0] and fix.diff in seen[0]
    assert improve.load_fixes()[0].gate == res


def test_gate_and_replay_refuse_a_prompt_b_edited_after_drafting():
    fix = _drafted()
    save(TRANSCRIPTS_DIR / "c1.json", _transcript("c1"))
    (ROOT / fix.prompt_b_path).write_text("something else\n", encoding="utf-8")
    llm.set_stub(lambda *a: pytest.fail("must not call the model"))
    with pytest.raises(ValueError, match="sha256"):
        improve.gate(fix)
    with pytest.raises(ValueError, match="sha256"):
        improve.replay(fix, _rc(), [])


# --- replay --------------------------------------------------------------------------------

def _replay_stub(calls):
    def stub(model, messages, schema):
        calls.append((model, messages))
        if schema is not None:                                                  # persona
            return {"need": "PP bags for packing", "product": "PP bags", "quantity": "500 piece",
                    "language": "Hinglish in Latin script", "temperament": "impatient"}
        system = _sys_text(messages)
        if "SIMULATION" in system:                                              # the bot under test
            if model.startswith("anthropic/"):
                assert messages[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
            asked = sum(m["role"] == "assistant" for m in messages)
            if "ASK ONCE" in system and asked >= 1:
                return "Dhanyavaad, main aapko seller se connect karti hoon. [END_CALL]"
            return "Kitne piece chahiye?"                                       # A never stops asking
        assert "500 piece" in system and "impatient" in system                  # the persona drives the buyer
        return "500 piece, bola na"
    return stub


def _fake_judge(t: Transcript, model=None) -> CallAudit:
    asks = sum(x.speaker == "bot" and "Kitne" in x.text for x in t.turns)
    grades = {"qty": "fatal" if asks > 1 else "pass", "tone": "non_fatal" if t.variant == "B" else "pass"}
    return CallAudit(call_id=t.call_id, prompt_version=t.prompt_version, overall=worst(grades.values()),
                     model="fake-judge", rubric_set_hash="h",
                     verdicts=tuple(RubricVerdict(rubric_id=r, rubric_version=1, grade=g, confidence=1.0, reason="r")
                                    for r, g in grades.items()))


def test_replay_simulates_a_and_b_on_the_same_personas_and_judges_both(monkeypatch):
    fix = _drafted({"mode": "append", "text": "ASK ONCE"})
    for c in ("c1", "c2", "c3"):
        save(TRANSCRIPTS_DIR / f"{c}.json", _transcript(c))
    monkeypatch.setattr(improve, "judge_call", _fake_judge)
    calls = []
    llm.set_stub(_replay_stub(calls))
    gated = GateResult(passed=True, checks=(), model="m")
    improve._update_fix(fix, gate=gated)                                        # `fix` is now stale
    audits = [_audit("c1", qty="fatal"), _audit("c2", qty="non_fatal"), _audit("c3", qty="fatal")]
    res = improve.replay(fix, _rc(), audits, n=2, models={"bot": "anthropic/bot", "buyer": "x/buyer"})
    assert [c.call_id for c in res.cases] == ["c1", "c2"]                       # n, in the root cause's order
    assert (res.fail_rate_real, res.fail_rate_a, res.fail_rate_b) == (1.0, 1.0, 0.0)
    assert res.regressions == {"tone": 1.0}
    assert res.models == {"bot": "anthropic/bot", "buyer": "x/buyer", "judge": "fake-judge"}
    sim_a = load(improve.REPLAY_DIR / fix.id / "c1-A.json", Transcript)
    sim_b = load(improve.REPLAY_DIR / fix.id / "c1-B.json", Transcript)
    assert sum(t.speaker == "bot" for t in sim_a.turns) == improve.MAX_BOT_TURNS
    assert [t.speaker for t in sim_b.turns] == ["bot", "buyer", "bot"] and "[END_CALL]" not in sim_b.turns[-1].text
    assert (sim_b.source, sim_b.provider, sim_b.variant, sim_b.prompt_version) == \
        ("replay", "replay:anthropic/bot", "B", fix.id)
    assert sim_a.meta["replay_of"] == "c1" and sim_a.meta["persona_temperament"] == "impatient"
    assert improve.load_fixes()[0].replay == res and improve.load_fixes()[0].gate == gated  # both kept
    assert {m for m, _ in calls} == {"anthropic/bot", "x/buyer"}


def test_an_empty_model_turn_fails_loudly_instead_of_ending_the_call(monkeypatch):
    fix = _drafted()
    save(TRANSCRIPTS_DIR / "c1.json", _transcript("c1"))
    monkeypatch.setattr(improve, "judge_call", _fake_judge)
    llm.set_stub(lambda model, messages, schema: {"need": "x", "product": "y", "quantity": "z", "language": "hi",
                                                  "temperament": "calm"} if schema else "")
    with pytest.raises(RuntimeError, match="empty turn"):
        improve.replay(fix, _rc(("c1",)), [])


def test_replay_refuses_to_score_when_the_judge_skips_the_target_rubric(monkeypatch):
    fix = _drafted()
    save(TRANSCRIPTS_DIR / "c1.json", _transcript("c1"))
    monkeypatch.setattr(improve, "judge_call", lambda t, model=None: _fake_judge(t).model_copy(
        update={"verdicts": tuple(v for v in _fake_judge(t).verdicts if t.variant == "A" or v.rubric_id != "qty")}))
    llm.set_stub(_replay_stub([]))
    with pytest.raises(ValueError, match="no qty verdict for c1-B"):
        improve.replay(fix, _rc(("c1",)), [])


def test_a_version_born_from_a_fix_replays_its_b_simulations(monkeypatch):
    fix = _drafted()
    v2 = PromptVersion(version="v2", path=fix.prompt_b_path, sha256=fix.prompt_b_sha256, parent="v1",
                       fix_id="fx-earlier", status="production", created_ts="2026-10-10")
    save(REGISTRY, [*load(REGISTRY, PromptVersion), v2])
    save(improve.REPLAY_DIR / "fx-earlier" / "c1-B.json",
         _transcript("c1-B").model_copy(update={"source": "replay", "prompt_version": "v2"}))
    monkeypatch.setattr(improve, "judge_call", _fake_judge)
    llm.set_stub(_replay_stub([]))
    rc = _rc(("c1-B",)).model_copy(update={"prompt_version": "v2"})
    assert improve.replay(fix, rc, []).cases[0].call_id == "c1-B"


def test_replay_refuses_real_transcripts_unless_allowed(monkeypatch):
    fix = _drafted()
    save(TRANSCRIPTS_DIR / "c1.json", _transcript("c1", source="production"))
    monkeypatch.setattr(improve, "judge_call", _fake_judge)
    calls = []
    llm.set_stub(_replay_stub(calls))
    with pytest.raises(PermissionError, match="real customer calls"):
        improve.replay(fix, _rc(("c1",)), [])
    assert calls == []                                                          # refused before any model call
    assert improve.replay(fix, _rc(("c1",)), [], allow_real=True).n_cases == 1


def test_cli_refuses_real_audits_without_the_flag():
    save(improve.AUDITS_DIR / "v1" / "c1.json", _audit("c1", qty="fatal"))     # no transcript: counts as real
    llm.set_stub(lambda *a: pytest.fail("must not call the model"))
    with pytest.raises(SystemExit, match="refusing"):
        improve.main(["causes"])
    json.dumps(improve._GROUP_SCHEMA)                                           # schemas stay plain JSON
