"""Rubric loading, hashing and the §L2.2 formulas, checked by hand."""

import sys
import types

import pytest
import yaml

from abtest.loop.contracts import (Criterion, JudgeAnswer, JudgeSettings, PassThresholds, RUBRICS_DIR, Rubric,
                                   sha256_json)
from abtest.loop.rubric import goodness, known_rubric, load_rubric, rubric_hash, score_from_answers

JUDGE = JudgeSettings(model="stub-judge")


def crit(id_, kind="yes_no", polarity="bad", weight=1.0, hard=False):
    return Criterion(id=id_, kind=kind, polarity=polarity, weight=weight, text=f"question about {id_}", hard=hard)


def rubric(*criteria, risk=(), **kw):
    return Rubric(id="t", judge=JUDGE, criteria=tuple(criteria), risk=tuple(risk), **kw)


def ans(id_, value):
    return JudgeAnswer(criterion_id=id_, value=value, provider="test")


def answers(**values):
    return {k: ans(k, v) for k, v in values.items()}


# --- load and hash ----------------------------------------------------------------------------

def test_load_shipped_rubric_and_hash():
    path = RUBRICS_DIR / "vani-call-quality-v1.yaml"
    r = load_rubric(path)
    assert r.id == "vani-call-quality-v1" and len(r.criteria) == 8
    assert rubric_hash(r) == sha256_json(r.model_dump(mode="json"))
    assert rubric_hash(load_rubric(str(path))) == rubric_hash(r)          # str and Path, same hash
    assert known_rubric(rubric_hash(r)) == r


def test_hash_changes_with_any_content_change():
    a = rubric(crit("x", weight=1), crit("y", weight=2))
    heavier = rubric(crit("x", weight=1), crit("y", weight=3))
    stricter = rubric(crit("x", weight=1), crit("y", weight=2), pass_thresholds=PassThresholds(bad=0.6))
    assert len({rubric_hash(a), rubric_hash(heavier), rubric_hash(stricter)}) == 3
    assert rubric_hash(a) == rubric_hash(rubric(crit("x", weight=1), crit("y", weight=2)))


def test_load_rejects_inconsistent_rubric(tmp_path):
    doc = yaml.safe_load((RUBRICS_DIR / "vani-call-quality-v1.yaml").read_text())
    doc["risk"] = ["looping", "no_such_criterion"]
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(doc))
    with pytest.raises(ValueError, match="unknown criteria"):
        load_rubric(p)


# --- goodness ---------------------------------------------------------------------------------

@pytest.mark.parametrize("kind,polarity,value,expected", [
    ("yes_no", "bad", 0.2, 0.8), ("yes_no", "bad", 1.0, 0.0), ("yes_no", "bad", 0.0, 1.0),
    ("yes_no", "good", 0.2, 0.2), ("yes_no", "good", 1.0, 1.0),
    ("score_1_5", "good", 5, 1.0), ("score_1_5", "good", 3, 0.5), ("score_1_5", "good", 1, 0.0),
    ("score_1_5", "good", 2, 0.25),
    ("score_1_5", "bad", 5, 0.0), ("score_1_5", "bad", 1, 1.0), ("score_1_5", "bad", 4, 0.25),
    # out of range answers are clipped, not rejected
    ("yes_no", "bad", 1.3, 0.0), ("yes_no", "good", -0.2, 0.0),
    ("score_1_5", "good", 7, 1.0), ("score_1_5", "good", 0, 0.0), ("score_1_5", "bad", 7, 0.0),
])
def test_goodness(kind, polarity, value, expected):
    c = crit("c", kind=kind, polarity=polarity)
    assert goodness(c, ans("c", value)) == pytest.approx(expected)


def test_goodness_rejects_wrong_criterion_and_non_finite_values():
    with pytest.raises(ValueError, match="given to criterion"):
        goodness(crit("a"), ans("b", 0.1))
    with pytest.raises(ValueError, match="finite"):
        goodness(crit("a"), ans("a", float("nan")))


# --- score_from_answers -----------------------------------------------------------------------

def shipped():
    return load_rubric(RUBRICS_DIR / "vani-call-quality-v1.yaml")


