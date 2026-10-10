// #/calls: every call with a transcript, fetched once, then filtered, sorted worst first and paged
// here. #/calls/<id>: why the judge graded the call so, the recording, the transcript with the
// judge's margin notes, and the scorecard beside IndiaMART's human QA grades.
import { get, state } from "../api.js";
import { $, $$, html, raw, esc, mount, int, pct, clock, mmss, cap, grade, flagChips, FLAGS, GRADE, empty, enc, toast, reducedMotion, kappaWord } from "../ui.js";
import { decodePeaks, drawWave } from "../viz.js";

const PAGE = 25;
const LANG = { hi: "Hindi", en: "English", te: "Telugu", mixed: "Hinglish" };
const SOURCE = { production: "Production", audited: "Audited", replay: "Replay", fixture: "Fixture", live: "Live" };
const SEV = { fatal: 2, non_fatal: 1, pass: 0 };
const ACTION = "action";  // pseudo-flag: any flag but low confidence (the ones a QA lead acts on)
const sev = (g) => SEV[g] ?? -1;
const differs = (c) => Boolean(c.overall && c.human_overall && c.overall !== c.human_overall);
const agrees = (c) => Boolean(c.overall && c.human_overall && c.overall === c.human_overall);
const weight = (c) => 3 * c.n_fatal + c.n_non_fatal;
const smooth = () => (reducedMotion() ? "auto" : "smooth");

/** "openrouter:google/gemini-2.5-flash" -> "Gemini 2.5 Flash"; the raw id stays under Details. */
export const modelName = (s) => (!s ? "—" : s === "fixture" ? "test fixture"
  : String(s).split(/[/:]/).pop().split("-").map((w) => (/^\d/.test(w) ? w : cap(w))).join(" "));

/** The command behind an empty state, folded away for engineers. */
export const forEngineers = (cmd) => html`<details class="eng"><summary>For engineers</summary><code class="cmd">${cmd}</code></details>`;

// ------------------------------------------------------------- where Back and Prev / Next lead
const MEMO = "vani-list-memo";
const readMemo = () => { try { return JSON.parse(sessionStorage.getItem(MEMO)); } catch { return null; } };
/** As the reader opens a call from a list: the list to return to, its scroll, the order Prev / Next walks. */
export function remember(ids, label) {
  try { sessionStorage.setItem(MEMO, JSON.stringify({ hash: location.hash, y: Math.round(scrollY), ids, label })); } catch { /* no Prev / Next */ }
}
/** Back on the list the reader left: put them where they were, once. */
export function restoreScroll() {
  const m = readMemo();
  if (!m || m.hash !== location.hash || m.y == null) return;
  scrollTo({ top: m.y });
  try { sessionStorage.setItem(MEMO, JSON.stringify({ ...m, y: null })); } catch { /* fine */ }
}

// ---------------------------------------------------------------------------------- the list
const KEYS = ["q", "source", "overall", "human", "vs", "flag", "disp"];
const TESTS = {
  q: (c, q) => `${c.call_id} ${c.summary || ""} ${c.disposition || ""}`.toLowerCase().includes(q.toLowerCase()),
  source: (c, v) => c.source === v,
  overall: (c, v) => c.overall === v,
  human: (c, v) => (v === "none" ? !c.human_overall : c.human_overall === v),
  vs: (c, v) => (v === "disagree" ? differs(c) : agrees(c)),
  flag: (c, v) => (v === ACTION ? c.risk_flags.some((x) => x !== "low_confidence") : c.risk_flags.includes(v)),
  disp: (c, v) => (c.disposition || "") === v,
};
const keep = (c, f, skip) => KEYS.every((k) => k === skip || !f[k] || TESTS[k](c, f[k]));
const SORTS = {  // one sensible direction each; ties fall back to the call id
  worst: ["Worst first", (a, b) => sev(b.overall) - sev(a.overall) || differs(b) - differs(a) || weight(b) - weight(a)],
  qa: ["QA grade, worst first", (a, b) => sev(b.human_overall) - sev(a.human_overall) || sev(b.overall) - sev(a.overall)],
  disagree: ["Judge and QA disagree first", (a, b) => differs(b) - differs(a) || sev(b.overall) - sev(a.overall) || weight(b) - weight(a)],
  params: ["Most failed parameters", (a, b) => weight(b) - weight(a) || sev(b.overall) - sev(a.overall)],
  length: ["Longest first", (a, b) => (b.duration_s ?? -1) - (a.duration_s ?? -1)],
  id: ["Call id", () => 0],
};
const LABELS = { source: "Source", overall: "Judge grade", human: "QA grade", vs: "Judge vs QA", flag: "Flag", disp: "Disposition" };
const GRADES = [["", "Any"], ["fatal", "Fatal"], ["non_fatal", "Non-fatal"], ["pass", "Pass"]];
const SEGS = { source: [["", "All"], ["production", "Production"], ["audited", "Audited"], ["replay", "Replay"], ["live", "Live"]], overall: GRADES };
const REPLAY_NOTE = "Simulated replay conversations are not listed here: open Audit and pick the candidate prompt version.";

function readParams(params) {
  const f = Object.fromEntries(KEYS.map((k) => [k, params.get(k) || ""]));
  f.sort = SORTS[params.get("sort")] ? params.get("sort") : "worst";
  f.page = Math.max(1, parseInt(params.get("page"), 10) || 1);
  return f;
}
function hashOf(f) {
  const qs = new URLSearchParams(KEYS.filter((k) => f[k]).map((k) => [k, f[k]]));
  if (f.sort !== "worst") qs.set("sort", f.sort);
  if (f.page > 1) qs.set("page", String(f.page));
  const s = qs.toString();
  return `#/calls${s ? `?${s}` : ""}`;
}

