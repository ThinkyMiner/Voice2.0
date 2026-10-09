// #/experiments and #/experiments/<id>: state, split, looks, the z-path against the win and harm
// boundaries, the engine's decision log, impact, and the human approval gate.
import { get, send, state as app } from "../api.js";
import { $, html, raw, mount, int, pct, spct, fix, sint, when, day, stateChip, empty, toast, enc, pad2 } from "../ui.js";
import { zChart } from "../viz.js";

const simLabel = (x) => html`<span class="pill" style="height:26px"><i class="dot demo"></i>Simulated traffic${x.assumed_effect ? html` · ${x.assumed_effect}` : ""}</span>`;
const splitBar = (al) => html`<div class="splitbar"><div class="bar"><span class="s-a" style="flex-basis:${(al.A * 100).toFixed(1)}%"></span><span class="s-b" style="flex-basis:${(al.B * 100).toFixed(1)}%"></span></div>
  <div class="lbls"><span>A ${pct(al.A, 0)}</span><span>B ${pct(al.B, 0)}</span></div></div>`;
const dots = (done, n) => html`<span class="looks-dots" title="${done} of ${n} looks">${Array.from({ length: n }, (_, i) => html`<i class="${i < done ? (i === done - 1 ? "last" : "on") : ""}"></i>`)}</span>`;

