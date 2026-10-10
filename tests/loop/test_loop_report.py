"""The rubric report holds aggregates only: right numbers, small groups flagged, no ids anywhere."""

import math
import re

import numpy as np
import pandas as pd
import pytest

from abtest.loop.contracts import Criterion, JudgeSettings, Rubric
from abtest.loop.report import rubric_report
from abtest.loop.scorer import per_lead

Z = 1.959963984540054


def make_rubric():
    return Rubric(id="rep-test", judge=JudgeSettings(model="m"), risk=("claimed_human",), criteria=(
        Criterion(id="claimed_human", kind="yes_no", polarity="bad", weight=4, text="human?", hard=True),
        Criterion(id="looping", kind="yes_no", polarity="bad", weight=3, text="loop?"),
        Criterion(id="closure", kind="score_1_5", polarity="good", weight=2, text="closure?")))


def corpus(n_big=12, n_small=3):
    """Per-call scores and calls: `big` bot version with n_big leads, `small` with n_small leads; some
    leads called twice; a few short and unanswered calls that were never scored."""
    rng = np.random.default_rng(11)
    sc, ca = [], []
    k = 0
    for version, n_leads in (("big", n_big), ("small", n_small)):
        for i in range(n_leads):
            for rep in range(1 + (i % 2)):
                cid = f"CALL-SECRET-{k:04d}"
                ca.append({"call_id": cid, "lead_id": f"LEAD-SECRET-{version}{i:02d}", "bot_version": version,
                           "answered": True, "duration_s": float(rng.integers(20, 90))})
                sc.append({"call_id": cid, "rubric_score": float(rng.uniform(0.3, 1)), "rubric_risk": float(rng.uniform(0, 0.5)),
                           "failed": (), "claimed_human": float(rng.choice([1.0, 0.98, 0.0])),
                           "looping": float(rng.uniform(0.4, 1)), "closure": float(rng.choice([0, .25, .5, .75, 1]))})
                k += 1
    for j, (ver, ans, dur) in enumerate([("big", True, 4.0), ("big", True, 7.5), ("small", True, 9.9),
                                         ("big", False, 0.0), ("big", True, 30.0)]):
        ca.append({"call_id": f"CALL-SECRET-X{j}", "lead_id": f"LEAD-SECRET-X{j}", "bot_version": ver,
                   "answered": ans, "duration_s": dur})
    return pd.DataFrame(sc), pd.DataFrame(ca)


@pytest.fixture
def built():
    scores, calls = corpus()
    return per_lead(scores, calls), calls, scores


def test_report_writes_the_named_file_and_returns_its_path(tmp_path, built):
    pl, calls, _ = built
    path = rubric_report(pl, calls, make_rubric(), "squadstack_2026-10-06", tmp_path / "reports")
    assert path == tmp_path / "reports" / "rubric_squadstack_2026-10-06.md" and path.is_file()
    text = path.read_text()
    assert text.startswith("# Rubric report: squadstack_2026-10-06") and "`rep-test`" in text


def test_report_contains_no_ids_numbers_or_names(tmp_path, built):
    pl, calls, scores = built
    text = rubric_report(pl, calls, make_rubric(), "c", tmp_path).read_text()
    for forbidden in [*calls["call_id"], *calls["lead_id"], "SECRET", "CALL-", "LEAD-"]:
        assert forbidden not in text
    assert not re.search(r"\d{7,}", text)                                    # no phone-number-like digit runs
    assert not re.search(r"\b[0-9a-f]{16,}\b", text.replace(text[text.index("hash `") + 6:][:12], ""))  # only the 12-char hash prefix
    assert "BOT:" not in text and "PERSON:" not in text


def test_corpus_counts_and_early_hangup_share(tmp_path, built):
    pl, calls, scores = built
    text = rubric_report(pl, calls, make_rubric(), "c", tmp_path).read_text()
    n_calls = len(calls)
    assert f"| Calls in corpus | {n_calls} |" in text
    assert f"| Calls answered | {int(calls['answered'].sum())} |" in text
    assert f"| Calls scored | {len(scores)} |" in text and f"| Leads scored | {len(pl)} |" in text
    answered = calls[calls["answered"]]
    early = answered[answered["duration_s"] < 10]
    assert len(early) == 3
    assert f"| all | {len(answered)} | 3 | {100 * 3 / len(answered):.1f}% |" in text
    small = answered[answered["bot_version"] == "small"]
    assert f"| small | {len(small)} | 1 | {100 / len(small):.1f}% |" in text   # 9.9 s counts; unanswered calls do not


def test_early_hangup_edge_cases(tmp_path):
    scores, calls = corpus()
    pl = per_lead(scores, calls)
    no_dur = rubric_report(pl, calls.drop(columns="duration_s"), make_rubric(), "nodur", tmp_path).read_text()
    assert "Early hang-up share: n/a" in no_dur
    no_answered = rubric_report(pl, calls.drop(columns="answered"), make_rubric(), "noans", tmp_path).read_text()
    assert f"| Calls answered | {len(calls)} |" in no_answered                    # no column: every call counts as answered
    blank = calls.assign(duration_s=np.nan)
    assert "No answered call has a duration" in rubric_report(pl, blank, make_rubric(), "blank", tmp_path).read_text()


