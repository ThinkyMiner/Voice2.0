"""Lead-level diagnosis and masked excerpts, on constructed frames (no model, no network)."""

import math
import sys
import types

import numpy as np
import pandas as pd
import pytest

from abtest.loop.contracts import Criterion, JudgeSettings, Rubric, Transcript, Turn
from abtest.loop.diagnose import diagnose, excerpts, mean_ci95

Z = 1.959963984540054


def make_rubric(*specs):
    """specs: (id, weight)"""
    return Rubric(id="t", judge=JudgeSettings(model="m"),
                  criteria=tuple(Criterion(id=i, kind="yes_no", polarity="bad", weight=w, text=f"{i}?") for i, w in specs))


def frame(rows, cols):
    return pd.DataFrame(rows, columns=["lead_id", *cols])


# --- mean_ci95 --------------------------------------------------------------------------------

def test_mean_ci95_matches_hand_formula_and_clips():
    v = [0.0, 0.25, 0.5, 0.75, 1.0, 0.5]
    mean, lo, hi = mean_ci95(v)
    sd = float(np.std(v, ddof=1))
    assert mean == pytest.approx(0.5)
    assert lo == pytest.approx(0.5 - Z * sd / math.sqrt(6)) and hi == pytest.approx(0.5 + Z * sd / math.sqrt(6))
    m2, lo2, hi2 = mean_ci95([0.0, 1.0])                  # sd 0.707, se 0.5: the raw interval leaves [0, 1]
    assert (lo2, hi2) == (0.0, 1.0) and m2 == 0.5


def test_mean_ci95_single_value_is_the_point_and_empty_raises():
    assert mean_ci95([0.3]) == (0.3, 0.3, 0.3)
    assert mean_ci95([0.3, float("nan")]) == (0.3, 0.3, 0.3)       # NaNs are ignored
    assert mean_ci95([0.4, 0.4, 0.4]) == (pytest.approx(0.4), pytest.approx(0.4), pytest.approx(0.4))  # no spread
    with pytest.raises(ValueError):
        mean_ci95([float("nan")])


# --- diagnose ---------------------------------------------------------------------------------

def test_lead_is_the_unit_not_the_call():
    r = make_rubric(("a", 1.0))
    # lead L1 was called four times and failed every time; L2 once and was fine
    scores = frame([["L1", 0.0], ["L1", 0.0], ["L1", 0.0], ["L1", 0.0], ["L2", 1.0]], ["a"])
    out = diagnose(scores, r, top_k=1)
    assert out.loc[0, "failure_rate"] == pytest.approx(0.5)          # not 4/5
    assert out.loc[0, "n_leads"] == 2


def test_failure_is_one_minus_mean_goodness_within_a_lead():
    r = make_rubric(("a", 1.0))
    scores = frame([["L1", 1.0], ["L1", 0.5], ["L2", 0.8], ["L3", 0.6], ["L3", 0.2]], ["a"])
    # lead goodness: L1 0.75, L2 0.8, L3 0.4  -> failures 0.25, 0.2, 0.6
    out = diagnose(scores, r, top_k=1).iloc[0]
    fails = np.array([0.25, 0.2, 0.6])
    assert out["failure_rate"] == pytest.approx(fails.mean())
    half = Z * fails.std(ddof=1) / math.sqrt(3)
    assert out["ci_low"] == pytest.approx(fails.mean() - half) and out["ci_high"] == pytest.approx(fails.mean() + half)


