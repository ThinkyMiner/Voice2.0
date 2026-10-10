"""loop/sim_scores.py: synthetic per-call rubric_risk, deterministic per (seed, call_id)."""

import numpy as np
import pandas as pd
import pytest

from abtest.loop.sim_scores import score_sim_calls

RISK = {"BL Enriched": 0.08, "Abruptly Disconnected Call": 0.55, "default": 0.20}


def _calls(n=200, answered=True):
    disp = np.resize(["BL Enriched", "Abruptly Disconnected Call", "Queue Drop"], n)
    return pd.DataFrame({"call_id": [f"c{i}" for i in range(n)], "lead_id": [f"l{i // 2}" for i in range(n)],
                         "answered": answered, "disposition": disp})


def _variants(n=200):
    return {f"l{i}": ("B" if i % 2 else "A") for i in range(n // 2)}


def test_columns_and_answered_only():
    calls = _calls(12)
    calls.loc[[1, 5], "answered"] = False
    out = score_sim_calls(calls, _variants(12), 0.0, RISK, 0.05, 1)
    assert list(out.columns) == ["call_id", "rubric_risk"]
    assert list(out.call_id) == [c for i, c in enumerate(calls.call_id) if i not in (1, 5)]
    assert out.rubric_risk.between(0, 1).all()


def test_deterministic_and_seed_sensitive():
    calls, var = _calls(), _variants()
    a = score_sim_calls(calls, var, 0.05, RISK, 0.08, 7)
    b = score_sim_calls(calls, var, 0.05, RISK, 0.08, 7)
    c = score_sim_calls(calls, var, 0.05, RISK, 0.08, 8)
    pd.testing.assert_frame_equal(a, b)
    assert not np.allclose(a.rubric_risk, c.rubric_risk)


def test_a_call_keeps_its_score_whatever_else_is_in_the_frame():
    """Later looks score a bigger frame: earlier calls must not move (noise depends on seed+call_id only)."""
    calls, var = _calls(), _variants()
    full = score_sim_calls(calls, var, 0.05, RISK, 0.08, 3).set_index("call_id").rubric_risk
    part = score_sim_calls(calls.iloc[::-1].iloc[:50], var, 0.05, RISK, 0.08, 3).set_index("call_id").rubric_risk
    assert np.allclose(full.loc[part.index], part)


def test_shift_applies_to_arm_b_only():
    calls, var = _calls(), _variants()
    base = score_sim_calls(calls, var, 0.0, RISK, 0.0, 1)
    shifted = score_sim_calls(calls, var, 0.10, RISK, 0.0, 1)
    in_b = calls.lead_id.map(var).eq("B").to_numpy()
    diff = (shifted.rubric_risk - base.rubric_risk).to_numpy()
    assert np.allclose(diff[in_b], 0.10)
    assert np.allclose(diff[~in_b], 0.0)


def test_base_level_follows_the_disposition_with_default():
    calls, var = _calls(30), _variants(30)
    out = score_sim_calls(calls, var, 0.0, RISK, 0.0, 1).rubric_risk.to_numpy()
    want = calls.disposition.map({**RISK}).fillna(RISK["default"]).to_numpy()
    assert np.allclose(out, want)


def test_noise_has_the_requested_spread():
    n = 6000
    calls = pd.DataFrame({"call_id": [f"n{i}" for i in range(n)], "lead_id": "x", "answered": True,
                          "disposition": "default-ish"})
    out = score_sim_calls(calls, {}, 0.0, {"default": 0.5}, 0.05, 11).rubric_risk
    assert out.mean() == pytest.approx(0.5, abs=0.003)
    assert out.std() == pytest.approx(0.05, rel=0.06)


def test_clipped_to_unit_interval():
    calls, var = _calls(), _variants()
    out = score_sim_calls(calls, var, 0.9, RISK, 0.2, 1).rubric_risk
    assert out.min() >= 0.0 and out.max() <= 1.0
    assert (out == 1.0).any()
    low = score_sim_calls(calls, var, -0.9, RISK, 0.2, 1).rubric_risk
    assert (low == 0.0).any()


def test_unlisted_lead_is_arm_a_and_missing_default_raises():
    calls = _calls(4)
    same = score_sim_calls(calls, {}, 0.3, RISK, 0.0, 1)
    assert np.allclose(same.rubric_risk, score_sim_calls(calls, {}, 0.0, RISK, 0.0, 1).rubric_risk)
    with pytest.raises(ValueError, match="default"):
        score_sim_calls(calls, {}, 0.0, {"BL Enriched": 0.1}, 0.0, 1)
    with pytest.raises(ValueError, match="missing columns"):
        score_sim_calls(calls.drop(columns="disposition"), {}, 0.0, RISK, 0.0, 1)


def test_no_answered_calls_gives_an_empty_frame():
    out = score_sim_calls(_calls(5, answered=False), {}, 0.0, RISK, 0.1, 1)
    assert out.empty and list(out.columns) == ["call_id", "rubric_risk"]
