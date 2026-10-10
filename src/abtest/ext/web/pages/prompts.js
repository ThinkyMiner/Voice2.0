// #/prompts: every prompt version as a card, parents to the left, each with where it stands in the
// loop; the reader opens a candidate on its edits and any version's full text with a find box.
import { get, text, state } from "../api.js";
import { $, $$, html, mount, int, pct, words, day, short, stateChip, empty, enc } from "../ui.js";
import { wordDiff, causesOf } from "./fixes.js";

const AB = { PROMOTED: "A/B promoted", PENDING_APPROVAL: "A/B waiting for approval", STOPPED_HARM: "A/B stopped for harm", BLOCKED_GUARDRAIL: "A/B blocked by the guardrail",
  INCONCLUSIVE: "A/B inconclusive", HALTED_SRM: "A/B halted, untrusted", REJECTED: "A/B rejected", RUNNING: "A/B running" };

export async function render(el, { params }) {
  const [pr, ov, fx, xs] = await Promise.all([get("/api/prompts"), get("/api/overview").catch(() => null),
    get("/api/fixes").catch(() => ({ items: [] })), get("/api/experiments").catch(() => ({ items: [] }))]);
  if (!pr.items.length) return mount(el, empty({ title: "No prompt versions", body: "The registry starts with the production prompt, v1.", cmd: "make loop", glyph: "doc" }));
  const by = Object.fromEntries(pr.items.map((p) => [p.version, p]));
  const fixBy = Object.fromEntries(fx.items.map((f) => [f.id, f])), rc = await causesOf(fx.items);
  const cycBy = Object.fromEntries((ov?.cycles || []).filter((c) => c.fix_id).map((c) => [c.fix_id, c]));
  const xBy = Object.fromEntries(xs.items.map((x) => [x.experiment_id, x]));
  const depth = (p, n = 0) => (p.parent && by[p.parent] && n < 64 ? depth(by[p.parent], n + 1) + 1 : 0);
  const gens = [];  // versions by generation, oldest first in each
  for (const p of [...pr.items].sort((a, b) => String(a.created_ts).localeCompare(String(b.created_ts)))) (gens[depth(p)] ||= []).push(p);
  const where = (p) => {
    if (!p.fix_id) return "The original prompt";
    const f = fixBy[p.fix_id], x = xBy[cycBy[p.fix_id]?.experiment_id];
    return [f?.gate ? (f.gate.passed ? "gate passed" : "gate blocked") : "gate not run",
      f?.replay ? `replay ${pct(f.replay.fail_rate_a, 0)} → ${pct(f.replay.fail_rate_b, 0)} failing, ${f.replay.n_cases} cases` : "replay not run",
      x ? AB[x.state] || `A/B ${x.state.toLowerCase()}` : "no A/B yet"].join(" · ");
  };
  let sel = by[params.get("v")] ? params.get("v") : pr.production || pr.items[0].version;
  let mode = by[sel].fix_id ? "changes" : "text";

  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Improve</div><h1 class="title">Prompt <em>versions</em></h1>
      <p class="lede">Every prompt Vani has run or tested; each new one is a single fix to its parent. Production is <b class="mono ink">${pr.production}</b>.</p></div></div>
    <div class="lineage-x" aria-label="Prompt versions, oldest generation first">${gens.map((g, i) => html`
      ${i ? html`<div class="gen-arrow" aria-hidden="true">→</div>` : ""}
      <div class="gen">${g.map((p) => html`<button class="pv ${p.status}" type="button" data-v="${p.version}" aria-pressed="${p.version === sel}">
        <span class="pv-top"><span class="v">${p.version}</span>${stateChip(p.status)}</span>
        <span class="m">${p.parent ? `from ${p.parent}` : "original"} · ${day(p.created_ts)}</span>
        ${p.fix_id ? html`<span class="pv-fix" title="${p.fix_id}">Fixes: ${rc[fixBy[p.fix_id]?.root_cause_id]?.name || p.fix_id}</span>` : ""}
        <span class="w">${where(p)}</span>
        <span class="go">Read ${p.version} →</span></button>`)}</div>`)}</div>

    <section class="section" id="reader-sec">
      <div class="section-head"><h2 id="rt"></h2><p id="rs"></p></div>
      <div class="reader-bar">
        <div id="modes"></div>
        <label class="search find" id="find-box"><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.6"/><path d="M10.5 10.5L14 14"/></svg>
          <input class="input" id="find" type="search" placeholder="Find in this prompt" aria-label="Find in this prompt" autocomplete="off" spellcheck="false"></label>
        <span class="find-n" id="find-n" aria-live="polite"></span>
      </div>
      <div id="reader-body"></div>
    </section>`);

  // --- find, with the CSS Custom Highlight API (no DOM rewrites of a 24,000-word text)
  const input = $("#find", el), out = $("#find-n", el);
  let hits = [], at = -1;
  const clear = () => { CSS.highlights?.delete("find"); CSS.highlights?.delete("find-now"); };
  const go = (k) => {
    const pre = $("#reader", el);
    if (!hits.length || !pre) { out.textContent = input.value.trim().length > 1 ? "no match" : ""; return; }
    at = (k + hits.length) % hits.length;
    if (window.Highlight) CSS.highlights.set("find-now", new Highlight(hits[at]));
    const r = hits[at].getBoundingClientRect(), b = pre.getBoundingClientRect();
    pre.scrollTop += r.top - b.top - pre.clientHeight / 3;
    out.textContent = `${at + 1} of ${hits.length}${hits.length >= 2000 ? "+" : ""}`;
  };
  const find = () => {
    clear();
    hits = []; at = -1;
    const pre = $("#reader", el), node = pre?.firstChild, q = input.value.trim().toLowerCase();
    if (q.length > 1 && node?.nodeType === 3) {
      const s = node.data.toLowerCase();
      for (let i = s.indexOf(q); i >= 0 && hits.length < 2000; i = s.indexOf(q, i + q.length)) {
        const r = new Range(); r.setStart(node, i); r.setEnd(node, i + q.length); hits.push(r);
      }
      if (window.Highlight && hits.length) CSS.highlights.set("find", new Highlight(...hits));
    }
    go(0);
  };
  input.addEventListener("input", find);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); go(at + (e.shiftKey ? -1 : 1)); } });

  const changes = (p) => {
    const f = fixBy[p.fix_id];
    if (!f) return html`<p class="muted">The fix behind ${p.version} (${p.fix_id}) is not on file; see Full text.</p>`;
    return html`<div class="edits">${f.edits.map((e, i) => html`<div class="edit">
        <div class="eyebrow">Edit ${i + 1} of ${f.edits.length} · ${e.mode === "append" ? "added at the end" : "replaces text"}</div>
        <p class="redline-line">${e.mode === "append" ? html`<ins>${e.text}</ins>` : wordDiff(e.find, e.replace)}</p>
        ${e.why ? html`<p class="why" title="${e.why}">${e.why}</p>` : ""}</div>`)}
      <p class="muted small">Nothing else changed from ${p.parent}. <a href="#/fixes/${enc(f.id)}">Open the fix, its gate and replay</a></p></div>`;
  };

  let ticket = 0;
  const read = async (v, scroll = false) => {
    if (!el.isConnected) return;  // the reader left this page while it loaded
    const mine = ++ticket, p = by[v];
    if (v !== sel) mode = p.fix_id ? "changes" : "text";
    sel = v;
    $$(".pv", el).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === v)));
    history.replaceState(null, "", `#/prompts?v=${enc(v)}`);
    mount($("#rt", el), html`Prompt <span class="fig">${v}</span> ${stateChip(p.status)}`);
    $("#rs", el).textContent = `sha ${short(p.sha256, 12)}`;
    $("#rs", el).title = p.path;
    mount($("#modes", el), p.fix_id ? html`<div class="seg" role="group" aria-label="Show">${[["changes", `What changed from ${p.parent}`], ["text", "Full text"]].map(([m, l]) => html`<button type="button" data-mode="${m}" aria-pressed="${m === mode}">${l}</button>`)}</div>` : "");
    $$("[data-mode]", el).forEach((b) => b.addEventListener("click", () => { mode = b.dataset.mode; read(sel); }));
    $("#find-box", el).hidden = mode !== "text";
    const body = $("#reader-body", el);
    clear(); out.textContent = "";
    if (mode === "changes") { mount(body, changes(p)); if (scroll) $("#reader-sec", el).scrollIntoView({ behavior: "smooth", block: "start" }); return; }
    mount(body, html`<pre class="reader" id="reader" tabindex="0">Loading…</pre>`);
    const box = $("#reader", el);
    try {
      const t = await text(`/api/prompts/${enc(v)}/text`);
      if (mine !== ticket) return;  // a newer selection owns the reader
      box.textContent = t.replace(/﻿/g, "");
      $("#rs", el).textContent = `${int(words(t))} words · sha ${short(p.sha256, 12)}`;
      if (input.value.trim()) find();
    } catch (err) {
      if (mine !== ticket) return;
      mount(box, html`<span class="muted">${err.status !== 404 ? err.message : state.demo ? `The text of ${v} is served by the platform.` : `${v}'s prompt file is not on this machine (${p.path}): ${err.message}.`}</span>`);
    }
    if (scroll) $("#reader-sec", el).scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $$(".pv", el).forEach((b) => b.addEventListener("click", () => read(b.dataset.v, true)));
  await read(sel);
  return clear;
}
