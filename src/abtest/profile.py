"""Data profile report (PLAN.md §3 Phase 1, Checkpoint 1): `make profile DATASET=bttc|buyer_logs`.

Definitions used throughout (§2.6):
- Trigger: a lead's first answered call.
- Success within H days: an answered call with a disposition in the metric set at
  trigger <= ts <= trigger + H days (the trigger call itself counts).
- Censoring: at horizon H only leads triggered at or before (last ts in the data - H days) are
  counted, so every counted lead had its full follow-up. Otherwise recent leads count as failures.
"""

import argparse
from pathlib import Path
from typing import get_args

import numpy as np
import pandas as pd

from abtest.contracts import (DATA_DIR, REPORTS_DIR, DataProfile, Dataset, ExperimentConfig,
                              VolumeStats)
from abtest.data import load

DAY = pd.Timedelta(days=1)
LAG_PCTS = (50, 75, 90, 95, 99)
CALL_PCTS = (50, 75, 90, 95, 99)
DUR_PCTS = (10, 50, 90, 99)
ENVELOPE_PCTS = (0.1, 1, 5, 50)
CALLS_CAP = 10       # calls-per-lead count table: 1..CALLS_CAP-1, then CALLS_CAP+
TOP_LEADS = 5
UNIT_FACTORS = {"milliseconds": 0.001, "seconds": 1.0, "minutes": 60.0}  # unit -> seconds
SECTIONS = (
    "## 1. Adapter",
    "## 2. Overview",
    "## 3. Monthly volume and bot_version mix",
    "## 4. Calls per lead",
    "## 5. Daily answer rate and triggered leads",
    "## 6. Baseline p",
    "## 7. Duration by disposition",
    "## 8. Follow-up lag",
    "## 9. Consistency checks",
)


# --- metrics ------------------------------------------------------------------------------

def triggers(df: pd.DataFrame, dispositions) -> pd.DataFrame:
    """One row per triggered lead: trigger_ts, the trigger call's bot_version, and `lag` from the
    trigger to the lead's first answered call in `dispositions` (NaT if none)."""
    ans = df[df.answered].sort_values(["lead_id", "ts", "call_id"], kind="stable")
    t = (ans.drop_duplicates("lead_id").set_index("lead_id")[["ts", "bot_version"]]
         .rename(columns={"ts": "trigger_ts"}))
    t["lag"] = ans[ans.disposition.isin(dispositions)].groupby("lead_id").ts.min().reindex(t.index) - t.trigger_ts
    return t


def _horizons(t: pd.DataFrame, end: pd.Timestamp, horizons) -> pd.DataFrame:
    rows = {}
    for h in horizons:
        full = t.trigger_ts <= end - h * DAY
        n, s = int(full.sum()), int((t.lag[full] <= h * DAY).sum())
        rows[h] = {"leads with full follow-up": n, "successes": s, "p": s / n if n else float("nan")}
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("H (days)")


def success_by_horizon(df: pd.DataFrame, dispositions, horizons) -> pd.DataFrame:
    """Baseline p per horizon H (days), censored as in the module docstring. Columns:
    'leads with full follow-up' (the denominator), 'successes', 'p' (NaN if no lead qualifies)."""
    return _horizons(triggers(df, dispositions), df.ts.max(), horizons)


def _blocks(df: pd.DataFrame, window_days: int) -> tuple[pd.Series, int, int]:
    """(block of each row, number of full blocks, block length in days). Blocks start on the
    first calendar day in the data; if the data spans fewer days than one block, the whole span
    is the single (partial) block."""
    offset = (df.ts.dt.normalize() - df.ts.min().normalize()).dt.days
    span = int(offset.max()) + 1
    n_blocks, length = span // window_days, window_days
    if not n_blocks:
        n_blocks, length = 1, span
    return offset // length, n_blocks, length


