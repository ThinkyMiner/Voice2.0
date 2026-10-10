"""Audit sheet export (stratification, determinism) and judge-vs-human agreement (rho, kappa, trust)."""

import math

import numpy as np
import pandas as pd
import pytest
from scipy.stats import spearmanr

from abtest.loop.audit import MIN_AUDIT_ROWS, _allocate, _kappa, agreement, export_audit
from abtest.loop.contracts import Criterion, JudgeSettings, PassThresholds, Rubric

Z = 1.959963984540054


def make_rubric(**kw):
    crit = (Criterion(id="claimed_human", kind="yes_no", polarity="bad", weight=4, text="human?", hard=True),
            Criterion(id="false_assurance", kind="yes_no", polarity="bad", weight=4, text="assure?", hard=True),
            Criterion(id="looping", kind="yes_no", polarity="bad", weight=3, text="loop?"),
            Criterion(id="closure", kind="score_1_5", polarity="good", weight=2, text="closure?"))
    return Rubric(id="t", judge=JudgeSettings(model="m"), criteria=crit, **kw)


def scores_and_calls(sizes, seed=0):
    """A scores frame (goodness columns) and calls frame; `sizes` = {bot_version: n_calls}."""
    rng = np.random.default_rng(seed)
    ids, versions = [], []
    for v, n in sizes.items():
        ids += [f"{v}-{i:03d}" for i in range(n)]
        versions += [v] * n
    n = len(ids)
    scores = pd.DataFrame({"call_id": ids, "rubric_score": rng.uniform(size=n), "rubric_risk": rng.uniform(size=n),
                           "failed": [()] * n, "claimed_human": rng.uniform(size=n), "false_assurance": rng.uniform(size=n),
                           "looping": rng.uniform(size=n), "closure": rng.choice([0, .25, .5, .75, 1], size=n)})
    return scores, pd.DataFrame({"call_id": ids, "lead_id": [f"L{i}" for i in range(n)], "bot_version": versions})


# --- export -----------------------------------------------------------------------------------

def test_export_is_stratified_proportional_with_one_per_stratum(tmp_path):
    scores, calls = scores_and_calls({"base": 80, "personalised": 19, "summary": 1})
    path = export_audit(scores, calls, n=20, seed=7, out=tmp_path / "audit" / "g1.csv")
    sheet = pd.read_csv(path, dtype=str, keep_default_na=False)
    counts = sheet["bot_version"].value_counts().to_dict()
    assert counts == {"base": 16, "personalised": 3, "summary": 1}         # proportional, summary kept
    assert len(sheet) == 20 and sheet["call_id"].is_unique
    assert set(sheet["call_id"]) <= set(scores["call_id"])


def test_export_columns_and_empty_reviewer_fields(tmp_path):
    scores, calls = scores_and_calls({"a": 10, "b": 10})
    sheet = pd.read_csv(export_audit(scores, calls, 8, 1, tmp_path / "s.csv"), dtype=str, keep_default_na=False)
    expected = ["call_id", "bot_version", "rubric_score",
                "judge_claimed_human", "judge_false_assurance", "judge_looping", "judge_closure",
                "human_score", "human_role_ok",
                "human_claimed_human", "human_false_assurance", "human_looping", "human_closure"]
    assert list(sheet.columns) == expected
    for col in [c for c in expected if c.startswith("human_")]:
        assert (sheet[col] == "").all()
    merged = sheet.merge(scores, on="call_id")
    assert np.allclose(merged["rubric_score_x"].astype(float), merged["rubric_score_y"], atol=1e-6)
    assert np.allclose(merged["judge_claimed_human"].astype(float), merged["claimed_human"])


