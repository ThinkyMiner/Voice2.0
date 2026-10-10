// #/issues: the judge's failed checks grouped into root causes and ranked by what they cost. Each cause says
// where it stands (a fix in the loop, not tried, or engineering work), how far the judge behind it agrees with
// IndiaMART's QA auditors, and which calls to open first. ?rc=<id> scrolls to one cause, ?type= filters.
import { get, state } from "../api.js";
import { $, $$, html, mount, int, pct, fix, when, stageName, typeChip, grade, empty, enc, seg, agreeLine, kappaWord } from "../ui.js";
import { fixSteps, fixPhrase, cycleAt } from "./fixes.js";

const FIXABLE = ["prompt", "flow"];  // loop.FIXABLE: the root types a prompt change can address
const TYPES = [["prompt", "Prompt"], ["flow", "Flow"], ["asr", "Speech"], ["knowledge", "Catalogue"], ["platform", "Platform"]];
const WORK = { asr: "speech recognition", knowledge: "catalogue", platform: "platform" };
const PAGE = 25;  // more calls per click in a cause's full list
const TOP = 3;  // causes drawn as full cards; the rest are one line each, opened on demand

function noCauses(v, pv, c) {
  if (pv?.fix_id) {
    return empty({ title: `${v} has no calls of its own yet`, glyph: "doc",
      body: `${v} is ${pv.status === "candidate" ? "an untested candidate" : `a ${pv.status} version`} written by fix ${pv.fix_id}. Root causes come from a version's own judged calls, and ${v} has none until its replay or A/B test runs.`,
      action: html`<div class="row" style="justify-content:center"><a class="btn sm" href="#/fixes/${enc(pv.fix_id)}">Open the fix</a><a class="btn sm ghost" href="#/issues">Production's root causes</a></div>` });
  }
  const mine = c?.production_version === v ? c : null;
  return empty({ title: `Nothing ranked for ${v} yet`, glyph: "ring", action: html`<a class="btn sm" href="#/">Open the Loop page</a>`,
    body: mine ? `Root causes are ranked at the Improve stage, from the judge's failed checks. ${cycleAt(mine)}`
      : `Root causes are ranked at the Improve stage of a loop cycle, from the judge's failed checks. ${c ? `The loop is in cycle ${c.n}, on ${c.production_version}.` : "No cycle has started yet: advance the loop from the Loop page."}` });
}