def test_score_quantiles(tmp_path, built):
    pl, calls, _ = built
    text = rubric_report(pl, calls, make_rubric(), "c", tmp_path).read_text()
    s = pl["rubric_score"].to_numpy()
    q = np.quantile(s, [0.10, 0.25, 0.50, 0.75, 0.90])
    for label, v in [("mean", s.mean()), ("min", s.min()), ("p10", q[0]), ("p25", q[1]), ("p50", q[2]),
                     ("p75", q[3]), ("p90", q[4]), ("max", s.max())]:
        assert f"| {label} | {v:.3f} |" in text


def test_failure_rate_with_lead_level_ci_overall_and_by_version(tmp_path, built):
    pl, calls, _ = built
    text = rubric_report(pl, calls, make_rubric(), "c", tmp_path).read_text()
    fail = 1 - pl["looping"].to_numpy()
    half = Z * fail.std(ddof=1) / math.sqrt(len(fail))
    expect = f"{100 * fail.mean():.1f}% ({100 * max(0, fail.mean() - half):.1f}% to {100 * min(1, fail.mean() + half):.1f}%)"
    assert f"| `looping` | 3 | " in text and expect in text
    assert "| `claimed_human` | 4 | yes |" in text                              # weight and hard flag shown

    big = pl[pl["bot_version"] == "big"]["closure"].to_numpy()
    f_big = 1 - big
    h = Z * f_big.std(ddof=1) / math.sqrt(len(f_big))
    cell_big = f"{100 * f_big.mean():.1f}% ({100 * max(0, f_big.mean() - h):.1f}% to {100 * min(1, f_big.mean() + h):.1f}%)"
    by_version = text.split("### By bot version")[1].split("##")[0]
    row = next(line for line in by_version.splitlines() if line.startswith("| `closure` |"))
    assert row.split("|")[2].strip() == cell_big                              # the "big" column (alphabetical: big, small)


def test_groups_under_five_leads_have_no_ci_and_are_flagged(tmp_path, built):
    pl, calls, _ = built
    text = rubric_report(pl, calls, make_rubric(), "c", tmp_path).read_text()
    assert "big (leads=12)" in text and "small (leads=3)" in text
    by_version = text.split("### By bot version")[1].split("##")[0]
    rows = [ln for ln in by_version.splitlines() if ln.startswith("| `")]
    assert len(rows) == 3
    for ln in rows:
        big_cell, small_cell = ln.split("|")[2], ln.split("|")[3]
        assert "(n=3, no CI)" in small_cell and " to " not in small_cell      # flagged, no interval
        assert " to " in big_cell and "no CI" not in big_cell                 # 12 leads: interval shown


def test_mean_rubric_risk_overall_and_by_version(tmp_path, built):
    pl, calls, _ = built
    text = rubric_report(pl, calls, make_rubric(), "c", tmp_path).read_text()
    risk = pl["rubric_risk"].to_numpy()
    section = text.split("## Mean rubric_risk")[1]
    assert f"| all | {len(pl)} | {risk.mean():.3f} (" in section
    assert "`claimed_human`" in section.splitlines()[2]                       # names the risk criteria
    small = pl[pl["bot_version"] == "small"]["rubric_risk"]
    assert f"| small | 3 | {small.mean():.3f} (n=3, no CI) |" in section


def test_report_is_deterministic_and_idempotent(tmp_path, built):
    pl, calls, _ = built
    first = rubric_report(pl, calls, make_rubric(), "c", tmp_path).read_bytes()
    second = rubric_report(pl.sample(frac=1, random_state=1), calls.sample(frac=1, random_state=1), make_rubric(),
                           "c", tmp_path).read_bytes()
    assert first == second                                                    # no timestamp, no dependence on row order


def test_corpus_name_cannot_escape_the_output_dir(tmp_path, built):
    pl, calls, _ = built
    path = rubric_report(pl, calls, make_rubric(), "../../evil name", tmp_path / "out")
    assert path.parent == tmp_path / "out" and path.name == "rubric_.._.._evil_name.md"


def test_report_with_a_single_version_or_no_version_column(tmp_path, built):
    pl, calls, _ = built
    only = rubric_report(pl[pl["bot_version"] == "big"], calls, make_rubric(), "one", tmp_path).read_text()
    assert "small (leads" not in only and "big (leads=12)" in only
    plain = rubric_report(pl.drop(columns="bot_version"), calls.drop(columns="bot_version"), make_rubric(),
                          "none", tmp_path).read_text()
    assert "### By bot version" not in plain and "### All leads" in plain


def test_report_rejects_a_frame_missing_a_criterion(tmp_path, built):
    pl, calls, _ = built
    with pytest.raises(ValueError, match="closure"):
        rubric_report(pl.drop(columns="closure"), calls, make_rubric(), "c", tmp_path)


def test_report_with_no_scored_leads_still_renders(tmp_path, built):
    pl, calls, _ = built
    text = rubric_report(pl.iloc[0:0], calls, make_rubric(), "empty", tmp_path).read_text()
    assert "No lead has a rubric_score" in text and "| Leads scored | 0 |" in text