def test_export_is_deterministic_per_seed_and_shuffled(tmp_path):
    scores, calls = scores_and_calls({"a": 50, "b": 50})
    one = export_audit(scores, calls, 30, 5, tmp_path / "1.csv").read_bytes()
    two = export_audit(scores, calls, 30, 5, tmp_path / "2.csv").read_bytes()
    other = export_audit(scores, calls, 30, 6, tmp_path / "3.csv").read_bytes()
    assert one == two and one != other
    sheet = pd.read_csv(tmp_path / "1.csv")
    assert not sheet["call_id"].is_monotonic_increasing                      # not grouped by arm or sorted
    # input order does not matter
    again = export_audit(scores.sample(frac=1, random_state=1), calls.sample(frac=1, random_state=2), 30, 5,
                         tmp_path / "4.csv").read_bytes()
    assert again == one


def test_export_takes_everything_when_n_exceeds_the_population(tmp_path):
    scores, calls = scores_and_calls({"a": 4, "b": 3})
    sheet = pd.read_csv(export_audit(scores, calls, 100, 1, tmp_path / "s.csv"))
    assert len(sheet) == 7


def test_export_handles_missing_bot_version_unscored_calls_and_bad_n(tmp_path):
    scores, calls = scores_and_calls({"a": 6})
    calls.loc[:2, "bot_version"] = None                                       # unknown stratum
    extra = pd.DataFrame({"call_id": ["never_scored"], "lead_id": ["L"], "bot_version": ["a"]})
    sheet = pd.read_csv(export_audit(scores, pd.concat([calls, extra]), 4, 1, tmp_path / "s.csv"))
    assert "never_scored" not in set(sheet["call_id"]) and set(sheet["bot_version"]) == {"a", "unknown"}
    with pytest.raises(ValueError, match="positive"):
        export_audit(scores, calls, 0, 1, tmp_path / "x.csv")
    with pytest.raises(ValueError, match="nothing to audit"):
        export_audit(scores, calls.assign(call_id="zzz"), 3, 1, tmp_path / "y.csv")
    no_version = export_audit(scores, calls.drop(columns="bot_version"), 3, 1, tmp_path / "z.csv")
    assert set(pd.read_csv(no_version)["bot_version"]) == {"unknown"}


@pytest.mark.parametrize("sizes,n", [({"a": 90, "b": 9, "c": 1}, 10), ({"a": 5, "b": 5, "c": 5}, 7),
                                     ({"a": 100, "b": 2}, 3), ({"a": 3, "b": 3, "c": 3, "d": 1}, 9)])
def test_allocate_properties(sizes, n):
    alloc = _allocate(sizes, n)
    assert sum(alloc.values()) == min(n, sum(sizes.values()))
    assert all(1 <= alloc[h] <= sizes[h] for h in sizes)


def test_allocate_exceeds_n_only_when_n_is_below_the_number_of_strata():
    alloc = _allocate({"a": 10, "b": 10, "c": 10}, 2)
    assert alloc == {"a": 1, "b": 1, "c": 1}


# --- agreement --------------------------------------------------------------------------------

def sheet_frame(n, human_score, judge_score, claimed=None, assured=None, role=None, p_claimed=None):
    """A filled sheet. `claimed`/`assured`: lists of 'y'/'n'. Judge columns hold goodness, as export writes."""
    claimed = claimed or ["n"] * n
    assured = assured or ["n"] * n
    # judge goodness for the bad criteria: the judge agrees with the human unless p_claimed overrides it
    p_c = p_claimed if p_claimed is not None else [0.95 if v == "y" else 0.02 for v in claimed]
    p_a = [0.9 if v == "y" else 0.03 for v in assured]
    return pd.DataFrame({
        "call_id": [f"c{i}" for i in range(n)], "bot_version": "v", "rubric_score": judge_score,
        "judge_claimed_human": [1 - p for p in p_c], "judge_false_assurance": [1 - p for p in p_a],
        "judge_looping": 0.9, "judge_closure": 0.5,
        "human_score": human_score, "human_role_ok": role if role is not None else "y",
        "human_claimed_human": claimed, "human_false_assurance": assured, "human_looping": "", "human_closure": "",
    })


def write(df, tmp_path, name="sheet.csv"):
    p = tmp_path / name
    df.to_csv(p, index=False)
    return p


