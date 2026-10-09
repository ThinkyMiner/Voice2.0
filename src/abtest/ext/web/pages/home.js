// #/ The loop: the ring, the KPI strip, the current cycle as a stepper, the cycle history.
import { get, send, state } from "../api.js";
import { $, html, mount, int, pct, fix, sint, when, short, pad2, stateChip, stageName, STAGES, gradeStack, shares, toast, enc, empty } from "../ui.js";
import { mountRing } from "../viz.js";

const ORDER = STAGES.map((s) => s[0]);
const STATION = { listen: 0, judge: 1, improve: 2, gate: 2, replay: 2, prove: 3, approval: 3, rollout: 4, done: 4, blocked: 2 };
const HALT_AT = { GATE_BLOCKED: "gate", REPLAY_NO_GAIN: "replay", REJECTED: "approval", STOPPED_HARM: "prove",
  BLOCKED_GUARDRAIL: "prove", INCONCLUSIVE: "prove", HALTED_SRM: "prove" };

function steps(c) {
  const halt = HALT_AT[c.outcome] || (c.stage === "blocked" ? "gate" : null);
  if (halt) { const h = ORDER.indexOf(halt); return ORDER.map((_, i) => (i < h ? "done" : i === h ? "halt" : "skip")); }
  if (c.stage === "done") return ORDER.map(() => "done");
  const k = ORDER.indexOf(c.stage);
  return ORDER.map((_, i) => (i < k ? "done" : i === k ? "current" : ""));
}
function station(c) {
  const halt = HALT_AT[c.outcome] || (c.stage === "blocked" ? "gate" : null);
  return halt ? { station: STATION[halt], halt: true } : { station: STATION[c.stage] ?? 0, halt: false };
}
const tone = (c) => (c.stage !== "done" && c.stage !== "blocked" ? "live" : c.outcome === "PROMOTED" ? "win" : "harm");