def lead_window_p(df: pd.DataFrame, dispositions, window_days: int) -> tuple[float, int]:
    """(success rate, n) per triggered lead-window: one lead's calls inside one full block, the
    unit the simulator bootstraps. This is the rate the experiment actually samples."""
    block, n_blocks, _ = _blocks(df, window_days)
    d = df[block < n_blocks].assign(block=block, hit=df.answered & df.disposition.isin(dispositions))
    g = d.groupby(["lead_id", "block"])[["answered", "hit"]].any()
    t = g[g.answered]
    return float(t.hit.mean()), len(t)


def volume_stats(df: pd.DataFrame, window_days: int) -> VolumeStats:
    """Blocks as in _blocks; the daily figure divides by the block length."""
    block, n_blocks, length = _blocks(df, window_days)
    keep = block < n_blocks

    def per_day(m: pd.Series) -> float:
        return float(df.lead_id[m].groupby(block[m]).nunique().reindex(range(n_blocks), fill_value=0).mean() / length)

    return VolumeStats(window_days=window_days, daily_leads=per_day(keep),
                       daily_triggered_leads=per_day(keep & df.answered))


# --- markdown helpers ---------------------------------------------------------------------

def _fmt(v) -> str:
    if isinstance(v, (bool, np.bool_)):
        return str(bool(v))
    if isinstance(v, (int, np.integer)):
        return f"{v:,}"
    if isinstance(v, (float, np.floating)):
        return "n/a" if np.isnan(v) else f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.4g}"
    return "n/a" if v is None or v is pd.NA else str(v)


def _md(df: pd.DataFrame) -> str:
    d = df.reset_index()
    out = ["| " + " | ".join(map(str, d.columns)) + " |", "|" + "---|" * len(d.columns)]
    out += ["| " + " | ".join(_fmt(v) for v in row) + " |" for row in d.itertuples(index=False)]
    return "\n".join(out)


def _kv(items: dict, key: str = "item", value: str = "value") -> str:
    return _md(pd.DataFrame({value: pd.Series(items, dtype=object)}).rename_axis(key))


def _pcts(values: pd.Series, pcts, cols: dict) -> pd.DataFrame:
    """Percentile table; `cols` maps column name -> scale applied to the raw percentiles."""
    v = np.percentile(values, pcts) if len(values) else np.full(len(pcts), np.nan)
    return pd.DataFrame({c: v * k for c, k in cols.items()},
                        index=pd.Index([f"p{p:g}" for p in pcts], name="percentile"))


def _volume_by(df: pd.DataFrame, t: pd.DataFrame, key) -> pd.DataFrame:
    k, a = key(df.ts), df.answered
    out = pd.DataFrame({
        "days": df.ts.dt.normalize().groupby(k).nunique(),
        "calls": df.groupby(k).size(),
        "answer rate": a.groupby(k).mean(),
        "leads dialed": df.lead_id.groupby(k).nunique(),
        "leads answered": df.lead_id[a].groupby(k[a]).nunique(),
        "new triggered leads": key(t.trigger_ts).value_counts(),
    })
    cnt = ["days", "calls", "leads dialed", "leads answered", "new triggered leads"]
    out[cnt] = out[cnt].fillna(0).astype("int64")
    out.index = out.index.astype(str)
    return out


# --- profile ------------------------------------------------------------------------------

