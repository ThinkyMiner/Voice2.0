// #/experiments and #/experiments/<id>: what B changed and what the engine decided, in plain words
// first; the numbers that decided it (lift, guardrail, split check) next; the z-path, the decision
// log and the human approval gate; the engine's own tables last, folded away.
import { get, send, state as app } from "../api.js";
import { $, html, raw, mount, int, pct, spct, fix, sint, when, cap, stateChip, empty, toast, enc, confirmDialog } from "../ui.js";
import { zChart } from "../viz.js";
import { wordDiff } from "./fixes.js";

const label = (m) => cap(String(m || "").replace(/_s$/, "").replaceAll("_", " ").replace(/\bbl\b/gi, "BL"));
const lc = (s) => (/^[A-Z][a-z]/.test(s) ? s[0].toLowerCase() + s.slice(1) : s);  // "Meeting fixed" -> "meeting fixed"; "BL success" stays
const assumed = (x) => String(x.assumed_effect || "").replace(/\s*\(assumed; simulated traffic\)\s*$/, "").replace(/ for B$/, "").replace(/^B assumed\s+/, "");
const pp = (v) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v * 100).toFixed(1)} pp`);
const scenario = (id) => /-v\d+-(.+)$/.exec(id)?.[1] || null;
const splitBar = (al) => html`<div class="splitbar"><div class="bar"><span class="s-a" style="flex-basis:${(al.A * 100).toFixed(1)}%"></span><span class="s-b" style="flex-basis:${(al.B * 100).toFixed(1)}%"></span></div>
  <div class="lbls"><span>A ${pct(al.A, 0)}</span><span>B ${pct(al.B, 0)}</span></div></div>`;
const dots = (done, n) => html`<span class="looks-dots" aria-hidden="true">${Array.from({ length: n }, (_, i) => html`<i class="${i < done ? (i === done - 1 ? "last" : "on") : ""}"></i>`)}</span>`;

/** What B changes, from the frozen config (variants.B.patch). */
function change(cfg, prompts) {
  const p = cfg?.variants?.B?.patch;
  if (!p) return { kind: "none", title: "B is the same prompt as A" };
  if (p.mode === "append") return { kind: "append", title: p.text, verb: "Adds to the prompt", b: p.text };
  if (p.mode === "replace_text") return { kind: "replace", title: `“${p.find}” becomes “${p.replace}”`, verb: "Changes one line", a: p.find, b: p.replace };
  const v = prompts.find((x) => x.path === p.path);
  return { kind: "full", title: v ? `Prompt ${v.version}${v.fix_id ? `, from fix ${v.fix_id}` : ""}` : `The prompt at ${p.path}`, verb: "Swaps in a new prompt version", v };
}

/** The guardrail at the last checkpoint: its change, its limit, and whether its interval crossed it. */
function guard(g, sec) {
  if (!g || !sec || !g.rci) return null;
  const abs = sec.margin_abs != null, up = sec.worse_if !== "decrease", lim = (up ? 1 : -1) * Math.abs(g.margin);
  const [lo, hi] = g.rci;
  return { name: label(sec.name), f: abs ? pp : (v) => spct(v), delta: g.delta, lo, hi, lim,
    crossed: up ? lo > lim : hi < lim, cleared: up ? hi < lim : lo > lim };
}

/** The plain story of one run, from its decision log. */
function story(x) {
  const cfg = x.config || {}, looks = x.looks || [], L = looks.at(-1);
  const D = (x.decisions || []).filter((d) => d.kind === "look").at(-1);
  const human = (x.decisions || []).find((d) => d.actor && d.actor !== "engine" && d.to_state);
  const primaryHarm = x.state === "STOPPED_HARM" && L != null && L.z <= -Math.abs(L.harm_bound);
  return { L, prim: label(cfg.primary?.name), g: guard(D?.guardrail, cfg.secondary), srm: D?.srm, human, primaryHarm,
    tone: ["PROMOTED", "PENDING_APPROVAL"].includes(x.state) ? "win" : primaryHarm ? "harm" : "muted" };
}

function outcome(x, s) {
  const g = s.g, lim = g ? ` (limit ${g.f(g.lim)})` : "";
  switch (x.state) {
    case "PROMOTED": return s.human ? `B won; promoted by ${s.human.actor}` : "B won; promoted automatically";
    case "PENDING_APPROVAL": return "B won; waiting for a human to approve";
    case "REJECTED": return `B won; ${s.human?.actor || "a human"} rejected it`;
    case "STOPPED_HARM": return s.primaryHarm ? `B lowered ${lc(s.prim)}; stopped` : g ? `Stopped: ${lc(g.name)} ${g.f(g.delta)}${lim}` : "Stopped for harm";
    case "BLOCKED_GUARDRAIL": return g ? `Won, but ${lc(g.name)} ${g.f(g.delta)}${lim}` : "Won, but the guardrail blocked it";
    case "INCONCLUSIVE": return "No clear effect by the last checkpoint";
    case "HALTED_SRM": return s.srm ? `Untrusted: B got ${pct(s.srm.observed_share_B)} of leads, not ${pct(s.srm.configured_share_B, 0)}` : "Untrusted: the split failed its check";
    case "RUNNING": return `Running: checkpoint ${x.looks_done ?? x.looks?.length ?? 0} of ${x.n_looks ?? x.config?.window?.looks ?? "?"}`;
    default: return cap(String(x.state || "").toLowerCase().replaceAll("_", " "));
  }
}

function verdict(x, s, dayOf) {
  const L = s.L, g = s.g;
  if (!L) return "No checkpoint yet: the first one comes once enough B leads have matured.";
  const d = dayOf(L.ts_sim), at = `checkpoint ${L.look}${d != null ? ` (simulated day ${d})` : ""}`;
  const ci = L.ci95 ? ` (95% CI ${spct(L.ci95[0])} to ${spct(L.ci95[1])})` : "";
  const moved = `${L.lift_rel >= 0 ? "raised" : "lowered"} ${lc(s.prim)} by ${pct(Math.abs(L.lift_rel))}${ci}`;
  switch (x.state) {
    case "PROMOTED": return `B ${moved} on simulated traffic. ${s.human ? `Promoted by ${s.human.actor}` : "Promoted automatically"} at ${at}.`;
    case "PENDING_APPROVAL": return `B ${moved} on simulated traffic and crossed the win line at ${at}. It waits for a human to approve or reject it.`;
    case "REJECTED": return `B ${moved} on simulated traffic, but ${s.human?.actor || "a human"} rejected it; all traffic is back on A.`;
    case "STOPPED_HARM": return s.primaryHarm ? `B ${moved}, past the harm line, so the engine stopped it at ${at}. All traffic is back on A.`
      : g ? `Stopped at ${at}: B raised ${lc(g.name)} by about ${g.f(g.delta)} against a limit of ${g.f(g.lim)}, despite ${spct(L.lift_rel)} ${lc(s.prim)}.`
        : `Stopped for harm at ${at}. All traffic is back on A.`;
    case "BLOCKED_GUARDRAIL": return `B ${moved}, but at the last checkpoint ${g ? `${lc(g.name)} was ${g.f(g.delta)}, not clearly inside its ${g.f(g.lim)} limit` : "the guardrail had not cleared"}, so B is blocked and A stays.`;
    case "INCONCLUSIVE": return `No clear effect after all ${x.looks.length} checkpoints: lift ${spct(L.lift_rel)}${ci}. A stays.`;
    case "HALTED_SRM": return `Halted at ${at}: ${s.srm ? `B got ${pct(s.srm.observed_share_B)} of the leads where the plan said ${pct(s.srm.configured_share_B, 0)}` : "the traffic split did not match the plan"}, so the results are untrusted.`;
    case "RUNNING": return `Running: ${x.looks.length} of ${x.config?.window?.looks ?? "?"} checkpoints so far, no line crossed yet. Lift ${spct(L.lift_rel)}${ci}.`;
    default: return outcome(x, s);
  }
}

// ----------------------------------------------------------------------------------- the list
export async function renderList(el) {
  const [r, lp, pr] = await Promise.all([get("/api/experiments"), get("/api/loop").catch(() => null), get("/api/prompts").catch(() => ({ items: [] }))]);
  // ponytail: one detail request per run, for its change and deciding numbers; add them to the list route if runs reach the hundreds
  const det = await Promise.all(r.items.map((x) => get(`/api/experiments/${enc(x.experiment_id)}`).catch(() => null)));
  const rows = r.items.map((x, i) => ({ x, d: det[i], s: det[i] ? story(det[i]) : null }));
  const fromLoop = rows.filter((o) => o.x.cycle != null), engine = rows.filter((o) => o.x.cycle == null);
  const groups = [];
  for (const o of engine) {
    const ch = change(o.d?.config, pr.items), key = `${ch.title}|${o.d?.config?.primary?.name}`;
    let grp = groups.find((g) => g.key === key);
    if (!grp) groups.push(grp = { key, ch, cfg: o.d?.config, rows: [] });
    grp.rows.push(o);
  }
  const c = lp?.cycle;
  const row = (o, name) => {
    const { x, s } = o, lift = x.lift_rel;
    return html`<a class="xrow" href="#/experiments/${enc(x.experiment_id)}">
      <div class="c-what"><div class="nm">${name}</div><div class="sub">Assumed for B: ${assumed(x) || "—"}</div></div>
      <div class="c-out">${stateChip(x.state)}<div class="sub">${s ? outcome(x, s) : ""}</div></div>
      <div class="c-looks">${dots(x.looks_done, x.n_looks)}<span class="sub">${x.looks_done} of ${x.n_looks}</span></div>
      <div class="c-lift"><div class="v ${s?.tone || "muted"}">${lift == null ? "—" : spct(lift)}</div><div class="sub">${s?.prim || "primary"}${x.state === "HALTED_SRM" ? ", untrusted" : ""}</div></div>
    </a>`;
  };
  const head = html`<div class="xrow xhead-row" aria-hidden="true"><span>Run</span><span>Outcome</span><span>Checkpoints</span><span>Lift</span></div>`;
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Prove</div><h1 class="title">A/B <em>experiments</em></h1>
      <p class="lede">Prompt B gets 10% of leads while a sequential test checks the evidence at each checkpoint: promote, stop for harm, block on the guardrail, or call it inconclusive.
        The traffic is simulated: the engine replays real call history with an effect assumed for B, which it never sees.</p></div></div>

    <section class="section" style="margin-top:0">
      <div class="section-head"><h2>From the <em>loop</em></h2><p>A cycle's Prompt B against the production prompt.</p></div>
      ${fromLoop.length ? html`<div class="card xlist">${head}${fromLoop.map((o) => row(o, html`Cycle ${o.x.cycle}: ${change(o.d?.config, pr.items).title}`))}</div>`
        : empty({ compact: true, glyph: "flask", title: "No cycle has reached its A/B test yet",
          body: c ? `Cycle ${c.n} is at ${cap(c.stage)}. A test starts once a fix passes the safety gate and the replay.` : "The loop has not started. A test starts once a cycle's fix passes the safety gate and the replay.",
          action: html`<a class="btn sm" href="#/">Open the Loop page</a>` })}
    </section>

    <section class="section">
      <div class="section-head"><h2>Engine <em>scenarios</em></h2><p>Fixed test runs that show how the engine decides. None of them changed production.</p></div>
      ${groups.length ? groups.map((g) => html`<div class="card xlist">
        <div class="xgroup"><div class="eyebrow">${g.ch.verb || "The change"} · primary metric ${label(g.cfg?.primary?.name)} · guardrail ${label(g.cfg?.secondary?.name)}</div>
          <h3>${g.ch.kind === "append" ? html`“${g.ch.title}”` : g.ch.title}</h3></div>
        ${head}${g.rows.map((o) => row(o, html`<span class="mono">${scenario(o.x.experiment_id) || o.x.experiment_id}</span>`))}</div>`)
        : empty({ compact: true, glyph: "flask", title: "No engine scenarios", body: "make demo runs the three required scenarios.", cmd: "make demo" })}
    </section>`);
}