const paramsLine = (c) => `${[c.n_fatal && `${c.n_fatal} fatal`, c.n_non_fatal && `${c.n_non_fatal} non-fatal`].filter(Boolean).join(" · ")} parameter${c.n_fatal + c.n_non_fatal === 1 ? "" : "s"}`;
const vsMark = (c) => (differs(c) ? html`<span class="agree n">✕ Differ</span>` : agrees(c) ? html`<span class="agree y">✓ Same</span>` : "");
const callRow = (c) => html`<tr class="link" data-id="${c.call_id}">
  <td class="c-id"><a href="#/calls/${enc(c.call_id)}">${c.call_id}</a><div class="sub">${SOURCE[c.source] || c.source}${c.duration_s != null ? ` · ${mmss(c.duration_s)}` : ""}${c.language && c.language !== "hi" ? ` · ${LANG[c.language] || c.language}` : ""}</div></td>
  <td class="c-judge"><span class="lbl">Judge</span>${c.overall ? grade(c.overall) : html`<span class="muted">not judged</span>`}${c.n_fatal + c.n_non_fatal ? html`<div class="sub">${paramsLine(c)}</div>` : ""}</td>
  <td class="c-qa${c.human_overall ? "" : " none"}"><span class="lbl">QA</span>${c.human_overall ? grade(c.human_overall) : html`<span class="muted">not audited</span>`}</td>
  <td class="c-vs">${vsMark(c)}</td>
  <td class="c-sum"><div class="sum">${c.summary || ""}</div>${c.disposition ? html`<span class="disp">${c.disposition}</span>` : ""}${c.risk_flags.length ? html`<div class="flags">${flagChips(c.risk_flags)}</div>` : ""}</td></tr>`;
const sortTh = (f, key, label) => html`<th aria-sort="${f.sort === key ? (key === "id" ? "ascending" : "descending") : "none"}"><button type="button" class="th-sort" data-sort="${key}" title="Sort: ${SORTS[key][0]}">${label}<span aria-hidden="true">${f.sort === key ? " ↓" : ""}</span></button></th>`;
const pager = (p, n) => html`<nav class="pager" aria-label="Pages"><button type="button" class="btn sm" data-page="${p - 1}" ${p <= 1 ? raw("disabled") : ""}>‹ Previous</button>
  <span>Page ${p} of ${n}</span><button type="button" class="btn sm" data-page="${p + 1}" ${p >= n ? raw("disabled") : ""}>Next ›</button></nav>`;

const MORE = ["human", "vs", "flag", "disp"];  // the selects; folded behind "More filters" on a phone
function facetsHTML(f, items, count, moreOpen) {
  const seg = (key) => {
    const n = count(key);
    return html`<div class="fgroup"><span class="flbl" id="fl-${key}">${LABELS[key]}</span><div class="seg" role="group" aria-labelledby="fl-${key}">${SEGS[key].map(([v, l]) => {
      const k = n(v), on = f[key] === v, dead = !k && !on;
      return html`<button type="button" data-f="${key}" data-v="${v}" aria-pressed="${on ? "true" : "false"}" ${dead ? raw("disabled") : ""} ${dead && v === "replay" ? raw(`title="${esc(REPLAY_NOTE)}"`) : ""}>${l}<span class="n">${int(k)}</span></button>`;
    })}</div></div>`;
  };
  const sel = (key, opts) => {
    const n = count(key);
    return html`<label class="fsel"><span class="flbl">${LABELS[key]}</span><select class="select" data-f="${key}">${opts.map(([v, l]) =>
      html`<option value="${v}" ${f[key] === v ? raw("selected") : ""}>${l}${v ? ` · ${int(n(v))}` : ""}</option>`)}</select></label>`;
  };
  const disps = [...new Set(items.map((c) => c.disposition).filter(Boolean))].sort();
  const on = MORE.filter((k) => f[k]).length, open = moreOpen;  // folded filters still apply: the button counts them
  return html`${seg("source")}${seg("overall")}<button type="button" class="btn sm more-f" data-more aria-expanded="${open ? "true" : "false"}" aria-controls="fselects">${open ? "Fewer filters" : "More filters"}${on ? ` · ${on} on` : ""}</button>
    <div class="fselects${open ? " open" : ""}" id="fselects">
    ${sel("human", [...GRADES, ["none", "Not audited"]])}${sel("vs", [["", "Any"], ["disagree", "Disagree"], ["agree", "Agree"]])}
    ${sel("flag", [["", "Any"], [ACTION, "Any but low confidence"], ...Object.entries(FLAGS)])}${sel("disp", [["", "Any"], ...disps.map((d) => [d, d])])}
    ${KEYS.some((k) => f[k]) ? html`<button type="button" class="btn sm ghost" data-clear>Clear filters</button>` : ""}</div>`;
}