def test_ranking_weighted_cost_and_target_flag():
    r = make_rubric(("light", 1.0), ("heavy", 5.0), ("rare", 3.0), ("mid", 2.0))
    scores = frame([
        # light fails a lot, but its weight is 1; heavy fails moderately at weight 5
        ["L1", 0.0, 0.5, 1.0, 0.5], ["L2", 0.0, 0.5, 1.0, 0.5], ["L3", 0.0, 0.5, 1.0, 0.5], ["L4", 0.2, 0.5, 1.0, 0.5],
    ], ["light", "heavy", "rare", "mid"])
    out = diagnose(scores, r, top_k=2)
    assert list(out.columns) == ["criterion_id", "failure_rate", "ci_low", "ci_high", "weight", "weighted_cost",
                                 "n_leads", "rank", "target"]
    by = out.set_index("criterion_id")
    assert by.loc["light", "failure_rate"] == pytest.approx(0.95) and by.loc["light", "weighted_cost"] == pytest.approx(0.95)
    assert by.loc["heavy", "weighted_cost"] == pytest.approx(2.5)
    assert by.loc["rare", "weighted_cost"] == 0.0
    assert by.loc["mid", "weighted_cost"] == pytest.approx(1.0)
    assert out["criterion_id"].tolist() == ["heavy", "mid", "light", "rare"]          # sorted by rank
    assert out["rank"].tolist() == [1, 2, 3, 4]
    assert out["target"].tolist() == [True, True, False, False]
    assert out["weighted_cost"].is_monotonic_decreasing


def test_ties_are_broken_by_rubric_order_and_top_k_larger_than_criteria():
    r = make_rubric(("first", 1.0), ("second", 1.0))
    scores = frame([["L1", 0.5, 0.5], ["L2", 0.5, 0.5]], ["first", "second"])
    out = diagnose(scores, r, top_k=10)
    assert out["criterion_id"].tolist() == ["first", "second"] and out["target"].all()


def test_single_lead_ci_equals_the_point():
    r = make_rubric(("a", 2.0))
    out = diagnose(frame([["L1", 0.7], ["L1", 0.9]], ["a"]), r, top_k=1).iloc[0]
    assert out["n_leads"] == 1 and out["failure_rate"] == pytest.approx(0.2)
    assert out["ci_low"] == out["ci_high"] == out["failure_rate"]


def test_nan_goodness_is_skipped_per_criterion():
    r = make_rubric(("a", 1.0), ("b", 1.0))
    scores = frame([["L1", 0.0, np.nan], ["L2", 1.0, 0.5], ["L3", 1.0, np.nan]], ["a", "b"])
    by = diagnose(scores, r, top_k=1).set_index("criterion_id")
    assert by.loc["a", "n_leads"] == 3 and by.loc["b", "n_leads"] == 1
    assert by.loc["b", "failure_rate"] == pytest.approx(0.5)


def test_diagnose_rejects_bad_input():
    r = make_rubric(("a", 1.0), ("b", 1.0))
    with pytest.raises(ValueError, match=r"\['b'\]"):
        diagnose(frame([["L1", 0.0]], ["a"]), r, 1)
    with pytest.raises(ValueError, match="lead_id"):
        diagnose(pd.DataFrame({"a": [0.1], "b": [0.1]}), r, 1)
    with pytest.raises(ValueError, match="no scored calls"):
        diagnose(frame([], ["a", "b"]), r, 1)
    # a criterion with no applicable call (e.g. closure when no bot test ended) is reported, never a target
    out = diagnose(frame([["L1", 0.0, np.nan]], ["a", "b"]), r, 2).set_index("criterion_id")
    assert out.at["b", "n_leads"] == 0 and not out.at["b", "target"] and out.at["a", "target"]


def test_diagnose_ignores_extra_columns_and_input_order():
    r = make_rubric(("a", 1.0), ("b", 3.0))
    rows = [["L1", 0.2, 0.9], ["L2", 0.4, 0.5], ["L3", 0.6, 0.7], ["L1", 0.0, 0.8]]
    base = frame(rows, ["a", "b"]).assign(call_id=list("wxyz"), prompt_hash="h", failed=[()] * 4, rubric_score=0.5)
    shuffled = base.sample(frac=1, random_state=3)
    pd.testing.assert_frame_equal(diagnose(base, r, 1), diagnose(shuffled, r, 1))


# --- excerpts ---------------------------------------------------------------------------------

def tr(call_id, *turns):
    return Transcript(call_id=call_id, source="corpus", language=None,
                      turns=tuple(Turn(role=r, start_s=float(i), end_s=i + 1.0, text=t) for i, (r, t) in enumerate(turns)),
                      role_confidence=1.0, stt_model="t", audio_seconds=9.0)


