// #/audit: coverage, the Fatal / Non-fatal / Pass split (judge vs human QA), agreement with
// IndiaMART's auditors (headline, kappa, per-parameter confusion), by-rubric bars, risky calls.
import { get } from "../api.js";
import { $$, html, mount, int, pct, fix, shares, gradeStack, gradeLegend, grade, flagChip, empty, enc } from "../ui.js";
import { confusion } from "../viz.js";

export async function render(el, { params }) {
  const pr = await get("/api/prompts").catch(() => ({ items: [], production: null }));
  const versions = pr.items.map((p) => p.version);
  const v = params.get("version") || (versions.includes("v1") ? "v1" : pr.production || "v1");
  const chips = html`<div class="seg" role="group" aria-label="Prompt version">${(versions.length ? versions : [v]).map((x) =>
    html`<a href="#/audit?version=${enc(x)}" aria-current="${x === v}">${x}${x === pr.production ? " · prod" : ""}</a>`)}</div>`;
  let a;
  try { a = await get(`/api/audit?version=${enc(v)}`); } catch (err) {
    if (err.status !== 404) throw err;
    return mount(el, html`<div class="page-head"><div><div class="eyebrow">Audit · Prompt ${v}</div><h1 class="title">No audit for <em>${v}</em> yet</h1></div>${chips}</div>
      ${empty({ title: "No transcripts judged for this prompt", body: "The judge grades every transcript of a prompt version with every active rubric. Listen first if there are no transcripts.", cmd: "make judge", glyph: "doc" })}`);
  }
  if (!a.n_audited) {
    return mount(el, html`<div class="page-head"><div><div class="eyebrow">Audit · Prompt ${v}</div><h1 class="title">Nothing judged for <em>${v}</em> yet</h1>
      <p class="lede">${int(a.n_calls)} transcript${a.n_calls === 1 ? "" : "s"} on record for ${v}. The judge grades each one with every active rubric; the split, the agreement with IndiaMART's auditors and the risky calls appear here.</p></div>${chips}</div>
      ${empty({ title: a.n_calls ? "Transcripts are waiting for the judge" : "No transcripts yet", body: a.n_calls ? "Run the judge on them." : "Listen turns the recordings into transcripts first; then the judge grades them.", cmd: a.n_calls ? "make judge" : "make listen", glyph: "doc" })}`);
  }
  const judged = shares(a.split), human = shares(a.human_split), ag = a.agreement;
  const names = Object.fromEntries((a.by_rubric || []).map((r) => [r.rubric_id, r]));
  const fails = (r) => (3 * r.fatal + r.non_fatal) / Math.max(1, 3 * r.n_applicable);
  const bars = [...(a.by_rubric || [])].sort((x, y) => fails(y) - fails(x));

  mount(el, html`
    <div class="page-head">
      <div><div class="eyebrow">Audit · Prompt ${a.version}${ag?.model ? ` · judge ${ag.model}` : ""}</div>
        <h1 class="title">What the judge found in <em>${int(a.n_audited)}</em> calls</h1>
        <p class="lede">Every call is graded on IndiaMART's QA checklist, Pass, Non-fatal or Fatal per parameter, plus any custom rubric.
          A call's grade is its worst parameter: the QA team's own rule.</p></div>
      ${chips}
    </div>

    <div class="grid cols-3">
      <div class="card"><div class="eyebrow">Coverage</div>
        <div class="big" style="margin-top:14px">${int(a.n_audited)}<small>/ ${int(a.n_calls)}</small></div>
        <div class="bar tall" style="margin-top:16px"><span class="s-a" style="flex-basis:${a.n_calls ? (100 * a.n_audited / a.n_calls).toFixed(1) : 0}%"></span></div>
        <p class="muted" style="font-size:12.5px;margin-top:10px">${pct(a.n_calls ? a.n_audited / a.n_calls : null)} of the calls on record for ${a.version} have a transcript and a verdict.</p></div>
      <div class="card"><div class="card-head"><div class="eyebrow">Fatal · Non-fatal · Pass</div></div>
        <div class="split-row"><div class="who"><b>Judge</b>${judged?.n ? `${int(judged.n)} calls` : "auto"}</div><div>${judged ? gradeStack(judged, "xl") : "—"}
          <div class="split-nums"><span>${pct(judged?.fatal)} fatal</span><span>${pct(judged?.non_fatal)} non-fatal</span><span>${pct(judged?.pass)} pass</span></div></div></div>
        <div class="split-row"><div class="who"><b>Human QA</b>${human?.n ? `${int(human.n)} calls` : ""}</div><div>${human ? html`${gradeStack(human, "xl")}
          <div class="split-nums"><span>${pct(human.fatal)} fatal</span><span>${pct(human.non_fatal)} non-fatal</span><span>${pct(human.pass)} pass</span></div>` : html`<p class="muted" style="font-size:12.5px">No IndiaMART QA grades exist for ${a.version} calls.</p>`}</div></div></div>
      <div class="card headline"><div class="row"><span class="eyebrow">Agreement with IndiaMART QA</span>${ag ? html`<span class="chip ${ag.held_out ? "win" : "warn"}">${ag.held_out ? "held out" : "tuned on these"}</span>` : ""}</div>
        ${ag ? html`<div class="big xl accent" style="margin-top:6px">${pct(ag.fatal_vs_rest_accuracy)}</div>
          <p style="font-size:13px">Fatal vs rest accuracy on ${int(ag.n_calls)} calls graded by both.</p>
          <div class="statline" style="margin-top:6px"><span><b>${fix(ag.overall_kappa)}</b>Cohen's κ</span><span><b>${pct(ag.overall_accuracy)}</b>3-class accuracy</span></div>`
        : html`<p class="muted" style="margin-top:12px;font-size:13px">${a.version === "v1" ? html`No agreement report yet. It compares the judge with IndiaMART's auditors on the audited calls: run <code>make agreement</code> after judging them.` : "Agreement is measured where human QA grades exist: the SquadStack inbound calls IndiaMART audited, which ran on v1."}</p>
          ${a.version !== "v1" ? html`<a class="btn sm" style="margin-top:14px;justify-self:start" href="#/audit?version=v1">See v1's agreement</a>` : ""}`}</div>
    </div>

    ${ag ? html`<section class="section">
      <div class="section-head"><h2>Agreement <em>by parameter</em></h2><p>Click a row for its confusion matrix. N/A counts as a class.</p></div>
      <div class="card flush"><div class="table-wrap"><table class="table" id="agree">
        <thead><tr><th>Parameter</th><th class="r">Calls</th><th style="width:30%">Accuracy</th><th class="r" style="text-transform:none">κ</th><th class="r">Fatal recall</th><th></th></tr></thead>
        <tbody>${ag.rows.map((r, i) => html`
          <tr class="link" data-i="${i}" tabindex="0" aria-expanded="false"><td>${names[r.rubric_id]?.name || r.rubric_id}</td><td class="r">${int(r.n)}</td>
            <td><div class="row" style="flex-wrap:nowrap"><div class="bar" style="flex:1">${raw_bar(r.accuracy)}</div><span class="mono" style="width:48px;text-align:right">${pct(r.accuracy)}</span></div></td>
            <td class="r mono">${fix(r.kappa)}</td><td class="r mono">${pct(r.fatal_recall)}</td><td class="r muted">›</td></tr>
          <tr class="detail" data-for="${i}" hidden><td colspan="6"><div class="cm-wrap">${confusion(r.confusion)}
            <div class="cm-explain"><b>${names[r.rubric_id]?.name || r.rubric_id}</b>
              <span>Rows are the auditors' grades, columns the judge's. The outlined diagonal is agreement.</span>
              ${r.confusion?.fatal ? html`<span>Of ${int(Object.values(r.confusion.fatal).reduce((s, x) => s + x, 0))} calls the auditors graded Fatal, the judge called ${int(r.confusion.fatal.fatal)} Fatal (recall ${pct(r.fatal_recall)}).</span>` : ""}</div></div></td></tr>`)}</tbody>
      </table></div></div></section>` : ""}

    <section class="section">
      <div class="section-head"><h2>By <em>rubric</em></h2>${gradeLegend()}</div>
      <div class="card"><div class="rubric-bars">${bars.map((r) => {
        const n = Math.max(1, r.n_applicable);
        return html`<div class="rb"><div class="nm">${r.name}<small>${r.category} · ${r.root_type}</small></div>
          <div>${r.n_applicable ? gradeStack({ pass: r.pass / n, non_fatal: r.non_fatal / n, fatal: r.fatal / n, n }, "tall") : html`<span class="muted" style="font-size:12px">Not graded: needs audio, or never applied</span>`}</div>
          <div class="nums"><b>${int(r.fatal)}</b> fatal · ${int(r.non_fatal)} nf<br>of ${int(r.n_applicable)} applicable</div></div>`;
      })}</div></div>
    </section>

    <section class="section">
      <div class="section-head"><h2>Risky <em>calls</em></h2><p>Calls the judge flagged for a person to listen to first.</p></div>
      ${a.risky?.length ? html`<div class="card" style="padding:4px 18px">${a.risky.map((r) => html`
        <a class="risk" href="#/calls/${enc(r.call_id)}"><span><span class="mono" style="font-size:12px">${r.call_id}</span><br>${grade(r.overall)}</span>
          <span class="sum muted" style="font-size:13px">${r.summary}</span><span class="flags">${r.flags.map(flagChip)}</span></a>`)}</div>`
        : empty({ title: "Nothing flagged", body: "No call raised a wrong-requirement, frustration, loop, low-confidence or human-request flag.", compact: true })}
    </section>`);

  $$("#agree tr.link", el).forEach((tr) => {
    const toggle = () => {
      const d = el.querySelector(`tr.detail[data-for="${tr.dataset.i}"]`);
      d.hidden = !d.hidden;
      tr.classList.toggle("open", !d.hidden);
      tr.setAttribute("aria-expanded", String(!d.hidden));
    };
    tr.addEventListener("click", toggle);
    tr.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
  });
}

const raw_bar = (x) => html`<span class="s-a" style="flex-basis:${((x || 0) * 100).toFixed(1)}%;background:var(--accent)"></span>`;
