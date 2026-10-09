"""Per-lead outcomes with trigger and maturation (PLAN.md §2.6).

The analysis unit is the lead, never the call: calls of one lead are correlated and share a
variant, so counting calls overstates n and produces false wins. Everything below is a groupby
over lead_id, vectorised, because the runner calls it at every look.
"""

from datetime import datetime

import numpy as np
import pandas as pd

from abtest.contracts import ExperimentConfig, LeadOutcome

OUTCOME_COLUMNS: tuple[str, ...] = tuple(LeadOutcome.model_fields)
_ASSIGNED_COLUMNS = ("lead_id", "variant", "ts")
_CALL_COLUMNS = ("lead_id", "ts", "answered", "disposition")
_TS_DTYPE = "datetime64[ns]"  # pandas 3 infers us/s resolution from strings; pin one unit


def _ts(s: pd.Series) -> pd.Series:
    """Datetimes as datetime64[ns]. The store keeps ISO-8601 strings of mixed precision."""
    if not pd.api.types.is_datetime64_any_dtype(s):
        s = pd.to_datetime(s, format="ISO8601")
    return s.astype(_TS_DTYPE)


def _require(df: pd.DataFrame, name: str, columns: tuple[str, ...]) -> None:
    if missing := [c for c in columns if c not in df.columns]:
        raise ValueError(f"{name} is missing columns {missing}")


def _first_exposures(exposures: pd.DataFrame, look_ts: pd.Timestamp) -> pd.DataFrame:
    """One row per lead, sorted by lead_id: its earliest exposure at or before look_ts
    (intent-to-treat, so a repeated exposure never moves a lead between arms or restarts its
    clock)."""
    _require(exposures, "exposures", _ASSIGNED_COLUMNS)
    e = pd.DataFrame({"lead_id": exposures["lead_id"].astype("str"),
                      "variant": exposures["variant"].astype("str"),
                      "assigned_ts": _ts(exposures["ts"])})
    e = e[e["assigned_ts"] <= look_ts]
    # Sorting by variant too makes a same-instant tie independent of row order.
    e = e.sort_values(["lead_id", "assigned_ts", "variant"], kind="stable")
    return e.drop_duplicates("lead_id", keep="first").reset_index(drop=True)


def lead_outcomes(calls: pd.DataFrame, exposures: pd.DataFrame, cfg: ExperimentConfig,
                  look_ts: datetime) -> pd.DataFrame:
    """One LeadOutcome row per assigned lead, using only exposures and calls with ts <= look_ts.

    A lead counts calls from its first exposure on. It is triggered at its first answered call
    (the first moment it hears the bot; a later pickup never re-triggers, §2.6) and mature once
    triggered_ts + maturation_days <= look_ts. A call of a lead with no exposure is ignored, so
    a dropped B exposure shows up as an SRM, by design. Rows are sorted by lead_id.
    """
    sec = cfg.secondary
    need = _CALL_COLUMNS + ((sec.column,) if sec.type == "continuous" else ())
    _require(calls, "calls", need)
    look = pd.Timestamp(look_ts)

    leads = _first_exposures(exposures, look)
    assigned_ts = pd.Series(leads["assigned_ts"].to_numpy(), index=pd.Index(leads["lead_id"]))

    # The lookup is NaT for an unexposed lead, and a NaT comparison is False: those calls drop.
    # (Series.map breaks on an empty mapper, so reindex.)
    c = pd.DataFrame({"lead_id": calls["lead_id"].astype("str"), "ts": _ts(calls["ts"])})
    c = c.assign(answered=calls["answered"].fillna(False).astype(bool),
                 disposition=calls["disposition"])
    if sec.type == "continuous":
        c["value"] = calls[sec.column].astype("float64")
    first_ts = assigned_ts.reindex(c["lead_id"]).to_numpy()
    c = c[(c["ts"] <= look) & (c["ts"].to_numpy() >= first_ts)]

    # Per-call flags, zero on unanswered calls: an unanswered call has no outcome, and its
    # disposition (or a null duration) must never leak into the lead.
    answered = c["answered"]
    c["answered_ts"] = c["ts"].where(answered)
    c["primary"] = (answered & c["disposition"].isin(cfg.primary.dispositions)).astype("int8")
    if sec.type == "binary":
        c["secondary"] = (answered & c["disposition"].isin(sec.dispositions)).astype("int8")
        sec_agg = ("secondary", "max")
    else:
        c["secondary"] = c["value"].where(answered)
        sec_agg = ("secondary", "mean")  # NaN-skipping: a lead's mean over non-null durations
    c["n_answered"] = answered.astype("int64")
    per_lead = c.groupby("lead_id", sort=False).agg(
        n_calls=("ts", "size"), n_answered=("n_answered", "sum"),
        triggered_ts=("answered_ts", "min"), primary=("primary", "max"), secondary=sec_agg,
    ).reindex(leads["lead_id"])

    triggered_ts = per_lead["triggered_ts"].to_numpy(dtype=_TS_DTYPE)
    triggered = ~np.isnat(triggered_ts)
    maturity = pd.Timedelta(days=cfg.maturation_days).to_timedelta64().astype("timedelta64[ns]")
    mature = triggered & (triggered_ts + maturity <= look.to_datetime64().astype(_TS_DTYPE))
    primary = pd.arrays.IntegerArray(per_lead["primary"].fillna(0).to_numpy(dtype="int64"),
                                     mask=~triggered)  # NA until the lead's first answered call
    secondary = np.where(triggered, per_lead["secondary"].to_numpy(dtype="float64"), np.nan)

    # `leads` is already sorted by lead_id (_first_exposures), and per_lead was reindexed to it.
    out = pd.DataFrame({
        "lead_id": leads["lead_id"],
        "variant": leads["variant"],
        "assigned_ts": leads["assigned_ts"],
        "triggered_ts": pd.Series(triggered_ts, dtype=_TS_DTYPE),
        "mature": mature,
        "primary": pd.Series(primary),
        "secondary": secondary,
        "n_calls": per_lead["n_calls"].fillna(0).to_numpy(dtype="int64"),
        "n_answered": per_lead["n_answered"].fillna(0).to_numpy(dtype="int64"),
    })
    assert tuple(out.columns) == OUTCOME_COLUMNS
    return out
