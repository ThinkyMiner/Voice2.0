"""Sample ratio mismatch check and split evidence report (PLAN.md §2.4, §2.6, §2.8 rule 1).

CLI: python -m abtest.srm <scenario>   (make split-report SCENARIO=win)
"""

import math
import sqlite3
import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from scipy.stats import binomtest, chisquare

from abtest import config, router, store
from abtest.contracts import REPORTS_DIR, DaySplit, SplitSummary, SrmResult

NAVY, GOLD, CRIMSON = "#003366", "#e8a517", "#C8102E"
FONT = "Calibri, Carlito, 'Segoe UI', Arial, sans-serif"
CONFIDENCE = 0.95  # SrmResult.ci95
MAX_DATE_TICKS, MS_PER_DAY = 10, 86_400_000  # chart layout; plotly date dtick is in ms


def srm_test(n_a: int, n_b: int, share_b: float) -> SrmResult:
    """Chi-square goodness of fit (1 df) against the configured share; Wilson 95% CI."""
    n = n_a + n_b
    if n == 0:  # no evidence either way
        return SrmResult(n_A=0, n_B=0, configured_share_B=share_b, observed_share_B=math.nan,
                         ci95=(0.0, 1.0), chi2=0.0, p=1.0)
    chi2, p = chisquare([n_a, n_b], f_exp=[n * (1 - share_b), n * share_b])
    ci = binomtest(n_b, n).proportion_ci(confidence_level=CONFIDENCE, method="wilson")
    return SrmResult(n_A=n_a, n_B=n_b, configured_share_B=share_b, observed_share_B=n_b / n,
                     ci95=(float(ci.low), float(ci.high)), chi2=float(chi2), p=float(p))


def _srm(leads: pd.DataFrame, share_b: float) -> SrmResult:
    n_b = int((leads.variant == "B").sum())
    return srm_test(len(leads) - n_b, n_b, share_b)


def split_summary(conn: sqlite3.Connection, experiment_id: str) -> SplitSummary:
    cfg = store.get_config(conn, experiment_id)  # frozen config: configured share and salt
    share = cfg.allocation.B
    exp = store.read_df(conn, "SELECT lead_id, variant, bucket, ts FROM exposures "
                              "WHERE experiment_id = ? ORDER BY ts, rowid",
                        (experiment_id,))
    # Not parse_dates: isoformat() drops zero microseconds, and mixed formats parse to NaT.
    exp["ts"] = pd.to_datetime(exp.ts, format="ISO8601")
    # RUNNING period: from DRAFT -> RUNNING (DRAFT routes everyone to A) to the first RUNNING -> X
    # transition (a later allocation change is legit).
    start, end = conn.execute(
        "SELECT MIN(CASE WHEN json_extract(payload, '$.to_state') = 'RUNNING' THEN ts_sim END), "
        "MIN(CASE WHEN json_extract(payload, '$.from_state') = 'RUNNING' THEN ts_sim END) "
        "FROM decisions WHERE experiment_id = ? AND kind = 'state_change'", (experiment_id,)).fetchone()
    if start is not None:
        exp = exp[exp.ts >= pd.Timestamp(start)]
    if end is not None:
        exp = exp[exp.ts < pd.Timestamp(end)]

    first = exp.drop_duplicates("lead_id")  # sorted by ts: each lead's first exposure
    answered = store.read_df(conn, "SELECT DISTINCT lead_id FROM calls "
                                   "WHERE experiment_id = ? AND answered = 1", (experiment_id,))
    days = first.groupby(first.ts.dt.date).variant.value_counts().unstack(fill_value=0)
    per_day = [DaySplit(date=d, n_A=int(r.get("A", 0)), n_B=int(r.get("B", 0)),
                        share_B=r.get("B", 0) / r.sum()) for d, r in days.iterrows()]

    fresh = exp.lead_id.map(lambda lead: router.bucket(cfg.salt, lead))
    want = fresh.map(lambda b: router.variant_for(b, cfg.allocation))
    mismatches = int(((exp.bucket != fresh) | (exp.variant != want)).sum())
    return SplitSummary(
        experiment_id=experiment_id, configured_share_B=share,
        assigned=_srm(first, share), triggered=_srm(first[first.lead_id.isin(answered.lead_id)], share),
        per_day=per_day, leads_in_both_arms=int((exp.groupby("lead_id").variant.nunique() > 1).sum()),
        rehash_checked=len(exp), rehash_mismatches=mismatches, rehash_pass=mismatches == 0,
    )


def _share_line(r: SrmResult) -> str:
    if r.n_A + r.n_B == 0:
        return f"configured {r.configured_share_B:.2%}, achieved n/a (no leads)"
    return (f"configured {r.configured_share_B:.2%}, achieved {r.observed_share_B:.2%} "
            f"[{r.ci95[0]:.2%}, {r.ci95[1]:.2%}]")