export async function renderList(el, { params }) {
  const f = readParams(params), kp = state.overview?.kpis;
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Listen</div><h1 class="title">Calls</h1>
      <p class="lede">Every call with a transcript, worst first: the judge's grade and, where IndiaMART's QA team audited the call, their grade beside it.${kp?.agreement_overall != null
        ? html` On the overall grade the judge matches QA on ${pct(kp.agreement_overall, 0)} of ${kp.agreement_n != null ? int(kp.agreement_n) : "the"} ${kp.agreement_held_out ? "held-out " : ""}audited calls${kp.agreement_kappa != null && kp.agreement_kappa < 0.4 ? `, ${kappaWord(kp.agreement_kappa)}: a call takes its worst check's grade, so read the Judge column as a first pass and open a call for its check-by-check grades` : ""} (<a href="#/audit">where they agree</a>).` : ""}</p>
      <p class="calls-start">Start with the <a href="#/calls?overall=fatal">fatal calls</a>, the <a href="#/calls?vs=disagree">calls where the judge and QA disagree</a> or the <a href="#/calls?flag=human_request">callers who asked for a human</a>.</p></div></div>
    <div class="calls-tools">
      <label class="search"><span class="sr-only">Search calls</span><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.6"/><path d="M10.5 10.5L14 14"/></svg>
        <input class="input" id="q" type="search" placeholder="Search call id, summary or disposition" value="${f.q}" autocomplete="off"></label>
      <div class="facets" id="facets"></div>
    </div>
    <div id="list"></div>`);

  const host = $("#list", el), facets = $("#facets", el), q = $("#q", el);
  let items = null, total = 0, ids = [], t = 0, alive = true, moreOpen = MORE.some((k) => f[k]);
  const load = async () => {
    mount(host, html`<div class="skeleton" aria-busy="true"><i style="width:24%"></i><i></i><i></i><i style="width:70%"></i></div>`);
    try {
      ({ items, total } = await get("/api/calls?limit=5000"));  // ponytail: one fetch, filtered here; page on the server past 5,000
      return alive;
    } catch (err) {
      if (alive) mount(host, empty({ title: "Calls could not load", body: err.message, glyph: "lock", compact: true, action: html`<button type="button" class="btn sm" data-retry>Try again</button>` }));
      return false;
    }
  };
  const sig = (n) => (n && n !== q && el.contains(n) && n.matches("button, select") ? `${n.tagName}|${Object.entries(n.dataset).filter(([k]) => k !== "page").join("|")}` : null);
  const draw = () => {
    if (!items || !el.isConnected) return;  // still loading, or the reader already left for another page
    const focus = sig(document.activeElement);  // re-rendering must not drop a keyboard user's place
    const count = (key) => { const base = items.filter((c) => keep(c, f, key)); return (v) => (v ? base.filter((c) => TESTS[key](c, v)).length : base.length); };
    mount(facets, facetsHTML(f, items, count, moreOpen));
    const rows = items.filter((c) => keep(c, f)).sort((a, b) => SORTS[f.sort][1](a, b) || a.call_id.localeCompare(b.call_id));
    ids = rows.map((c) => c.call_id);
    const pages = Math.max(1, Math.ceil(rows.length / PAGE));
    f.page = Math.min(f.page, pages);
    history.replaceState(null, "", hashOf(f));
    const from = (f.page - 1) * PAGE, shown = rows.slice(from, from + PAGE);
    if (!rows.length) {
      mount(host, items.length
        ? empty({ title: "No calls match these filters", body: f.source === "replay" ? REPLAY_NOTE : "Loosen a filter or change the search.", compact: true, glyph: "wave", action: html`<button type="button" class="btn sm" data-clear>Clear filters</button>` })
        : html`${empty({ title: "No transcripts yet", body: "Listen turns the call recordings into transcripts the judge can read; they appear here.", glyph: "wave" })}${forEngineers("make listen")}`);
    } else {
      mount(host, html`<div class="calls-bar"><p class="calls-count" tabindex="-1"><b>${int(rows.length)}</b> ${rows.length === 1 ? "call" : "calls"}${KEYS.some((k) => f[k]) ? " match" : ""} · showing ${int(from + 1)}–${int(from + shown.length)}${total > items.length ? ` · searching the first ${int(items.length)} of ${int(total)}` : ""}</p>
          <label class="fsel"><span class="flbl">Sort</span><select class="select" data-sort-sel>${Object.entries(SORTS).map(([k, [l]]) => html`<option value="${k}" ${k === f.sort ? raw("selected") : ""}>${l}</option>`)}</select></label>
          ${pages > 1 ? pager(f.page, pages) : ""}</div>
        <div class="card calls-card"><table class="table calls-table">
          <thead><tr>${sortTh(f, "id", "Call")}${sortTh(f, "worst", "Judge")}${sortTh(f, "qa", "IndiaMART QA")}${sortTh(f, "disagree", "Judge vs QA")}<th>What happened</th></tr></thead>
          <tbody>${shown.map(callRow)}</tbody></table></div>
        ${pages > 1 ? html`<div class="calls-foot">${pager(f.page, pages)}</div>` : ""}`);
    }
    if (focus) $$("button, select", el).find((n) => sig(n) === focus)?.focus();
  };
  const redraw = (patch) => { Object.assign(f, { page: 1 }, patch); draw(); };

  el.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (b?.dataset.f) return redraw({ [b.dataset.f]: b.dataset.v });
    if (b?.dataset.sort) return redraw({ sort: b.dataset.sort });
    if (b?.dataset.page) {
      f.page = Number(b.dataset.page);
      draw();
      const top = host.getBoundingClientRect().top;
      if (top < 0) scrollTo({ top: scrollY + top - 72 });
      return $(".calls-count", host)?.focus({ preventScroll: true });
    }
    if (b && "more" in b.dataset) { moreOpen = b.getAttribute("aria-expanded") !== "true"; return draw(); }  // folding keeps the filters; Clear filters clears
    if (b && "clear" in b.dataset) { q.value = ""; return redraw(Object.fromEntries(KEYS.map((k) => [k, ""]))); }
    if (b && "retry" in b.dataset) return load().then((ok) => ok && draw());
    const tr = e.target.closest("tr[data-id]");
    if (!tr || b) return;
    remember(ids, "Back to the list");
    if (!e.target.closest("a")) location.hash = `#/calls/${enc(tr.dataset.id)}`;
  });
  el.addEventListener("change", (e) => {
    const s = e.target.closest("select");
    if (s?.dataset.f) redraw({ [s.dataset.f]: s.value });
    else if (s && "sortSel" in s.dataset) redraw({ sort: s.value });
  });
  q.addEventListener("input", () => { clearTimeout(t); t = setTimeout(() => redraw({ q: q.value.trim() }), 200); });
  if (await load()) { draw(); restoreScroll(); }
  return () => { alive = false; clearTimeout(t); };
}