// ------------------------------------------------------------------------------ one experiment
/** The decision log in order; each run of checkpoints that only said "continue" folds into one line. */
function logRows(decisions, dayOf) {
  const rows = decisions.filter((d, i, all) => !(d.kind === "look" && all[i + 1]?.kind === "state_change" && all[i + 1].look === d.look && all[i + 1].reason === d.reason));
  const row = (d) => html`<div class="dl"><div class="ts">${d.ts_sim ? `day ${dayOf(d.ts_sim)}` : ""}</div>
    <div>${d.kind === "look" ? html`<span class="chip">checkpoint ${d.look}</span>` : stateChip(d.to_state || d.decision)}</div>
    <div><div class="why">${d.reason}</div><div class="who">${d.actor === "engine" ? "ENGINE" : `HUMAN · ${d.actor}`}${d.from_state ? ` · ${d.from_state} → ${d.to_state}` : ""}</div></div></div>`;
  const out = [];
  let quiet = [];
  const flush = () => {
    if (quiet.length) out.push(html`<details class="dl-quiet"><summary>${quiet.length === 1 ? "1 checkpoint" : `${quiet.length} checkpoints`} that changed nothing (${quiet.map((d) => `day ${dayOf(d.ts_sim)}`).join(", ")})</summary>${quiet.map(row)}</details>`);
    quiet = [];
  };
  for (const d of rows) {
    if (d.kind === "look" && d.decision === "CONTINUE") { quiet.push(d); continue; }
    flush();
    out.push(row(d));
  }
  flush();
  return out;
}

