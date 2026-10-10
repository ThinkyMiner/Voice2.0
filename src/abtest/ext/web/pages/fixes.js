// #/fixes and #/fixes/<id>: the prompt fix as a legal redline (Prompt A collapsed, deletions struck,
// insertions underlined, a margin note per edit), where the fix stands in the loop, the safety gate and the
// simulated replay.
import { get, text, state } from "../api.js";
import { html, raw, esc, mount, int, pct, spp, fix, words, short, when, stageName, stateChip, typeChip, callLink, empty, enc } from "../ui.js";

// Prompt variables sit between BOMs ("﻿ bot_name ﻿", "﻿@ call_step﻿"): each becomes one marked
// word (so the word diff keeps it whole) and the stray space before punctuation goes; tok() then draws it as a token.
const clean = (s) => String(s ?? "").replace(/﻿\s*(@?)\s*([\w.]+)\s*﻿(?: (?=[,.;:!?]))?/g, "⁣$1$2⁣").replace(/﻿/g, "");
const tok = (h) => h.replace(/⁣(@?)([\w.]+)⁣/g, (_, at, name) => `<span class="tok">${at ? `@${name}` : `{${name}}`}</span>`);
const prose = (s) => raw(tok(esc(clean(s))));
const SHORT = 40;  // words: an unchanged stretch of Prompt A this short is shown as context, not collapsed

// ------------------------------------------------------------------------- where a fix stands
const AB_FAIL = { STOPPED_HARM: "stopped for harm", BLOCKED_GUARDRAIL: "blocked by a guardrail", INCONCLUSIVE: "inconclusive", HALTED_SRM: "halted: traffic split fault" };
const waitingOn = (c) => Boolean(c?.notes?.length && c.notes[c.notes.length - 1].startsWith("waiting"));

/** Drafted, Gate, Replay, A/B, Approval: each step done, fail, now (running or waiting) or "" (not reached).
 *  `cycle` is the loop cycle carrying this fix (Cycle.fix_id); a failed try drops out of its cycle. */
export function fixSteps(f, cycle) {
  const c = cycle?.fix_id === f.id ? cycle : null, at = c?.stage, out = c?.outcome, g = f.gate, rp = f.replay;
  const wait = waitingOn(c) ? "waiting for a go-ahead" : null;
  const step = (label, st, note) => ({ label, state: st, note });
  const better = rp && rp.fail_rate_b < rp.fail_rate_a;
  const gate = g ? step("Gate", g.passed ? "done" : "fail", g.passed ? "passed" : "blocked")
    : step("Gate", at === "gate" ? "now" : "", at === "gate" ? wait || "running next" : "not run");
  const replay = rp ? step("Replay", better ? "done" : "fail", better ? "B fails less" : rp.fail_rate_b > rp.fail_rate_a ? "B fails more" : "no gain")
    : g?.passed && at === "replay" ? step("Replay", "now", wait || "waiting to run") : step("Replay", "", "not run");
  const ab = !c?.experiment_id ? step("A/B", "", "not started") : at === "prove" ? step("A/B", "now", "running, simulated")
    : AB_FAIL[out] ? step("A/B", "fail", AB_FAIL[out]) : step("A/B", "done", "B won");
  const ok = at === "approval" ? step("Approval", "now", "waiting for a person") : out === "PROMOTED" ? step("Approval", "done", "promoted")
    : out === "REJECTED" ? step("Approval", "fail", "rejected") : step("Approval", "", "");
  return [step("Drafted", "done", ""), gate, replay, ab, ok];
}

/** The steps as a compact tracker: Drafted, Gate passed, Replay waiting to run, A/B not started, Approval. */
export const fixTrack = (steps, cls = "") => html`<ol class="ftrack ${cls}" aria-label="Where this fix stands">${steps.map((s) =>
  html`<li class="${s.state}"><i aria-hidden="true"></i><b>${s.label}</b>${s.state && s.note ? html`<span>${s.note}</span>` : html`<span class="sr-only">${s.note || "not reached"}</span>`}</li>`)}</ol>`;

/** The latest one or two steps reached, as a phrase: "Gate passed · Replay waiting to run". */
export const fixPhrase = (steps) => steps.slice(1).filter((s) => s.state).slice(-2).map((s) => `${s.label} ${s.note}`).join(" · ") || "drafted, not gated yet";