// ------------------------------------------------------------------------------------- one call
const SRC_LONG = { production: "Production call", audited: "Audited by IndiaMART QA", replay: "Simulated replay conversation", fixture: "Test fixture", live: "Live call" };
const VENDOR = { SQUADSTACK: "SquadStack", SARVAM: "Sarvam" };
const META_KEY = { bot: "Bot", hotlead_type: "Lead type" };
const spaced = (s) => String(s).replaceAll("_", " ");
const RANK = { fatal: 0, non_fatal: 1, pass: 2, na: 3 };
const byRank = (x, y) => (RANK[x] ?? 4) - (RANK[y] ?? 4);

function marked(text, quotes) {  // wrap each verdict's quote in <mark data-v data-g>, skipping overlaps
  const spans = [];
  for (const { q, v, g } of quotes) {
    let at = text.indexOf(q);
    if (at < 0) at = text.toLowerCase().indexOf(q.toLowerCase());
    if (at >= 0 && q) spans.push([at, at + q.length, v, g]);
  }
  spans.sort((a, b) => a[0] - b[0]);
  let out = "", i = 0;
  for (const [s, e, v, g] of spans) {
    if (s < i) continue;
    out += esc(text.slice(i, s)) + `<mark data-v="${v}" data-g="${esc(g)}">${esc(text.slice(s, e))}</mark>`;
    i = e;
  }
  return raw(out + esc(text.slice(i)));
}

