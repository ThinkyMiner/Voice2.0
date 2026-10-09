"""Profile tests: hand-computed volume and baseline-p cases, plus a real-data smoke test."""

import math

import pandas as pd
import pytest
import yaml

from abtest.contracts import CONFIGS_DIR, DATA_DIR, ExperimentConfig
from abtest.data import FILES
from abtest.profile import build_profile, success_by_horizon, volume_stats, write_profile

T0 = pd.Timestamp("2026-01-01 10:00")
MF = ("Meeting Fixed",)


def _frame(rows):
    """rows: (lead_id, hours after T0, answered, disposition)."""
    df = pd.DataFrame(rows, columns=["lead_id", "h", "answered", "disposition"])
    df.insert(0, "call_id", [str(i) for i in range(len(df))])
    df["ts"] = T0 + pd.to_timedelta(df.pop("h"), unit="h")
    df["bot_version"] = None
    return df


def test_volume_stats_drops_partial_block():
    # window 3: block 0 = days 0-2, block 1 = days 3-5, day 6 is a partial block and is dropped
    df = _frame([
        ("a", 0, True, "x"), ("a", 24, True, "x"), ("b", 24, False, "x"), ("c", 48, False, "x"),
        ("a", 72, False, "x"), ("d", 120, True, "x"),
        ("e", 144, True, "x"),
    ])
    v = volume_stats(df, 3)
    assert v.window_days == 3
    assert math.isclose(v.daily_leads, (3 + 2) / 2 / 3)          # {a,b,c}, {a,d}
    assert math.isclose(v.daily_triggered_leads, (1 + 1) / 2 / 3)  # {a}, {d}


def test_volume_stats_counts_empty_full_block_as_zero():
    df = _frame([("a", 0, True, "x"), ("b", 24 * 5, True, "x")])  # days 0 and 5, window 2
    v = volume_stats(df, 2)  # blocks {a}, {}, {b}
    assert math.isclose(v.daily_leads, (1 + 0 + 1) / 3 / 2)


def test_volume_stats_span_shorter_than_window():
    df = _frame([("a", 0, True, "x"), ("b", 24, False, "x")])  # 2 days, window 5
    assert math.isclose(volume_stats(df, 5).daily_leads, 2 / 2)


def test_baseline_horizon_rule():
    df = _frame([
        ("L1", 0, True, "General (talked)"), ("L1", 48, True, "Meeting Fixed"),   # success at exactly 2 days
        ("L2", 0, True, "Meeting Fixed"),                                         # success at the trigger
        ("L3", 0, True, "General (talked)"), ("L3", 24, False, "Meeting Fixed"),  # unanswered: not a success
        ("L4", 216, True, "Meeting Fixed"),                                       # triggered on day 9
        ("L5", 0, False, "Not Answered"), ("L5", 240, False, "Not Answered"),     # never triggered; sets end = day 10
    ])
    tab = success_by_horizon(df, MF, [1, 2, 3, 11])
    # H=1: all four triggered leads have a full day; L1's success is after 1 day
    assert tab.loc[1].tolist() == [4, 2, 0.5]
    # H=2: L4 (trigger day 9 > day 10 - 2) is censored; L1 counts (inclusive boundary)
    assert tab.loc[2, "leads with full follow-up"] == 3 and tab.loc[2, "successes"] == 2
    assert tab.loc[3, "p"] == pytest.approx(2 / 3)
    assert tab.loc[11, "leads with full follow-up"] == 0 and math.isnan(tab.loc[11, "p"])


@pytest.mark.data
def test_profile_smoke_buyer_logs(tmp_path):
    if not (DATA_DIR / FILES["buyer_logs"]).exists():
        pytest.skip("buyer_logs data absent")
    raw = yaml.safe_load((CONFIGS_DIR / "experiments" / "vani-buyer-qty-asks-v1.yaml").read_text())
    cfg = ExperimentConfig.model_validate(raw)
    prof = build_profile(cfg)
    for heading in ("## 1. Adapter", "## 2. Overview", "## 3. Monthly volume and bot_version mix",
                    "## 4. Calls per lead", "## 5. Daily answer rate and triggered leads",
                    "## 6. Baseline p", "## 7. Duration by disposition", "## 8. Follow-up lag",
                    "## 9. Consistency checks"):
        assert heading in prof.markdown, heading
    assert prof.adapter.excluded_leads == {"0": 28}
    assert sorted(prof.baseline_p_by_horizon_days) == list(range(1, cfg.window.days + 1))
    assert 0 < prof.baseline_p < 1 and prof.volume.daily_leads > 0
    assert "Baseline p both ways" in prof.markdown
    out = write_profile(prof, tmp_path / "p.md")
    assert out.read_text(encoding="utf-8") == prof.markdown
