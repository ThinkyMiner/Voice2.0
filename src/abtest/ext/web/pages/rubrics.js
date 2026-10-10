// #/rubrics: the library (IndiaMART QA matrix built-ins, prompt rules and custom rubrics: the judge's grades and
// how far the judge agrees with IndiaMART's QA auditors on each) and #/rubrics/<id|new>: the Rubric Studio, a
// form with a live preview of what the judge reads.
import { get, send, state } from "../api.js";
import { $, $$, html, mount, int, pct, fix, typeChip, weight, gradeStack, gradeLegend, grade, empty, toast, enc, esc, seg, agreement, kappaWord, confirmDialog, reducedMotion, goodChecks, audioIds } from "../ui.js";

const CATEGORIES = ["Outcome", "Quantity", "Specs", "Details", "Conversation", "Speech", "Catalog", "Compliance", "Custom"];
const ROOTS = ["prompt", "asr", "knowledge", "flow", "platform"];
const ROOT_WORDS = [["prompt", "the prompt"], ["flow", "the call flow"], ["asr", "speech recognition"], ["knowledge", "catalogue knowledge"], ["platform", "telephony and audio"]];
const kind = (r) => (!r.builtin ? "Custom" : r.qa_column ? "QA matrix" : "Prompt rule");
const REAL = ["production", "audited"];  // transcript sources that are real customer calls (loop.REAL)
const slug = (s) => s.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48);

/** The production version's grades per rubric, and the judge's agreement with QA (measured on v1's audited calls).
 *  A 404 means nothing graded yet; any other failure is reported, never shown as "not graded". */
async function grades() {
  const one = (v) => get(`/api/audit?version=${enc(v)}`).catch((err) => { if (err.status === 404) return null; throw err; });
  try {
    const prod = state.overview?.production?.version || "v1";
    const a = (await one(prod)) || (prod !== "v1" ? await one("v1") : null);
    const ag = a?.agreement || (a && a.version !== "v1" ? (await one("v1"))?.agreement : null) || null;
    return { v: a?.n_audited ? a.version : null, n: a?.n_audited || 0, by: Object.fromEntries((a?.by_rubric || []).map((r) => [r.rubric_id, r])),
      ag, agRow: Object.fromEntries((ag?.rows || []).map((r) => [r.rubric_id, r])), err: null };
  } catch (err) { return { v: null, n: 0, by: {}, ag: null, agRow: {}, err }; }
}