def _png(s: SplitSummary, threshold: float, path: Path) -> None:
    fig = make_subplots(rows=1, cols=2, horizontal_spacing=0.1, subplot_titles=(
        "Configured vs achieved share B (95% Wilson CI)", "Share B by first-exposure date"))
    for name, r in (("Assigned", s.assigned), ("Triggered", s.triggered)):
        fig.add_trace(go.Scatter(
            x=[name], y=[r.observed_share_B], mode="markers+text", name=f"{name} achieved",
            text=[f"{r.observed_share_B:.2%}"], textposition="middle right",
            marker=dict(size=14, color=CRIMSON if r.p < threshold else NAVY),
            error_y=dict(type="data", symmetric=False, array=[r.ci95[1] - r.observed_share_B],
                         arrayminus=[r.observed_share_B - r.ci95[0]], thickness=2, width=10)),
            row=1, col=1)
    day_colour = CRIMSON if s.assigned.p < threshold else NAVY
    fig.add_trace(go.Scatter(x=[d.date for d in s.per_day], y=[d.share_B for d in s.per_day],
                             mode="lines+markers", name="Daily share B",
                             line=dict(color=day_colour, width=2)), row=1, col=2)
    for col in (1, 2):
        fig.add_hline(y=s.configured_share_B, line=dict(color=GOLD, width=2, dash="dash"),
                      annotation_text=f"configured {s.configured_share_B:.2%}",
                      annotation_position="top left" if col == 1 else "top right", row=1, col=col)
    fig.update_yaxes(tickformat=".1%", gridcolor="#e6e6e6")
    fig.update_xaxes(range=[-0.5, 1.5], row=1, col=1)  # room for the value labels
    day_step = math.ceil(len(s.per_day) / MAX_DATE_TICKS) or 1
    fig.update_xaxes(tickformat="%b %d", dtick=day_step * MS_PER_DAY, row=1, col=2)
    fig.update_layout(title=f"Split evidence: {s.experiment_id}", font=dict(family=FONT, color=NAVY),
                      plot_bgcolor="white", paper_bgcolor="white", width=1200, height=500,
                      legend=dict(orientation="h", y=-0.15))
    fig.write_image(path)


def split_report(conn: sqlite3.Connection, experiment_id: str,
                 out_dir: Path = REPORTS_DIR) -> Path:
    """Writes out_dir / f"split_{scenario or experiment_id}.md" and a PNG beside it."""
    s = split_summary(conn, experiment_id)
    threshold = store.get_config(conn, experiment_id).srm_p_threshold
    scenario = conn.execute("SELECT scenario FROM experiments WHERE experiment_id = ?",
                            (experiment_id,)).fetchone()[0]
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"split_{scenario or experiment_id}.md"
    png = md.with_suffix(".png")
    _png(s, threshold, png)

    def count(name: str, r: SrmResult) -> str:
        flag = "**SRM DETECTED**" if r.p < threshold else "OK"
        return (f"- {name} leads (A {r.n_A:,} / B {r.n_B:,}): {_share_line(r)}; "
                f"SRM χ² = {r.chi2:.2f}, p = {r.p:.4g}: {flag}")

    lines = [
        f"# Split evidence: {experiment_id}", "",
        count("Assigned", s.assigned), count("Triggered", s.triggered),
        f"- Leads in both arms: {s.leads_in_both_arms:,}",
        f"- Re-hash determinism: {'PASS' if s.rehash_pass else 'FAIL'} "
        f"(checked {s.rehash_checked:,}, mismatches {s.rehash_mismatches:,})", "",
        f"SRM is flagged when p < {threshold:g} (`srm_p_threshold`).", "",
        "## Per-day split (assigned leads, by first-exposure date)", "",
        "| Date | A | B | Share B |", "|---|---:|---:|---:|",
        *(f"| {d.date} | {d.n_A:,} | {d.n_B:,} | {d.share_B:.2%} |" for d in s.per_day), "",
        "## Method", "",
        "Counts are leads, not calls: each lead is counted once, at its first exposure logged while "
        "the experiment was RUNNING (exposures after the first RUNNING state change are a legitimate "
        "allocation change and are excluded). Triggered leads are assigned leads with at least one "
        "answered call. Each count is tested with a 1-df chi-square goodness-of-fit test against the "
        "configured share from the frozen config, and the achieved share carries a Wilson 95% score "
        "interval. Leads in both arms counts leads whose RUNNING-period exposures carry more than one "
        "variant. The re-hash check recomputes `router.bucket(salt, lead_id)` for every RUNNING-period "
        "exposure and fails if any stored bucket, or stored variant under the configured allocation, "
        "disagrees.", "",
        f"![split]({png.name})", "",
    ]
    md.write_text("\n".join(lines), encoding="utf-8")
    return md


def main(argv: list[str] | None = None) -> None:
    (name,) = argv if argv is not None else sys.argv[1:]
    eid = config.scenario_experiment(config.load_scenario(name)).id
    conn = store.connect()
    if store.get_state(conn, eid) is None:
        sys.exit(f"experiment {eid} is not in the store; run `make simulate SCENARIO={name}` first")
    md = split_report(conn, eid)
    print("\n".join(line for line in md.read_text(encoding="utf-8").splitlines() if line.startswith("- ")))
    print(f"report -> {md}\nchart  -> {md.with_suffix('.png')}")


if __name__ == "__main__":
    main()
