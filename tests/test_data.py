"""Real-data adapter tests (skipped when the data files are absent)."""

import pandas as pd
import pytest

from abtest.contracts import CANONICAL_COLUMNS, DATA_DIR

pytestmark = pytest.mark.data
from abtest.data import FILES, load


def _need(dataset):
    if not (DATA_DIR / FILES[dataset]).exists():
        pytest.skip(f"{FILES[dataset]} not in {DATA_DIR}")


@pytest.fixture(scope="module")
def bttc():
    _need("bttc")
    return load("bttc")


@pytest.fixture(scope="module")
def buyer():
    _need("buyer_logs")
    return load("buyer_logs")


def _conforms(df, rep):
    assert tuple(df.columns[:len(CANONICAL_COLUMNS)]) == CANONICAL_COLUMNS
    for col in ("call_id", "lead_id", "disposition", "bot_version"):
        assert pd.api.types.is_string_dtype(df[col]), col
    assert df.ts.dtype.kind == "M" and df.ts.dt.tz is None
    assert df.answered.dtype == bool
    assert df.duration_s.dtype == "float64"
    assert df.attempt_no.dtype == "Int64"
    assert df[["call_id", "lead_id", "ts", "disposition"]].notna().all().all()
    assert not df.call_id.str.contains(r"\.").any() and not df.lead_id.str.contains(r"\.").any()
    assert df.call_id.is_unique
    keys = df[["lead_id", "ts", "call_id"]]
    assert keys.equals(keys.sort_values(["lead_id", "ts", "call_id"], ignore_index=True))
    assert rep.rows_out == len(df) == rep.rows_read - sum(rep.dropped.values())


def test_bttc(bttc):
    df, rep = bttc
    _conforms(df, rep)
    assert rep.rows_read == 530_748 and rep.dropped["ts is null"] == 4_030
    assert (df.answered == df.lead_call_status.eq("Answered")).all()
    assert set(df.disposition) == {"Not Answered", "Not Interested", "General (talked)",
                                   "Call Later / Busy", "Meeting Fixed"}
    assert df.bot_version.notna().all()  # the null-bot_version rows are exactly the null-ts rows


def test_buyer_logs(buyer):
    df, rep = buyer
    _conforms(df, rep)
    assert len(df) == 997 and rep.dropped["lead_id is null"] == 3
    assert sum(v for k, v in rep.inferred.items() if k.startswith("answered:")) == 142
    assert rep.inferred["disposition: null -> UNKNOWN"] == 8
    assert (df.disposition == "UNKNOWN").sum() == 5  # 3 of the 8 are in null-lead rows
    assert df.duration_s.isna().all() and df.attempt_no.isna().all() and df.bot_version.isna().all()
    # "05/10/26" is 5 Oct 2026, not 2005 or May
    assert df.ts.min() == pd.Timestamp("2026-10-05 00:08") and df.ts.max() == pd.Timestamp("2026-10-09 08:02")
    # the spec rule holds wherever the source value is known
    known = df.is_call_answered.notna()
    assert (df.answered[known] == df.is_call_answered[known].eq(1)).all()


def test_buyer_logs_outlier_exclusion(buyer):
    df, rep = load("buyer_logs", exclude_leads_with_calls_gt=10)
    _conforms(df, rep)
    full, _ = buyer
    assert rep.excluded_leads == {"0": 28}
    assert len(df) == len(full) - 28 and "0" not in set(df.lead_id)