function missing(el, id) {
  const m = readMemo(), back = m?.hash?.startsWith("#/calls") ? m.hash : "#/calls";
  return mount(el, html`<div class="call-head"><div class="eyebrow">Call</div></div>
    ${empty({ title: `Call ${id} isn't in this dataset`, glyph: "wave", action: html`<a class="btn sm" href="${back}">Back to calls</a>`,
      body: state.demo ? "The app is showing invented demo data, which holds only the sample calls." : "The id may be mistyped, or this call was never transcribed on this machine." })}`);
}

export async function renderCall(el, { id }) {
  let d;
  try { d = await get(`/api/calls/${enc(id)}`); } catch (err) {
    if (err.status !== 404) throw err;
    return missing(el, id);
  }
  const t = d.transcript, a = d.audit, h = d.human, meta = d.rubrics || {};
  const turns = t.turns || [], sim = t.source === "replay";
  const verdicts = (a?.verdicts || []).map((v, i) => ({ ...v, i }));
  const noted = verdicts.filter((v) => v.evidence?.length && v.grade !== "na").sort((x, y) => byRank(x.grade, y.grade));
  const notesAt = {}, quotesAt = {}, firstTurn = {};
  for (const v of noted) {
    const first = Math.min(...v.evidence.map((e) => e.turn));
    if (!(first >= 0 && first < turns.length)) continue;
    firstTurn[v.i] = first;
    (notesAt[first] ||= []).push(v);
    for (const e of v.evidence) (quotesAt[e.turn] ||= []).push({ q: e.quote, v: v.i, g: v.grade });
  }
  const passNotes = noted.filter((v) => v.grade === "pass" && firstTurn[v.i] != null).length;
  const name = (rid) => meta[rid]?.name || rid;
  const agree = h && a ? h.overall === a.overall : null;
  const rows = [...new Set([...verdicts.map((v) => v.rubric_id), ...Object.keys(h?.grades || {})])]
    .map((rid) => ({ rid, v: verdicts.find((x) => x.rubric_id === rid), hg: h?.grades?.[rid], remark: h?.remarks?.[rid] }))
    .sort((x, y) => byRank(x.v?.grade, y.v?.grade) || byRank(x.hg, y.hg) || name(x.rid).localeCompare(name(y.rid)));
  const applies = (r) => (r.v && r.v.grade !== "na") || (r.hg && r.hg !== "na");
  const shown = rows.filter(applies), notApplied = rows.filter((r) => !applies(r));
  const failing = verdicts.filter((v) => v.grade === "fatal" || v.grade === "non_fatal").sort((x, y) => byRank(x.grade, y.grade));
  const naSide = (r) => (r.v.grade === "na" ? 2 : r.hg === "na" ? 1 : 0);  // real grade clashes before "did it apply" ones
  const qaDiff = h ? rows.filter((r) => r.v && r.hg && r.v.grade !== r.hg && !meta[r.rid]?.needs_audio).sort((x, y) => naSide(x) - naSide(y)) : [];
  const tEnd = turns.reduce((mx, x) => Math.max(mx, x.end_s ?? x.start_s ?? 0), 0);
  const total = t.duration_s || tEnd || 0;
  const m = readMemo(), at = m?.ids?.indexOf(t.call_id) ?? -1;
  const prev = at > 0 ? m.ids[at - 1] : null, next = at >= 0 ? m.ids[at + 1] : null;
  const step = (cid, label) => (cid ? html`<a class="btn sm" href="#/calls/${enc(cid)}">${label}</a>` : html`<button type="button" class="btn sm" disabled>${label}</button>`);
  const CAP = 4;
  const more = (n) => (n > CAP ? html`<button type="button" class="why-chip more" data-goto="scorecard">+${n - CAP} more in the scorecard</button>` : "");

  mount(el, html`
    ${at >= 0 ? html`<nav class="queue" aria-label="Your list"><a class="btn sm ghost" href="${m.hash}">← ${m.label || "Back"}</a>
      <span class="muted">${int(at + 1)} of ${int(m.ids.length)}</span>${step(prev, "‹ Previous")}${step(next, "Next ›")}</nav>` : ""}
    <div class="call-head">
      <div class="eyebrow">${SRC_LONG[t.source] || t.source}${t.vendor ? ` · ${VENDOR[t.vendor] || t.vendor}` : ""}${t.language ? ` · ${LANG[t.language] || t.language}` : ""} · ${t.call_id}</div>
      <h1 class="title${a?.summary ? " sum" : ""}">${a?.summary || a?.disposition || (sim ? "Simulated conversation" : "Call")}</h1>
      <div class="meta">${t.duration_s || tEnd ? html`<span id="call-len">${t.duration_s ? html`<b>${mmss(t.duration_s)}</b> recording` : html`<b>${mmss(tEnd)}</b> of transcript`}</span>` : ""}
        ${a?.disposition ? html`<span>Disposition <b>${a.disposition}</b></span>` : ""}
        <span>Prompt <b class="mono">${t.prompt_version}</b>${t.variant ? ` · variant ${t.variant}` : ""}</span>
        ${t.provider === "fixture" ? html`<span>Transcript <b>${state.demo ? "invented for the demo" : "a test fixture"}</b></span>`
          : t.provider ? html`<span>${sim ? "Buyer simulated by" : "Transcribed by"} <b>${modelName(t.provider)}</b></span>` : ""}${a ? html`<span>Judged by <b>${modelName(a.model)}</b></span>` : ""}
        ${Object.entries(t.meta || {}).filter(([k]) => !SIM_META.test(k)).map(([k, v]) => html`<span>${META_KEY[k] || cap(spaced(k))} <b>${META_KEY[k] ? cap(spaced(v)) : v}</b></span>`)}
        <details class="more"><summary>Details</summary><dl>
          <dt>Transcriber</dt><dd class="mono">${t.provider || "—"}</dd>${a ? html`<dt>Judge model</dt><dd class="mono">${a.model}</dd><dt>Rubric set</dt><dd class="mono">${a.rubric_set_hash}</dd>${a.cost_usd != null ? html`<dt>Judging cost</dt><dd class="mono">$${a.cost_usd.toFixed(3)}</dd>` : ""}` : ""}
        </dl></details></div>
      <div class="verdict-pair">
        <span class="vbox">Judge ${a ? grade(a.overall) : html`<span>not judged</span>`}</span>
        <span class="vbox">IndiaMART QA ${h ? grade(h.overall) : html`<span>${sim ? "not a real call" : "not audited"}</span>`}</span>
        ${agree != null ? html`<span class="agree ${agree ? "y" : "n"}">${agree ? "✓ Judge and QA agree on the overall grade" : "✕ Judge and QA disagree on the overall grade"}</span>` : ""}
        ${flagChips(a?.risk_flags || [])}
      </div>
      ${a ? html`<div class="why-strip"><span class="flbl">Why ${GRADE[a.overall] || "this grade"}</span>${failing.length
        ? html`${failing.slice(0, CAP).map((v) => html`<button type="button" class="why-chip" data-goto="${v.i}">${grade(v.grade)}<span>${name(v.rubric_id)}</span>${firstTurn[v.i] != null ? html`<small>turn ${firstTurn[v.i] + 1}</small>` : ""}</button>`)}${more(failing.length)}`
        : html`<span class="muted">No parameter failed.</span>`}</div>` : ""}
      ${qaDiff.length ? html`<div class="why-strip qa"><span class="flbl">QA disagrees on</span><button type="button" class="why-chip qa-all" data-goto="scorecard">${qaDiff.length} parameter${qaDiff.length === 1 ? "" : "s"}<small>see the scorecard</small></button>${qaDiff.slice(0, CAP).map((r) => html`<button type="button" class="why-chip" data-row="${r.rid}"><span>${name(r.rid)}</span><small>judge ${GRADE[r.v.grade]}, QA ${GRADE[r.hg] || r.hg}</small></button>`)}${more(qaDiff.length)}</div>` : ""}
    </div>
    ${sim ? simNote(t) : ""}

    <div class="listen">
      <div class="player-dock"><div class="card flush" id="player"></div></div>
      <section class="section" id="transcript">
        <div class="section-head"><h2>Transcript, <em>as ${sim ? "simulated" : "recorded"}</em></h2><div class="row"><p>${turns.length} turns · the judge's notes sit beside the turn they quote</p>
          ${passNotes ? html`<button type="button" class="btn sm ghost" id="show-pass" aria-pressed="false">Show ${passNotes} pass note${passNotes === 1 ? "" : "s"}</button>` : ""}</div></div>
        ${turns.length ? html`<div class="record" id="record">${turns.map((tu, k) => html`
          <div class="turn ${tu.speaker}" data-k="${k}" data-s="${tu.start_s ?? ""}">
            <span class="ln">${String(k + 1).padStart(2, "0")}</span>
            ${tu.start_s != null ? html`<button class="ts" type="button" data-seek="${tu.start_s}" aria-label="Play from ${clock(tu.start_s)}">${clock(tu.start_s)}</button>` : html`<span class="ts">—</span>`}
            <span class="sp">${tu.speaker}</span>
            <div class="tx">${marked(tu.text, quotesAt[k] || [])}${tu.confidence != null && tu.confidence < 0.7 ? html`<span class="conf" title="Speech recognition confidence">conf ${tu.confidence.toFixed(2)}</span>` : ""}</div>
            <div class="notes-col${(notesAt[k] || []).every((v) => v.grade === "pass") ? " pass-only" : ""}">${(notesAt[k] || []).map((v) => html`<div class="mnote g-${v.grade}" tabindex="0" data-v="${v.i}">
              <div class="h">${name(v.rubric_id)} ${grade(v.grade)}</div>${v.reason}</div>`)}</div>
          </div>`)}</div>` : empty({ title: "No turns in this transcript", body: "The recording may be silent or the transcriber returned nothing.", compact: true, glyph: "wave" })}
      </section>
    </div>

    <section class="section" id="scorecard">
      <div class="section-head"><h2>Scorecard</h2><p>${a ? `${shown.length} of ${rows.length} rubrics applied to this call${h ? " · IndiaMART QA's grades beside the judge's" : ""}` : "Not judged yet"}</p></div>
      ${rows.length ? html`<div class="card score-card"><table class="table scorecard${h ? "" : " no-qa"}">
        <thead><tr><th>Rubric</th><th>Judge</th>${h ? html`<th>IndiaMART QA</th><th>Agree?</th>` : ""}<th>Why</th></tr></thead>
        <tbody>${shown.map((r) => scoreRow(r, { h, name, meta, firstTurn }))}</tbody>
        ${notApplied.length ? html`<tbody id="na-rows" hidden>${notApplied.map((r) => scoreRow(r, { h, name, meta, firstTurn }))}</tbody>` : ""}
      </table>${notApplied.length ? html`<button type="button" class="na-toggle" aria-expanded="false" aria-controls="na-rows">Show the ${notApplied.length} rubric${notApplied.length === 1 ? "" : "s"} that did not apply</button>` : ""}</div>`
        : html`${empty({ title: "Not judged yet", body: "The judge grades every call with every active rubric; the scorecard appears here.", compact: true, glyph: "doc" })}${forEngineers("make judge")}`}
    </section>`);

  if (at >= 0 && m.hash.startsWith("#/calls")) $('#crumbs a[href="#/calls"]')?.setAttribute("href", m.hash);  // the crumb keeps the filters
  const record = $("#record", el);
  const flash = (n) => { n.classList.remove("flash"); void n.offsetWidth; n.classList.add("flash"); setTimeout(() => n.classList.remove("flash"), 1700); };
  const show = (n) => { if (!n) return; n.scrollIntoView({ block: "center", behavior: smooth() }); flash(n); };
  const hl = (v, on) => {
    $$(`mark[data-v="${v}"]`, el).forEach((x) => x.classList.toggle("on", on));
    $(`.mnote[data-v="${v}"]`, el)?.classList.toggle("hover", on);
  };
  $$(".mnote", el).forEach((n) => {
    for (const [ev, on] of [["mouseenter", true], ["mouseleave", false], ["focus", true], ["blur", false]]) n.addEventListener(ev, () => hl(n.dataset.v, on));
  });
  $$("mark[data-v]", el).forEach((x) => {
    x.addEventListener("mouseenter", () => hl(x.dataset.v, true));
    x.addEventListener("mouseleave", () => hl(x.dataset.v, false));
    x.addEventListener("click", () => { const n = $(`.mnote[data-v="${x.dataset.v}"]`, el); if (n?.offsetParent) n.scrollIntoView({ block: "nearest", behavior: smooth() }); });
  });
  $("#show-pass", el)?.addEventListener("click", (e) => {
    const on = record.classList.toggle("show-pass");
    e.currentTarget.setAttribute("aria-pressed", String(on));
    e.currentTarget.textContent = on ? "Hide pass notes" : `Show ${passNotes} pass note${passNotes === 1 ? "" : "s"}`;
  });
  $(".na-toggle", el)?.addEventListener("click", (e) => {
    const body = $("#na-rows", el), open = body.hidden;
    body.hidden = !open;
    e.currentTarget.setAttribute("aria-expanded", String(open));
    e.currentTarget.textContent = open ? "Hide the rubrics that did not apply" : `Show the ${notApplied.length} rubric${notApplied.length === 1 ? "" : "s"} that did not apply`;
  });
  el.addEventListener("click", (e) => {
    const b = e.target.closest("[data-goto], [data-row], [data-turn]");
    if (!b) return;
    const g = b.dataset.goto, r = b.dataset.row ?? (g != null && firstTurn[g] == null ? verdicts[g]?.rubric_id : null);
    if (b.dataset.turn != null) show($(`.turn[data-k="${b.dataset.turn}"]`, el));
    else if (g === "scorecard") show($("#scorecard", el));
    else if (r) { const row = $(`tr[data-rid="${r}"]`, el); if (row?.parentElement.hidden) $(".na-toggle", el)?.click(); show(row); }
    else if (g != null) { show($(`.mnote[data-v="${g}"]`, el)); hl(g, true); setTimeout(() => hl(g, false), 1700); }
  });
  return mountPlayer($("#player", el), el, { url: d.audio_url, turns, total, tEnd, sim, vendor: t.vendor });
}

const SIM_META = /^(sample|replay_of|fix_id|persona_.*)$/;  // shown in the simulation note, not the meta line
function simNote(t) {
  const mt = t.meta || {}, persona = Object.entries(mt).filter(([k]) => k.startsWith("persona_") && k !== "persona_need");
  return html`<div class="note sim-note"><div><b>Simulated conversation.</b> The replay test had a simulated buyer talk to ${/^v\d+$/.test(t.prompt_version) ? `prompt ${t.prompt_version}` : "the candidate prompt"}${mt.replay_of ? html`, replaying real call <a href="#/calls/${enc(mt.replay_of)}">${mt.replay_of}</a>` : ""}${mt.fix_id ? html` to test fix <a href="#/fixes/${enc(mt.fix_id)}">${mt.fix_id}</a>` : ""}. No real customer was on this call, and there is no recording.
    ${persona.length ? html`<div class="persona">The simulated buyer: ${persona.map(([k, v]) => html`<span>${spaced(k.slice(8))} <b>${v}</b></span>`)}</div>` : ""}
    ${mt.persona_need ? html`<details class="brief"><summary>The buyer's brief</summary><p>${mt.persona_need}</p></details>` : ""}</div></div>`;
}

function scoreRow({ rid, v, hg, remark }, { h, name, meta, firstTurn }) {
  const turn = v ? firstTurn[v.i] : null;
  return html`<tr data-rid="${rid}">
    <td class="sc-name"><div>${name(rid)}</div><div class="sub">${meta[rid]?.category || ""}</div></td>
    <td class="sc-judge" title="${v?.confidence != null ? `Judge's confidence ${v.confidence.toFixed(2)}` : ""}"><span class="lbl">Judge</span>${v ? grade(v.grade) : html`<span class="muted">—</span>`}</td>
    ${h ? html`<td class="sc-qa"><span class="lbl">QA</span>${hg ? grade(hg) : html`<span class="muted">—</span>`}</td>
      <td class="sc-agree">${v && hg ? (meta[rid]?.needs_audio ? html`<span class="muted" title="Heard, not read: the judge cannot grade this from a transcript, so there is nothing to compare">—</span>`
        : v.grade === hg ? html`<span class="agree y">✓ Yes</span>` : html`<span class="agree n">✕ No</span>`) : ""}</td>` : ""}
    <td class="sc-why">${v?.reason || ""}${remark ? html`<div class="qa-remark"><b>QA:</b> ${remark}</div>` : ""}${turn != null ? html`<button type="button" class="link-btn" data-turn="${turn}">Turn ${turn + 1} →</button>` : ""}</td></tr>`;
}

// ---------------------------------------------------------------------------------- the player
const PLAY = '<svg viewBox="0 0 16 16"><path d="M4 2.5l9 5.5-9 5.5z"/></svg>';
const PAUSE = '<svg viewBox="0 0 16 16"><path d="M4 3h3v10H4zM9 3h3v10H9z"/></svg>';

function mountPlayer(host, page, { url, turns, total, tEnd, sim, vendor }) {
  const laneHTML = (len) => (len ? turns.filter((x) => x.start_s != null).map((x) => {  // clamped: a turn past the recording's end draws nothing
    const left = Math.min(100, (100 * x.start_s) / len), w = Math.min(100 - left, Math.max(0.3, (100 * ((x.end_s ?? x.start_s + 1) - x.start_s)) / len));
    return `<span class="${esc(x.speaker)}" style="left:${left.toFixed(3)}%;width:${w.toFixed(3)}%" title="${esc(x.speaker)} ${clock(x.start_s)}"></span>`;
  }).join("") : "");
  const legend = html`<span class="legend"><span><i class="lane-bot" style="background:var(--accent)"></i>Bot</span><span><i style="background:var(--accent-2)"></i>Buyer</span></span>`;
  const noAudio = (title, body) => mount(host, html`<div class="player"><button class="play" disabled aria-label="No recording">${raw(PLAY)}</button>
    <div class="wave-area no-audio"><div class="wave-top"><span>${title}</span>${legend}<span>${total ? mmss(total) : ""}</span></div>
      <div class="flat"></div><div class="lane">${raw(laneHTML(total))}</div><p>${body}</p></div></div>`);
  if (!url) {
    noAudio("No recording", sim ? "A simulated conversation has no audio: the replay test wrote it turn by turn."
      : state.demo ? "Invented sample calls have no recordings, except sample-0001's synthetic placeholder."
        : `No recording of this call on this machine${/sarvam/i.test(vendor || "") ? " (Sarvam recordings are not downloadable)" : ""}.`);
    return () => {};
  }
  mount(host, html`<div class="player"><button class="play" id="play" type="button" aria-label="Play">${raw(PLAY)}</button>
    <div class="wave-area"><div class="wave-top"><span class="now" id="now">0:00</span>${legend}<span id="len">${mmss(total)}</span></div>
      <div class="wave-box" id="wave" role="slider" tabindex="0" aria-label="Seek in the recording" aria-valuemin="0" aria-valuemax="${Math.round(total)}" aria-valuenow="0"><canvas></canvas><span class="hover-t" hidden></span></div>
      <div class="lane" id="lane">${raw(laneHTML(total))}</div>
      <p class="approx" id="approx" hidden></p>
      ${state.demo ? html`<p class="muted" style="font-size:11.5px">Synthetic placeholder audio for an invented call: a murmur timed to the transcript, not speech.</p>` : ""}</div></div>`);
  const mini = document.createElement("div");  // on <body>: the page wrapper animates a transform, which would trap position:fixed
  mini.className = "mini-player";
  mini.hidden = true;
  mini.innerHTML = `<button type="button" class="mini-play" aria-label="Play">${PLAY}</button><span class="mini-t">0:00</span><span class="mini-bar" title="Seek"><i></i></span>`;
  document.body.append(mini);
  const audio = new Audio();
  audio.preload = "auto";
  audio.src = url;
  const btn = $("#play", host), wave = $("#wave", host), canvas = $("canvas", wave), tip = $(".hover-t", wave);
  const miniBtn = $(".mini-play", mini), miniT = $(".mini-t", mini), miniBar = $(".mini-bar", mini), miniFill = $("i", miniBar);
  let peaks = new Float32Array(400).fill(0.04), raf = 0, hover = null, alive = true, cur = -1, started = false, away = false;
  const length = () => (Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : total || 1);
  const rows = $$(".turn", page), record = $("#record", page);
  const dockBottom = () => Math.max(56, host.getBoundingClientRect().bottom + 8);
  const inView = (n) => { if (!n) return false; const r = n.getBoundingClientRect(); return r.bottom > dockBottom() && r.top < innerHeight; };
  const syncMini = () => { mini.hidden = !(away && started); };
  const draw = () => {
    if (!alive) return;
    const p = Math.min(1, audio.currentTime / length());
    drawWave(canvas, peaks, p, hover);
    $("#now", host).textContent = mmss(audio.currentTime);
    miniT.textContent = `${mmss(audio.currentTime)} / ${mmss(length())}`;
    miniFill.style.width = `${(p * 100).toFixed(2)}%`;
    wave.setAttribute("aria-valuenow", String(Math.round(audio.currentTime)));
    wave.setAttribute("aria-valuetext", `${mmss(audio.currentTime)} of ${mmss(length())}`);
    let k = -1;
    for (let i = 0; i < turns.length; i++) if (turns[i].start_s != null && turns[i].start_s <= audio.currentTime + 0.05) k = i;
    if (k !== cur) {
      const was = rows[cur];
      was?.classList.remove("current");
      if (k >= 0 && (audio.currentTime > 0 || !audio.paused)) {
        rows[k]?.classList.add("current");
        // follow along only while the reader does: the turn that was playing is still on screen
        if (!audio.paused && (cur < 0 ? inView(rows[k]) || inView(record) : inView(was))) {
          const r = rows[k]?.getBoundingClientRect(), top = dockBottom();
          if (r && (r.top < top || r.bottom > innerHeight - 20)) scrollBy({ top: r.top - (top + innerHeight) / 2 + r.height / 2, behavior: smooth() });
        }
      }
      cur = k;
    }
  };
  const loop = () => { draw(); if (!audio.paused) raf = requestAnimationFrame(loop); };
  const icon = (playing) => {
    for (const b of [btn, miniBtn]) { b.innerHTML = playing ? PAUSE : PLAY; b.setAttribute("aria-label", playing ? "Pause" : "Play"); }
  };
  const toggle = () => (audio.paused ? audio.play().catch((e) => toast(`Playback failed: ${e.message}`, "harm")) : audio.pause());
  btn.addEventListener("click", toggle);
  miniBtn.addEventListener("click", toggle);
  audio.addEventListener("play", () => { started = true; syncMini(); icon(true); cancelAnimationFrame(raf); loop(); });
  audio.addEventListener("pause", () => { icon(false); draw(); });
  audio.addEventListener("seeked", draw);
  audio.addEventListener("loadedmetadata", () => {
    const len = length();
    $("#len", host).textContent = mmss(len);
    wave.setAttribute("aria-valuemax", String(Math.round(len)));
    $("#lane", host).innerHTML = laneHTML(len);  // the lane on the recording's own clock, like the waveform
    const meta = $("#call-len", page);
    if (meta) meta.innerHTML = `<b>${mmss(len)}</b> recording`;
    if (tEnd && Math.abs(tEnd - len) / len > 0.1) {
      const n = $("#approx", host);
      n.hidden = false;
      n.textContent = `Turn times are approximate: the transcript ends at ${mmss(tEnd)}, the recording runs ${mmss(len)}.`;
    }
    draw();
  });
  audio.addEventListener("error", () => {
    if (!alive) return;
    dispose();  // the controls are about to go: stop everything that draws into them
    noAudio("Recording unavailable", "The recording could not be loaded: the file is missing on this machine.");
  });
  const seekTo = (clientX, box = wave) => { const r = box.getBoundingClientRect(); audio.currentTime = Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * length(); draw(); };
  let dragging = false;
  wave.addEventListener("pointerdown", (e) => { dragging = true; wave.setPointerCapture(e.pointerId); seekTo(e.clientX); });
  wave.addEventListener("pointermove", (e) => {
    const r = wave.getBoundingClientRect(), x = e.clientX - r.left;
    hover = x; tip.hidden = false; tip.style.left = `${x}px`; tip.textContent = mmss((x / r.width) * length());
    if (dragging) seekTo(e.clientX); else if (audio.paused) draw();
  });
  wave.addEventListener("pointerup", () => { dragging = false; });
  wave.addEventListener("pointerleave", () => { hover = null; tip.hidden = true; if (audio.paused) draw(); });
  miniBar.addEventListener("click", (e) => seekTo(e.clientX, miniBar));
  wave.addEventListener("keydown", (e) => {
    const s = { ArrowLeft: -5, ArrowRight: 5 }[e.key];
    if (s) { audio.currentTime = Math.max(0, Math.min(length(), audio.currentTime + s)); e.preventDefault(); }
    else if (e.key === "Home") { audio.currentTime = 0; e.preventDefault(); }
    else if (e.key === "End") { audio.currentTime = length(); e.preventDefault(); }
    else if (e.key === " " || e.key === "Enter") { toggle(); e.preventDefault(); }
  });
  page.addEventListener("click", (e) => {
    const b = e.target.closest("[data-seek]");
    if (!b) return;
    audio.currentTime = Math.min(Number(b.dataset.seek), length());
    if (audio.paused) audio.play().catch(() => {});
  });
  const io = new IntersectionObserver(([e]) => { away = !e.isIntersecting; syncMini(); }, { rootMargin: "-56px 0px 0px 0px" });
  io.observe(host);
  const ro = new ResizeObserver(() => draw());
  ro.observe(wave);
  const onTheme = () => draw();
  window.addEventListener("vani:theme", onTheme);
  decodePeaks(url).then((r) => { peaks = r.peaks; draw(); }).catch(() => {
    if (!alive) return;
    const note = document.createElement("p");
    note.className = "muted";
    note.style.fontSize = "11.5px";
    note.textContent = "Waveform unavailable for this recording (it plays from its original link); the lane below shows who spoke when.";
    $(".wave-area", host)?.append(note);
  });
  draw();
  function dispose() { alive = false; cancelAnimationFrame(raf); ro.disconnect(); io.disconnect(); mini.remove(); window.removeEventListener("vani:theme", onTheme); }
  return () => { dispose(); audio.pause(); audio.removeAttribute("src"); audio.load(); };
}