export async function renderLibrary(el) {
  const [{ items }, gr] = await Promise.all([get("/api/rubrics"), grades()]);
  const active = items.filter((r) => r.active !== false), archived = items.filter((r) => r.active === false);
  const groups = CATEGORIES.map((c) => [c, active.filter((r) => r.category === c)]).filter(([, rs]) => rs.length);
  const other = active.filter((r) => !CATEGORIES.includes(r.category));
  if (other.length) groups.push(["Other", other]);
  if (archived.length) groups.push(["Archived", archived]);
  const audio = audioIds(items), { text, good } = goodChecks(gr.ag, audio);  // the same counts as the Audit page's
  const weak = text.filter((r) => r.kappa == null || r.kappa < 0.4);
  const agCell = (r) => {
    if (!r.qa_column) return html`<div class="ag none"><b>Judge only</b><small>no QA column</small></div>`;
    if (r.needs_audio) return html`<div class="ag none"><b>Needs audio</b><small>not compared</small></div>`;
    const x = gr.agRow[r.id];
    if (!x) return html`<div class="ag none"><b>—</b><small>${gr.ag ? "not compared" : "not measured yet"}</small></div>`;
    const a = agreement(x);
    return html`<div class="ag ${a.weak ? "weak" : "ok"}" title="${a.text}. Cohen's κ discounts agreement by chance: 0 is chance level, 1 is perfect."><b>${pct(x.accuracy, 0)} · κ ${fix(x.kappa)}</b><small>${kappaWord(x.kappa)}, ${int(x.n)} calls</small></div>`;
  };
  const row = (r) => {
    const b = gr.by[r.id], n = b ? Math.max(1, b.n_applicable) : 0;
    return html`<a class="rrow ${r.active === false ? "archived" : ""}" href="#/rubrics/${enc(r.id)}">
      <div class="nm"><b>${r.name}</b><span class="q" title="${r.question}">${r.question}</span></div>
      <div class="ty">${typeChip(r.root_type)}<small>${kind(r)} · v${r.version}${r.needs_audio ? " · audio" : ""}</small></div>
      <div class="gr">${b && b.n_applicable ? html`${gradeStack({ pass: b.pass / n, non_fatal: b.non_fatal / n, fatal: b.fatal / n, n })}<small><b>${int(b.fatal)}</b> fatal · <b>${int(b.non_fatal)}</b> non-fatal · ${int(b.n_applicable)} graded</small>`
        : html`<small>${gr.err ? "grades did not load" : r.needs_audio ? "needs audio: N/A from a transcript" : gr.v ? `not graded in ${gr.v}` : "not graded yet"}</small>`}</div>
      ${agCell(r)}
      <div class="wt">${weight(r.weight)}${r.builtin ? "" : html`<small>${r.author}</small>`}</div></a>`;
  };
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Judge</div><h1 class="title">Rubrics</h1>
      <p class="lede">The checks the judge grades every call on: IndiaMART's QA matrix, prompt rules and your own.</p></div>
      <a class="btn primary" href="#/rubrics/new"><svg viewBox="0 0 16 16"><path d="M8 3v10M3 8h10"/></svg>New rubric</a></div>
    ${gr.err ? html`<div class="note rb-err" role="alert"><b>Grades could not load.</b> ${gr.err.message || gr.err}.</div>` : ""}
    ${items.length ? html`<div class="rb-sum">
      <p>${gr.v ? html`Judge's grades on <b>${int(gr.n)}</b> ${gr.v} calls.` : "No calls are graded yet."}
        ${gr.ag ? html`Agreement with QA on ${int(gr.ag.n_calls)} ${gr.ag.held_out ? "held-out " : ""}calls: good (κ 0.6+) on <b>${int(good.length)} of ${int(text.length)}</b> checks, weak (under 0.4) on <b>${int(weak.length)}</b>. <a href="#/audit">Audit</a>` : ""}</p>
      <div class="rb-legends">${gradeLegend()}<div class="legend" aria-label="Root type: where a failure gets fixed"><span class="muted">Fixed in:</span>${ROOT_WORDS.map(([t, w]) => html`<span>${typeChip(t)} ${w}</span>`)}</div></div>
    </div>
    <div class="rlist">
      <div class="rrow rhead" aria-hidden="true"><span>Rubric</span><span>Type</span><span>Judge's grades${gr.v ? ` in ${gr.v}` : ""}</span><span>Agrees with QA</span><span>Weight</span></div>
      ${groups.map(([c, rs]) => html`<div class="rgroup"><h3>${c}</h3><span>${rs.length}</span></div>${rs.map(row)}`)}
    </div>` : empty({ title: "No rubrics loaded", body: "Built-in rubrics live in configs/rubrics; add your own in the studio.", glyph: "doc" })}`);
}