def all_good(r):
    return {c.id: ans(c.id, (0.0 if c.polarity == "bad" else 1.0) if c.kind == "yes_no"
                      else (5 if c.polarity == "good" else 1)) for c in r.criteria}


def test_all_good_call_scores_one_with_no_risk():
    r = shipped()
    score, risk, failed = score_from_answers(r, all_good(r))
    assert score == 1.0 and risk == 0.0 and failed == ()


def test_all_bad_call_scores_zero_with_full_risk_and_every_criterion_failed():
    r = shipped()
    worst = {c.id: ans(c.id, (1.0 if c.polarity == "bad" else 0.0) if c.kind == "yes_no"
                       else (1 if c.polarity == "good" else 5)) for c in r.criteria}
    score, risk, failed = score_from_answers(r, worst)
    assert score == 0.0 and risk == 1.0 and failed == tuple(c.id for c in r.criteria)


def test_one_hard_failure_is_listed_and_costs_its_weight():
    r = shipped()
    a = all_good(r)
    a["claimed_human"] = ans("claimed_human", 0.9)
    score, risk, failed = score_from_answers(r, a)
    total_weight = 4 + 4 + 2 + 3 + 3 + 2 + 3 + 2
    assert score == pytest.approx((total_weight - 4 * 0.9) / total_weight)
    assert risk == pytest.approx(0.9)
    assert failed == ("claimed_human",)


def test_weights_matter():
    heavy_a = rubric(crit("a", weight=1), crit("b", weight=3))
    heavy_b = rubric(crit("a", weight=3), crit("b", weight=1))
    a = answers(a=1.0, b=0.0)                      # a fails completely, b is clean
    assert score_from_answers(heavy_a, a)[0] == pytest.approx(0.75)   # (0*1 + 1*3) / 4
    assert score_from_answers(heavy_b, a)[0] == pytest.approx(0.25)   # (0*3 + 1*1) / 4


def test_score_criterion_enters_through_normalised_value():
    r = rubric(crit("q", kind="score_1_5", polarity="good", weight=1), crit("bad_p", weight=1))
    score, risk, failed = score_from_answers(r, answers(q=3, bad_p=0.0))
    assert score == pytest.approx((0.5 + 1.0) / 2) and failed == () and risk == 0.0


def test_risk_is_one_minus_product_over_risk_criteria_only():
    r = rubric(crit("a"), crit("b"), crit("c"), crit("q", kind="score_1_5", polarity="good"), risk=["a", "b"])
    _, risk, _ = score_from_answers(r, answers(a=0.5, b=0.5, c=1.0, q=1))
    assert risk == pytest.approx(0.75)             # c and q are not risk criteria
    _, none, _ = score_from_answers(rubric(crit("a"), crit("b")), answers(a=1.0, b=1.0))
    assert none == 0.0                              # no risk list, no risk


def test_risk_uses_the_yes_probability_even_for_a_good_polarity_criterion():
    r = rubric(crit("disclosed", polarity="good"), risk=["disclosed"])
    _, risk, _ = score_from_answers(r, answers(disclosed=0.3))
    assert risk == pytest.approx(0.3)


@pytest.mark.parametrize("p,fails", [(0.5, True), (0.49, False), (0.51, True)])
def test_bad_yes_no_fails_at_threshold_inclusive(p, fails):
    _, _, failed = score_from_answers(rubric(crit("x")), answers(x=p))
    assert (failed == ("x",)) is fails


@pytest.mark.parametrize("p,fails", [(0.5, False), (0.49, True), (0.0, True), (1.0, False)])
def test_good_yes_no_fails_below_threshold(p, fails):
    _, _, failed = score_from_answers(rubric(crit("x", polarity="good")), answers(x=p))
    assert (failed == ("x",)) is fails


@pytest.mark.parametrize("polarity,s,fails", [("good", 3, False), ("good", 2, True), ("bad", 3, False),
                                              ("bad", 4, True), ("bad", 1, False)])
def test_score_fails_when_goodness_is_below_threshold(polarity, s, fails):
    _, _, failed = score_from_answers(rubric(crit("x", kind="score_1_5", polarity=polarity)), answers(x=s))
    assert (failed == ("x",)) is fails


