"""Corpus adapter: CSV of answered calls -> the canonical call frame (PLAN_LOOP §L2.1)."""

from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

from abtest.contracts import ROOT
from abtest.loop.corpus import CANONICAL, load_corpus

REAL = ROOT / "data" / "squadstack" / "squadstack_answered_2026-10-06_sample250.csv"

HEADER = "glid,call_id,bot,hotlead_type,call_status,duration_sec,call_start_ist,recording_url\n"


def _write(tmp_path: Path, rows: str, header: str = HEADER) -> Path:
    path = tmp_path / "corpus.csv"
    path.write_text(header + rows, encoding="utf-8")
    return path


ROWS = (
    "0042,900003,personalised_vani,PIM,Answered,64,2026-10-06 09:17:50,https://rec.example/900003.mp3\n"
    "0042,900001,personalised_vani,PIM,Answered,24,2026-10-06 09:10:00,https://rec.example/900001.mp3\n"
    "0007,900002,base_vani,UA,answered ,196,2026-10-06 09:20:00,\n"
    "0100,900004,summary_vani,PUA,Missed,0,2026-10-06 08:00:00,https://rec.example/900004.mp3\n"
)


def test_canonical_columns_come_first_then_source_columns(tmp_path):
    df = load_corpus(_write(tmp_path, ROWS))
    assert list(df.columns)[:len(CANONICAL)] == CANONICAL
    assert list(df.columns)[len(CANONICAL):] == ["hotlead_type", "recording_url", "call_status"]


def test_ids_are_strings_and_keep_leading_zeros(tmp_path):
    df = load_corpus(_write(tmp_path, ROWS))
    assert set(df["lead_id"]) == {"0042", "0007", "0100"}
    assert all(isinstance(v, str) for v in df["call_id"])


def test_sorted_by_lead_ts_call_id(tmp_path):
    df = load_corpus(_write(tmp_path, ROWS))
    assert list(df["call_id"]) == ["900002", "900001", "900003", "900004"]   # 0007, 0042 (09:10, 09:17), 0100
    assert list(df.index) == [0, 1, 2, 3]


def test_ties_on_lead_and_ts_break_by_call_id(tmp_path):
    rows = ("1,b,personalised_vani,X,Answered,5,2026-10-06 09:00:00,\n"
            "1,a,personalised_vani,X,Answered,5,2026-10-06 09:00:00,\n")
    assert list(load_corpus(_write(tmp_path, rows))["call_id"]) == ["a", "b"]


def test_mapping_of_the_canonical_fields(tmp_path):
    df = load_corpus(_write(tmp_path, ROWS)).set_index("call_id")
    assert df.loc["900001", "bot_version"] == "personalised_vani"
    assert df.loc["900001", "duration_s"] == 24.0
    assert df.loc["900001", "ts"] == pd.Timestamp("2026-10-06 09:10:00")
    assert df.loc["900002", "answered"] and not df.loc["900004", "answered"]   # case/space-insensitive
    assert df["answered"].dtype == bool
    assert df["duration_s"].dtype == float


def test_timestamps_are_naive(tmp_path):
    df = load_corpus(_write(tmp_path, ROWS))
    assert df["ts"].dt.tz is None
    assert isinstance(df["ts"].iloc[0].to_pydatetime(), datetime)


def test_an_offset_in_the_source_is_converted_to_naive_ist(tmp_path):
    rows = "1,a,personalised_vani,X,Answered,5,2026-10-06T03:30:00+00:00,\n"
    assert load_corpus(_write(tmp_path, rows))["ts"].iloc[0] == pd.Timestamp("2026-10-06 09:00:00")


def test_disposition_and_attempt_no_are_none(tmp_path):
    df = load_corpus(_write(tmp_path, ROWS))
    assert df["disposition"].isna().all() and df["attempt_no"].isna().all()
    assert all(v is None for v in df["disposition"])


def test_pass_through_columns_and_missing_urls(tmp_path):
    df = load_corpus(_write(tmp_path, ROWS)).set_index("call_id")
    assert df.loc["900003", "hotlead_type"] == "PIM"
    assert df.loc["900003", "recording_url"] == "https://rec.example/900003.mp3"
    assert pd.isna(df.loc["900002", "recording_url"])


def test_extra_source_columns_survive(tmp_path):
    header = HEADER.rstrip("\n") + ",region\n"
    df = load_corpus(_write(tmp_path, "1,a,personalised_vani,X,Answered,5,2026-10-06 09:00:00,u,north\n", header))
    assert df["region"].iloc[0] == "north"


def test_missing_required_columns_raise(tmp_path):
    with pytest.raises(ValueError, match="missing columns"):
        load_corpus(_write(tmp_path, "1,a\n", header="glid,call_id\n"))


def test_duplicate_call_ids_raise(tmp_path):
    rows = ("1,a,personalised_vani,X,Answered,5,2026-10-06 09:00:00,\n"
            "2,a,personalised_vani,X,Answered,5,2026-10-06 09:01:00,\n")
    with pytest.raises(ValueError, match="duplicate call_id"):
        load_corpus(_write(tmp_path, rows))


def test_blank_ids_raise(tmp_path):
    with pytest.raises(ValueError, match="no glid"):
        load_corpus(_write(tmp_path, ",a,personalised_vani,X,Answered,5,2026-10-06 09:00:00,\n"))


def test_a_bad_timestamp_raises(tmp_path):
    with pytest.raises(ValueError):
        load_corpus(_write(tmp_path, "1,a,personalised_vani,X,Answered,5,not-a-time,\n"))


@pytest.mark.skipif(not REAL.exists(), reason="the SquadStack sample is not on this machine")
def test_the_real_squadstack_sample():
    df = load_corpus(REAL)
    assert len(df) == 250 and df["lead_id"].nunique() == 239
    assert df["bot_version"].value_counts().to_dict() == {"personalised_vani": 239, "base_vani": 9, "summary_vani": 2}
    assert df["answered"].all() and df["call_id"].is_unique
    assert df["ts"].dt.tz is None
    assert df["recording_url"].notna().all()
    assert list(df.columns)[:len(CANONICAL)] == CANONICAL
    assert df["duration_s"].median() == pytest.approx(24, abs=2)
    assert df.equals(df.sort_values(["lead_id", "ts", "call_id"], kind="stable").reset_index(drop=True))
