// #/issues: root causes ranked by priority, with the formula, frequency, severity, type and examples.
import { get } from "../api.js";
import { html, mount, int, pct, fix, typeChip, callLink, empty, enc } from "../ui.js";

export async function render(el, { params }) {
  const pr = await get("/api/prompts").catch(() => ({ items: [], production: null }));
  const versions = pr.items.map((p) => p.version);
  const v = params.get("version") || pr.production || "v1";
  const chips = html`<div class="seg" role="group" aria-label="Prompt version">${(versions.length ? versions : [v]).map((x) =>
    html`<a href="#/issues?version=${enc(x)}" aria-current="${x === v}">${x}${x === pr.production ? " · prod" : ""}</a>`)}</div>`;
  const head = (n) => html`<div class="page-head"><div><div class="eyebrow">Improve · Prompt ${v}</div>
    <h1 class="title">${n ? html`${int(n)} root causes, <em>ranked</em>` : "Root causes"}</h1>
    <p class="lede">Failed verdicts grouped by what went wrong. The top one is what the next fix targets; prompt causes are fixable by editing the prompt, the others need speech, catalogue or flow work.</p></div>${chips}</div>`;
  let r;
  try { r = await get(`/api/issues?version=${enc(v)}`); } catch (err) {
    if (err.status !== 404) throw err;
    return mount(el, html`${head(0)}${empty({ title: `No issues for ${v} yet`, body: "Root causes come from the judge's failed verdicts. Judge the calls first.", cmd: "make loop" })}`);
  }
  const maxP = Math.max(1, ...r.items.map((i) => i.priority));
  mount(el, html`${head(r.items.length)}
    <div class="formula"><span class="eyebrow">Priority</span><code>${r.formula}</code>
      <p>Fatal counts three times a non-fatal. Divided by every audited call, so a cause hurting many calls a little can outrank one hurting a few calls badly.</p></div>
    ${r.items.length ? html`<div style="margin-top:12px">${r.items.map((i, k) => html`
      <article class="issue">
        <div class="rank">${k + 1}</div>
        <div><div class="row" style="margin-bottom:8px">${typeChip(i.type)}<span class="eyebrow">${i.rubric_id}</span></div>
          <h3>${i.name}</h3><p class="desc">${i.description}</p>
          ${i.example_call_ids?.length ? html`<div class="ex"><span class="eyebrow" style="margin-right:4px">Examples</span>${i.example_call_ids.map(callLink)}</div>` : ""}</div>
        <div class="mets">
          <div><div class="row between"><span>Frequency</span><span class="mono ink">${pct(i.frequency)}</span></div><div class="bar"><span class="s-a" style="flex-basis:${(i.frequency * 100).toFixed(1)}%"></span></div>
            <div style="margin-top:4px">${int(i.n_calls)} of ${int(i.n_audited)} calls</div></div>
          <div><div class="row between"><span>Severity</span><span class="mono ink">${pct(i.severity)}</span></div><div class="bar"><span class="s-fatal" style="flex-basis:${(i.severity * 100).toFixed(1)}%"></span></div>
            <div style="margin-top:4px">${int(i.n_fatal)} fatal · ${int(i.n_non_fatal)} non-fatal</div></div>
        </div>
        <div class="pri"><div class="eyebrow">Priority</div><div class="big ${k === 0 ? "accent" : ""}" style="margin-top:6px">${fix(i.priority)}</div>
          <div class="bar" style="margin-top:10px"><span class="s-a" style="flex-basis:${(100 * i.priority / maxP).toFixed(1)}%;background:var(--accent)"></span></div></div>
      </article>`)}</div>` : empty({ title: `No root causes for ${v} yet`, body: "They come from the judge's failed verdicts: once calls are judged, the loop groups the failures and ranks them here.", cmd: "make loop", compact: true })}`);
}
