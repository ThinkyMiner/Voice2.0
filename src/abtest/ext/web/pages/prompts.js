// #/prompts: the version lineage v1 -> v2 -> ..., each with its status, the fix and experiment behind
// it, and the text itself.
import { get, text } from "../api.js";
import { $, $$, html, raw, mount, int, words, when, short, stateChip, empty, enc, pad2 } from "../ui.js";
import { lineage } from "../viz.js";

export async function render(el, { params }) {
  const [pr, ov] = await Promise.all([get("/api/prompts"), get("/api/overview").catch(() => null)]);
  if (!pr.items.length) return mount(el, empty({ title: "No prompt versions", body: "The registry starts with the production prompt, v1.", cmd: "make loop", glyph: "doc" }));
  const byFix = Object.fromEntries((ov?.cycles || []).filter((c) => c.fix_id).map((c) => [c.fix_id, c]));
  let sel = params.get("v") || pr.production || pr.items[0].version;
  const ordered = [...pr.items].sort((a, b) => String(b.created_ts).localeCompare(String(a.created_ts)));

  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Improve</div><h1 class="title">Prompt <em>lineage</em></h1>
      <p class="lede">Every version Vani has run or tested. A version is born from one fix to its parent, proven in one experiment, and either becomes production or is retired.</p></div></div>
    <div class="card lineage" id="tree"></div>
    <section class="section">
      <div class="section-head"><h2>Versions</h2><p>Production is <b class="mono ink">${pr.production}</b>.</p></div>
      <div class="card flush"><div class="table-wrap"><table class="table">
        <thead><tr><th>Version</th><th>Status</th><th>Parent</th><th>Fix</th><th>Experiment</th><th>sha256</th><th>Created</th><th></th></tr></thead>
        <tbody>${ordered.map((p) => {
          const c = p.fix_id && byFix[p.fix_id];
          return html`<tr><td style="font:400 22px var(--f-display)">${p.version}</td><td>${stateChip(p.status)}</td><td class="mono">${p.parent || "—"}</td>
            <td>${p.fix_id ? html`<a class="chip id" href="#/fixes/${enc(p.fix_id)}">${p.fix_id}</a>` : html`<span class="muted">original</span>`}</td>
            <td>${c?.experiment_id ? html`<a class="chip id" href="#/experiments/${enc(c.experiment_id)}">${c.experiment_id}</a>${c ? html`<div class="muted mono" style="font-size:10.5px;margin-top:4px">cycle ${pad2(c.n)}</div>` : ""}` : html`<span class="muted">—</span>`}</td>
            <td class="mono muted" style="font-size:11.5px">${short(p.sha256, 12)}</td><td class="muted" style="white-space:nowrap">${when(p.created_ts)}</td>
            <td><button class="btn sm" type="button" data-read="${p.version}">Read</button></td></tr>`;
        })}</tbody></table></div></div>
    </section>
    <section class="section" id="reader-sec">
      <div class="section-head"><h2 id="rt"></h2><p id="rs"></p></div>
      <pre class="reader" id="reader" tabindex="0"></pre>
    </section>`);

  const tree = $("#tree", el);
  const drawTree = () => {
    mount(tree, raw(lineage(pr.items, sel)));
    $$(".node", tree).forEach((n) => {
      const go = () => read(n.dataset.v, true);
      n.addEventListener("click", go);
      n.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    });
  };
  let ticket = 0;
  const read = async (v, scroll = false) => {
    if (!el.isConnected) return;  // the reader left this page while it loaded
    const mine = ++ticket;
    sel = v;
    drawTree();
    history.replaceState(null, "", `#/prompts?v=${enc(v)}`);
    const p = pr.items.find((x) => x.version === v);
    mount($("#rt", el), html`Prompt <em>${v}</em>`);
    $("#rs", el).textContent = `${p?.path || ""}`;
    const box = $("#reader", el);
    box.textContent = "Loading…";
    try {
      const t = await text(`/api/prompts/${enc(v)}/text`);
      if (mine !== ticket) return;  // a newer selection owns the reader
      box.textContent = t.replace(/\uFEFF/g, "");
      $("#rs", el).textContent = `${p?.path || ""} · ${int(words(t))} words · ${int(t.split("\n").length)} lines`;
    } catch (err) {
      if (mine !== ticket) return;
      mount(box, html`<span class="muted">${err.status === 404 ? `The text of ${v} is served by the platform.` : err.message}</span>`);
    }
    if (scroll) $("#reader-sec", el).scrollIntoView({ behavior: "smooth", block: "start" });
  };
  $$("[data-read]", el).forEach((b) => b.addEventListener("click", () => read(b.dataset.read, true)));
  drawTree();
  await read(sel);
}
