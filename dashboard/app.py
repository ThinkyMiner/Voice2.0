"""Voice bot A/B dashboard: experiments list and experiment detail (PLAN.md §2.10).

make dashboard -> http://localhost:8501, or ?exp=<experiment_id> for one experiment. The page
re-reads the store every REFRESH_S seconds (sidebar toggle), so `make live` can be watched.
"""

import math
import os
from contextlib import closing

import plotly.graph_objects as go
import streamlit as st

import common as c
from abtest import analysis, boundaries, contracts, decision, store
from abtest.contracts import State

REFRESH_S = 3  # seconds between store re-reads (make live SPEED=3 is 3 s per simulated day)
WIN_STATES = {State.PROMOTED, State.PENDING_APPROVAL, State.REJECTED}


def _layout(fig: go.Figure, height: int, **kw) -> go.Figure:
    fig.update_layout(height=height, font=dict(family=c.FONT, color=c.INK, size=14),
                      plot_bgcolor="white", paper_bgcolor="white", margin=dict(l=70, r=20, t=40, b=60),
                      legend=dict(orientation="h", x=0, y=1.02, yanchor="bottom"))
    fig.update_layout(**kw)
    fig.update_xaxes(gridcolor="#eef1f5", linecolor="#c9d0db", zeroline=False)
    fig.update_yaxes(gridcolor="#eef1f5", linecolor="#c9d0db", zeroline=False)
    return fig


def _chart(fig: go.Figure, key: str) -> None:
    st.plotly_chart(fig, theme=None, key=key, config={"displaylogo": False})


# --- experiments list ---------------------------------------------------------------------

def experiments_table(exps: list[c.Exp], selected: str) -> None:
    rows = []
    for e in exps:
        d, (value, note) = e.last, c.impact(e)[0]
        reason = e.decisions[-1].reason if e.decisions else ""
        rows.append(("ab-sel" if e.id == selected else "", [
            f'<a href="?exp={c.esc(e.id)}" target="_self">{c.esc(e.id)}</a><br>'
            f'<span class="ab-sub">{c.esc(e.scenario or "")} · {e.cfg.dataset} · seed {e.seed}</span>',
            c.badge(e.state.state),
            f'<b style="color:{c.NAVY if e.state.state == State.PENDING_APPROVAL else c.BADGE[e.state.state][0]}">'
            f'{c.esc(value)}</b><br>'
            f'<span class="ab-sub">{c.esc(note)}</span>',
            f'<span class="ab-num">{e.state.looks_done} / {e.cfg.window.looks}</span>',
            f'<span class="ab-num">{d.n_A:,} / {d.n_B:,}</span>' if d else "–",
            f'<span title="{c.esc(reason)}">{c.esc(c.shorten(reason, 110))}</span>',
        ]))
    c.html_block(c.table(["Experiment", "State", "Business impact", "Look",
                          "n A / B", "Current decision"], rows,
                         ["24%", "14%", "17%", "8%", "11%", "26%"]))
    st.caption("Business impact: the engine's estimate of extra (or lost) primary successes per week if B "
               "took 100% of traffic. n = matured triggered leads at the latest look. Pick an experiment in the "
               "sidebar or click its id.")


# --- experiment detail --------------------------------------------------------------------

def crossing(e: c.Exp):
    """(look row, text) for the look that decided, or None."""
    looks, s = e.looks, e.state.state
    if not looks:
        return None
    d, first_win = looks[-1], next((x for x in looks if x.primary.z >= x.primary.win_boundary), None)
    p = d.primary
    if s == State.STOPPED_HARM and p.z <= p.harm_boundary:
        return d, f"Harm boundary crossed at look {d.look}: z = {p.z:.2f} ≤ {p.harm_boundary:.2f}. B stopped."
    if s == State.STOPPED_HARM:
        even = ", although z crossed the win boundary" if p.z >= p.win_boundary else ""
        return d, f"Look {d.look}: the guardrail RCI lies beyond its margin{even}. B stopped."
    if s in WIN_STATES:
        then = {State.PROMOTED: "B promoted", State.PENDING_APPROVAL: "B awaits approval",
                State.REJECTED: "B rejected by a human"}[s]
        return d, f"Win boundary crossed at look {d.look}: z = {p.z:.2f} ≥ {p.win_boundary:.2f}. {then}."
    if s == State.HALTED_SRM:
        return d, f"Halted at look {d.look}: the traffic split is broken (SRM), so z is not trusted."
    if first_win is not None:
        tail = "never cleared" if s == State.BLOCKED_GUARDRAIL else "has not cleared yet"
        return first_win, f"Win boundary crossed at look {first_win.look}, but the guardrail {tail}."
    if s == State.INCONCLUSIVE:
        return d, f"Final look {d.look}: no boundary crossed."
    return None