def build_profile(cfg: ExperimentConfig, data_dir: Path = DATA_DIR) -> DataProfile:
    df, rep = load(cfg.dataset, data_dir, cfg.exclude_leads_with_calls_gt)
    W, prim = cfg.window.days, cfg.primary.dispositions
    end = df.ts.max()
    span = (end.normalize() - df.ts.min().normalize()).days + 1
    t = triggers(df, prim)
    tab = _horizons(t, end, range(1, W + 1))
    h = max((x for x in tab.index if tab.at[x, "leads with full follow-up"]), default=W)
    baseline_p = float(tab.at[h, "p"])
    vol = volume_stats(df, W)
    has_bv = bool(df.bot_version.notna().any())
    md = [f"# Data profile: {cfg.dataset}", "",
          f"Experiment `{cfg.id}`: primary `{cfg.primary.name}` = any answered call with disposition "
          f"in {list(prim)}; window {W} days. Generated by `make profile DATASET={cfg.dataset}` "
          "from the full data (deterministic, no sampling).", "",
          "**Definitions.** Trigger = a lead's first answered call (§2.6). Baseline p at horizon H = "
          "share of triggered leads with an answered call in the primary set between the trigger and "
          "trigger + H days (inclusive of the trigger call). Censoring: the denominator for H holds only "
          "leads triggered at or before (last ts − H days), so every counted lead had full follow-up."]

    # 1. Adapter
    md += ["", SECTIONS[0], "",
           _kv({"source file": rep.source_file, "rows read": rep.rows_read, "rows out": rep.rows_out}),
           "", "Dropped rows (applied in this order):", "", _kv(rep.dropped, "reason", "rows"),
           "", "Inferred values:", "",
           _kv(rep.inferred, "rule", "rows") if rep.inferred else "none",
           "", "Excluded leads:", "",
           _kv(rep.excluded_leads, "lead_id", "calls") if rep.excluded_leads else "none"]
    md += [""] + [f"- {n}" for n in rep.notes]

    # 2. Overview
    md += ["", SECTIONS[1], "", _kv({
        "rows (calls)": len(df), "date from": df.ts.min(), "date to": end, "span (calendar days)": span,
        "leads": df.lead_id.nunique(), "triggered leads": len(t),
        "triggered share of leads": len(t) / df.lead_id.nunique(), "answered calls": int(df.answered.sum()),
        "answer rate (calls)": df.answered.mean()})]

    # 3. Monthly volume and bot_version mix
    md += ["", SECTIONS[2], "", "Monthly volume (new triggered leads = leads whose first answered call "
           "falls in the month):", "",
           _md(_volume_by(df, t, lambda s: s.dt.to_period("M")).drop(columns="days").rename_axis("month"))]
    if has_bv:
        bv, tbv = df.bot_version.fillna("NA"), t.bot_version.fillna("NA")
        mix = pd.DataFrame({"calls": bv.value_counts()})
        mix["share of calls"] = mix.calls / len(df)
        mix["answer rate"] = df.answered.groupby(bv).mean()
        mix["triggered leads"] = tbv.value_counts().reindex(mix.index, fill_value=0)
        per_bv = pd.DataFrame({b: _horizons(g, end, [h]).loc[h] for b, g in t.groupby(tbv)}).T
        mix[f"baseline p (H={h})"] = per_bv.p
        md += ["", "bot_version mix (a lead's bot_version is its trigger call's):", "",
               _md(mix.rename_axis("bot_version"))]
    else:
        md += ["", "No bot_version in this dataset."]

    # 4. Calls per lead (including leads excluded afterwards as outliers, so they show in context)
    cpl = pd.concat([df.lead_id.value_counts(), pd.Series(rep.excluded_leads, dtype="int64")])
    counts = cpl.clip(upper=CALLS_CAP).value_counts().reindex(range(1, CALLS_CAP + 1), fill_value=0)
    counts.index = [str(i) for i in range(1, CALLS_CAP)] + [f"{CALLS_CAP}+"]
    top = cpl.sort_index().sort_values(ascending=False, kind="stable").head(TOP_LEADS)
    k = cfg.exclude_leads_with_calls_gt
    md += ["", SECTIONS[3], "",
           f"Over all {len(cpl):,} leads after the adapter's row drops, including leads then excluded as "
           f"outliers. Mean {cpl.mean():.3g}, max {int(cpl.max()):,}.", "",
           _md(_pcts(cpl, CALL_PCTS, {"calls": 1})), "",
           _md(pd.DataFrame({"leads": counts, "share": counts / len(cpl)}).rename_axis("calls")), "",
           f"Top {TOP_LEADS} leads by calls:", "",
           _md(pd.DataFrame({"calls": top, "excluded": top.index.isin(list(rep.excluded_leads))}).rename_axis("lead_id")),
           "", (f"Outliers excluded by `exclude_leads_with_calls_gt = {k}`: "
                + (", ".join(f"lead {lid} ({n} calls)" for lid, n in rep.excluded_leads.items()) or "none")
                + ". They are absent from every other section.")
           if k is not None else "`exclude_leads_with_calls_gt` is not set: no outlier exclusion."]

    # 5. Daily answer rate and triggered leads
    daily = _volume_by(df, t, lambda s: s.dt.normalize()).drop(columns="days")
    md += ["", SECTIONS[4], ""]
    if span > W:
        stats = daily.agg(["mean", "std", "min", "median", "max"]).rename_axis("over days")
        weekly = _volume_by(df, t, lambda s: s.dt.to_period("W-SUN"))
        weekly["new triggered leads per day"] = weekly["new triggered leads"] / weekly.days
        md += [f"Summary over the {len(daily)} days with calls:", "", _md(stats), "",
               "Weekly (Monday to Sunday):", "", _md(weekly.rename_axis("week"))]
    else:
        md += ["Per day:", "", _md(daily.rename_axis("date"))]
    fallback = span < W
    md += ["", f"VolumeStats (input to the simulator's `volume.daily_leads`): the mean, over non-overlapping "
           f"{W}-day blocks starting {df.ts.min():%Y-%m-%d} (partial trailing block dropped), of the distinct "
           f"leads dialed (daily_leads) or answered (daily_triggered_leads) in the block, divided by {W}.", "",
           _kv({"window_days": vol.window_days, "full blocks": span // W, "daily_leads": vol.daily_leads,
                "daily_triggered_leads": vol.daily_triggered_leads})]
    if fallback:
        md += ["", f"**Warning:** the data spans {span} days, less than one {W}-day block, so the whole span "
               f"is used as one partial block and divided by {span} days."]

    # 6. Baseline p
    md += ["", SECTIONS[5], "", _md(tab), "",
           f"**baseline_p = {baseline_p:.4f}** (H = {h} days, "
           f"{tab.at[h, 'leads with full follow-up']:,} leads with full follow-up)."]
    if h != W:
        md += ["", f"**Warning:** no lead has {W} days of follow-up (the data spans {span} days), so "
               f"baseline_p uses H = {h}, the longest horizon with full follow-up. Shorter horizons have "
               f"larger denominators; section 8(c) shows how fast successes accrue. Uncensored rate over all "
               f"{len(t):,} triggered leads (follow-up cut at the end of the data): {t.lag.notna().mean():.4f}."]
    lw_p, lw_n = lead_window_p(df, prim, W)
    md += ["", f"**Lead-window baseline = {lw_p:.4f}** over {lw_n:,} triggered lead-windows (one lead's calls "
           f"inside one {W}-day block, the unit the simulator bootstraps and the experiment samples). "
           "This is the rate a live experiment sees, because repeat sellers re-enter each window; it is the "
           "value `baseline_p` uses for n_max (Checkpoint 2 decision)."]
    if has_bv:
        md += ["", f"Per bot_version of the trigger call (H = {h}):", "", _md(per_bv.rename_axis("bot_version"))]
    s = cfg.secondary
    if s.type == "continuous":
        full = df[df.answered & df.lead_id.isin(t.index[t.trigger_ts <= end - h * DAY])]
        in_win = full[full.ts <= full.lead_id.map(t.trigger_ts) + h * DAY]
        per_lead = in_win.groupby("lead_id")[s.column].mean()
        sec = {"leads": per_lead.count(), "mean of per-lead mean": per_lead.mean(), "SD": per_lead.std()}
        what = f"per-lead mean `{s.column}` over answered calls within H = {h} days of the trigger"
    else:
        st = success_by_horizon(df, s.dispositions, [h]).loc[h]
        sec = {"leads": int(st["leads with full follow-up"]), "per-lead rate": st.p}
        what = f"per-lead rate of an answered call in {list(s.dispositions)} within H = {h} days"
    md += ["", f"Secondary `{s.name}` ({s.type}, {s.role}) baseline: {what}, same censoring:", "", _kv(sec, "statistic")]

    # 7. Duration by disposition
    md += ["", SECTIONS[6], ""]
    if df.duration_s.notna().any():
        a = df[df.answered & df.duration_s.notna()]
        g = a.groupby("disposition").duration_s
        dur = pd.DataFrame({"count": g.size(), "mean": g.mean()})
        for p in DUR_PCTS:
            dur[f"p{p}"] = g.quantile(p / 100)
        dur["max"], dur["share zero"] = g.max(), g.apply(lambda x: (x == 0).mean())
        md += ["Answered calls, raw `duration_s` values:", "", _md(dur)]
        if "vendor_response_time" in df:
            e = a[a.vendor_response_time.notna()]
            gap = (e.vendor_response_time - e.ts).dt.total_seconds()
            share = {u: float((e.duration_s * f <= gap).mean()) for u, f in UNIT_FACTORS.items()}
            unit = max((u for u in share if share[u] > 0.5), key=UNIT_FACTORS.get)  # most calls fit the gap
            pos = e.duration_s > 0
            ev = pd.DataFrame({"seconds per unit": UNIT_FACTORS, "share of calls with duration ≤ gap": share,
                               "implied median answered call (s)": {u: e.duration_s.median() * f
                                                                    for u, f in UNIT_FACTORS.items()}})
            md += ["", f"Unit evidence: the vendor reports back after the call ends, so the duration must fit in "
                   f"the gap `vendor_response_time − call_start_time` (seconds). Over {len(e):,} answered calls:",
                   "", _md(ev.rename_axis("unit hypothesis")), "",
                   "Lower envelope of gap ÷ duration (calls with duration > 0); a value just above 1 means the "
                   "vendor reports right after the call ends:", "",
                   _md(_pcts(gap[pos] / e.duration_s[pos], ENVELOPE_PCTS, {"gap ÷ duration": 1})), "",
                   f"**Verdict: `duration_s` is in {unit}**: the largest unit under which most answered calls "
                   f"({share[unit]:.4%}, all but {int((e.duration_s * UNIT_FACTORS[unit] > gap).sum()):,}) "
                   f"fit inside the gap. Smaller units fit "
                   f"trivially but imply implausibly short calls; larger ones put most calls beyond the gap."]
    else:
        md += ["Not applicable: this dataset has no duration column (PLAN.md §2.0)."]

    # 8. Follow-up lag
    same = df.lead_id.eq(df.lead_id.shift())
    gap_h = df.ts.diff().dt.total_seconds() / 3600
    gaps = _pcts(gap_h[same], LAG_PCTS, {"hours": 1, "days": 1 / 24})
    gap_note = f"{int(same.sum()):,} gaps"
    if df.attempt_no.notna().any():  # retry gap within one hot lead: attempt_no keeps increasing
        retry = same & df.attempt_no.gt(df.attempt_no.shift()).fillna(False)
        gaps["hours, within a hot lead"] = _pcts(gap_h[retry], LAG_PCTS, {"h": 1}).h
        gap_note += f"; {int(retry.sum()):,} within a hot lead, where attempt_no increases"
    lags = t.lag[(t.trigger_ts <= end - h * DAY) & (t.lag <= h * DAY)]
    cum = pd.DataFrame({"share of successes reached": {d: float((lags <= d * DAY).mean()) for d in range(h + 1)}})
    md += ["", SECTIONS[7], "", "Inputs for `maturation_days`.", "",
           f"(a) Gap between consecutive calls of the same lead ({gap_note}):", "", _md(gaps), "",
           f"(b) Trigger to first success, among the {len(lags):,} leads with full {h}-day follow-up that "
           f"succeed within {h} days ({int((lags == pd.Timedelta(0)).sum()):,} at the trigger call itself):", "",
           _md(_pcts(lags.dt.total_seconds() / 3600, LAG_PCTS, {"hours": 1, "days": 1 / 24})), "",
           "(c) Cumulative share of those successes reached by day h (lag ≤ h days):", "",
           _md(cum.rename_axis("h (days)"))]

    # 9. Consistency checks
    md += ["", SECTIONS[8], ""]
    if "meeting_fixed" in df:
        ct = pd.crosstab(df.disposition, df.meeting_fixed).rename(columns=lambda c: f"meeting_fixed={c}")
        mf = df.disposition.isin(prim)
        md += ["`meeting_fixed` flag vs disposition:", "", _md(ct), "",
               f"{int((mf & df.meeting_fixed.eq(0)).sum()):,} rows with a primary disposition have "
               f"meeting_fixed = 0; {int((~mf & df.meeting_fixed.eq(1)).sum()):,} other rows have "
               "meeting_fixed = 1. The disposition is the source of truth; the flag is ignored."]
    if "is_call_answered" in df:
        raw = df.is_call_answered.map({1.0: "1", 0.0: "0"}).fillna("null")
        ct = pd.crosstab(df.disposition, raw).rename(columns=lambda c: f"is_call_answered={c}")
        alt = df.assign(answered=df.answered | df.disposition.isin(prim))
        ta = triggers(alt, prim)
        two = pd.DataFrame({
            "as spec (answered = is_call_answered == 1, nulls inferred)": {"triggered leads": len(t), **_horizons(t, end, [h]).loc[h]},
            "primary dispositions treated as answered": {"triggered leads": len(ta), **_horizons(ta, end, [h]).loc[h]},
        }).T.astype({"triggered leads": "int64", "leads with full follow-up": "int64", "successes": "int64"})
        md += ["Raw `is_call_answered` by disposition (analysed rows):", "", _md(ct), "",
               "Rows with a primary or secondary disposition but is_call_answered = 0: "
               + ", ".join(f"{d} {int((df.disposition.eq(d) & df.is_call_answered.eq(0)).sum()):,}"
                           for d in (*prim, *(s.dispositions or ()))) + ". The adapter follows the spec "
               "(answered = is_call_answered == 1); decide whether that is right.", "",
               f"Baseline p both ways (H = {h}):", "", _md(two.rename_axis("answered rule"))]
    if "meeting_fixed" not in df and "is_call_answered" not in df:
        md += ["No consistency checks apply to this dataset."]

    return DataProfile(
        dataset=cfg.dataset, adapter=rep, rows=len(df), leads=int(df.lead_id.nunique()),
        date_from=df.ts.min().to_pydatetime(), date_to=end.to_pydatetime(), triggered_leads=len(t),
        baseline_p=baseline_p, baseline_horizon_days=int(h), baseline_p_by_horizon_days={int(x): float(p) for x, p in tab.p.items()},
        volume=vol, markdown="\n".join(md) + "\n")


def write_profile(profile: DataProfile, path: Path | None = None) -> Path:
    path = Path(path) if path else REPORTS_DIR / f"data_profile_{profile.dataset}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(profile.markdown, encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> None:
    from abtest.config import load_experiment

    ap = argparse.ArgumentParser(prog="python -m abtest.profile", description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", required=True, choices=get_args(Dataset))
    ap.add_argument("--exp", required=True, type=Path, help="experiment YAML for that dataset")
    args = ap.parse_args(argv)
    cfg = load_experiment(args.exp)
    if cfg.dataset != args.dataset:
        ap.error(f"{args.exp} is for dataset {cfg.dataset!r}, not {args.dataset!r}")
    prof = build_profile(cfg)
    print(write_profile(prof))
    print(f"baseline_p            = {prof.baseline_p:.4f}")
    print(f"daily_leads           = {prof.volume.daily_leads:.2f}")
    print(f"daily_triggered_leads = {prof.volume.daily_triggered_leads:.2f}")


if __name__ == "__main__":
    main()