def three_calls():
    ts = [tr("good", ("bot", "Hello"), ("buyer", "Hi")),
          tr("bad", ("bot", "Same question?"), ("buyer", "I said pump."), ("bot", "Same question?")),
          tr("worse", ("bot", "Q?"), ("buyer", "Call me on 9876543210"), ("bot", "Q?"), ("bot", "Q?"))]
    scores = pd.DataFrame({"call_id": ["good", "bad", "worse"], "looping": [1.0, 0.4, 0.1]})
    return ts, scores


@pytest.fixture
def fake_mask(monkeypatch):
    mod = types.ModuleType("abtest.loop.pii")
    mod.mask = lambda text, known_names=(): text.replace("9876543210", "<NUMBER>")
    monkeypatch.setitem(sys.modules, "abtest.loop.pii", mod)


def test_excerpts_worst_first_labelled_and_masked(fake_mask):
    ts, scores = three_calls()
    out = excerpts(ts, scores, "looping", n=2)
    assert out == ["BOT: Q?\nPERSON: Call me on <NUMBER>\nBOT: Q?\nBOT: Q?",
                   "BOT: Same question?\nPERSON: I said pump.\nBOT: Same question?"]


def test_excerpts_never_show_a_call_that_has_no_failure(fake_mask):
    ts, scores = three_calls()
    assert len(excerpts(ts, scores, "looping", n=10)) == 2           # "good" has goodness 1.0
    assert excerpts(ts, scores.assign(looping=1.0), "looping") == []


def test_excerpts_cut_to_max_chars_after_masking(fake_mask):
    ts, scores = three_calls()
    (cut,) = excerpts(ts, scores, "looping", n=1, max_chars=30)
    assert len(cut) <= 30 and cut.endswith("...") and cut.startswith("BOT: Q?\nPERSON: Call me on")
    assert "98765" not in cut                                        # masked first, so no partial number survives
    (tiny,) = excerpts(ts, scores, "looping", n=1, max_chars=5)
    assert len(tiny) == 5
    assert excerpts(ts, scores, "looping", n=1, max_chars=10_000)[0].endswith("BOT: Q?")


def test_excerpts_skip_calls_without_a_transcript_and_use_the_worst_row_per_call(fake_mask):
    ts, scores = three_calls()
    scores = pd.concat([scores, pd.DataFrame({"call_id": ["ghost", "bad"], "looping": [0.0, 0.9]})])
    out = excerpts(ts, scores, "looping", n=2)                       # "ghost" has no transcript; "bad" keeps its 0.4
    assert len(out) == 2 and out[0].startswith("BOT: Q?") and out[1].startswith("BOT: Same question?")


def test_excerpts_ties_break_by_call_id_and_n_zero(fake_mask):
    ts = [tr("b", ("bot", "B"), ("buyer", "x")), tr("a", ("bot", "A"), ("buyer", "x"))]
    scores = pd.DataFrame({"call_id": ["b", "a"], "looping": [0.5, 0.5]})
    assert [e.split("\n")[0] for e in excerpts(ts, scores, "looping", n=2)] == ["BOT: A", "BOT: B"]
    assert excerpts(ts, scores, "looping", n=0) == []


def test_excerpts_fall_back_to_identity_when_pii_is_not_importable(monkeypatch):
    monkeypatch.setitem(sys.modules, "abtest.loop.pii", None)        # makes `from abtest.loop.pii import mask` fail
    ts, scores = three_calls()
    assert "9876543210" in excerpts(ts, scores, "looping", n=1)[0]


def test_excerpts_use_the_real_masker_when_it_exists():
    pii = pytest.importorskip("abtest.loop.pii")
    ts, scores = three_calls()
    out = excerpts(ts, scores, "looping", n=1)[0]
    assert "9876543210" not in out and out == pii.mask("BOT: Q?\nPERSON: Call me on 9876543210\nBOT: Q?\nBOT: Q?")


def test_excerpts_missing_columns():
    ts, scores = three_calls()
    with pytest.raises(ValueError, match="nope"):
        excerpts(ts, scores, "nope")