export async function render(el, { params }) {
  const pr = await get("/api/prompts").catch(() => ({ items: [], production: null }));
  const v = params.get("version") || pr.production || "v1";
  const pv = pr.items.find((p) => p.version === v);
  const [r, ov, fx, rb, au, cl] = await Promise.all([
    get(`/api/issues?version=${enc(v)}`).catch((err) => { if (err.status === 404) return null; throw err; }),
    get("/api/overview").catch(() => state.overview), get("/api/fixes").catch(() => ({ items: [] })),
    get("/api/rubrics").catch(() => ({ items: [] })), get("/api/audit?version=v1").catch(() => null), get("/api/calls?limit=5000").catch(() => ({ items: [] })),
  ]);
  const items = r?.items || [], cycles = ov?.cycles || [];  // cycles: newest first
  const versions = pr.items.map((p) => p.version);
  const tabs = html`<div class="seg" role="group" aria-label="Prompt version">${(versions.length ? versions : [v]).map((x) =>
    html`<a href="#/issues?version=${enc(x)}" aria-current="${x === v}">${x}${x === pr.production ? " · production" : ""}</a>`)}</div>`;
  const head = (title) => html`<div class="page-head"><div><div class="eyebrow">Improve · Prompt ${v}</div><h1 class="title">${title}</h1>
    <p class="lede">The judge's failed checks, grouped by what went wrong and ranked by what they cost. The loop fixes the top untried prompt or flow cause; speech, catalogue and platform causes need engineering work.</p></div>${tabs}</div>`;
  if (!items.length) return mount(el, html`${head("Root causes")}${noCauses(v, pv, cycles[0])}`);

  const rubric = Object.fromEntries(rb.items.map((x) => [x.id, x]));
  const ag = au?.agreement || null, agRow = Object.fromEntries((ag?.rows || []).map((x) => [x.rubric_id, x]));
  const call = Object.fromEntries((cl.items || []).map((c) => [c.call_id, c]));
  const fixOf = {};
  for (const f of fx.items) fixOf[f.root_cause_id] ??= f;  // newest first: the latest try at each cause
  const candOf = Object.fromEntries(pr.items.filter((p) => p.fix_id).map((p) => [p.fix_id, p.version]));
  const under = {}, nth = items.map((i) => (under[i.rubric_id] = (under[i.rubric_id] || 0) + 1));
  const byId = Object.fromEntries(items.map((i) => [i.id, i]));
  const ranked = [...cycles].reverse().find((c) => c.production_version === v);  // the first cycle on v ranked them
  const cur = cycles[0]?.production_version === v && cycles[0].root_cause_id && !cycles[0].outcome ? cycles[0] : null;
  const curK = cur ? items.findIndex((i) => i.id === cur.root_cause_id) : -1;
  const curFix = cur?.fix_id ? fx.items.find((f) => f.id === cur.fix_id) : null;
  const maxP = Math.max(1e-9, ...items.map((i) => i.priority));
  const count = {};
  items.forEach((i) => (count[i.type] = (count[i.type] || 0) + 1));
  const focus = params.get("rc");
  let type = !focus && count[params.get("type")] ? params.get("type") : "";
  const n0 = items[0].n_audited;
  const srcs = new Set(items.flatMap((i) => i.example_call_ids || []).map((id) => call[id]?.source).filter(Boolean));
  const src = srcs.size === 1 ? [...srcs][0] : null;  // which corpus was ranked: read from the calls, not assumed

  const trust = (rid) => (agRow[rid] ? agreeLine(agRow[rid]) : rubric[rid] && !rubric[rid].qa_column ? agreeLine(null) : "");
  const status = (i) => {
    const f = fixOf[i.id];
    if (f) {
      const st = fixSteps(f, cycles.find((c) => c.fix_id === f.id));
      const tone = st.some((s) => s.state === "fail") ? "fail" : st.some((s) => s.state === "now") ? "now" : "done";
      return html`<a class="ix-status ${tone}" href="#/fixes/${enc(f.id)}"><i aria-hidden="true"></i><span>Fix drafted${candOf[f.id] ? ` (${candOf[f.id]})` : ""}: ${fixPhrase(st)}</span><b>Open the fix →</b></a>`;
    }
    return FIXABLE.includes(i.type) ? html`<div class="ix-status"><i aria-hidden="true"></i><span>Not tried yet: the loop takes untried prompt and flow causes in this order</span></div>`
      : html`<div class="ix-status ix-eng"><i aria-hidden="true"></i><span>Needs ${WORK[i.type] || i.type} work: the loop does not edit the prompt for this</span></div>`;
  };
  const callRow = (i, id, j) => {  // example_call_ids are fatal first, so when the list is whole its index gives the grade
    const g = (i.example_call_ids.length === i.n_calls) ? (j < i.n_fatal ? "fatal" : "non_fatal") : null, c = call[id];
    return html`<a class="ix-call ${g ? "" : "nog"}" href="#/calls/${enc(id)}">${g ? grade(g) : ""}<span class="mono">${id}</span><span class="sum">${c?.summary || ""}</span></a>`;
  };
  const meta = (i, k, chip = true) => {
    const rub = rubric[i.rubric_id];
    return html`<div class="ix-meta">${chip ? typeChip(i.type) : ""}${rub ? html`<a href="#/rubrics/${enc(i.rubric_id)}">${rub.name}</a>` : html`<span>${i.rubric_id}</span>`}${under[i.rubric_id] > 1 ? html`<span class="muted">cause ${nth[k]} of ${under[i.rubric_id]} under this rubric</span>` : ""}</div>`;
  };
  const body = (i) => {
    const ids = i.example_call_ids || [];
    return html`<p class="ix-desc">${i.description}</p>
      <div class="ix-facts">${status(i)}${trust(i.rubric_id)}</div>
      ${ids.length ? html`<div class="ix-ex"><div class="eyebrow">${ids.length > 3 ? "Open these first" : ids.length === 1 ? "The call" : "The calls"}</div>${ids.slice(0, 3).map((id, j) => callRow(i, id, j))}
        ${ids.length > 3 ? html`<details class="ix-all" data-id="${i.id}"><summary>See the other ${int(ids.length - 3)} calls</summary><div class="ix-more"></div></details>` : ""}</div>` : ""}`;
  };
  const priBar = (i) => html`<span class="bar"><span style="flex-basis:${((100 * i.priority) / maxP).toFixed(1)}%;background:var(--accent)"></span></span>`;
  const card = ({ i, k }) => {
    const fb = i.n_calls ? (100 * i.n_fatal) / i.n_calls : 0;
    return html`<article class="cause" id="${i.id}">
      <div class="rank">${k + 1}</div>
      <div class="ix-head">${meta(i, k)}<h3>${i.name}</h3></div>
      <div class="ix-mets">
        <div><div class="lbl">Calls affected</div><div class="val"><b>${int(i.n_calls)}</b> of ${int(i.n_audited)} · ${pct(i.frequency)}</div><div class="bar"><span class="s-a" style="flex-basis:${(i.frequency * 100).toFixed(1)}%"></span></div></div>
        <div><div class="lbl">How bad</div><div class="val"><b>${int(i.n_fatal)}</b> fatal · <b>${int(i.n_non_fatal)}</b> non-fatal</div>
          <div class="bar" role="img" aria-label="${int(i.n_fatal)} fatal, ${int(i.n_non_fatal)} non-fatal">${fb > 0 ? html`<span class="s-fatal" style="flex-basis:${fb.toFixed(1)}%"></span>` : ""}${fb < 100 ? html`<span class="s-non_fatal" style="flex-basis:${(100 - fb).toFixed(1)}%"></span>` : ""}</div></div>
      </div>
      <div class="ix-pri" title="As costly as ${fix(i.priority, 1)} fatal calls in every 100 judged; a non-fatal counts as a third.">
        <div class="eyebrow">Priority</div><div class="big ${k === 0 ? "accent" : ""}">${fix(i.priority, 1)}</div><div class="cap">fatal-call equivalents per 100 calls</div>${priBar(i)}</div>
      <div class="ix-body">${body(i)}</div>
    </article>`;
  };
  const row = ({ i, k }) => {  // a cause past the top few: one line, opens to the same detail as a card
    const where = fixOf[i.id] ? "fix drafted" : FIXABLE.includes(i.type) ? "not tried yet" : `needs ${WORK[i.type] || i.type} work`;
    return html`<details class="ix-row" id="${i.id}"><summary>
      <span class="rank">${k + 1}</span>
      <span class="nm"><b>${i.name}</b><small>${typeChip(i.type)}<span>${rubric[i.rubric_id]?.name || i.rubric_id}</span><span>· ${where}</span></small></span>
      <span class="n"><b>${int(i.n_calls)}</b> of ${int(i.n_audited)} calls<small>${int(i.n_fatal)} fatal · ${int(i.n_non_fatal)} non-fatal</small></span>
      <span class="p" title="Priority: as costly as ${fix(i.priority, 1)} fatal calls in every 100 judged"><b>${fix(i.priority, 1)}</b>${priBar(i)}</span>
    </summary><div class="ix-row-body">${meta(i, k, false)}${body(i)}</div></details>`;
  };
  const list = () => {
    const shown = items.map((i, k) => ({ i, k })).filter(({ i }) => !type || i.type === type);
    const top = shown.slice(0, TOP), rest = shown.slice(TOP);
    return html`${top.map(card)}
      ${rest.length ? html`<section class="ix-rest" aria-label="More root causes"><div class="ix-rest-head"><h3>${int(rest.length)} more cause${rest.length === 1 ? "" : "s"}, ranked</h3><span>Open one to see what went wrong and its calls.</span></div>
        <div class="ix-cols" aria-hidden="true"><span>#</span><span>Cause</span><span>Calls affected</span><span>Priority</span></div>${rest.map(row)}</section>` : ""}`;
  };

  mount(el, html`${head(html`${int(items.length)} root causes, <em>ranked</em>`)}
    <div class="ix-context">
      <p>Ranked from the judge's grades of <b>${int(n0)} ${src === "production" ? "production calls" : src === "replay" ? "simulated replay calls" : "calls"}</b> on ${v}${src === "audited" ? ", the buyer-side calls IndiaMART's QA team audited" : ""}${ranked ? html`, in <a href="#/">cycle ${ranked.n}</a> (opened ${when(ranked.started_ts)})` : ""}.${src === "production" ? " IndiaMART's QA-audited calls stay out of the ranking: they are kept to check the judge." : ""}</p>
      ${ag ? html`<p class="warn"><span>The judge gives the same overall grade as IndiaMART's QA auditors on <b>${pct(ag.overall_accuracy, 0)}</b> of ${int(ag.n_calls)} ${ag.held_out ? "held-out " : ""}calls (κ ${fix(ag.overall_kappa)}, ${kappaWord(ag.overall_kappa)}${ag.calibrated ? `; calibrated on ${int(ag.n_tuning)} other calls` : ""}). Treat each cause as a lead to confirm by listening.</span> <a href="#/audit">Agreement by rubric</a></p>`
        : html`<p class="warn"><span>The judge has not been compared with IndiaMART's QA auditors yet, so nothing here is checked by a person.</span></p>`}
      ${cur && curK >= 0 ? html`<p class="now"><span>Now: cycle ${cur.n} is working on <a href="#/issues?version=${enc(v)}&rc=${enc(cur.root_cause_id)}">#${curK + 1}, ${items[curK].name}</a>${curFix ? ` (${fixPhrase(fixSteps(curFix, cur))})` : ` (${stageName(cur.stage)})`}.</span>${curFix ? html` <a href="#/fixes/${enc(curFix.id)}">Open the fix</a>` : ""}</p>` : ""}
    </div>
    <div class="ix-tools">
      ${Object.keys(count).length > 1 ? html`<div class="row" id="ix-type"><span class="muted" style="font-size:12.5px">Show</span>${seg([["", `All ${items.length}`], ...TYPES.filter(([t]) => count[t]).map(([t, l]) => [t, `${l} ${count[t]}`])], type)}</div>` : html`<span></span>`}
      <details class="ix-how"><summary>How ranking works</summary><div>
        <p>Priority = 100 × (3 × fatal + non-fatal) ÷ (3 × calls judged). A fatal failure counts three times a non-fatal one, and every cause is divided by all ${int(n0)} judged calls, so causes compare across rubrics: one that hurts many calls a little can outrank one that hurts a few calls badly.</p>
        <p>Priority ${fix(items[0].priority, 1)} means as costly as ${int(items[0].priority)} fatal calls in every 100.</p>${r.formula ? html`<code>${r.formula}</code>` : ""}</div></details>
    </div>
    <div id="ix-list">${list()}</div>`);

  const host = $("#ix-list", el);
  const more = (det) => {  // the next PAGE calls of one cause, after the three shown
    const i = byId[det.dataset.id], ids = i.example_call_ids, from = Number(det.dataset.n || 3), to = Math.min(ids.length, from + PAGE);
    $(".ix-more-btn", det)?.remove();
    $(".ix-more", det).insertAdjacentHTML("beforeend", String(html`${ids.slice(from, to).map((id, j) => callRow(i, id, from + j))}${to < ids.length
      ? html`<button class="btn sm ix-more-btn" type="button">Show ${int(Math.min(PAGE, ids.length - to))} more · ${int(ids.length - to)} left</button>` : ""}`));
    det.dataset.n = to;
    return $$(".ix-call", $(".ix-more", det))[from - 3];
  };
  host.addEventListener("toggle", (e) => { if (e.target.matches?.(".ix-all") && e.target.open && !e.target.dataset.n) more(e.target); }, true);
  host.addEventListener("click", (e) => { const b = e.target.closest(".ix-more-btn"); if (b) more(b.closest(".ix-all"))?.focus(); });
  $("#ix-type", el)?.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    type = b.dataset.v;
    $$("#ix-type button", el).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    mount(host, list());
    history.replaceState(history.state, "", `#/issues?version=${enc(v)}${type ? `&type=${enc(type)}` : ""}`);
  });
  const target = focus && el.querySelector(`[id="${CSS.escape(focus)}"]`);
  if (target) { target.open = true; target.classList.add("flash"); requestAnimationFrame(() => target.scrollIntoView({ block: "start" })); }
}