def test_agreement_trusts_a_judge_that_ranks_like_the_human_and_agrees_on_hard_criteria(tmp_path):
    n = 40
    human = [1 + (i % 5) for i in range(n)]
    judge = [0.1 + 0.18 * h + 0.001 * i for i, h in enumerate(human)]          # monotone in the human score, a few ties
    claimed = ["y" if i in (3, 17) else "n" for i in range(n)]
    out = agreement(write(sheet_frame(n, human, judge, claimed=claimed), tmp_path), make_rubric(), 0.6, 0.9)
    assert out["n"] == n and out["rows"] == n
    assert out["rho"] > 0.95 and out["trusted"] is True and out["reason"].startswith("Trusted")
    lo, hi = out["rho_ci95"]
    assert lo < out["rho"] <= hi <= 1.0
    c = out["criteria"]["claimed_human"]
    assert c["n"] == n and c["agreement"] == 1.0 and c["kappa"] == 1.0 and c["prevalence"] == pytest.approx(2 / 40)
    assert c["judge_rate"] == pytest.approx(2 / 40)
    assert out["criteria"]["false_assurance"]["kappa"] is None                  # nobody ever says yes: kappa undefined
    assert out["role_accuracy"] == 1.0 and out["role_n"] == n
    assert set(out["criteria"]) == {"claimed_human", "false_assurance"}         # hard criteria only


def test_rho_interval_is_fisher_z(tmp_path):
    n = 60
    rng = np.random.default_rng(3)
    human = rng.integers(1, 6, size=n)
    judge = human + rng.normal(scale=1.4, size=n)
    out = agreement(write(sheet_frame(n, human, judge), tmp_path), make_rubric(), 0.0001, 0.5)
    rho = spearmanr(judge, human).statistic
    se = 1 / math.sqrt(n - 3)
    assert out["rho"] == pytest.approx(rho)
    assert out["rho_ci95"] == pytest.approx((math.tanh(math.atanh(rho) - Z * se), math.tanh(math.atanh(rho) + Z * se)))


def test_always_no_judge_has_high_raw_agreement_but_zero_kappa(tmp_path):
    """Hard failures are rare: the judge says 'no' every time, the human found 4 of 40."""
    n = 40
    human = [1 + (i % 5) for i in range(n)]
    judge = [0.1 + 0.18 * h for h in human]
    claimed = ["y" if i < 4 else "n" for i in range(n)]
    df = sheet_frame(n, human, judge, claimed=claimed, p_claimed=[0.01] * n)    # the judge never fires
    out = agreement(write(df, tmp_path), make_rubric(), 0.6, 0.9)
    c = out["criteria"]["claimed_human"]
    assert c["agreement"] == pytest.approx(0.9)                                 # looks fine ...
    assert c["kappa"] == 0.0                                                    # ... but is exactly chance level
    assert c["prevalence"] == pytest.approx(0.1) and c["judge_rate"] == 0.0
    assert out["trusted"] is True                                               # the spec's trust rule is raw agreement
    assert not any("claimed_human" in w for w in out["warnings"])               # 4 positives: enough to read kappa
    assert any("false_assurance: the reviewer marked only 0 of 40" in w for w in out["warnings"])


def test_scarce_positives_raise_a_warning_naming_the_criterion(tmp_path):
    n = 40
    human = [1 + (i % 5) for i in range(n)]
    judge = [0.1 + 0.18 * h for h in human]
    claimed = ["y" if i == 0 else "n" for i in range(n)]
    out = agreement(write(sheet_frame(n, human, judge, claimed=claimed, p_claimed=[0.01] * n), tmp_path),
                    make_rubric(), 0.6, 0.9)
    assert out["criteria"]["claimed_human"]["agreement"] == pytest.approx(0.975)
    assert out["criteria"]["claimed_human"]["kappa"] == 0.0
    assert any("claimed_human" in w for w in out["warnings"]) and any("false_assurance" in w for w in out["warnings"])