export async function renderList(el) {
  const r = await get("/api/experiments");
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Prove</div><h1 class="title">A/B <em>experiments</em></h1>
      <p class="lede">Prompt B gets 10% of leads and a sequential test decides: promote, stop for harm, block on the guardrail, or call it inconclusive.
        There is no live traffic here: every run replays real call history with an assumed effect for B, which the engine never sees.</p></div></div>
    ${r.items.length ? html`<div class="card" style="padding:4px 22px">${r.items.map((x) => html`
      <a class="xcard" href="#/experiments/${enc(x.experiment_id)}">
        <div><div class="row"><span class="id">${x.experiment_id}</span>${x.cycle != null ? html`<span class="chip accent">cycle ${pad2(x.cycle)}</span>` : ""}</div>
          <div class="sub">${x.assumed_effect ? `Simulated traffic · ${x.assumed_effect}` : "Simulated traffic"}</div></div>
        <div>${stateChip(x.state)}<div class="row" style="margin-top:10px">${dots(x.looks_done, x.n_looks)}<span class="mono muted" style="font-size:11px">${x.looks_done}/${x.n_looks} looks</span></div></div>
        <div>${splitBar(x.allocation)}</div>
        <div style="text-align:right"><div class="eyebrow">Lift</div><div class="big md ${x.lift_rel > 0 ? "win" : x.lift_rel < 0 ? "harm" : ""}" style="margin-top:4px">${spct(x.lift_rel, 1)}</div></div>
      </a>`)}</div>` : empty({ title: "No experiments yet", body: "Prove starts one when a fix clears the gate and the replay.", cmd: "make loop", glyph: "flask" })}`);
}

export async function renderExperiment(el, { id }) {
  const x = await get(`/api/experiments/${enc(id)}`);
  const cfg = x.config || {}, looks = x.looks || [];
  const last = looks[looks.length - 1];
  const started = x.decisions?.find((d) => d.ts_sim)?.ts_sim || looks[0]?.ts_sim;
  const dayOf = (ts) => (started && ts ? Math.round((new Date(ts) - new Date(started)) / 864e5) : null);
  const pending = x.state === "PENDING_APPROVAL";
  let actor = "";
  try { actor = localStorage.getItem("vani-actor") || ""; } catch { /* none */ }
  const prim = cfg.primary || {}, sec = cfg.secondary || {};

  mount(el, html`
    <div class="page-head"><div>
      <div class="eyebrow">Experiment${x.cycle != null ? ` · cycle ${pad2(x.cycle)}` : ""} · ${cfg.dataset || ""}</div>
      <h1 class="title" style="font-size:clamp(30px,3.8vw,46px);overflow-wrap:anywhere">${x.experiment_id}</h1>
      <div class="row" style="margin-top:14px">${stateChip(x.state)}${simLabel(x)}</div></div></div>

    <section class="kpis" style="margin-bottom:28px">
      <div class="kpi"><div class="eyebrow">Lift in ${prim.name || "primary"}</div><div class="big ${last?.lift_rel > 0 ? "win" : last?.lift_rel < 0 ? "harm" : ""}">${spct(last?.lift_rel)}</div>
        <div class="sub">95% CI ${last ? `${spct(last.ci95?.[0])} to ${spct(last.ci95?.[1])}` : "—"}</div></div>
      <div class="kpi"><div class="eyebrow">z at the last look</div><div class="big">${fix(last?.z)}</div><div class="sub">win ≥ ${fix(last?.win_bound)} · harm ≤ ${fix(last ? -Math.abs(last.harm_bound) : null)}</div></div>
      <div class="kpi"><div class="eyebrow">Looks</div><div class="big">${looks.length}<small>/ ${cfg.window?.looks ?? "—"}</small></div><div class="sub">${last ? `day ${dayOf(last.ts_sim)} of ${cfg.window?.days ?? "—"}` : "no look yet"}</div></div>
      <div class="kpi"><div class="eyebrow">Matured leads</div><div class="big">${int((last?.n_a || 0) + (last?.n_b || 0))}</div><div class="sub">A ${int(last?.n_a)} · B ${int(last?.n_b)} of n_B,max ${int(x.n_b_max)}</div></div>
      <div class="kpi"><div class="eyebrow">Impact a week</div><div class="big ${x.impact?.per_week > 0 ? "win" : x.impact?.per_week < 0 ? "harm" : ""}">${x.impact?.per_week == null ? "—" : sint(x.impact.per_week)}</div>
        <div class="sub">${x.impact?.per_week == null ? (x.state === "HALTED_SRM" ? "untrusted: the split failed its check" : "after the first look") : `${x.impact.unit} at 100% of traffic`}</div></div>
      <div class="kpi"><div class="eyebrow">Live split</div><div style="margin-top:14px">${splitBar(x.allocation || cfg.allocation || { A: 1, B: 0 })}</div><div class="sub">${pending ? "B keeps its share until someone decides" : x.state === "PROMOTED" ? "B serves everyone" : x.state === "RUNNING" ? "configured split" : "all traffic back on A"}</div></div>
    </section>

    ${pending ? html`<div class="decide-panel" id="decide">
      <div><div class="eyebrow" style="color:var(--accent-2-ink)">Awaiting a human</div>
        <div style="font:400 24px/1.2 var(--f-display);margin-top:6px">B crossed the win boundary. <em>Promote it to everyone?</em></div>
        <p class="muted" style="font-size:12.5px;margin-top:6px">${last?.reason || ""}</p></div>
      <div class="row"><label class="field"><span>Your name</span><input class="input" id="actor" value="${actor}" placeholder="e.g. kartik" autocomplete="name"></label>
        <button class="btn" id="reject" type="button">Reject</button><button class="btn accent" id="approve" type="button">Approve B</button></div>
    </div>` : ""}

    <section class="section" style="margin-top:${pending ? 28 : 0}px">
      <div class="card">
        <div class="card-head"><div><div class="eyebrow">Sequential test · ${looks.length} look${looks.length === 1 ? "" : "s"}</div><h3 class="serif" style="margin-top:6px">The z-path, <em>against its boundaries</em></h3></div>
          <div class="legend"><span><i style="background:var(--ink);height:2px;width:14px"></i>z of B vs A</span><span><i style="background:var(--win)"></i>Win (O'Brien-Fleming)</span><span><i style="background:var(--harm)"></i>Harm (Pocock)</span></div></div>
        <div id="z" style="min-height:300px"></div>
        <p class="muted" style="font-size:12px;margin-top:6px">z above zero means B is ahead on ${prim.name || "the primary metric"}. Crossing the dashed green line can promote B (if the guardrail clears); crossing the red one stops it.</p>
      </div>
    </section>

    <section class="section">
      <div class="section-head"><h2>Looks</h2><p>One row per scheduled look; the engine's own numbers.</p></div>
      ${looks.length ? html`<div class="card flush"><div class="table-wrap"><table class="table">
        <thead><tr><th>Look</th><th>Day</th><th class="r">Info</th><th class="r">n A</th><th class="r">n B</th><th class="r">Lift</th><th>95% CI</th><th class="r">z</th><th class="r">Win</th><th class="r">Harm</th><th>Decision</th></tr></thead>
        <tbody>${looks.map((l) => html`<tr><td class="mono">${l.look}</td><td class="mono">${dayOf(l.ts_sim) ?? "—"}</td><td class="r mono">${pct(l.info_fraction, 0)}</td>
          <td class="r mono">${int(l.n_a)}</td><td class="r mono">${int(l.n_b)}</td><td class="r mono">${spct(l.lift_rel)}</td>
          <td class="mono muted" style="white-space:nowrap">${spct(l.ci95?.[0])}, ${spct(l.ci95?.[1])}</td><td class="r mono"><b>${fix(l.z)}</b></td>
          <td class="r mono" style="color:var(--win)">${fix(l.win_bound)}</td><td class="r mono" style="color:var(--harm)">${fix(-Math.abs(l.harm_bound))}</td>
          <td>${l.decision === "CONTINUE" ? html`<span class="chip">continue</span>` : stateChip(l.decision)}</td></tr>`)}</tbody></table></div></div>`
        : empty({ title: "No looks yet", body: "The first look comes once enough B leads have matured.", compact: true, glyph: "flask" })}
    </section>

    <section class="section">
      <div class="section-head"><h2>Decision <em>log</em></h2><p>Append-only. The engine writes a reason for every look and every state change.</p></div>
      <div class="card" style="padding:4px 22px"><div class="dlog">${(x.decisions || []).filter((d, i, all) => !(d.kind === "look" && all[i + 1]?.kind === "state_change" && all[i + 1].look === d.look && all[i + 1].reason === d.reason)).map((d) => html`
        <div class="dl"><div class="ts">${d.ts_sim ? `day ${dayOf(d.ts_sim)}` : ""}<br>${when(d.ts_wall)}</div>
          <div>${d.kind === "look" ? html`<span class="chip">look ${d.look}</span>` : stateChip(d.to_state || d.decision)}</div>
          <div><div class="why">${d.reason}</div><div class="who">${d.actor === "engine" ? "ENGINE" : `HUMAN · ${d.actor}`}${d.from_state ? ` · ${d.from_state} → ${d.to_state}` : ""}</div></div></div>`)}</div></div>
    </section>

    <section class="section">
      <div class="section-head"><h2>Configuration</h2><p>Frozen when the experiment started.</p></div>
      <dl class="cfg">
        ${[["Dataset", cfg.dataset], ["Unit · trigger", `${cfg.unit} · ${cfg.trigger}`], ["Split", cfg.allocation ? `${pct(cfg.allocation.A, 0)} / ${pct(cfg.allocation.B, 0)}` : "—"], ["Promotion", cfg.promotion],
          ["Primary", `${prim.name} (${(prim.dispositions || []).join(", ")}), ${prim.better}`], ["Smallest lift sized for", prim.mde_rel != null ? pct(prim.mde_rel, 0) : "—"],
          ["Guardrail", `${sec.name}, worse if ${sec.worse_if}, margin ${sec.margin_rel != null ? pct(sec.margin_rel, 0) : sec.margin_abs != null ? `${fix(sec.margin_abs * 100, 0)} pp` : "—"}`],
          ["Alpha win · harm", `${cfg.alpha_win} · ${cfg.alpha_harm}`], ["Power", cfg.power], ["Window", cfg.window ? `${cfg.window.days} days, ${cfg.window.looks} looks` : "—"],
          ["Maturation", cfg.maturation_days != null ? `${cfg.maturation_days} days` : "—"], ["SRM threshold", cfg.srm_p_threshold], ["Baseline p", cfg.baseline_p], ["n_B,max", int(x.n_b_max)],
          ["Salt", cfg.salt], ["Started (simulated)", day(started)]].map(([k, v]) => html`<div><dt>${k}</dt><dd>${v ?? "—"}</dd></div>`)}
      </dl>
    </section>`);

  const z = $("#z", el);
  const draw = () => {
    const svg = zChart(looks, { width: Math.max(300, z.clientWidth), state: x.state, started });
    mount(z, svg ? raw(svg) : empty({ title: "No look to plot yet", body: "The z-path starts at the first look, once enough B leads have matured.", compact: true, glyph: "flask" }));
  };
  draw();
  let w = z.clientWidth;
  const ro = new ResizeObserver(() => { if (Math.abs(z.clientWidth - w) > 8) { w = z.clientWidth; draw(); z.querySelector(".zpath")?.classList.remove("draw"); } });
  ro.observe(z);

  if (pending) {
    const decide = async (approved) => {
      const name = $("#actor", el).value.trim();
      if (!name) { $("#actor", el).focus(); return toast("Add your name: approvals are recorded with the approver.", "harm"); }
      try { localStorage.setItem("vani-actor", name); } catch { /* not remembered */ }
      $("#approve", el).disabled = $("#reject", el).disabled = true;
      try {
        await send("POST", `/api/experiments/${enc(id)}/decision`, { actor: name, approved });
        toast(`${approved ? "Approved" : "Rejected"} by ${name}.${app.demo ? " Demo mode: kept in this tab only." : ""}`);
        window.dispatchEvent(new Event("vani:rerender"));
      } catch (err) {
        $("#approve", el).disabled = $("#reject", el).disabled = false;
        toast(`Decision failed: ${err.message}`, "harm");
      }
    };
    $("#approve", el).addEventListener("click", () => decide(true));
    $("#reject", el).addEventListener("click", () => decide(false));
  }
  return () => ro.disconnect();
}
