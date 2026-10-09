// #/rubrics: the library (IndiaMART QA matrix built-ins and custom rubrics, with their audit bars)
// and #/rubrics/<id|new>: the Rubric Studio, a form with a live preview of what the judge reads.
import { get, send, state } from "../api.js";
import { $, $$, html, mount, int, typeChip, weight, gradeStack, grade, empty, toast, enc, esc, seg } from "../ui.js";

const CATEGORIES = ["Outcome", "Quantity", "Specs", "Details", "Conversation", "Speech", "Catalog", "Compliance", "Custom"];
const ROOTS = ["prompt", "asr", "knowledge", "flow", "platform"];
const kind = (r) => (!r.builtin ? "Custom" : r.qa_column ? "QA matrix" : "Prompt rule");
const REAL = ["production", "audited"];  // transcript sources that are real customer calls (loop.REAL)
const slug = (s) => s.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 48);

async function auditBars() {
  const ov = state.overview;
  for (const v of [ov?.production?.version, "v1"]) {
    if (!v) continue;
    try { const a = await get(`/api/audit?version=${enc(v)}`); return { v, by: Object.fromEntries(a.by_rubric.map((r) => [r.rubric_id, r])) }; } catch { /* try the next */ }
  }
  return { v: null, by: {} };
}

export async function renderLibrary(el) {
  const [{ items }, bars] = await Promise.all([get("/api/rubrics"), auditBars()]);
  const active = items.filter((r) => r.active !== false), archived = items.filter((r) => r.active === false);
  const groups = CATEGORIES.map((c) => [c, active.filter((r) => r.category === c)]).filter(([, rs]) => rs.length);
  const other = active.filter((r) => !CATEGORIES.includes(r.category));
  if (other.length) groups.push(["Other", other]);
  const card = (r) => {
    const b = bars.by[r.id], n = b ? Math.max(1, b.n_applicable) : 0;
    return html`<a class="rcard ${r.active === false ? "archived" : ""}" href="#/rubrics/${enc(r.id)}">
      <div class="row between"><span class="eyebrow">${kind(r)} · v${r.version}</span>${typeChip(r.root_type)}</div>
      <div class="nm">${r.name}</div><div class="q">${r.question}</div>
      ${b && b.n_applicable ? html`<div>${gradeStack({ pass: b.pass / n, non_fatal: b.non_fatal / n, fatal: b.fatal / n, n })}<div class="mono muted" style="font-size:10.5px;margin-top:5px">${int(b.fatal)} fatal · ${int(b.non_fatal)} non-fatal · ${int(b.n_applicable)} graded in ${bars.v}</div></div>`
        : html`<div class="mono muted" style="font-size:10.5px">${r.needs_audio ? "needs audio: the transcript judge answers N/A" : bars.v ? `not graded in ${bars.v}` : "not graded yet"}</div>`}
      <div class="foot">${weight(r.weight)}<span>weight ${r.weight}</span>${r.needs_audio ? html`<span class="chip">audio</span>` : ""}<span style="margin-left:auto">${r.builtin ? "" : r.author}</span></div></a>`;
  };
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Judge</div><h1 class="title">Rubrics</h1>
      <p class="lede">What the judge grades every call on: IndiaMART's own QA matrix, parameter by parameter, plus rubrics anyone on the team writes in the studio.
        Every version is kept, and each verdict cites the version it used.</p></div>
      <a class="btn primary" href="#/rubrics/new"><svg viewBox="0 0 16 16"><path d="M8 3v10M3 8h10"/></svg>New rubric</a></div>
    ${groups.map(([c, rs]) => html`<div class="cat-head"><h3>${c}</h3><span>${rs.length}</span></div><div class="rcards">${rs.map(card)}</div>`)}
    ${archived.length ? html`<div class="cat-head"><h3>Archived</h3><span>${archived.length}</span></div><div class="rcards">${archived.map(card)}</div>` : ""}
    ${!items.length ? empty({ title: "No rubrics loaded", body: "Built-in rubrics live in configs/rubrics; add your own in the studio.", glyph: "doc" }) : ""}`);
}

export async function renderStudio(el, { id }) {
  const isNew = id === "new";
  const [{ items }, allCalls, settings] = await Promise.all([get("/api/rubrics"), get("/api/calls?limit=300").catch(() => ({ items: [] })),
    get("/api/settings").catch(() => null)]);
  // Testing sends the transcript to the model provider, so real customer calls stay out of the picker
  // unless the platform was started with LOOP_ALLOW_REAL=1 (demo calls are invented and never leave the tab).
  const allowReal = state.demo || settings?.allow_real_rubric_tests === true;
  const calls = { items: allCalls.items.filter((c) => allowReal || !REAL.includes(c.source)) };
  const hiddenReal = allCalls.items.length - calls.items.length;
  const cur = isNew ? null : items.find((r) => r.id === id);
  if (!isNew && !cur) throw Object.assign(new Error(`There is no rubric called ${id}.`), { status: 404 });
  const history = cur ? await get(`/api/rubrics/${enc(id)}/versions`).then((r) => r.items).catch(() => null) : null;
  let author = "";
  try { author = localStorage.getItem("vani-actor") || ""; } catch { /* none */ }
  const d = cur ? { ...cur, examples_pass: [...(cur.examples_pass || [])], examples_fail: [...(cur.examples_fail || [])] }
    : { id: "", name: "", category: "Custom", question: "", guidance: "", fatal_when: "", non_fatal_when: "", applies_when: "", root_type: "prompt", weight: 3,
      needs_audio: false, qa_column: null, builtin: false, author, active: true, examples_pass: [], examples_fail: [] };

  const field = (k, label, hint, rows = 2) => html`<label class="field"><span>${label}</span><textarea class="textarea" name="${k}" rows="${rows}" placeholder="${hint}">${d[k] || ""}</textarea></label>`;
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Rubric studio${cur ? ` · ${kind(cur)} · v${cur.version}` : ""}</div>
      <h1 class="title">${cur ? html`Edit <em>${cur.name}</em>` : html`Write a <em>rubric</em>`}</h1>
      <p class="lede">Describe one thing a good Vani call does, in the words your auditors use. The judge reads the card on the right exactly as shown and grades every call against it.</p></div>
      <a class="btn ghost" href="#/rubrics">Back to the library</a></div>
    ${state.demo ? html`<div class="note" style="margin-bottom:20px"><b>Demo mode.</b> Saving keeps the rubric in this browser only (localStorage), and "Try it" uses phrase matching instead of the judge model.</div>` : ""}
    <div class="studio">
      <form id="form" novalidate>
        <div class="two">
          <label class="field"><span>Name</span><input class="input" name="name" required maxlength="80" value="${d.name}" placeholder="e.g. No price promises"></label>
          <label class="field"><span>Category</span><select class="select" name="category">${[...CATEGORIES, ...(CATEGORIES.includes(d.category) ? [] : [d.category])].map((c) => html`<option ${c === d.category ? "selected" : ""}>${c}</option>`)}</select></label>
        </div>
        ${field("question", "Question the judge answers", "Did the bot … ?", 2)}
        <div class="two">${field("fatal_when", "Fatal when", "The failure that breaks the lead or the buyer's trust", 3)}${field("non_fatal_when", "Non-fatal when", "A slip worth coaching, not a broken call", 3)}</div>
        <div class="two">${field("applies_when", "Applies when", "Leave empty for every call; otherwise the grade is N/A", 2)}${field("guidance", "Guidance (optional)", "How your auditors decide edge cases", 2)}</div>
        <div class="field"><span>Root type</span><div id="root">${seg(ROOTS.map((r) => [r, r]), d.root_type)}</div><small>Where a failure is fixed: the prompt, speech recognition, catalogue knowledge, the call flow, or the platform.</small></div>
        <div class="two">
          <div class="field"><span>Weight</span><div class="range"><input type="range" name="weight" min="1" max="5" step="1" value="${d.weight}" aria-label="Weight, 1 to 5"><output id="wout">${d.weight}</output></div><small>How much this rubric counts when root causes are ranked.</small></div>
          <div class="field"><span>Needs audio</span><label class="switch"><input type="checkbox" name="needs_audio" ${d.needs_audio ? "checked" : ""}>Only gradable from the recording</label><small>A transcript-only judge answers N/A.</small></div>
        </div>
        <div class="two">
          <div class="field"><span>Passes, example phrases</span><div class="chips-input" data-k="examples_pass"><input placeholder="Type a phrase, press Enter" aria-label="Add a pass example"></div></div>
          <div class="field"><span>Fails, example phrases</span><div class="chips-input" data-k="examples_fail"><input placeholder="Type a phrase, press Enter" aria-label="Add a fail example"></div></div>
        </div>
        <label class="field"><span>Your name</span><input class="input" name="author" value="${d.builtin ? author : d.author || author}" placeholder="Recorded as the author of this version" autocomplete="name"></label>
        <div class="row" style="margin-top:4px">
          <button class="btn primary" type="submit">${cur ? `Save as v${cur.version + 1}` : "Save rubric"}</button>
          ${cur ? html`<button class="btn ${cur.active === false ? "" : "danger"}" type="button" id="archive">${cur.active === false ? "Restore" : "Archive"}</button>` : ""}
          <span class="muted" id="idline" style="font-size:12px"></span>
        </div>
      </form>
      <div class="preview">
        <div id="card"></div>
        <div class="card try"><div class="card-head"><h3>Try it on a call</h3><span class="eyebrow">POST /api/rubrics/test</span></div>
          ${calls.items.length ? html`<div class="row" style="flex-wrap:nowrap"><select class="select" id="call" aria-label="Call to grade">${calls.items.map((c) => html`<option value="${c.call_id}">${c.call_id} · ${(c.summary || "").slice(0, 46)}</option>`)}</select>
            <button class="btn" id="run" type="button">Grade</button></div><div id="verdict"></div>`
          : html`<p class="muted" style="font-size:12.5px">${hiddenReal ? "No fixture or replay calls to try it on." : html`No transcripts to try it on yet. Run <code>make listen</code>.`}</p>`}
          ${hiddenReal ? html`<p class="muted" style="font-size:12px;margin-top:8px">${int(hiddenReal)} real customer call${hiddenReal === 1 ? " is" : "s are"} hidden: grading one sends its transcript to the model provider. Start the platform with <code>LOOP_ALLOW_REAL=1</code> once that is approved.</p>` : ""}</div>
        ${cur ? html`<div class="card"><div class="card-head"><h3>Version history</h3><span class="eyebrow">${cur.active === false ? "archived" : "active"}</span></div>
          <div class="history">${(history || [cur]).slice().reverse().map((r) => html`<div class="h"><b>v${r.version}</b><span>${r.author || "—"}${r.version === cur.version ? " · current" : ""}</span><span class="muted">${r.active === false ? "archived" : ""}</span></div>`)}
          ${!history && cur.version > 1 ? html`<p class="muted" style="font-size:12px;padding-top:8px">Versions 1 to ${cur.version - 1} are kept on the platform; audits cite the version they used.</p>` : ""}</div></div>` : ""}
      </div>
    </div>`);

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
  const preview = () => {
    read();
    $("#wout", el).textContent = d.weight;
    const clash = isNew && d.id && taken.has(d.id);
    $("#idline", el).innerHTML = d.id ? `id <code>${esc(d.id)}</code>${clash ? ' <span style="color:var(--harm)">· taken, rename it</span>' : ""}` : "";
    const ph = (s, p) => (s ? html`<dd>${s}</dd>` : html`<dd class="ph">${p}</dd>`);
    mount($("#card", el), html`<div class="judge-card" aria-live="polite">
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
  // chip inputs for example phrases
  for (const box of $$(".chips-input", el)) {
    const k = box.dataset.k, input = $("input", box);
    const draw = () => {
      $$(".xchip", box).forEach((c) => c.remove());
      d[k].forEach((x, i) => {
        const c = document.createElement("span");
        c.className = `xchip ${k === "examples_pass" ? "pass" : "fail"}`;
        c.innerHTML = `<span>${esc(x)}</span><button type="button" aria-label="Remove ${esc(x)}">×</button>`;
        c.querySelector("button").addEventListener("click", () => { d[k].splice(i, 1); draw(); preview(); });
        box.insertBefore(c, input);
      });
    };
    input.addEventListener("keydown", (e) => {
      if ((e.key === "Enter" || e.key === ",") && input.value.trim()) { e.preventDefault(); d[k].push(input.value.trim()); input.value = ""; draw(); preview(); }
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
  form.addEventListener("input", preview);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    read();
    if (!d.name || !d.question) return toast("A rubric needs a name and a question.", "harm");
    if (isNew && (!d.id || taken.has(d.id))) return toast("That name gives an id that already exists; rename it.", "harm");
    if (!d.author) return toast("Add your name: every version records its author.", "harm");
    try { localStorage.setItem("vani-actor", d.author); } catch { /* not remembered */ }
    const { version, builtin, ...body } = d;
    try {
      const saved = await send(isNew ? "POST" : "PUT", isNew ? "/api/rubrics" : `/api/rubrics/${enc(id)}`, { ...body, actor: d.author });
      toast(`Saved ${saved?.name || d.name} as v${saved?.version ?? (cur ? cur.version + 1 : 1)}.${state.demo ? " Demo mode: in this browser only." : ""}`);
      if (isNew) location.hash = `#/rubrics/${enc(saved?.id || d.id)}`; else window.dispatchEvent(new Event("vani:rerender"));
    } catch (err) { toast(`Could not save: ${err.message}`, "harm"); }
  });
  $("#archive", el)?.addEventListener("click", async () => {
    try {
      if (cur.active === false) await send("PUT", `/api/rubrics/${enc(id)}`, { ...cur, version: undefined, builtin: undefined, active: true, actor: d.author || author });
      else await send("DELETE", `/api/rubrics/${enc(id)}?actor=${enc(read().author || author || "web")}`);
      toast(cur.active === false ? "Restored." : "Archived: the judge stops using it; past verdicts keep citing it.");
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
      mount(out, html`<div class="verdict"><div class="row between">${grade(v.grade, "boxed")}<span class="mono muted" style="font-size:11px">confidence ${(v.confidence ?? 0).toFixed(2)}</span></div>
        <p style="font-size:13px">${v.reason}</p>
        ${(v.evidence || []).map((e) => html`<blockquote>“${e.quote}”<small>line ${e.turn + 1}</small></blockquote>`)}
        <a class="btn sm" href="#/calls/${enc(cid)}" style="justify-self:start">Open the call</a></div>`);
    } catch (err) { mount(out, html`<p style="color:var(--harm);font-size:13px">${err.message}</p>`); }
    btn.disabled = false;
  });
  preview();
}
