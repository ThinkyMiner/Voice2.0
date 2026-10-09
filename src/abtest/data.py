"""Dataset adapters (PLAN.md §2.0, §2.5): each source CSV -> the canonical call schema.

Canonical columns come first (CANONICAL_COLUMNS order), then every source column that is not
renamed into a canonical one, unchanged (so `lead_call_status`, `is_call_answered`,
`meeting_fixed`, `vendor_response_time`, ... stay available). Rows are sorted by
(lead_id, ts, call_id).
"""

from pathlib import Path

import duckdb
import pandas as pd

from abtest.contracts import CANONICAL_COLUMNS, DATA_DIR, AdapterReport, Dataset

FILES: dict[str, str] = {
    "bttc": "Best-Time-to-Call - Call Attempts Apr-Sep 2026.csv",
    "buyer_logs": "Call_Logs_Hackathon.csv",
}
BUYER_TS_FORMAT = "%d/%m/%y %H:%M"  # "05/10/26 23:30" is 5 Oct 2026; DuckDB's sniffer misreads it
UNKNOWN = "UNKNOWN"

# buyer_logs dispositions whose is_call_answered is null on EVERY row, so no majority exists.
# Both are screen-outs before any conversation: none has a call_recording_url,
# last_modified_date == entered_on, and their call_disposition_reason codes (386; 383/385)
# differ from the 380 that connected calls carry. Hence: not answered.
ANSWERED_IF_ALWAYS_NULL: dict[str, bool] = {
    "No Enrichment Scope": False,
    "Foreign or Disabled Users": False,
}


def _read(path: Path, **opts: str) -> pd.DataFrame:
    quoted = str(path).replace("'", "''")
    extra = "".join(f", {k}='{v}'" for k, v in opts.items())
    return duckdb.connect().sql(f"SELECT * FROM read_csv('{quoted}'{extra})").df()


def _ids(s: pd.Series) -> pd.Series:
    """Integer IDs stored as floats ("45434.0") -> clean integer strings ("45434"); NA stays NA."""
    if (s.dropna() % 1 != 0).any():
        raise ValueError(f"{s.name}: non-integer id values")
    return s.astype("Int64").astype("str")


def _bttc(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str], dict[str, int], list[str]]:
    canon = pd.DataFrame({
        "call_id": _ids(raw.data_hotlead_disposition_dtlid),
        "lead_id": _ids(raw.fk_glusr_usr_id),
        "ts": raw.call_start_time,
        "answered": raw.lead_call_status.eq("Answered"),
        "disposition": raw.disposition_label,
        "duration_s": raw.lead_call_duration.astype("float64"),
        "attempt_no": raw.call_attempt_count.astype("Int64"),
        "bot_version": raw.lead_bot_version,
    })
    renamed = ["data_hotlead_disposition_dtlid", "fk_glusr_usr_id", "call_start_time",
               "disposition_label", "lead_call_duration", "call_attempt_count", "lead_bot_version"]
    no_ts = canon.ts.isna()
    notes = [f"Rows with null call_start_time also have null lead_bot_version in "
             f"{int(canon.bot_version[no_ts].isna().sum()):,} of {int(no_ts.sum()):,} cases."]
    return canon, renamed, {}, notes


def _buyer_logs(raw: pd.DataFrame) -> tuple[pd.DataFrame, list[str], dict[str, int], list[str]]:
    disp = raw.disposition_remarks.fillna(UNKNOWN)
    answered = raw.is_call_answered.eq(1)
    inferred = {f"disposition: null -> {UNKNOWN}": int(raw.disposition_remarks.isna().sum())}
    # Infer null is_call_answered from the disposition: majority of the known values for that
    # disposition (a tie counts as not answered); else the explicit map above.
    known = raw.is_call_answered.notna()
    majority = raw.is_call_answered[known].eq(1).groupby(disp[known]).mean() > 0.5
    for d, idx in disp[~known].groupby(disp[~known]).groups.items():
        if d in majority.index:
            value, rule = bool(majority[d]), f"majority of known values for '{d}'"
        elif d in ANSWERED_IF_ALWAYS_NULL:
            value, rule = ANSWERED_IF_ALWAYS_NULL[d], f"explicit map for '{d}' (never known)"
        else:
            raise ValueError(f"no answered rule for disposition {d!r}: add it to ANSWERED_IF_ALWAYS_NULL")
        answered.loc[idx] = value
        inferred[f"answered: {rule} -> {value}"] = len(idx)
    n = len(raw)
    canon = pd.DataFrame({
        "call_id": _ids(raw.fk_leap_call_records_id),
        "lead_id": _ids(raw.fk_glusr_usr_id),
        "ts": raw.entered_on,
        "answered": answered,
        "disposition": disp,
        "duration_s": pd.Series(float("nan"), index=raw.index, dtype="float64"),
        "attempt_no": pd.Series(pd.NA, index=raw.index, dtype="Int64"),
        "bot_version": pd.Series(None, index=raw.index, dtype="str"),
    })
    renamed = ["fk_leap_call_records_id", "fk_glusr_usr_id", "entered_on", "disposition_remarks"]
    notes = [f"Inferred counts are over all {n:,} source rows, before any row is dropped.",
             "duration_s, attempt_no and bot_version have no source column (PLAN.md §2.0)."]
    return canon, renamed, inferred, notes


def load(dataset: Dataset, data_dir: Path = DATA_DIR,
         exclude_leads_with_calls_gt: int | None = None) -> tuple[pd.DataFrame, AdapterReport]:
    """Canonical frame + report. Drops rows with a null call_id, lead_id or ts, then (if set)
    every lead with more than `exclude_leads_with_calls_gt` calls."""
    path = Path(data_dir) / FILES[dataset]
    if dataset == "bttc":
        raw = _read(path)
        canon, renamed, inferred, notes = _bttc(raw)
    else:
        raw = _read(path, timestampformat=BUYER_TS_FORMAT)
        canon, renamed, inferred, notes = _buyer_logs(raw)
    df = pd.concat([canon, raw.drop(columns=renamed)], axis=1)

    dropped: dict[str, int] = {}
    for col in ("call_id", "lead_id", "ts"):
        bad = df[col].isna()
        dropped[f"{col} is null"] = int(bad.sum())
        df = df[~bad]

    excluded: dict[str, int] = {}
    if exclude_leads_with_calls_gt is not None:
        counts = df.lead_id.value_counts()
        big = counts[counts > exclude_leads_with_calls_gt].sort_index()
        excluded = {str(k): int(v) for k, v in big.items()}
        dropped[f"lead has > {exclude_leads_with_calls_gt} calls (exclude_leads_with_calls_gt)"] = int(big.sum())
        df = df[~df.lead_id.isin(big.index)]

    df = df.sort_values(["lead_id", "ts", "call_id"], kind="stable", ignore_index=True)
    assert tuple(df.columns[:len(CANONICAL_COLUMNS)]) == CANONICAL_COLUMNS
    return df, AdapterReport(dataset=dataset, source_file=FILES[dataset], rows_read=len(raw),
                             rows_out=len(df), dropped=dropped, inferred=inferred,
                             excluded_leads=excluded, notes=notes)