def test_kappa_exact_values():
    assert _kappa(10, 0, 0, 30) == 1.0
    assert _kappa(20, 5, 10, 15) == pytest.approx(0.4)                          # po .7, pe .5
    assert _kappa(0, 5, 0, 35) == 0.0
    assert _kappa(0, 0, 0, 40) is None and _kappa(40, 0, 0, 0) is None         # both constant
    assert _kappa(0, 20, 20, 0) == -1.0                                         # complete disagreement


def test_judge_polarity_conversion_for_bad_and_good_criteria(tmp_path):
    rubric = Rubric(id="t", judge=JudgeSettings(model="m"), criteria=(
        Criterion(id="bad_c", kind="yes_no", polarity="bad", weight=1, text="?", hard=True),
        Criterion(id="good_c", kind="yes_no", polarity="good", weight=1, text="?", hard=True)),
        pass_thresholds=PassThresholds(bad=0.5, good=0.5))
    n = 24
    df = pd.DataFrame({"call_id": [f"c{i}" for i in range(n)], "rubric_score": np.linspace(0, 1, n),
                       "human_score": [1 + (i % 5) for i in range(n)], "human_role_ok": "y",
                       # bad_c: goodness 0.1 means p(yes) = 0.9 -> judge yes; human y for the first half
                       "judge_bad_c": [0.1] * (n // 2) + [0.9] * (n // 2),
                       "human_bad_c": ["y"] * (n // 2) + ["n"] * (n // 2),
                       # good_c: goodness is p(yes) itself
                       "judge_good_c": [0.9] * (n // 2) + [0.1] * (n // 2),
                       "human_good_c": ["y"] * (n // 2) + ["n"] * (n // 2)})
    out = agreement(write(df, tmp_path), rubric, 0.0001, 0.9)
    assert out["criteria"]["bad_c"]["agreement"] == 1.0 and out["criteria"]["good_c"]["agreement"] == 1.0
    flipped = df.assign(human_bad_c=["n"] * (n // 2) + ["y"] * (n // 2))
    assert agreement(write(flipped, tmp_path, "f.csv"), rubric, 0.0001, 0.9)["criteria"]["bad_c"]["agreement"] == 0.0


def test_nothing_filled_is_not_trusted_with_a_reason(tmp_path):
    scores, calls = scores_and_calls({"a": 20, "b": 20})
    path = export_audit(scores, calls, 30, 1, tmp_path / "blank.csv")           # straight from the export, untouched
    out = agreement(path, make_rubric(), 0.6, 0.9)
    assert out["trusted"] is False and out["n"] == 0 and out["rows"] == 30
    assert out["rho"] is None and out["rho_ci95"] is None and out["role_accuracy"] is None
    assert out["reason"] == "Not trusted: no row has a human_score yet; claimed_human has no human labels; " \
                            "false_assurance has no human labels."


def test_too_few_rows_is_not_trusted(tmp_path):
    n = MIN_AUDIT_ROWS - 1
    human = [1 + (i % 5) for i in range(n)]
    out = agreement(write(sheet_frame(n, human, [0.2 * h for h in human]), tmp_path), make_rubric(), 0.6, 0.9)
    assert out["trusted"] is False and f"only {n} rows" in out["reason"] and out["rho"] > 0.9


def test_low_rho_is_not_trusted(tmp_path):
    n = 40
    human = [1 + (i % 5) for i in range(n)]
    judge = [0.1 * ((i * 7) % 9) for i in range(n)]                              # unrelated to the human score
    out = agreement(write(sheet_frame(n, human, judge), tmp_path), make_rubric(), 0.6, 0.9)
    assert out["trusted"] is False and out["rho"] < 0.6 and "rho" in out["reason"]
    assert out["reason"].startswith("Not trusted: rho") and out["reason"].endswith(".")


def test_low_hard_agreement_is_not_trusted_and_named(tmp_path):
    n = 40
    human = [1 + (i % 5) for i in range(n)]
    judge = [0.1 + 0.18 * h for h in human]
    claimed = ["y" if i % 4 == 0 else "n" for i in range(n)]
    wrong = [0.9 if i % 8 == 0 else 0.02 for i in range(n)]                      # the judge misses half the human's 'y'
    out = agreement(write(sheet_frame(n, human, judge, claimed=claimed, p_claimed=wrong), tmp_path), make_rubric(), 0.6, 0.9)
    assert out["trusted"] is False
    assert out["criteria"]["claimed_human"]["agreement"] == pytest.approx(0.875)
    assert "claimed_human agreement 0.88 is below 0.9" in out["reason"] and "false_assurance" not in out["reason"]
    # table: a=5 (both y), b=5 (human y only), c=0, d=30 -> po .875, ph .25, pj .125, pe = .25*.125 + .75*.875 = .6875
    assert out["criteria"]["claimed_human"]["kappa"] == pytest.approx((0.875 - 0.6875) / (1 - 0.6875))   # = 0.6


def test_partially_filled_sheet_uses_only_filled_rows(tmp_path):
    n = 30
    human = [1 + (i % 5) for i in range(n)]
    df = sheet_frame(n, human, [0.2 * h for h in human], role=["y"] * 10 + ["n"] * 5 + [""] * 15)
    df["human_score"] = df["human_score"].astype(object)
    df.loc[20:, "human_score"] = ""                                              # only 20 scored by the reviewer
    df.loc[10:, ["human_claimed_human", "human_false_assurance"]] = ""           # 10 labelled for the hard criteria
    out = agreement(write(df, tmp_path), make_rubric(), 0.6, 0.9)
    assert out["n"] == 20 and out["rows"] == 30
    assert out["criteria"]["claimed_human"]["n"] == 10
    assert out["role_n"] == 15 and out["role_accuracy"] == pytest.approx(10 / 15)
    assert out["trusted"] is True


def test_constant_scores_make_rho_undefined(tmp_path):
    n = 30
    out = agreement(write(sheet_frame(n, [3] * n, [0.5] * n), tmp_path), make_rubric(), 0.6, 0.9)
    assert out["rho"] is None and out["rho_ci95"] is None and out["trusted"] is False and "undefined" in out["reason"]


def test_agreement_rejects_malformed_sheets(tmp_path):
    n = 25
    good = sheet_frame(n, [1 + i % 5 for i in range(n)], np.linspace(0, 1, n))
    with pytest.raises(ValueError, match="between 1 and 5"):
        agreement(write(good.assign(human_score=[7] + [3] * (n - 1)), tmp_path), make_rubric(), 0.6, 0.9)
    with pytest.raises(ValueError, match="1 to 5"):
        agreement(write(good.assign(human_score=["abc"] + ["3"] * (n - 1)), tmp_path, "b.csv"), make_rubric(), 0.6, 0.9)
    with pytest.raises(ValueError, match="expected y or n"):
        agreement(write(good.assign(human_claimed_human=["maybe"] + ["n"] * (n - 1)), tmp_path, "c.csv"), make_rubric(), 0.6, 0.9)
    with pytest.raises(ValueError, match="lacks columns"):
        agreement(write(good.drop(columns="human_false_assurance"), tmp_path, "d.csv"), make_rubric(), 0.6, 0.9)


def test_yes_no_cells_accept_common_spellings(tmp_path):
    n = 30
    human = [1 + (i % 5) for i in range(n)]
    claimed = (["Y", "yes", "TRUE", "1"] + ["N", "no", "false", "0"] * 3)[:n]
    claimed += ["n"] * (n - len(claimed))
    out = agreement(write(sheet_frame(n, human, [0.2 * h for h in human], claimed=claimed, p_claimed=[0.01] * n),
                          tmp_path), make_rubric(), 0.6, 0.5)
    assert out["criteria"]["claimed_human"]["prevalence"] == pytest.approx(4 / n)


def test_export_then_fill_then_agree_round_trip(tmp_path):
    """The sheet export writes is exactly what agreement reads, judge columns included."""
    scores, calls = scores_and_calls({"a": 30, "b": 30}, seed=4)
    path = export_audit(scores, calls, 40, 2, tmp_path / "sheet.csv")
    sheet = pd.read_csv(path, dtype=str, keep_default_na=False)
    judge_p = 1 - sheet["judge_claimed_human"].astype(float)                    # goodness -> p(yes) for a bad criterion
    sheet["human_score"] = (1 + (sheet["rubric_score"].astype(float) * 4.99).astype(int)).astype(str)
    sheet["human_role_ok"] = "y"
    sheet["human_claimed_human"] = np.where(judge_p >= 0.5, "y", "n")           # a human who agrees with the judge
    sheet["human_false_assurance"] = np.where(1 - sheet["judge_false_assurance"].astype(float) >= 0.5, "y", "n")
    sheet.to_csv(path, index=False)
    out = agreement(path, make_rubric(), 0.6, 0.9)
    assert out["n"] == 40 and out["rho"] > 0.9 and out["trusted"] is True
    assert out["criteria"]["claimed_human"]["agreement"] == 1.0


def test_sheet_from_scores_frame_lists_only_the_hard_criteria_and_round_trips(tmp_path):
    """scores_frame records its rubric, so the export needs no rubric argument and the reviewer sees
    only the hard criteria; agreement reads that sheet the same way."""
    from abtest.loop.contracts import RUBRICS_DIR, Transcript, Turn
    from abtest.loop.rubric import load_rubric
    from abtest.loop.scorer import score_calls, scores_frame

    class Judge:
        name = "fake"

        def complete_json(self, role, system, user, schema):
            human = "I am a human" in user
            return {"claimed_human": 0.95 if human else 0.02, "false_assurance": 0.03, "wrong_language": 0.03,
                    "looping": 0.05, "frustration": 0.08, "asked_for_human": 0.05, "requirement_captured": 4,
                    "closure": 3}

    def call(i):
        say = "I am a human, hello" if i % 4 == 0 else "Namaste, kaunsa product chahiye?"
        return Transcript(call_id=f"c{i:02d}", source="corpus", language=None, role_confidence=1.0, stt_model="t",
                          audio_seconds=9.0, turns=(Turn(role="bot", start_s=0, end_s=1, text=say),
                                                    Turn(role="buyer", start_s=1, end_s=2, text="Pump chahiye")))

    r = load_rubric(RUBRICS_DIR / "vani-call-quality-v1.yaml")
    frame = scores_frame(score_calls([call(i) for i in range(24)], r, Judge()))
    calls = pd.DataFrame({"call_id": frame["call_id"], "lead_id": frame["call_id"],
                          "bot_version": ["a" if i % 2 else "b" for i in range(24)]})
    path = export_audit(frame, calls, 24, 1, tmp_path / "sheet.csv")
    sheet = pd.read_csv(path, dtype=str, keep_default_na=False)
    assert [c for c in sheet.columns if c.startswith(("judge_", "human_"))] == [
        "judge_claimed_human", "judge_false_assurance", "human_score", "human_role_ok",
        "human_claimed_human", "human_false_assurance"]

    judge_yes = sheet["judge_claimed_human"].astype(float) < 0.5                  # goodness < .5 <=> p(yes) >= .5
    sheet["human_score"] = np.where(judge_yes, "1", "5")
    sheet["human_role_ok"] = "y"
    sheet["human_claimed_human"] = np.where(judge_yes, "y", "n")
    sheet["human_false_assurance"] = "n"
    sheet.to_csv(path, index=False)
    out = agreement(path, r, 0.5, 0.9)
    assert out["n"] == 24 and out["criteria"]["claimed_human"]["agreement"] == 1.0
    assert out["criteria"]["claimed_human"]["kappa"] == 1.0 and out["criteria"]["claimed_human"]["prevalence"] == 0.25
    assert out["criteria"]["false_assurance"]["kappa"] is None
