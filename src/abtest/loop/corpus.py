"""Corpus adapter: a CSV of answered calls with a recording URL -> the canonical call frame
(PLAN_LOOP.md §L2.1, §L7.2). Lives here, not in the base data.py, which is Lane A's.

Mapping to PLAN.md §2.5 names: lead_id = glid, call_id = call_id, ts = call_start_ist (naive IST),
answered = call_status == "Answered", duration_s = duration_sec, bot_version = bot,
attempt_no = None. A corpus has no disposition, so `disposition` is None on every row, and corpus
rows never enter the base `calls` table or any A/B decision. hotlead_type, recording_url and any
other source column pass through untouched.
"""

from pathlib import Path

import pandas as pd

CANONICAL = ["call_id", "lead_id", "ts", "answered", "disposition", "duration_s", "attempt_no", "bot_version"]
_RENAMED = {"glid": "lead_id", "bot": "bot_version", "call_start_ist": "ts", "duration_sec": "duration_s"}
_REQUIRED = ("glid", "call_id", "bot", "call_status", "duration_sec", "call_start_ist")


def load_corpus(path: str | Path) -> pd.DataFrame:
    """Columns: call_id, lead_id, ts, answered, disposition (None), duration_s, attempt_no (None),
    bot_version, then hotlead_type, recording_url and any other source column. Sorted by
    (lead_id, ts, call_id). Ids are strings exactly as written (never numbers, so no leading zeros
    are lost); `ts` is a naive datetime (an offset in the source is converted to IST first)."""
    raw = pd.read_csv(path, dtype=str)
    raw.columns = [c.strip() for c in raw.columns]
    if missing := [c for c in _REQUIRED if c not in raw.columns]:
        raise ValueError(f"corpus {Path(path).name} is missing columns {missing}")
    for col in ("glid", "call_id"):
        raw[col] = raw[col].str.strip()
        if bad := int((raw[col].isna() | (raw[col] == "")).sum()):
            raise ValueError(f"corpus {Path(path).name}: {bad} rows have no {col}")
    if dup := int(raw["call_id"].duplicated().sum()):
        raise ValueError(f"corpus {Path(path).name}: {dup} duplicate call_id values")

    ts = pd.to_datetime(raw["call_start_ist"].str.strip())
    if getattr(ts.dt, "tz", None) is not None:
        ts = ts.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)

    df = pd.DataFrame({
        "call_id": raw["call_id"],
        "lead_id": raw["glid"],
        "ts": ts,
        "answered": raw["call_status"].str.strip().str.lower() == "answered",
        "disposition": pd.Series([None] * len(raw), dtype=object),
        "duration_s": pd.to_numeric(raw["duration_sec"], errors="coerce").astype(float),
        "attempt_no": pd.Series([None] * len(raw), dtype=object),
        "bot_version": raw["bot"].str.strip(),
    })
    rest = [c for c in raw.columns if c not in _RENAMED and c != "call_id"]
    ordered = [c for c in ("hotlead_type", "recording_url") if c in rest] + \
              [c for c in rest if c not in ("hotlead_type", "recording_url")]
    for c in ordered:
        df[c] = raw[c]
    return df.sort_values(["lead_id", "ts", "call_id"], kind="stable").reset_index(drop=True)