function replayShort(f, steps, cycle) {
  const rp = f.replay;
  if (!rp) return steps[2].state === "now" ? html`<span class="now">${steps[2].note}${cycle ? ` (cycle ${cycle.n})` : ""}</span>` : html`<span class="muted">${f.gate && !f.gate.passed ? "not run: gate blocked" : "not run yet"}</span>`;
  const d = rp.fail_rate_b - rp.fail_rate_a, cls = d < 0 ? "win" : d > 0 ? "harm" : "";
  return html`<span class="mono">fail rate ${pct(rp.fail_rate_a, 0)} → <b class="${cls}">${pct(rp.fail_rate_b, 0)}</b></span> <span class="${cls}">${d < 0 ? "better" : d > 0 ? "worse" : "no gain"}</span>`;
}

/** "Cycle 01 is at Judge, waiting for a go-ahead to send real call content to the model provider." */
export function cycleAt(c) {
  const note = c.notes?.length ? c.notes[c.notes.length - 1] : "";
  const why = note.startsWith("waiting") ? ", waiting for a go-ahead to send real call content to the model provider" : note.startsWith("blocked") ? `: ${note.replace(/^blocked:\s*/, "")}` : "";
  return `Cycle ${c.n} is at ${stageName(c.stage)}${why}.`;
}

function noFixes(c) {
  return empty({
    title: "No fixes yet", glyph: "doc", action: html`<a class="btn sm" href="#/">Open the Loop page</a>`,
    body: c ? `The fixer writes Prompt B at the Gate stage, for the top untried prompt or flow root cause. ${cycleAt(c)}`
      : "The loop has not started. Advancing it judges the calls, ranks the root causes on the Issues page, then writes the first fix here.",
  });
}