def z_path_chart(e: c.Exp) -> go.Figure:
    cfg, looks = e.cfg, e.looks
    plan = boundaries.compute_boundaries(boundaries.planned_info_fractions(cfg), cfg)
    pt, pw, ph = list(plan.info_fractions), list(plan.win), [-h for h in plan.harm]
    t, z = [d.info_fraction for d in looks], [d.primary.z for d in looks]
    win, harm = [d.primary.win_boundary for d in looks], [d.primary.harm_boundary for d in looks]
    top, bottom = max(pw + z + win) + 1.0, min(ph + z + harm) - 1.0
    xmax = max([1.05] + [x + 0.05 for x in t])

    fig = go.Figure()
    fig.add_trace(go.Scatter(x=pt, y=[top] * len(pt), mode="lines", line=dict(width=0),
                             hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scatter(x=pt, y=pw, mode="lines", fill="tonexty", fillcolor="rgba(232,165,23,0.13)",
                             line=dict(color=c.GOLD, width=2, dash="dot"), name="Win boundary (planned looks)",
                             hovertemplate="planned t = %{x:.0%}<br>win z ≥ %{y:.2f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=pt, y=ph, mode="lines", line=dict(color=c.CRIMSON, width=2, dash="dot"),
                             name="Harm boundary (planned looks)",
                             hovertemplate="planned t = %{x:.0%}<br>harm z ≤ %{y:.2f}<extra></extra>"))
    fig.add_trace(go.Scatter(x=pt, y=[bottom] * len(pt), mode="lines", fill="tonexty",
                             fillcolor="rgba(200,16,46,0.08)", line=dict(width=0), hoverinfo="skip",
                             showlegend=False))
    fig.add_hline(y=0, line=dict(color="#9aa3b0", width=1))
    if looks:
        fig.add_trace(go.Scatter(x=t, y=win, mode="lines+markers", line=dict(color=c.GOLD, width=3),
                                 marker=dict(size=9, symbol="diamond"), name="Win boundary at each look",
                                 hovertemplate="t = %{x:.1%}<br>win z ≥ %{y:.2f}<extra></extra>"))
        fig.add_trace(go.Scatter(x=t, y=harm, mode="lines+markers", line=dict(color=c.CRIMSON, width=3),
                                 marker=dict(size=9, symbol="diamond"), name="Harm boundary at each look",
                                 hovertemplate="t = %{x:.1%}<br>harm z ≤ %{y:.2f}<extra></extra>"))
        last = c.BADGE[e.state.state][0] if e.state.state != State.RUNNING else c.NAVY
        fig.add_trace(go.Scatter(
            x=t, y=z, mode="lines+markers+text", text=[f"look {d.look}" for d in looks], legendrank=1,
            textposition=["bottom center" if d.primary.z < d.primary.harm_boundary
                          or d.primary.win_boundary - 0.6 < d.primary.z < d.primary.win_boundary else "top center"
                          for d in looks], textfont=dict(color=c.NAVY, size=12),
            line=dict(color=c.NAVY, width=4),
            marker=dict(size=[12] * (len(z) - 1) + [17], color=[c.NAVY] * (len(z) - 1) + [last],
                        line=dict(color="white", width=2)),
            name=f"z: B vs A on {c.primary_label(cfg)}",
            customdata=[[d.look, d.n_A, d.n_B, d.primary.lift_rel] for d in looks],
            hovertemplate="look %{customdata[0]}, t = %{x:.1%}<br>z = %{y:.2f}<br>lift %{customdata[3]:+.1%}"
                          "<br>n A %{customdata[1]:,} / B %{customdata[2]:,}<extra></extra>"))
    if (hit := crossing(e)) is not None:
        d, text = hit
        side = "right" if d.info_fraction > 0.65 else "left" if d.info_fraction < 0.35 else "center"
        fig.add_annotation(x=d.info_fraction, y=d.primary.z, text=text, showarrow=True, arrowhead=2,
                           arrowcolor=c.INK, ax={"right": -30, "left": 30, "center": 0}[side],
                           ay=130 if d.primary.z > (top + bottom) / 2 else -130, xanchor=side,
                           bgcolor="white", bordercolor=c.BADGE[e.state.state][0], borderwidth=2, borderpad=6,
                           font=dict(size=14, color=c.INK))
    # Zone labels where looks rarely land: z is seldom high early, and harm stops before it is late.
    fig.add_annotation(x=pt[0], y=top - 0.25, text="B better: promote zone", showarrow=False, xanchor="left",
                       yanchor="top", font=dict(color="#9a6b00", size=13))
    fig.add_annotation(x=xmax, y=bottom + 0.25, text="B worse: stop zone", showarrow=False, xanchor="right",
                       yanchor="bottom", font=dict(color=c.CRIMSON, size=13))
    fig.update_xaxes(title="Information fraction t = matured B leads ÷ n_B,max", range=[0, xmax],
                     tickformat=".0%")
    fig.update_yaxes(title="z-statistic (above 0: B better)", range=[bottom, top])
    return _layout(fig, 560, legend_traceorder="normal")


def guardrail_chart(e: c.Exp) -> go.Figure:
    cfg, looks, sec = e.cfg, e.looks, e.cfg.secondary
    k = 1 if sec.type == "continuous" else 100  # binary deltas are shown in percentage points
    sign = 1 if sec.worse_if != "decrease" else -1
    margin = sec.margin * k * sign if sec.margin else None
    fig = go.Figure()
    values = [0.0] + ([margin] if margin is not None else [])
    for d in looks:
        y, (lo, hi) = d.guardrail.delta * k, (x * k for x in d.guardrail.rci)
        values += [lo, hi]
        if margin is None:
            colour = c.NAVY
        elif (lo - margin) * sign > 0:
            colour = c.CRIMSON  # entirely beyond the margin: harm
        elif (hi - margin) * sign < 0:
            colour = c.GREEN  # entirely within: cleared
        else:
            colour = c.GOLD
        fig.add_trace(go.Scatter(
            x=[d.info_fraction], y=[y], mode="markers", showlegend=False,
            marker=dict(size=12, color=colour, line=dict(color="white", width=1.5)),
            error_y=dict(type="data", symmetric=False, array=[hi - y], arrayminus=[y - lo], color=colour,
                         thickness=3, width=10),
            hovertemplate=f"look {d.look}, t = {d.info_fraction:.1%}<br>Δ = {c.delta(d.guardrail.delta, cfg)}"
                          f"<br>RCI [{c.delta(d.guardrail.rci[0], cfg)}, {c.delta(d.guardrail.rci[1], cfg)}]"
                          "<extra></extra>"))
    span = (max(values) - min(values)) or 1
    top, bottom = max(values) + 0.12 * span, min(values) - 0.12 * span
    fig.add_hline(y=0, line=dict(color="#9aa3b0", width=1))
    if margin is not None:
        fig.add_hrect(y0=margin, y1=top if sign > 0 else bottom, fillcolor="rgba(200,16,46,0.07)", line_width=0)
        fig.add_hline(y=margin, line=dict(color=c.CRIMSON, width=2, dash="dash"),
                      annotation_text=f"margin {c.delta(sec.margin * sign, cfg)}", annotation_position="top left",
                      annotation_font=dict(color=c.CRIMSON))
    fig.update_xaxes(title="Information fraction t", range=[0, max([1.05] + [d.info_fraction + 0.05 for d in looks])],
                     tickformat=".0%")
    fig.update_yaxes(title=f"Δ {c.secondary_label(cfg)} (B vs A)", range=[bottom, top],
                     **(dict(tickformat="+.0%") if k == 1 else dict(ticksuffix=" pp")))
    return _layout(fig, 380, showlegend=False)


def split_panel(e: c.Exp, sp) -> None:
    thr = e.cfg.srm_p_threshold

    def verdict(ok: bool, good: str = "PASS", bad: str = "FAIL") -> str:
        return f'<span class="ab-ok">{good}</span>' if ok else f'<span class="ab-bad">{bad}</span>'

    rows = []
    for name, r in (("Assigned", sp.assigned), ("Triggered", sp.triggered)):
        achieved = "n/a" if r.n_A + r.n_B == 0 else (
            f"<b>{r.observed_share_B:.2%}</b><br><span class='ab-sub ab-num'>[{r.ci95[0]:.2%}, {r.ci95[1]:.2%}]</span>")
        rows.append([f'{name}<br><span class="ab-sub ab-num">A {r.n_A:,} / B {r.n_B:,}</span>',
                     f"{r.configured_share_B:.2%}", achieved,
                     f'<span class="ab-num">{c.pval(r.p)}</span><br>{verdict(r.p >= thr, bad="SRM")}'])
    c.html_block(c.table(["Leads", "Configured", "Achieved (95% CI)", "SRM p"], rows))
    st.caption(f"SRM: chi-square test of the achieved split against the configured one; p < {thr:g} halts the test.")
    c.html_block(
        f'<p style="margin:.5rem 0">Leads in both arms: <b>{sp.leads_in_both_arms:,}</b> '
        f'{verdict(sp.leads_in_both_arms == 0)} &nbsp;·&nbsp; Re-hash determinism: '
        f'{verdict(sp.rehash_pass)} <span class="ab-sub">({sp.rehash_checked:,} exposures re-bucketed, '
        f'{sp.rehash_mismatches:,} mismatches)</span></p>')
    if sp.per_day:
        r = sp.assigned
        fig = go.Figure(go.Scatter(x=[d.date for d in sp.per_day], y=[d.share_B for d in sp.per_day],
                                   mode="lines+markers", line=dict(color=c.NAVY, width=2), marker=dict(size=6),
                                   customdata=[[d.n_A, d.n_B] for d in sp.per_day],
                                   hovertemplate="%{x|%b %d}: B %{y:.2%}<br>A %{customdata[0]:,} / "
                                                 "B %{customdata[1]:,}<extra></extra>"))
        if r.n_A + r.n_B:
            fig.add_hrect(y0=r.ci95[0], y1=r.ci95[1], fillcolor="rgba(0,51,102,0.10)", line_width=0)
        fig.add_hline(y=r.configured_share_B, line=dict(color=c.GOLD, width=2, dash="dash"),
                      annotation_text=f"configured {r.configured_share_B:.0%}", annotation_position="top left")
        fig.update_yaxes(tickformat=".1%", title="B share, new leads")
        fig.update_xaxes(tickformat="%b %d")
        _chart(_layout(fig, 260, showlegend=False, margin=dict(l=70, r=20, t=20, b=40)), f"split-{e.id}")
        st.caption("Daily B share by first-exposure date; the band is the overall achieved share's 95% CI.")


def arm_cell(value: str, ci: tuple[float, float], fmt, n: int) -> str:
    band = "" if math.isnan(ci[0]) else f" <span class='ab-sub'>[{fmt(ci[0])}, {fmt(ci[1])}]</span>"
    return f'<span class="ab-num"><b>{value}</b>{band}<br><span class="ab-sub">n = {n:,}</span></span>'


def metrics_table(e: c.Exp) -> None:
    cfg, d, sec = e.cfg, e.last, e.cfg.secondary
    p = d.primary
    rows = [[f"<b>{c.esc(c.primary_label(cfg))} rate</b><br><span class='ab-sub'>primary: decides, "
             f"better if {cfg.primary.better}, MDE {cfg.primary.mde_rel:+.0%}</span>",
             arm_cell(f"{p.rate_A:.1%}", c.wilson(round(p.rate_A * d.n_A), d.n_A), "{:.1%}".format, d.n_A),
             arm_cell(f"{p.rate_B:.1%}", c.wilson(round(p.rate_B * d.n_B), d.n_B), "{:.1%}".format, d.n_B),
             f"<b>{p.lift_rel:+.1%}</b> relative ({p.lift_abs * 100:+.2f} pp)<br><span class='ab-sub'>95% CI "
             f"{p.ci95[0] * 100:+.2f} to {p.ci95[1] * 100:+.2f} pp</span>"]]
    o = c.outcomes(str(contracts.DB_PATH), e.id, e.created, d.ts_sim)
    o = o[o.mature]
    cells = []
    for arm in ("A", "B"):
        v = o.loc[o.variant.eq(arm), "secondary"].dropna()
        if sec.type == "binary":
            cells.append(arm_cell(f"{v.mean():.1%}", c.wilson(int(v.sum()), len(v)), "{:.1%}".format, len(v)))
        else:
            half = analysis.Z_CI * v.std(ddof=1) / math.sqrt(len(v)) if len(v) > 1 else math.nan
            cells.append(arm_cell(f"{v.mean():.1f}", (v.mean() - half, v.mean() + half), "{:.1f}".format, len(v)))
    g = d.guardrail
    role = (f"guardrail: can only veto, worse if {sec.worse_if}, margin {c.delta(sec.margin, cfg)}"
            if sec.role == "guardrail" else f"secondary goal: reported, never gates, better if {sec.better}")
    unit = " (s)" if sec.type == "continuous" and sec.column == "duration_s" else " rate"
    rows.append([f"<b>{c.esc(c.secondary_label(cfg))}{unit}</b><br><span class='ab-sub'>{role}</span>", *cells,
                 f"<b>{c.delta(g.delta, cfg)}</b><br><span class='ab-sub'>RCI {c.delta(g.rci[0], cfg)} to "
                 f"{c.delta(g.rci[1], cfg)} (repeated CI, valid at every look)</span>"])
    c.html_block(c.table(["Metric", "A (95% CI)", "B (95% CI)", "B vs A"], rows))
    st.caption(f"At look {d.look} ({d.ts_sim:%d %b %Y}, simulated). Per-arm intervals are Wilson for rates and "
               f"normal for means; the B-vs-A intervals are the engine's. Matured triggered leads only.")


def sample_progress(e: c.Exp) -> None:
    s, cfg, d = e.state, e.cfg, e.last
    if s.n_b_max is None:  # DRAFT: n_B,max is set when the experiment starts
        return
    n_b = d.n_B if d else 0
    day = (s.updated_ts_sim - s.started_ts_sim) / c.DAY if s.started_ts_sim else 0
    frac = n_b / s.n_b_max if s.n_b_max else 0
    st.progress(min(frac, 1.0), text=f"Sample: {n_b:,} of n_B,max = {s.n_b_max:,.0f} matured triggered B leads "
                                     f"({frac:.0%}, {f'look {d.look}' if d else 'no look yet'}) · simulated day "
                                     f"{day:.0f} of {cfg.window.days}")


def decision_log(e: c.Exp) -> None:
    rows, prev, cfg = [], None, e.cfg
    for d in e.decisions:
        if d.kind == "look":
            p, g = d.primary, d.guardrail
            stats = [f"{d.look} · {d.info_fraction:.2f}", f"{d.n_A:,} / {d.n_B:,}",
                     f"<b>{p.z:+.2f}</b> <span class='ab-sub'>({p.win_boundary:.2f} / {p.harm_boundary:.2f})</span>",
                     f"{p.lift_rel:+.1%}", f"{c.delta(g.delta, cfg)} <span class='ab-sub'>[{c.delta(g.rci[0], cfg)}, "
                     f"{c.delta(g.rci[1], cfg)}]</span>", f"{c.pval(d.srm.p)} / {c.pval(d.srm.p_triggered)}"]
            what = f"<b>{c.esc(d.decision)}</b>"
        else:
            stats = [""] * 6
            what = f"{c.esc(d.from_state or '')} → {c.badge(State(d.to_state))}"
        cls = "ab-change" if d.kind == "state_change" else ""
        rows.append((f"ab-stats {cls}", [f'<span class="ab-num">{d.ts_sim:%d %b}</span>' if d.ts_sim else "",
                                         *[f'<span class="ab-num">{x}</span>' for x in stats], what, c.esc(d.actor)]))
        rows.append((f"ab-why {cls}", [(9, "as above" if d.reason == prev else c.esc(d.reason))]))
        prev = d.reason
    c.html_block(c.table(["Sim date", "Look · t", "n A / B", "z (win / harm)", "Lift", "Guardrail Δ [RCI]",
                          "SRM p", "Decision", "Actor"], rows))
    st.caption("Append-only: one row per look (including CONTINUE) and one shaded row per state change, each "
               "with its reason; the same rows go to logs/decisions.jsonl. z is shown with the win / harm "
               "boundaries of that look; SRM p is for assigned / triggered leads.")


def config_panel(e: c.Exp) -> None:
    cfg, s = e.cfg, e.state
    sec, b = cfg.secondary, cfg.variants["B"].patch
    change = ("same prompt as A" if b is None else
              f"append “{b.text}”" if b.mode == "append" else
              f"replace “{b.find}” with “{b.replace}”" if b.mode == "replace_text" else f"{b.mode} patch")
    items = [
        ("Variant B", c.esc(change)),
        ("Dataset · unit · trigger", f"{cfg.dataset} · {cfg.unit} · {cfg.trigger}"),
        ("Allocation (configured)", f"A {cfg.allocation.A:.0%} / B {cfg.allocation.B:.0%}; live now "
                                    f"A {s.allocation.A:.0%} / B {s.allocation.B:.0%}"),
        ("Window · looks · maturation", f"{cfg.window.days} days · {cfg.window.looks} looks · "
                                        f"{cfg.maturation_days:g} days"),
        ("Primary", f"{c.esc(c.primary_label(cfg))} (baseline p {cfg.baseline_p:.1%}, MDE {cfg.primary.mde_rel:+.0%})"),
        ("Secondary", f"{c.esc(sec.name)} ({sec.type}, {sec.role}"
                      + (f", worse if {sec.worse_if}, margin {c.delta(sec.margin, cfg)})" if sec.margin else ")")),
        ("α win · α harm · power", f"{cfg.alpha_win:g} (OBF-type) · {cfg.alpha_harm:g} (Pocock-type) · {cfg.power:.0%}"),
        ("First look · SRM threshold", f"t ≥ {cfg.min_info_first_look:g} · p < {cfg.srm_p_threshold:g}"),
        ("n_B,max · promotion · salt", f"{s.n_b_max:,.0f} · {cfg.promotion} · {c.esc(cfg.salt)}"),
        ("Scenario · seed", f"{c.esc(e.scenario or '–')} · {e.seed}"),
    ]
    hashes = [("config_hash", s.config_hash), ("prompt_hash A", s.prompt_hash["A"]), ("prompt_hash B", s.prompt_hash["B"])]
    left, right = st.columns([3, 2])
    with left:
        c.html_block(c.table(["Setting", "Value"], [[k, v] for k, v in items], ["34%", "66%"]))
    with right:
        c.html_block(c.table(["Frozen at start (SHA-256)"],
                             [[f'<span class="ab-sub">{k}</span><br><span class="ab-hash">{c.esc(h)}</span>']
                              for k, h in hashes]))
        with st.expander("Frozen config (JSON)"):
            st.json(cfg.model_dump(mode="json"), expanded=False)


def truth_panel(e: c.Exp) -> None:
    with st.expander("Ground truth (engine never sees this)", expanded=False):
        t, d = e.truth, e.last
        if t is None or d is None:
            st.info("The simulator writes its ground truth when the run ends.")
            return
        p, g, cfg = d.primary, d.guardrail, e.cfg
        lo, hi = p.ci95[0] / p.rate_A, p.ci95[1] / p.rate_A
        sec = (lambda x: f"{x:+.1%}") if t.secondary_change_type == "relative" else (lambda x: f"{x * 100:+.1f} pp")
        covered = "yes" if lo <= t.realised_primary_rel_lift <= hi else "no"
        rows = [
            ["Primary: relative lift in " + c.esc(c.primary_label(cfg)), f"{t.injected_primary_rel_lift:+.1%}",
             f"{t.realised_primary_rel_lift:+.1%} <span class='ab-sub'>(B {t.rate_B:.1%} vs its counterfactual "
             f"{t.rate_B_counterfactual:.1%})</span>",
             f"{p.lift_rel:+.1%} <span class='ab-sub'>(95% CI {lo:+.1%} to {hi:+.1%}; covers the realised lift: "
             f"{covered})</span>"],
            [f"Secondary: change in {c.esc(c.secondary_label(cfg))}", sec(t.injected_secondary_change),
             f"{sec(t.realised_secondary_change)}", f"{c.delta(g.delta, cfg)} <span class='ab-sub'>(RCI "
             f"{c.delta(g.rci[0], cfg)} to {c.delta(g.rci[1], cfg)})</span>"],
            ["Leads (A / B), exposures dropped", "", f"{t.leads['A']:,} / {t.leads['B']:,}, {t.exposures_dropped:,}",
             f"{d.n_A:,} / {d.n_B:,} matured triggered at look {d.look}"],
        ]
        c.html_block(c.table(["", "Injected (sim config)", "Realised in this run", "Engine estimate (last look)"], rows))
        st.caption("From the sim_truth table, which only the simulator writes. Realised = B's actual outcome "
                   "vs its own counterfactual (what B's leads did historically), over every triggered lead.")


def approval_box(e: c.Exp) -> None:
    with st.container(border=True):
        st.markdown(f"**Decision needed.** {c.impact(e)[1]} Approving gives B 100% of traffic; "
                    "rejecting returns all traffic to A. Either choice is logged with your name.")
        actor = st.text_input("Approver (recorded in the decision log)", value=os.environ.get("USER", ""),
                              key="actor")
        left, right, _ = st.columns([1.3, 1, 4])
        approve = left.button("Approve: promote B", type="primary", key="approve")
        reject = right.button("Reject: keep A", key="reject")
    if approve or reject:
        try:
            with closing(store.connect(contracts.DB_PATH)) as conn:  # the dashboard's only write
                out = decision.approve(conn, e.id, actor, approved=approve)
        except (KeyError, ValueError) as err:
            st.error(str(err))
            return
        st.session_state["flash"] = f"{e.id}: {State.PENDING_APPROVAL.value} → {out.state.value} by {actor.strip()}."
        st.rerun()


def kpis(e: c.Exp, sp) -> None:
    """Headline numbers; a value turns crimson when it is what stopped B, green when it cleared."""
    cfg, d, r = e.cfg, e.last, sp.assigned
    cards = [("Look", f"{e.state.looks_done} of {cfg.window.looks}", c.NAVY,
              f"information fraction t = {d.info_fraction:.2f}" if d else f"first look at t ≥ {cfg.min_info_first_look:g}")]
    if d:
        p, g = d.primary, d.guardrail
        z_col = c.CRIMSON if p.z <= p.harm_boundary else c.GREEN if p.z >= p.win_boundary else c.NAVY
        sign = -1 if cfg.secondary.worse_if == "decrease" else 1
        g_col = (c.NAVY if g.margin is None else c.CRIMSON if (g.rci[0] - g.margin * sign) * sign > 0
                 else c.GREEN if (g.rci[1] - g.margin * sign) * sign < 0 else c.NAVY)
        cards += [
            (f"{c.primary_label(cfg)} rate", f"{p.rate_A:.1%} → {p.rate_B:.1%}", c.NAVY,
             f"lift {p.lift_rel:+.1%} (95% CI {p.ci95[0] / p.rate_A:+.0%} to {p.ci95[1] / p.rate_A:+.0%})"),
            ("z vs boundaries", f"{p.z:+.2f}", z_col, f"win ≥ {p.win_boundary:.2f} · harm ≤ {p.harm_boundary:.2f}"),
            ("Guardrail Δ", c.delta(g.delta, cfg), g_col,
             f"{c.secondary_label(cfg)}: RCI {c.delta(g.rci[0], cfg)} to {c.delta(g.rci[1], cfg)}"
             + (f", margin {c.delta(g.margin * sign, cfg)}" if g.margin else "")),
        ]
    if r.n_A + r.n_B:
        srm_col = c.CRIMSON if min(r.p, sp.triggered.p) < cfg.srm_p_threshold else c.NAVY
        cards.append(("Traffic to B", f"{r.observed_share_B:.2%}", srm_col,
                      f"configured {r.configured_share_B:.0%} · SRM p = {c.pval(r.p)}"))
    c.html_block('<div class="ab-kpis">' + "".join(
        f'<div class="ab-kpi"><div class="l">{c.esc(label)}</div><div class="v" style="color:{col}">{c.esc(v)}</div>'
        f'<div class="s">{c.esc(sub)}</div></div>' for label, v, col, sub in cards) + "</div>")


def detail(e: c.Exp) -> None:
    s, cfg = e.state, e.cfg
    scale = c.scale(e.scenario)
    st.divider()
    c.banner([(cfg.dataset, scale)])
    c.html_block(f'<div style="display:flex;align-items:center;gap:.9rem;flex-wrap:wrap">'
                 f'<h2 style="margin:0;padding:.2rem 0;color:{c.NAVY}">{c.esc(e.id)}</h2>{c.badge(s.state)}</div>')
    st.caption(f"Scenario {e.scenario or '–'} · seed {e.seed} · dataset {cfg.dataset} · promotion {cfg.promotion} · "
               f"started {s.started_ts_sim:%d %b %Y} (simulated)" if s.started_ts_sim else "Not started")
    if msg := st.session_state.pop("flash", None):
        st.success(msg)
    head = c.impact(e)[1] + (f" Weekly figures are at the simulated ×{scale:g} volume." if scale and scale != 1 else "")
    why = e.decisions[-1].reason if e.decisions else ""
    c.html_block(f'<div class="ab-head" style="--c:{c.BADGE[s.state][0]}"><div class="big">{c.esc(head)}</div>'
                 f'<div class="why">Engine: {c.esc(why)}</div></div>')
    if s.state == State.PENDING_APPROVAL:
        approval_box(e)
    sp = c.split_summary(str(contracts.DB_PATH), e.id, e.version)
    kpis(e, sp)

    st.subheader("Sequential test: z-path across looks")
    st.caption("Each point is a look. B is promoted once z reaches the gold win boundary and the guardrail clears, "
               "and stopped as soon as z reaches the crimson harm boundary. Boundaries are recomputed at the "
               "observed information fractions (Lan-DeMets alpha spending), so looking often never inflates the "
               "2.5% false-win rate; dotted lines show the planned schedule.")
    _chart(z_path_chart(e), f"z-{e.id}")

    st.subheader("Sample size and metrics per arm")
    sample_progress(e)
    if e.last:
        metrics_table(e)

    left, right = st.columns(2, gap="large")
    with left:
        st.subheader("Guardrail: repeated CI vs margin")
        if e.looks:
            _chart(guardrail_chart(e), f"g-{e.id}")
            st.caption("Green: RCI entirely inside the margin (cleared). Gold: not cleared yet. Crimson: entirely "
                       "beyond the margin, which stops B. The guardrail can only veto; the primary decides.")
        else:
            st.info("No look yet.")
    with right:
        st.subheader("Traffic split")
        split_panel(e, sp)

    st.subheader("Decision log")
    decision_log(e)
    st.subheader("Config and hashes")
    config_panel(e)
    truth_panel(e)


# --- page -----------------------------------------------------------------------------------

st.set_page_config(page_title="Voice Bot A/B", page_icon=":material/call:", layout="wide")
c.style()
first = c.load_experiments()
with st.sidebar:
    exp_id = st.selectbox("Experiment", [e.id for e in first], key="exp", bind="query-params") if first else None
    live = st.toggle(f"Auto-refresh every {REFRESH_S} s", value=True,
                     help="Re-reads the store so a `make live` run can be watched as it happens.")


@st.fragment(run_every=REFRESH_S if live else None)
def page() -> None:
    exps = c.load_experiments()
    if {e.id for e in exps} != {e.id for e in first}:
        st.rerun()  # experiments appeared or went: rebuild the sidebar selector
    c.banner([(e.cfg.dataset, c.scale(e.scenario)) for e in exps])
    st.title("Voice Bot A/B experiments")
    if not exps:
        st.info(f"No experiments in the store yet ({contracts.DB_PATH}). Run `make demo`, "
                "`make simulate SCENARIO=win`, or `make live SCENARIO=harm SPEED=3` to watch one run.")
        return
    experiments_table(exps, exp_id)
    detail(next(e for e in exps if e.id == exp_id))


page()