def test_custom_pass_thresholds_are_used():
    r = rubric(crit("x"), crit("y", polarity="good"), pass_thresholds=PassThresholds(bad=0.8, good=0.3))
    _, _, failed = score_from_answers(r, answers(x=0.7, y=0.25))
    assert failed == ("y",)                         # bad yes_no needs p >= 0.8; good fails below 0.3


def test_missing_answers_are_named():
    r = rubric(crit("a"), crit("b"), crit("c"))
    with pytest.raises(ValueError, match=r"\['b', 'c'\]"):
        score_from_answers(r, answers(a=0.1))


def test_extra_answers_are_ignored():
    r = rubric(crit("a"))
    assert score_from_answers(r, answers(a=0.0, zzz=1.0)) == (1.0, 0.0, ())


# --- custom aggregate hook --------------------------------------------------------------------

@pytest.fixture
def agg_module(monkeypatch):
    seen = {}
    mod = types.ModuleType("fake_agg_pkg_mod")

    def worst_of(answers, rubric):
        seen["args"] = (answers, rubric)
        return 1.0 - max(a.value for a in answers.values())

    mod.worst_of = worst_of
    mod.too_big = lambda answers, rubric: 1.5
    mod.negative = lambda answers, rubric: -0.01
    mod.not_a_number = lambda answers, rubric: float("nan")
    mod.not_callable = 3
    monkeypatch.setitem(sys.modules, "fake_agg_pkg_mod", mod)
    return seen


@pytest.mark.parametrize("path", ["fake_agg_pkg_mod:worst_of", "fake_agg_pkg_mod.worst_of"])
def test_custom_aggregate_replaces_only_the_score(agg_module, path):
    r = rubric(crit("a", weight=5), crit("b", weight=1), risk=["a"], aggregate=path)
    a = answers(a=0.9, b=0.2)
    score, risk, failed = score_from_answers(r, a)
    assert score == pytest.approx(0.1)             # the hook's value, not the weighted mean
    assert risk == pytest.approx(0.9)              # risk and failed are still the rubric's own
    assert failed == ("a",)
    got_answers, got_rubric = agg_module["args"]
    assert got_answers == a and isinstance(got_answers, dict) and got_rubric is r


@pytest.mark.parametrize("name", ["too_big", "negative", "not_a_number"])
def test_custom_aggregate_outside_unit_interval_is_an_error(agg_module, name):
    r = rubric(crit("a"), aggregate=f"fake_agg_pkg_mod:{name}")
    with pytest.raises(ValueError, match="must be in"):
        score_from_answers(r, answers(a=0.1))


@pytest.mark.parametrize("path", ["no_such_module_xyz:fn", "fake_agg_pkg_mod:missing", "justaname",
                                  "fake_agg_pkg_mod:not_callable"])
def test_unusable_aggregate_path_is_a_clear_error(agg_module, path):
    with pytest.raises(ValueError, match="aggregate"):
        score_from_answers(rubric(crit("a"), aggregate=path), answers(a=0.1))


def test_hook_is_not_called_when_an_answer_is_missing(agg_module):
    r = rubric(crit("a"), crit("b"), aggregate="fake_agg_pkg_mod:worst_of")
    with pytest.raises(ValueError, match="no judge answer"):
        score_from_answers(r, answers(a=0.1))
    assert "args" not in agg_module


def test_a_skipped_criterion_is_out_of_the_score_and_never_failed():
    # closure needs the call's end; a bot test cut off at max_turns must not fail it (10 Oct)
    from abtest.loop.rubric import score_from_answers
    r = load_rubric(RUBRICS_DIR / "vani-call-quality-v1.yaml")
    bad_closure = {c.id: JudgeAnswer(criterion_id=c.id, value=(1.0 if c.id == "closure" else
                   (0.0 if c.kind == "yes_no" else 5.0)), provider="t") for c in r.criteria}
    full, _, failed = score_from_answers(r, bad_closure)
    skipped, _, failed_skip = score_from_answers(r, bad_closure, ("closure",))
    assert "closure" in failed and "closure" not in failed_skip
    assert skipped == pytest.approx(1.0) and full < 1.0