/** The guardrail's part in this run, in words, with the tone of its number and of its interval. */
function guardRole(g, x, s) {
  if (x.state === "HALTED_SRM") return { note: "untrusted: the split failed its check", tone: "muted", iv: "" };
  if (g.crossed) return { note: x.state === "STOPPED_HARM" && !s.primaryHarm ? "crossed: this stopped B" : "crossed", tone: "harm", iv: "harm" };
  if (g.cleared) return { note: "cleared", tone: "muted", iv: "ok" };
  if (x.state === "BLOCKED_GUARDRAIL") return { note: "not cleared at the end: this blocked B", tone: "harm", iv: "harm" };
  if (["RUNNING", "DRAFT"].includes(x.state)) return { note: "not cleared yet", tone: "muted", iv: "open" };
  return { note: "not cleared; it did not decide this run", tone: "muted", iv: "" };
}

function rciBar(g, iv) {
  const lo = Math.min(g.lo, 0, g.lim), hi = Math.max(g.hi, 0, g.lim), pad = (hi - lo) * 0.12 || 0.01;
  const a = lo - pad, b = hi + pad, X = (v) => (((v - a) / (b - a)) * 100).toFixed(1);
  return html`<div class="rci" role="img" aria-label="Interval ${g.f(g.lo)} to ${g.f(g.hi)} against a limit of ${g.f(g.lim)}">
    <span class="zero" style="left:${X(0)}%"></span><span class="lim" style="left:${X(g.lim)}%"><b>limit</b></span>
    <span class="iv ${iv}" style="left:${X(g.lo)}%;width:${(X(g.hi) - X(g.lo)).toFixed(1)}%"></span></div>`;
}