export async function renderStudio(el, { id }) {
  const isNew = id === "new";
  const [{ items }, all, settings] = await Promise.all([get("/api/rubrics"), get("/api/calls?limit=5000").catch(() => ({ items: [] })),
    get("/api/settings").catch(() => null)]);
  // Testing sends the transcript to the model provider, so real customer calls stay out of the picker
  // unless the platform was started with LOOP_ALLOW_REAL=1 (demo calls are invented and never leave the tab).
  const allowReal = state.demo || settings?.allow_real_rubric_tests === true;
  const calls = all.items.filter((c) => allowReal || !(c.real ?? REAL.includes(c.source)));  // real: replays of real calls too
  const hiddenReal = all.items.length - calls.length;
  const cur = isNew ? null : items.find((r) => r.id === id);
  if (!isNew && !cur) throw Object.assign(new Error(`There is no rubric called ${id}.`), { status: 404 });
  const history = cur ? await get(`/api/rubrics/${enc(id)}/versions`).then((r) => r.items).catch(() => null) : null;
  let author = "";
  try { author = localStorage.getItem("vani-actor") || ""; } catch { /* none */ }
  const d = cur ? { ...cur, examples_pass: [...(cur.examples_pass || [])], examples_fail: [...(cur.examples_fail || [])] }
    : { id: "", name: "", category: "Custom", question: "", guidance: "", fatal_when: "", non_fatal_when: "", applies_when: "", root_type: "prompt", weight: 3,
      needs_audio: false, qa_column: null, builtin: false, author, active: true, examples_pass: [], examples_fail: [] };

  const req = html`<i class="req" aria-hidden="true">*</i>`;
  const rows = (k, min) => Math.min(12, Math.max(min, Math.ceil(String(d[k] || "").length / 50) + 1));  // long built-in texts open in full
  const field = (k, label, hint, min = 2, required = false) => html`<label class="field"><span>${label}${required ? req : ""}</span>
    <textarea class="textarea" name="${k}" rows="${rows(k, min)}" placeholder="${hint}" ${required ? "required" : ""} aria-describedby="err-${k}">${d[k] || ""}</textarea><small class="err" id="err-${k}" hidden></small></label>`;
  const archived = cur?.active === false;
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Rubric studio${cur ? ` · ${kind(cur)} · v${cur.version}` : ""}</div>
      <h1 class="title">${cur ? html`Edit <em>${cur.name}</em>` : html`Write a <em>rubric</em>`}</h1>
      <p class="lede">Describe one thing a good Vani call does, in the words your auditors use.</p></div>
      <div class="row"><button class="btn sm narrow-only" type="button" id="to-preview">See the preview</button><a class="btn ghost" href="#/rubrics">Back to the library</a></div></div>
    ${state.demo ? html`<div class="note" style="margin-bottom:20px"><b>Demo mode.</b> Saves stay in this browser; "Try it" uses phrase matching, not the judge.</div>` : ""}
    <div class="studio">
      <form id="form" novalidate>
        <div class="two">
          <label class="field"><span>Name${req}</span><input class="input" name="name" required maxlength="80" value="${d.name}" placeholder="e.g. No price promises" aria-describedby="idline err-name">
            <small id="idline" class="muted"></small><small class="err" id="err-name" hidden></small></label>
          <label class="field"><span>Category</span><select class="select" name="category">${[...CATEGORIES, ...(CATEGORIES.includes(d.category) ? [] : [d.category])].map((c) => html`<option ${c === d.category ? "selected" : ""}>${c}</option>`)}</select></label>
        </div>
        ${field("question", "Question the judge answers", "Did the bot … ?", 2, true)}
        <div class="two">${field("fatal_when", "Fatal when", "The failure that breaks the lead or the buyer's trust", 3)}${field("non_fatal_when", "Non-fatal when", "A slip worth coaching, not a broken call", 3)}</div>
        <div class="two">${field("applies_when", "Applies when", "Leave empty to apply to every call", 2)}${field("guidance", "Guidance (optional)", "How your auditors decide edge cases", 2)}</div>
        <div class="field"><span>Root type</span><div id="root">${seg(ROOTS.map((r) => [r, r]), d.root_type)}</div><small title="The prompt, speech recognition (asr), catalogue knowledge, the call flow, or the platform">Where a failure gets fixed.</small></div>
        <div class="two">
          <div class="field"><span>Weight</span><div class="range"><input type="range" name="weight" min="1" max="5" step="1" value="${d.weight}" aria-label="Weight, 1 to 5"><output id="wout">${d.weight}</output></div><small>Shown with the rubric; not used in ranking.</small></div>
          <div class="field"><span>Needs audio</span><label class="switch"><input type="checkbox" name="needs_audio" ${d.needs_audio ? "checked" : ""}>Only gradable from the recording</label><small>A transcript-only judge answers N/A.</small></div>
        </div>
        <div class="two">
          <div class="field"><span>Passes, example phrases</span><div class="chips-input" data-k="examples_pass"><input placeholder="Add a phrase…" aria-label="Add a pass example" aria-describedby="chip-hint"></div></div>
          <div class="field"><span>Fails, example phrases</span><div class="chips-input" data-k="examples_fail"><input placeholder="Add a phrase…" aria-label="Add a fail example" aria-describedby="chip-hint"></div></div>
        </div>
        <small class="muted chip-hint" id="chip-hint">Enter adds a phrase; Backspace removes the last.</small>
        <label class="field"><span>Your name${req}</span><input class="input" name="author" required value="${d.builtin ? author : d.author || author}" placeholder="Recorded as the author of this version" autocomplete="name" aria-describedby="err-author"><small class="err" id="err-author" hidden></small></label>
        <div class="row" style="margin-top:4px"><button class="btn primary" type="submit">${cur ? `Save as v${cur.version + 1}` : "Save rubric"}</button>
          <span class="muted" style="font-size:12px">${cur ? `v${cur.version} stays on record.` : "Saved as version 1."}</span></div>
      </form>
      <div class="preview" id="preview">
        <div id="card"></div>
        ${calls.length ? html`<div class="card try"><div class="card-head"><h3>Try it on a call</h3>${state.demo ? html`<span class="chip">phrase match</span>` : ""}</div>
            <div class="row" style="flex-wrap:nowrap"><select class="select" id="call" aria-label="Call to grade">${calls.map((c) => html`<option value="${c.call_id}">${c.call_id} · ${(c.summary || "").slice(0, 46)}</option>`)}</select>
            <button class="btn" id="run" type="button">Grade</button></div><div id="verdict"></div></div>`
          : html`<div class="card try off"><div class="card-head"><h3>Try it on a call</h3><span class="chip">off here</span></div>
            <p class="muted">${hiddenReal ? "Off for real calls: a test sends the transcript to the model provider. Start the platform with LOOP_ALLOW_REAL=1 to allow it." : "No transcripts to try it on yet."}</p></div>`}
        ${cur ? html`<div class="card"><div class="card-head"><h3>Version history</h3><span class="eyebrow">${archived ? "archived" : "active"}</span></div>
          <div class="history">${(history || [cur]).slice().reverse().map((r) => html`<div class="h"><b>v${r.version}</b><span>${r.author || "—"}${r.version === cur.version ? " · current" : ""}</span><span class="muted">${r.active === false ? "archived" : ""}</span></div>`)}
          ${!history && cur.version > 1 ? html`<p class="muted" style="font-size:12px;padding-top:8px">Versions 1 to ${cur.version - 1} are kept on the platform.</p>` : ""}</div>
          <div class="archive-row"><p class="muted">${archived ? "Archived: not graded on new calls." : "Archiving stops grading on new calls; past verdicts stay."}</p>
            <button class="btn sm ${archived ? "" : "danger"}" type="button" id="archive">${archived ? "Restore…" : "Archive…"}</button></div></div>` : ""}
      </div>
    </div>`);

  // The crumb and tab title name the rubric, not its id or "new".
  // ponytail: app.js titles a page by its route id after render; this overrides it until app.js takes a display name.
  const label = isNew ? "New rubric" : cur.name;
  const crumb = $("#crumbs li:last-child");
  if (crumb) { crumb.textContent = label; crumb.classList.remove("mono"); }
  setTimeout(() => { document.title = `${label} · Vani Lab`; });

  const form = $("#form", el);
  const read = () => {
    const fd = new FormData(form);
    for (const k of ["name", "category", "question", "guidance", "fatal_when", "non_fatal_when", "applies_when", "author"]) d[k] = String(fd.get(k) || "").trim();
    d.weight = Number(fd.get("weight"));
    d.needs_audio = fd.get("needs_audio") === "on";
    if (isNew) d.id = slug(d.name);
    return d;
  };
  const taken = new Set(items.map((r) => r.id));
  const problems = () => ({  // field -> what is wrong with it, "" when fine
    name: !d.name ? "Give the rubric a name." : isNew && !d.id ? "Use letters or digits in the name." : isNew && taken.has(d.id) ? `A rubric with the id ${d.id} exists: choose another name.` : "",
    question: d.question ? "" : "Write the one question the judge answers.",
    author: d.author ? "" : "Add your name: every version records its author.",
  });
  const showErrors = (only = null) => {  // only: just clear errors already fixed (while typing)
    let first = null;
    for (const [k, msg] of Object.entries(problems())) {
      const s = $(`#err-${k}`, el);
      if (only && s.hidden) continue;
      s.hidden = !msg;
      s.textContent = msg;
      form.elements[k].setAttribute("aria-invalid", String(Boolean(msg)));
      first ||= msg ? form.elements[k] : null;
    }
    return first;
  };
  const preview = () => {
    read();
    $("#wout", el).textContent = d.weight;
    const clash = isNew && d.id && taken.has(d.id);
    $("#idline", el).innerHTML = d.id ? `id <code>${esc(d.id)}</code>${clash ? ' <span style="color:var(--harm)">· taken, rename it</span>' : isNew ? "" : " · fixed"}` : "";
    const ph = (s, p) => (s ? html`<dd>${s}</dd>` : html`<dd class="ph">${p}</dd>`);
    mount($("#card", el), html`<div class="judge-card">
      <div class="row between"><span class="eyebrow">Rubric · ${d.category}</span><span class="chip ${cur ? "" : "pending"}">${cur ? `v${cur.version} → v${cur.version + 1}` : "draft · v1"}</span></div>
      <div class="nm">${d.name || "Untitled rubric"}</div>
      <div class="q">${d.question || "The one question the judge answers about the transcript."}</div>
      <dl>
        <div><dt>Fatal</dt>${ph(d.fatal_when, "When is the call broken?")}</div>
        <div><dt>Non-fatal</dt>${ph(d.non_fatal_when, "When is it a slip?")}</div>
        <div><dt>Pass</dt><dd>Otherwise.</dd></div>
        <div><dt>N/A</dt><dd>${d.applies_when ? html`Unless: ${d.applies_when}` : "Never: applies to every call."}${d.needs_audio ? " Also N/A for a transcript-only judge." : ""}</dd></div>
        ${d.guidance ? html`<div><dt>Guidance</dt><dd>${d.guidance}</dd></div>` : ""}
        <div><dt>Passes</dt>${d.examples_pass.length ? html`<dd>${d.examples_pass.map((x) => html`“${x}” `)}</dd>` : html`<dd class="ph">Example phrases help the judge.</dd>`}</div>
        <div><dt>Fails</dt>${d.examples_fail.length ? html`<dd>${d.examples_fail.map((x) => html`“${x}” `)}</dd>` : html`<dd class="ph">—</dd>`}</div>
      </dl>
      <div class="foot">${typeChip(d.root_type)}${weight(d.weight)}<span class="muted" style="font-size:12px">weight ${d.weight}</span><span class="muted" style="font-size:12px;margin-left:auto">${d.author || "author"}</span></div></div>`);
  };
  // chip inputs for example phrases: Enter adds one (commas stay inside a phrase), never submits the form
  for (const box of $$(".chips-input", el)) {
    const k = box.dataset.k, input = $("input", box);
    const draw = () => {
      $$(".xchip", box).forEach((c) => c.remove());
      d[k].forEach((x, i) => {
        const c = document.createElement("span");
        c.className = `xchip ${k === "examples_pass" ? "pass" : "fail"}`;
        c.innerHTML = `<span title="${esc(x)}">${esc(x)}</span><button type="button" aria-label="Remove ${esc(x)}">×</button>`;
        c.querySelector("button").addEventListener("click", () => { d[k].splice(i, 1); draw(); preview(); input.focus(); });
        box.insertBefore(c, input);
      });
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); if (input.value.trim()) { d[k].push(input.value.trim()); input.value = ""; draw(); preview(); } }
      else if (e.key === "Backspace" && !input.value && d[k].length) { d[k].pop(); draw(); preview(); }
    });
    box.addEventListener("click", (e) => { if (e.target === box) input.focus(); });
    draw();
  }
  $("#root", el).addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    d.root_type = b.dataset.v;
    $$("#root button", el).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    preview();
  });
  $("#to-preview", el).addEventListener("click", () => $("#preview", el).scrollIntoView({ block: "start" }));
  form.addEventListener("keydown", (e) => { if (e.key === "Enter" && e.target instanceof HTMLInputElement) e.preventDefault(); });  // only the Save button saves
  form.addEventListener("input", () => { preview(); showErrors(true); });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    read();
    const bad = showErrors();
    if (bad) { bad.focus(); return; }
    if (cur?.builtin && !(await confirmDialog({ title: `Save v${cur.version + 1} of ${cur.name}?`, ok: `Save v${cur.version + 1}`,
      body: html`<p>This is a built-in ${kind(cur).toLowerCase()} rubric. The judge grades with v${cur.version + 1} from now on; past verdicts keep citing v${cur.version}.</p>` }))) return;
    try { localStorage.setItem("vani-actor", d.author); } catch { /* not remembered */ }
    const { version, builtin, ...body } = d;
    try {
      const saved = await send(isNew ? "POST" : "PUT", isNew ? "/api/rubrics" : `/api/rubrics/${enc(id)}`, { ...body, actor: d.author });
      toast(`Saved ${saved?.name || d.name} as v${saved?.version ?? (cur ? cur.version + 1 : 1)}.${state.demo ? " Demo mode: in this browser only." : ""}`);
      if (isNew) location.hash = `#/rubrics/${enc(saved?.id || d.id)}`; else window.dispatchEvent(new Event("vani:rerender"));
    } catch (err) { toast(`Could not save: ${err.message}`, "harm"); }
  });
  $("#archive", el)?.addEventListener("click", async () => {
    read();
    if (!d.author) { const s = $("#err-author", el); s.hidden = false; s.textContent = `Add your name: ${archived ? "restoring" : "archiving"} records who did it.`; form.elements.author.focus(); return; }
    const ok = await confirmDialog({ title: `${archived ? "Restore" : "Archive"} ${cur.name}?`, ok: archived ? "Restore" : "Archive", tone: archived ? "primary" : "danger",
      body: html`<p>${archived ? `The judge grades ${cur.name} again on the calls it grades from now on.` : `The judge stops grading ${cur.name} on new calls. Past verdicts keep it, and you can restore it here.`}</p><p class="muted">Recorded as ${d.author}.</p>` });
    if (!ok) return;
    try {
      if (archived) await send("PUT", `/api/rubrics/${enc(id)}`, { ...cur, version: undefined, builtin: undefined, active: true, actor: d.author });
      else await send("DELETE", `/api/rubrics/${enc(id)}?actor=${enc(d.author)}`);
      toast(archived ? "Restored." : "Archived: the judge stops using it; past verdicts keep citing it.");
      window.dispatchEvent(new Event("vani:rerender"));
    } catch (err) { toast(`Could not change it: ${err.message}`, "harm"); }
  });
  $("#run", el)?.addEventListener("click", async () => {
    const btn = $("#run", el), out = $("#verdict", el), cid = $("#call", el).value;
    read();
    btn.disabled = true;
    mount(out, html`<div class="skeleton" style="margin-top:4px"><i style="width:40%"></i><i style="width:90%"></i></div>`);
    try {
      const { version, builtin, ...rubric } = d;
      const v = await send("POST", "/api/rubrics/test", { rubric: { ...rubric, id: d.id || "draft", version: cur ? cur.version + 1 : 1, builtin: false }, call_id: cid });
      mount(out, html`<div class="verdict" role="status"><div class="row between">${grade(v.grade, "boxed")}${v.confidence != null ? html`<span class="mono muted" style="font-size:11px">confidence ${v.confidence.toFixed(2)}</span>` : ""}</div>
        <p style="font-size:13px">${v.reason}</p>
        ${(v.evidence || []).map((e) => html`<blockquote>“${e.quote}”<small>turn ${e.turn + 1}</small></blockquote>`)}
        <a class="btn sm" href="#/calls/${enc(cid)}" style="justify-self:start">Open the call</a></div>`);
      out.scrollIntoView({ block: "nearest", behavior: reducedMotion() ? "auto" : "smooth" });  // the verdict lands on screen
    } catch (err) { mount(out, html`<p style="color:var(--harm);font-size:13px">${err.message}</p>`); }
    btn.disabled = false;
  });
  preview();
}