export async function renderList(el) {
  const [fx, pr, ov] = await Promise.all([get("/api/fixes"), get("/api/prompts").catch(() => ({ items: [] })), get("/api/overview").catch(() => state.overview)]);
  const bases = [...new Set(fx.items.map((f) => f.base_version))];
  const iss = await Promise.allSettled(bases.map((v) => get(`/api/issues?version=${enc(v)}`)));
  const rc = Object.fromEntries(iss.flatMap((r) => (r.status === "fulfilled" ? r.value.items.map((i) => [i.id, i]) : [])));
  const cand = Object.fromEntries(pr.items.filter((p) => p.fix_id).map((p) => [p.fix_id, p]));
  const cycles = ov?.cycles || [];
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Improve</div><h1 class="title">Prompt <em>fixes</em></h1>
      <p class="lede">Each fix is a small set of edits to Prompt A aimed at one root cause. Before any traffic it must clear the safety gate and fail less than Prompt A in a simulated replay; then a simulated A/B test runs and a person approves it.</p></div></div>
    ${fx.items.length ? html`<div class="card flist">${fx.items.map((f) => {
      const r = rc[f.root_cause_id], p = cand[f.id], c = cycles.find((x) => x.fix_id === f.id) || null, st = fixSteps(f, c), ts = f.created_ts || p?.created_ts;
      return html`<a class="frow" href="#/fixes/${enc(f.id)}">
        <div class="fmain"><div class="nm">${r?.name || f.root_cause_id}</div><p class="why">${f.rationale}</p>
          <div class="fmeta">${r ? typeChip(r.type) : ""}<span>${f.base_version} → ${p?.version || "Prompt B"}</span>${ts ? html`<span>drafted ${when(ts)}</span>` : ""}${c ? html`<span>cycle ${c.n}</span>` : ""}<span class="mono">${f.id}</span>${p && p.status !== "candidate" ? html`<span>Prompt B ${stateChip(p.status)}</span>` : ""}</div></div>
        <div class="fside">${fixTrack(st)}<div class="rp"><span class="eyebrow">Replay, simulated</span>${replayShort(f, st, c)}</div></div></a>`;
    })}</div>` : noFixes(cycles[0])}`);
}

// ------------------------------------------------------------------------------- the redline
function parseDiff(diff) {
  const hunks = [];
  let h = null;
  const lines = String(diff || "").split("\n");
  if (lines[lines.length - 1] === "") lines.pop();
  for (const line of lines) {
    const m = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
    if (m) { h = { a: +m[1], an: m[2] == null ? 1 : +m[2], b: +m[3], lines: [] }; hunks.push(h); continue; }
    if (!h) continue;  // file headers before the first hunk
    if (line === "") h.lines.push([" ", ""]);  // an empty context line whose leading space was stripped
    else if (" -+".includes(line[0])) h.lines.push([line[0], line.slice(1)]);  // skips "\ No newline" and anything unknown
  }
  return hunks;
}

export function wordDiff(a, b) {  // LCS over words and spaces: equal runs plain, deletions struck, insertions underlined
  const A = clean(a).split(/(\s+)/), B = clean(b).split(/(\s+)/);
  const n = A.length, m = B.length;
  if (n * m > 4e6) return html`<del>${prose(a)}</del> <ins>${prose(b)}</ins>`;
  const L = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) L[i][j] = A[i] === B[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const out = [];
  let i = 0, j = 0;
  const push = (op, s) => { const last = out[out.length - 1]; if (last && last[0] === op) last[1] += s; else out.push([op, s]); };
  while (i < n && j < m) {
    if (A[i] === B[j]) { push("=", A[i]); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) push("-", A[i++]);
    else push("+", B[j++]);
  }
  while (i < n) push("-", A[i++]);
  while (j < m) push("+", B[j++]);
  // A redline reads as "struck old words, then the new words": merge a change run whose pieces are
  // separated only by matching spaces into one deletion followed by one insertion.
  let s = "";
  for (let k = 0; k < out.length;) {
    if (out[k][0] === "=") { s += esc(out[k][1]); k++; continue; }
    let del = "", ins = "";
    while (k < out.length && (out[k][0] !== "=" || (/^\s+$/.test(out[k][1]) && k + 1 < out.length && out[k + 1][0] !== "="))) {
      const [op, t] = out[k++];
      if (op !== "+") del += t;
      if (op !== "-") ins += t;
    }
    const d = del.trim(), i = ins.trim(), ref = ins.trim() ? ins : del;  // keep the run's outer spacing, as Prompt B reads
    s += /^\s*/.exec(ref)[0] + (d ? `<del>${esc(d)}</del>` : "") + (d && i ? " " : "") + (i ? `<ins>${esc(i)}</ins>` : "") + /\s*$/.exec(ref)[0];
  }
  return raw(tok(s));
}

function hunkBody(h) {
  const paras = [];
  let aLine = h.a, k = 0;
  const L = h.lines;
  while (k < L.length) {
    if (L[k][0] === " ") { paras.push({ n: aLine++, cls: "ctx", body: prose(L[k][1]) }); k++; continue; }
    const del = [], ins = [];
    while (k < L.length && L[k][0] === "-") del.push(L[k++][1]);
    while (k < L.length && L[k][0] === "+") ins.push(L[k++][1]);
    const pairs = Math.min(del.length, ins.length);
    for (let i = 0; i < Math.max(del.length, ins.length); i++) {
      if (i < pairs) paras.push({ n: aLine++, body: wordDiff(del[i], ins[i]) });
      else if (i < del.length) paras.push({ n: aLine++, body: html`<del>${prose(del[i])}</del>` });
      else paras.push({ n: null, body: html`<ins>${prose(ins[i])}</ins>` });
    }
  }
  return paras;
}

function editsByHunk(hunks, edits) {
  const at = hunks.map(() => []);
  const side = (h, sign) => clean(h.lines.filter((l) => l[0] === sign).map((l) => l[1]).join("\n"));
  edits.forEach((e, i) => {
    const plus = clean(e.mode === "append" ? e.text : e.replace).slice(0, 60), minus = clean(e.find).slice(0, 60);
    let k = plus ? hunks.findIndex((h) => side(h, "+").includes(plus)) : -1;
    if (k < 0 && minus) k = hunks.findIndex((h) => side(h, "-").includes(minus));
    at[k >= 0 ? k : Math.min(i, hunks.length - 1)]?.push(i);
  });
  return at;
}

function replayEmpty(f, st, cycle, rc, real) {
  const g = f.gate;
  if (st[2].state === "now") {  // real calls: the web never sends them to the model, so the replay needs the CLI go-ahead
    return empty({ title: waitingOn(cycle) ? "Waiting for a go-ahead" : "Waiting to run", compact: true, glyph: "flask",
      body: `Cycle ${cycle.n} is at the Replay stage. A few of the ${rc ? `${int(rc.n_calls)} ` : ""}calls behind this cause (fatal ones first) become simulated buyers; each plays once against Prompt A and once against Prompt B, and the judge grades both.`,
      action: html`${real ? html`<p class="fix-go">The buyers are built from real calls, so their content goes to the model provider: the replay runs only after a go-ahead on the platform machine, <code>make loop ARGS=--yes-send-real-transcripts</code>.</p>` : ""}<a class="btn sm" href="#/">See the loop</a>` });
  }
  if (g && !g.passed) return empty({ title: "No replay", body: "The safety gate blocked this fix, so it was never replayed.", compact: true, glyph: "flask" });
  if (!g) return empty({ title: "No replay yet", body: "The replay runs once the safety gate passes.", compact: true, glyph: "flask" });
  return empty({ title: "Not replayed", body: "The gate passed, but the loop has not replayed this fix.", compact: true, glyph: "flask" });
}

export async function renderFix(el, { id }) {
  const [got, pr, ov, rb] = await Promise.all([get(`/api/fixes/${enc(id)}`).catch((err) => err), get("/api/prompts").catch(() => ({ items: [] })),
    get("/api/overview").catch(() => state.overview), get("/api/rubrics").catch(() => ({ items: [] }))]);
  if (got instanceof Error) {
    if (got.status !== 404) throw got;
    return mount(el, empty({ title: `No fix called ${id}`, glyph: "doc", body: "It is not among the loop's fixes: the address may be mistyped, or the fix was written on another machine.",
      action: html`<a class="btn" href="#/fixes">All fixes</a>` }));
  }
  const { fix: f, root_cause: rc } = got;
  const baseText = await text(`/api/prompts/${enc(f.base_version)}/text`).catch(() => null);
  const cand = pr.items.find((p) => p.fix_id === f.id);
  const cycle = ov?.cycles?.find((c) => c.fix_id === f.id) || null;
  const st = fixSteps(f, cycle);
  const names = Object.fromEntries(rb.items.map((r) => [r.id, r.name]));
  const rn = (rid) => names[rid] || rid;
  const ts = f.created_ts || cand?.created_ts;
  const ex = (rc?.example_call_ids || []).slice(0, 3);
  const hunks = parseDiff(f.diff);
  const notes = editsByHunk(hunks, f.edits);
  const aLines = baseText != null ? baseText.split("\n") : null;
  const totalLines = aLines ? aLines.length - (aLines[aLines.length - 1] === "" ? 1 : 0) : null;
  const stretch = (from, to) => {  // an unchanged 1-based range of Prompt A: shown as context when short, else one collapsed bar
    if (to < from) return { lines: [], bar: "" };
    if (!aLines) return { lines: [], bar: html`<div class="collapsed">… ${int(to - from + 1)} lines unchanged …</div>` };
    const ls = aLines.slice(from - 1, to), w = words(ls.join(" "));
    if (w > SHORT) return { lines: [], bar: html`<div class="collapsed">… ${int(w)} words unchanged …</div>` };
    return { lines: ls.map((t, j) => ({ n: from + j, cls: "ctx", body: prose(t), blank: !t.trim() })).filter((p) => !p.blank), bar: "" };
  };
  const blocks = hunks.map((h, k) => ({ h, pre: stretch(k === 0 ? 1 : hunks[k - 1].a + hunks[k - 1].an, h.a - 1), post: { lines: [], bar: "" } }));
  if (blocks.length && totalLines) { const last = hunks[hunks.length - 1]; blocks[blocks.length - 1].post = stretch(last.a + last.an, totalLines); }
  const g = f.gate, rp = f.replay;
  const delta = rp ? rp.fail_rate_b - rp.fail_rate_a : null;
  const thresholds = g ? [...new Set(g.checks.map((c) => c.threshold))] : [];
  const one = thresholds.length === 1 ? thresholds[0] : null;  // one line for every check: say it once, not per row
  const noReasons = g && g.checks.every((c) => !c.reason);

  mount(el, html`
    <div class="page-head"><div class="fix-head">
      <div class="eyebrow">Prompt fix · ${f.base_version} → ${cand?.version || "Prompt B"}${cycle ? html` · <a href="#/">cycle ${cycle.n}</a>` : ""}</div>
      <h1 class="title">Fix: <em>${rc?.name || f.root_cause_id}</em></h1>
      <p class="lede">${f.rationale}</p>
      ${fixTrack(st, "lg")}
      <div class="row fix-links">
        ${ex.length ? html`<span class="muted">Listen to ${ex.length === 1 ? "the example call" : `${ex.length} example calls`}</span>${ex.map(callLink)}` : ""}
        ${cand ? html`<a class="btn sm" href="#/prompts?v=${enc(cand.version)}">Open Prompt B (${cand.version})</a>` : ""}
        ${cycle?.experiment_id ? html`<a class="btn sm" href="#/experiments/${enc(cycle.experiment_id)}">See the A/B test</a>` : ""}
        ${rc ? html`<a class="btn sm ghost" href="#/issues?version=${enc(rc.prompt_version)}&rc=${enc(rc.id)}">The root cause on Issues</a>` : ""}
      </div>
      <p class="fix-fine">${int(f.edits.length)} edit${f.edits.length === 1 ? "" : "s"} · written by <span class="mono">${f.model}</span>${ts ? ` · drafted ${when(ts)}` : ""} · Prompt B <span class="mono">${short(f.prompt_b_sha256, 10)}</span> · <span class="mono">${f.id}</span>${cand && cand.status !== "candidate" ? html` · ${stateChip(cand.status)}` : ""}</p>
    </div></div>

    <section class="redline" aria-label="Redline from Prompt A to Prompt B">
      <div class="redline-head"><span class="eyebrow">Redline · ${f.base_version} to ${cand?.version || "Prompt B"}${baseText ? ` · ${int(words(baseText))} words in Prompt A` : ""}</span>
        <span class="legend"><span><del>struck</del> removed</span><span><ins>underlined</ins> added</span><span><span class="tok">{name}</span> a prompt variable</span></span></div>
      ${blocks.length ? blocks.map(({ h, pre, post }, k) => {
        const paras = [...pre.lines, ...hunkBody(h), ...post.lines];
        return html`${pre.bar}
        <div class="hunk">
          <div class="gut">L${paras.find((p) => p.n)?.n ?? h.a}</div>
          <div class="doc">${paras.map((p) => html`<p class="${p.cls || ""}" title="${p.n ? `Prompt A line ${p.n}` : "new line"}">${p.body}</p>`)}</div>
          <aside class="margin">${notes[k].length ? notes[k].map((i) => html`
            <div class="eyebrow accent">Change ${i + 1} of ${f.edits.length} · ${f.edits[i].mode === "append" ? "appended" : "replaced"}</div>
            <div class="why">${f.edits[i].why}</div>`) : html`<div class="eyebrow">Context</div>`}
            ${rc ? html`<div>Targets <a href="#/issues?version=${enc(rc.prompt_version)}&rc=${enc(rc.id)}">${rc.name}</a>: ${int(rc.n_calls)} of ${int(rc.n_audited)} judged calls.</div>` : ""}</aside>
        </div>${post.bar}`;
      }) : empty({ title: "No textual change", body: "This fix's diff is empty.", compact: true })}
      ${blocks.length && !totalLines ? html`<div class="collapsed">… the rest of Prompt A unchanged …</div>` : ""}
    </section>

    <div class="grid cols-2 section fix-cards">
      <div class="card">
        <div class="card-head"><div><div class="eyebrow">Safety gate</div><h3 class="serif" style="margin-top:6px">${g ? (g.passed ? html`Clear to <em>test</em>` : "Blocked") : "Not run yet"}</h3></div>
          ${g ? html`<span class="chip ${g.passed ? "win" : "harm"}"><i></i>${g.passed ? "passed" : "blocked"}</span>` : ""}</div>
        ${g ? html`<p class="muted gate-intro">${int(g.checks.length)} questions about Prompt B, each scored as a risk by <span class="mono">${g.model}</span>.
            ${one != null ? `A blocking check at ${pct(one, 0)} risk or more stops the fix; an advisory one only warns.` : "A blocking check at or above its line stops the fix; an advisory one only warns."}${noReasons ? " The gate model gave no reasons for its scores." : ""}</p>
          ${g.checks.map((c) => {
            const over = c.probability >= c.threshold, level = over ? "High" : c.probability >= 0.4 * c.threshold ? "Medium" : "Low";
            const fill = over ? (c.blocking ? "var(--harm)" : "var(--warn)") : level === "Medium" ? "var(--warn)" : "var(--win)";
            const ink = over ? (c.blocking ? "var(--harm)" : "var(--warn-ink)") : level === "Medium" ? "var(--warn-ink)" : "var(--win)";
            return html`<div class="gate-row" title="Check ${c.id}"><div class="q">${c.question}${c.reason ? html`<span class="why">${c.reason}</span>` : ""}<small>${c.blocking ? "Blocking" : "Advisory"}${one == null ? ` · stops at ${pct(c.threshold, 0)}` : ""}</small></div>
              <div class="meter" role="img" aria-label="${pct(c.probability, 0)} risk, ${level.toLowerCase()}; ${c.blocking ? "blocks" : "warns"} at ${pct(c.threshold, 0)}"><span class="fill" style="width:${(c.probability * 100).toFixed(1)}%;background:${raw(fill)}"></span><span class="tick" style="left:${(c.threshold * 100).toFixed(1)}%" ${one == null ? raw(`data-label="${esc(pct(c.threshold, 0))}"`) : ""}></span></div>
              <div class="p" style="color:${raw(ink)}">${pct(c.probability, 0)} risk<small>${level}</small></div></div>`;
          })}` : empty({ title: "The gate has not run", body: "The gate model scores each safety question before any replay.", compact: true, glyph: "lock" })}
      </div>
      <div class="card">
        <div class="card-head"><div><div class="sim-label">Simulated conversations</div><h3 class="serif" style="margin-top:6px">Replay, <em>before and after</em></h3></div>
          ${rp ? html`<span class="chip ${delta < 0 ? "win" : delta > 0 ? "harm" : ""}">${spp(delta)}</span>` : ""}</div>
        ${rp ? html`<p class="muted" style="font-size:12.5px;margin-bottom:16px">${int(rp.n_cases)} scenarios built from real calls behind this cause, each played twice by a simulated buyer: once against Prompt A, once against Prompt B, then judged on <b class="ink">${rn(rp.rubric_id)}</b>.${rp.n_cases < 30 ? ` ${int(rp.n_cases)} cases is a small sample: read it as a direction, not a measurement.` : ""}</p>
          <div class="ba">
            ${[["Real calls", "where the scenarios came from", rp.fail_rate_real, "var(--faint)"], ["Simulated A", "Prompt A, the fair before", rp.fail_rate_a, "var(--ink)"], ["Simulated B", "Prompt B, the after", rp.fail_rate_b, "var(--accent)"]].map(([l, s, v, c]) => html`
              <div class="ba-row"><div class="lbl">${l}<small>${s}</small></div><div class="track"><span style="width:${(v * 100).toFixed(1)}%;background:${raw(c)}"></span></div><div class="v">${pct(v, 0)}<small>${int(Math.round(v * rp.n_cases))} of ${int(rp.n_cases)}</small></div></div>`)}
          </div>
          ${Object.keys(rp.regressions || {}).length ? html`<div style="margin-top:18px"><div class="eyebrow" style="margin-bottom:6px">Other rubrics, B minus A (fail rate)</div>
            <div class="row" style="gap:6px">${Object.entries(rp.regressions).map(([r, d]) => html`<span class="chip ${d > 0.0001 ? "harm" : d < -0.0001 ? "win" : ""}" style="text-transform:none;letter-spacing:0">${rn(r)} ${spp(d, 0)}</span>`)}</div></div>` : ""}
          <div style="margin-top:16px"><div class="eyebrow" style="margin-bottom:6px">Cases that changed</div>
            <div class="row" style="gap:6px">${rp.cases.filter((c) => c.grade_a !== c.grade_b).map((c) => html`<span class="row" style="gap:6px">${callLink(c.call_id)}<span class="mono muted" style="font-size:11px">${c.grade_a} → ${c.grade_b}</span></span>`)}
            <span class="muted" style="font-size:12px">${int(rp.cases.filter((c) => c.grade_a === c.grade_b).length)} unchanged</span></div></div>
          <p class="fix-fine" style="margin-top:14px">Bot <span class="mono">${rp.models?.bot}</span> · buyer <span class="mono">${rp.models?.buyer}</span> · judge <span class="mono">${rp.models?.judge}</span> · ${fix(rp.seconds, 0)} s</p>`
        : replayEmpty(f, st, cycle, rc, !pr.items.find((p) => p.version === f.base_version)?.fix_id)}
      </div>
    </div>`);
}