export async function render(el) {
  const [ov, lp] = await Promise.all([get("/api/overview"), get("/api/loop")]);
  const cycles = ov.cycles || [];
  const versions = [...new Set(cycles.map((c) => c.production_version))];
  const [iss, fx, xs] = await Promise.all([
    Promise.allSettled(versions.map((v) => get(`/api/issues?version=${enc(v)}`))),
    get("/api/fixes").catch(() => ({ items: [] })),
    get("/api/experiments").catch(() => ({ items: [] })),
  ]);
  const rcName = Object.fromEntries(iss.flatMap((r) => (r.status === "fulfilled" ? r.value.items.map((i) => [i.id, i]) : [])));
  const fixBy = Object.fromEntries(fx.items.map((f) => [f.id, f]));
  const expBy = Object.fromEntries(xs.items.map((x) => [x.experiment_id, x]));
  const cur = lp.cycle || ov.cycle;
  const k = ov.kpis || {};
  const sp = shares({ pass: k.pass_rate, non_fatal: k.non_fatal_rate, fatal: k.fatal_rate });

  mount(el, html`
    <section class="hero">
      <div>
        <div class="eyebrow accent">${cur ? `The Vani loop · Cycle ${pad2(cur.n)} · ${stageName(cur.stage)}` : "The Vani loop"}</div>
        <h1 class="title" style="margin-top:14px">Every call teaches the <em>next</em> prompt.</h1>
        <p class="lede">Vani Loop listens to buyer calls, grades each one against IndiaMART's QA checklist and your own rubrics,
          writes a prompt fix for the costliest failure, and proves it on 10% of simulated traffic before it ships.</p>
        <div class="advance">
          <div class="row">
            <button class="btn primary" id="adv" type="button"><svg viewBox="0 0 16 16"><path d="M4 3l8 5-8 5z"/></svg>Advance the loop</button>
            ${cur?.stage === "approval" && cur.experiment_id ? html`<a class="btn" href="#/experiments/${enc(cur.experiment_id)}">Review the experiment</a>` : ""}
          </div>
          <p class="next" id="next">Next: <b>${lp.next_action || "nothing queued"}</b></p>
          <details class="runlog" id="runlog" ${lp.busy ? "open" : ""} ${lp.log?.length ? "" : "hidden"}><summary><span class="eyebrow">Run log</span><span class="last-line" id="lastline"></span></summary><pre class="log" id="log"></pre></details>
        </div>
      </div>
      <div class="ring-wrap" id="ring"></div>
    </section>

    <section class="kpis" aria-label="Key numbers">
      <a class="kpi" href="#/audit" style="text-decoration:none"><div class="eyebrow">Calls audited</div><div class="big">${int(k.calls_audited)}</div>
        <div class="sub">of ${int(k.calls_total)} calls · ${pct(k.calls_total ? k.calls_audited / k.calls_total : null, 0)}</div>
        <div class="bar minibar"><span class="s-a" style="flex-basis:${k.calls_total ? (100 * k.calls_audited / k.calls_total).toFixed(1) : 0}%"></span></div></a>
      <a class="kpi" href="#/audit" style="text-decoration:none"><div class="eyebrow">Fatal rate</div><div class="big harm">${pct(k.fatal_rate)}</div>
        <div class="sub">non-fatal ${pct(k.non_fatal_rate)} · pass ${pct(k.pass_rate)}</div>${sp ? gradeStack(sp, "minibar") : ""}</a>
      <a class="kpi" href="#/audit" style="text-decoration:none"><div class="eyebrow">Agreement with QA</div><div class="big accent">${pct(k.fatal_vs_rest_accuracy)}</div>
        <div class="sub">fatal vs rest · κ ${fix(k.agreement_kappa)} · 3-class ${pct(k.agreement_overall)}</div></a>
      <a class="kpi" href="#/issues" style="text-decoration:none"><div class="eyebrow">Root causes</div><div class="big">${int(k.root_causes)}</div>
        <div class="sub">ranked by priority, top one fixed first</div></a>
      <a class="kpi" href="#/fixes" style="text-decoration:none"><div class="eyebrow">Fixes tested</div><div class="big">${int(k.fixes_tested)}</div>
        <div class="sub">gated, replayed, then ${int(k.experiments)} A/B tests</div></a>
      <a class="kpi" href="#/experiments" style="text-decoration:none"><div class="eyebrow">Weekly impact</div><div class="big win">${sint(k.per_week_gained)}</div>
        <div class="sub">successes a week gained · ${int(k.per_week_protected)} protected</div></a>
    </section>

    ${cur ? html`<section class="section" id="current"></section>` : html`<section class="section">${empty({ title: "The loop has not started", body: "Advance the loop to transcribe the production calls and start cycle 1.", cmd: "make loop" })}</section>`}

    <section class="section">
      <div class="section-head"><h2>Cycle <em>history</em></h2><p>Newest first. Each cycle starts on the prompt the last one left in production.</p></div>
      ${cycles.length ? html`<div class="timeline">${cycles.map((c) => {
        const rc = rcName[c.root_cause_id], x = expBy[c.experiment_id];
        return html`<div class="tl ${tone(c)}">
          <div class="when"><div class="c">Cycle ${pad2(c.n)}</div><div class="d">${when(c.started_ts)}</div></div>
          <div class="spine"><i></i></div>
          <div class="body">
            <div class="head"><span class="v">${c.production_version}${c.candidate_version ? html` → ${c.candidate_version}` : ""}</span>
              ${c.outcome ? stateChip(c.outcome) : html`<span class="chip accent"><i></i>In progress · ${stageName(c.stage)}</span>`}</div>
            ${rc ? html`<div class="rc"><a href="#/issues?version=${enc(rc.prompt_version)}">${rc.name}</a></div>` : c.root_cause_id ? html`<div class="rc">${c.root_cause_id}</div>` : ""}
            <div class="row">
              ${c.fix_id ? html`<a class="chip id" href="#/fixes/${enc(c.fix_id)}">${c.fix_id}</a>` : ""}
              ${c.experiment_id ? html`<a class="chip id" href="#/experiments/${enc(c.experiment_id)}">${c.experiment_id}</a>` : ""}
              ${x && !c.outcome ? stateChip(x.state) : ""}
            </div>
            ${c.notes?.length && c.n !== cur?.n ? html`<ul class="notes" style="margin-top:4px">${c.notes.map((n) => html`<li>${n}</li>`)}</ul>` : ""}
          </div></div>`;
      })}</div>` : empty({ title: "No cycles yet", body: "The first cycle starts when the loop is advanced.", compact: true })}
    </section>`);

  const prod = ov.production;
  let ring = null, shown = null;
  const drawRing = (c, busy, start = 0) => {
    const s = c ? station(c) : { station: 0, halt: false };
    if (ring && shown && shown.station === s.station && shown.halt === s.halt) { ring.setBusy(busy); return; }
    ring?.stop();
    ring = mountRing($("#ring", el), { ...s, version: prod?.version || "—", sha: prod?.sha256 ? `sha ${short(prod.sha256)}` : "", sub: c ? `Cycle ${pad2(c.n)}` : "", busy, start });
    shown = s;
  };
  const drawCurrent = (c) => {
    const host = $("#current", el);
    if (!host || !c) return;
    const rc = rcName[c.root_cause_id], f = fixBy[c.fix_id], x = expBy[c.experiment_id];
    const st = steps(c);
    mount(host, html`
      <div class="section-head"><h2>Cycle ${pad2(c.n)}, <em>${c.outcome ? "closed" : stageName(c.stage).toLowerCase()}</em></h2>
        <p>Prompt A is ${c.production_version}${c.candidate_version ? html`; Prompt B is ${c.candidate_version}` : ""} · started ${when(c.started_ts)} · updated ${when(c.updated_ts)}</p></div>
      <div class="stepper" role="list">${STAGES.map(([key, name, what], i) => html`
        <div class="step ${st[i]}" role="listitem" aria-current="${st[i] === "current" ? "step" : "false"}"><span class="node"></span><span class="name">${name}</span><span class="what">${what}</span></div>`)}</div>
      <div class="links3">
        <a class="linkcard" href="${rc ? `#/issues?version=${enc(rc.prompt_version)}` : "#/issues"}"><span class="eyebrow">Root cause</span><span class="t">${rc?.name || c.root_cause_id || "Not chosen yet"}</span>
          <span class="s">${rc ? `${int(rc.n_calls)} of ${int(rc.n_audited)} calls · priority ${fix(rc.priority)}` : "Chosen after the judge"}</span></a>
        <a class="linkcard" href="${c.fix_id ? `#/fixes/${enc(c.fix_id)}` : "#/fixes"}"><span class="eyebrow">Prompt fix</span><span class="t">${f?.rationale || c.fix_id || "Not written yet"}</span>
          <span class="s">${f ? `${f.edits.length} edit${f.edits.length === 1 ? "" : "s"} · gate ${f.gate ? (f.gate.passed ? "clear" : "blocked") : "pending"}` : "Written after the root cause"}</span></a>
        <a class="linkcard" href="${c.experiment_id ? `#/experiments/${enc(c.experiment_id)}` : "#/experiments"}"><span class="eyebrow">Experiment</span><span class="t">${c.experiment_id || "Not started"}</span>
          <span class="s">${x ? html`${x.looks_done} of ${x.n_looks} looks · ${x.assumed_effect || "simulated traffic"}` : "Simulated traffic, 90/10"}</span></a>
      </div>
      ${c.tried?.length > 1 ? html`<p class="muted" style="font-size:12.5px;margin-top:16px">Root causes tried this cycle, in order: ${c.tried.map((t, i) => html`${i ? " → " : ""}<span class="mono">${rcName[t]?.name || t}</span>`)}</p>` : ""}
      ${c.notes?.length ? html`<ul class="notes">${c.notes.map((n) => html`<li>${n}</li>`)}</ul>` : ""}`);
  };
  const drawLog = (log) => {
    const pre = $("#log", el);
    if (!log?.length) return;
    $("#runlog", el).hidden = false;
    const lines = log.slice(-9);
    mount(pre, lines.map((l, i) => html`<span class="${i === lines.length - 1 ? "last" : ""}">${l}</span>\n`));
    $("#lastline", el).textContent = lines[lines.length - 1];
    pre.scrollTop = pre.scrollHeight;
  };
  drawRing(cur, lp.busy);
  drawCurrent(cur);
  drawLog(lp.log);

  // --- advancing, with live progress from GET /api/loop
  const btn = $("#adv", el);
  let timer = 0, alive = true;
  const busyUi = (b) => { btn.disabled = b; btn.lastChild.textContent = b ? "Running…" : "Advance the loop"; };
  const poll = async () => {
    if (!alive) return;
    try {
      const now = await get("/api/loop");
      if (!alive) return;
      $("#next", el).lastElementChild.textContent = now.next_action || "nothing queued";
      drawLog(now.log);
      if (now.cycle) { const prev = shown?.station ?? 0; if (station(now.cycle).station !== prev) { ring?.stop(); ring = null; } drawRing(now.cycle, now.busy, prev); drawCurrent(now.cycle); }
      if (now.busy) timer = setTimeout(poll, 900);
      else { busyUi(false); window.dispatchEvent(new Event("vani:rerender")); }
    } catch (err) { busyUi(false); toast(`Loop status failed: ${err.message}`, "harm"); }
  };
  btn.addEventListener("click", async () => {
    busyUi(true);
    try {
      await send("POST", "/api/loop/advance", { until: null });
      $("#runlog", el).hidden = false;
      $("#runlog", el).open = true;
      ring?.setBusy(true);
      timer = setTimeout(poll, 500);
      if (state.demo) toast("Demo mode: a scripted run from the cache, nothing is written.");
    } catch (err) { busyUi(false); toast(err.status === 409 ? err.message : `Could not advance: ${err.message}`, "harm"); }
  });
  if (lp.busy) { busyUi(true); timer = setTimeout(poll, 900); }
  return () => { alive = false; clearTimeout(timer); ring?.stop(); };
}