export async function renderExperiment(el, { id }) {
  let x;
  try { x = await get(`/api/experiments/${enc(id)}`); } catch (err) {
    if (err.status !== 404) throw err;
    return mount(el, empty({ title: `No experiment called ${id}`, glyph: "flask", body: "It is not in the experiment store: the address may be mistyped, or the run was removed.",
      action: html`<a class="btn" href="#/experiments">All experiments</a>` }));
  }
  const pr = await get("/api/prompts").catch(() => ({ items: [] }));
  const cfg = x.config || {}, looks = x.looks || [], s = story(x), L = s.L, g = s.g;
  const started = x.decisions?.find((d) => d.ts_sim)?.ts_sim || looks[0]?.ts_sim;
  const dayOf = (ts) => (started && ts ? Math.round((new Date(ts) - new Date(started)) / 864e5) : null);
  const pending = x.state === "PENDING_APPROVAL", untrusted = x.state === "HALTED_SRM";
  const ch = change(cfg, pr.items), baseV = pr.items.find((p) => p.path === cfg.variants?.A?.base);
  const sec = cfg.secondary || {};
  let actor = "";
  try { actor = localStorage.getItem("vani-actor") || ""; } catch { /* none */ }
  const impact = untrusted ? null : x.impact?.per_week;
  const iTone = impact == null ? "" : s.tone === "win" && impact > 0 ? "win" : s.primaryHarm ? "harm" : "muted";

  mount(el, html`
    <div class="page-head xhead"><div>
      <div class="eyebrow">${x.cycle != null ? `Loop experiment · cycle ${x.cycle}` : `Engine scenario${scenario(x.experiment_id) ? ` · ${scenario(x.experiment_id)}` : ""}`}${x.decisions?.[0]?.ts_wall ? ` · run ${when(x.decisions[0].ts_wall)}` : ""}</div>
      <h1 class="title xtitle">${ch.kind === "append" ? html`“${ch.title}”` : ch.title}</h1>
      <div class="row xchips">${stateChip(x.state)}${s.human ? html`<span class="chip">${s.human.to_state === "PROMOTED" ? "Approved" : "Rejected"} by ${s.human.actor}${dayOf(s.human.ts_sim) != null ? ` · day ${dayOf(s.human.ts_sim)}` : ""}</span>` : ""}
        <span class="mono muted xid">${x.experiment_id}</span></div>
      <p class="xverdict">${verdict(x, s, dayOf)}</p>
      <p class="xsim"><i class="dot demo" aria-hidden="true"></i><span>Simulated traffic · assumed for B: ${assumed(x) || "—"}${x.cycle == null ? ". Engine scenario: B is a test patch, not a prompt version, and production is unchanged whatever the result." : ""}</span></p>
    </div></div>

    ${pending ? html`<div class="decide-panel" id="decide">
      <div><div class="eyebrow" style="color:var(--accent-2-ink)">Waiting for a human</div>
        <div class="decide-q">B crossed the win line. <em>Promote it to all traffic?</em></div>
        <p class="muted" style="font-size:12.5px;margin-top:6px">Your name goes on the decision log with the choice.${x.cycle == null ? " This is an engine scenario: production does not change." : ""}</p></div>
      <div class="row"><label class="field"><span>Your name</span><input class="input" id="actor" value="${actor}" placeholder="e.g. kartik" autocomplete="name"></label>
        <button class="btn" id="reject" type="button">Reject</button><button class="btn accent" id="approve" type="button">Approve B</button></div>
    </div>` : ""}

    <section class="card change">
      <div class="card-head"><h3>The change</h3><span class="eyebrow">A is ${baseV ? `prompt ${baseV.version}` : cfg.variants?.A?.base || "the base prompt"}</span></div>
      ${ch.kind === "replace" ? html`<p class="redline-line">${wordDiff(ch.a, ch.b)}</p><p class="muted small">B is A with this one line changed.</p>`
        : ch.kind === "append" ? html`<p class="redline-line"><ins>${ch.b}</ins></p><p class="muted small">B is A with this sentence added at the end.</p>`
        : ch.kind === "full" ? html`<p>B is ${ch.v ? html`<a href="#/prompts?v=${enc(ch.v.version)}">prompt ${ch.v.version}</a>` : "a full prompt"}.${ch.v?.fix_id ? html` <a href="#/fixes/${enc(ch.v.fix_id)}">Read its fix as a redline</a>.` : ""}</p>`
        : html`<p class="muted">${ch.title}.</p>`}
    </section>

    <section class="kpis xkpis" aria-label="What decided it">
      <div class="kpi"><div class="eyebrow">Lift in ${s.prim}</div><div class="big ${untrusted ? "muted" : s.tone}">${spct(L?.lift_rel)}</div>
        <div class="sub">${untrusted ? "untrusted: the split failed its check" : L?.ci95 ? `95% CI ${spct(L.ci95[0])} to ${spct(L.ci95[1])}` : "no checkpoint yet"}</div></div>
      <div class="kpi"><div class="eyebrow">Guardrail · ${g?.name || label(sec.name)}</div>
        ${g ? html`<div class="big ${guardRole(g, x, s).tone}">${g.f(g.delta)}</div>
          <div class="sub">limit ${g.f(g.lim)} · ${guardRole(g, x, s).note}</div>${rciBar(g, guardRole(g, x, s).iv)}`
          : html`<div class="big muted">—</div><div class="sub">no checkpoint yet</div>`}</div>
      <div class="kpi"><div class="eyebrow" title="The engine checks the evidence at planned checkpoints; it can stop early at any of them.">Checkpoints</div><div class="big">${looks.length}<small>of ${cfg.window?.looks ?? "—"}</small></div>
        <div class="sub">${L ? `simulated day ${dayOf(L.ts_sim)}; planned over ${cfg.window?.days ?? "—"} days` : "none yet"}</div></div>
      <div class="kpi"><div class="eyebrow">Leads counted</div><div class="big">${int((L?.n_a || 0) + (L?.n_b || 0))}</div><div class="sub">A ${int(L?.n_a)} · B ${int(L?.n_b)} of ${int(x.n_b_max)} planned for B</div></div>
      <div class="kpi"><div class="eyebrow">Impact a week</div><div class="big ${iTone}">${impact == null ? "—" : sint(impact)}</div>
        <div class="sub">${untrusted ? "no estimate: the split failed its check" : impact == null ? "after the first checkpoint" : `${x.impact.unit} if B took all traffic${s.tone === "win" ? "" : "; not taken"}`}</div></div>
      <div class="kpi"><div class="eyebrow">Split at the end</div><div style="margin-top:12px">${splitBar(x.allocation || cfg.allocation || { A: 1, B: 0 })}</div>
        <div class="sub">${pending ? "B keeps its share until someone decides" : x.state === "PROMOTED" ? "B serves all simulated traffic" : x.state === "RUNNING" ? "the planned split" : "all simulated traffic back on A"}</div></div>
    </section>

    <section class="section">
      <div class="card">
        <div class="card-head"><div><div class="eyebrow">${looks.length} checkpoint${looks.length === 1 ? "" : "s"}</div><h3 class="serif" style="margin-top:6px">The evidence for B, <em>against its lines</em></h3></div>
          <div class="legend"><span><i style="background:var(--ink);height:2px;width:14px"></i>Evidence for B (z)</span><span><i style="background:var(--win)"></i>Win line</span><span><i style="background:var(--harm)"></i>Harm line</span></div></div>
        <div id="z" style="min-height:300px"></div>
        <p class="muted" style="font-size:12px;margin-top:6px">Each dot is a checkpoint. Above zero, B is ahead on ${lc(s.prim)}. Above the green line the engine promotes B (if the guardrail clears); below the red line it stops B. The lines start strict and loosen as leads come in.</p>
      </div>
    </section>

    <section class="section">
      <div class="section-head"><h2>Decision <em>log</em></h2><p>Append-only: the engine writes a reason at every checkpoint and state change; a human decision carries the name.</p></div>
      <div class="card" style="padding:4px 22px"><div class="dlog">${logRows(x.decisions || [], dayOf)}</div></div>
    </section>

    <details class="section engine-details">
      <summary><span class="eyebrow">Engine details</span><span class="muted">checkpoint table, frozen configuration, terms</span></summary>
      ${looks.length ? html`<div class="card flush" style="margin-top:14px"><div class="table-wrap"><table class="table">
        <thead><tr><th title="Checkpoint">#</th><th>Day</th><th class="r hide-sm">Share counted</th><th class="r hide-sm">n A</th><th class="r hide-sm">n B</th><th class="r">Lift</th><th class="hide-sm">95% CI</th><th class="r">z</th><th class="r hide-sm">Win line</th><th class="r hide-sm">Harm line</th><th>Decision</th></tr></thead>
        <tbody>${looks.map((l) => html`<tr><td class="mono">${l.look}</td><td class="mono">${dayOf(l.ts_sim) ?? "—"}</td><td class="r mono hide-sm">${pct(l.info_fraction, 0)}</td>
          <td class="r mono hide-sm">${int(l.n_a)}</td><td class="r mono hide-sm">${int(l.n_b)}</td><td class="r mono">${spct(l.lift_rel)}</td>
          <td class="mono muted hide-sm" style="white-space:nowrap">${spct(l.ci95?.[0])}, ${spct(l.ci95?.[1])}</td><td class="r mono"><b>${fix(l.z)}</b></td>
          <td class="r mono hide-sm" style="color:var(--win)">${fix(l.win_bound)}</td><td class="r mono hide-sm" style="color:var(--harm)">${fix(-Math.abs(l.harm_bound))}</td>
          <td>${l.decision === "CONTINUE" ? html`<span class="chip">continue</span>` : stateChip(l.decision)}</td></tr>`)}</tbody></table></div></div>` : ""}
      <dl class="cfg" style="margin-top:18px">
        ${[["Dataset", cfg.dataset], ["Unit · trigger", `${cfg.unit} · ${cfg.trigger}`], ["Split", cfg.allocation ? `${pct(cfg.allocation.A, 0)} / ${pct(cfg.allocation.B, 0)}` : "—"], ["Promotion", cfg.promotion === "approval" ? "a human approves" : cfg.promotion],
          ["Primary", `${s.prim} (${(cfg.primary?.dispositions || []).join(", ")}), ${cfg.primary?.better}`], ["Smallest lift sized for", cfg.primary?.mde_rel != null ? pct(cfg.primary.mde_rel, 0) : "—"],
          ["Guardrail", `${label(sec.name)}, worse if ${sec.worse_if}, limit ${sec.margin_rel != null ? pct(sec.margin_rel, 0) : sec.margin_abs != null ? pp(sec.margin_abs) : "—"}`],
          ["Alpha win · harm", `${cfg.alpha_win} · ${cfg.alpha_harm}`], ["Power", cfg.power], ["Window", cfg.window ? `${cfg.window.days} days, ${cfg.window.looks} checkpoints` : "—"],
          ["Maturation", cfg.maturation_days != null ? `${cfg.maturation_days} days` : "—"], ["Split check threshold", cfg.srm_p_threshold], ["Baseline rate", cfg.baseline_p], ["Planned B leads", int(x.n_b_max)],
          ["Salt", cfg.salt], ["Config hash", x.decisions?.[0]?.config_hash ? String(x.decisions[0].config_hash).slice(0, 12) : "—"]].map(([k, v]) => html`<div><dt>${k}</dt><dd>${v ?? "—"}</dd></div>`)}
      </dl>
      <p class="muted small" style="margin-top:14px"><b>z</b>: how many standard errors B is ahead of A. <b>Win line</b> (O'Brien-Fleming) and <b>harm line</b> (Pocock): the boundaries z must cross. <b>RCI</b>: the guardrail's confidence interval, valid at every checkpoint. <b>SRM</b>: sample-ratio mismatch, the check that B got its planned share of leads.</p>
    </details>`);

  const z = $("#z", el);
  const draw = () => {
    const svg = zChart(looks, { width: Math.max(300, z.clientWidth), state: x.state, started });
    mount(z, svg ? raw(svg) : empty({ title: "No checkpoint to plot yet", body: "The line starts at the first checkpoint, once enough B leads have matured.", compact: true, glyph: "flask" }));
  };
  draw();
  let w = z.clientWidth;
  const ro = new ResizeObserver(() => { if (Math.abs(z.clientWidth - w) > 8) { w = z.clientWidth; draw(); z.querySelector(".zpath")?.classList.remove("draw"); } });
  ro.observe(z);

  if (pending) {
    const decide = async (approved) => {
      const name = $("#actor", el).value.trim();
      if (!name) { $("#actor", el).focus(); return toast("Add your name: approvals are recorded with the approver.", "harm"); }
      const ok = await confirmDialog({
        title: approved ? "Promote B to all traffic?" : "Reject B?", ok: approved ? "Approve B" : "Reject B", tone: approved ? "accent" : "danger",
        body: html`<p>${approved ? "B gets 100% of the simulated traffic." : "All simulated traffic returns to A."} This is recorded on the decision log as <b>${name}</b>.</p>
          ${x.cycle != null ? html`<p>${approved ? "The loop's next step rolls Prompt B out as production." : "Prompt B is rejected and production stays as it is."}</p>` : html`<p class="muted">Engine scenario: this changes only this simulated run. Production is unchanged.</p>`}`,
      });
      if (!ok) return;
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
