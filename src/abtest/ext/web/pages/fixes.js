// #/fixes and #/fixes/<id>: the prompt fix as a legal redline (Prompt A collapsed, deletions struck,
// insertions underlined, a margin note per edit), the safety gate, and the simulated replay.
import { get, text } from "../api.js";
import { html, raw, esc, mount, int, pct, spp, fix, words, short, when, stateChip, typeChip, callLink, empty, enc } from "../ui.js";

const clean = (s) => String(s ?? "").replace(/\uFEFF/g, "");

export async function renderList(el) {
  const [fx, pr] = await Promise.all([get("/api/fixes"), get("/api/prompts").catch(() => ({ items: [] }))]);
  const bases = [...new Set(fx.items.map((f) => f.base_version))];
  const iss = await Promise.allSettled(bases.map((v) => get(`/api/issues?version=${enc(v)}`)));
  const rc = Object.fromEntries(iss.flatMap((r) => (r.status === "fulfilled" ? r.value.items.map((i) => [i.id, i]) : [])));
  const cand = Object.fromEntries(pr.items.filter((p) => p.fix_id).map((p) => [p.fix_id, p]));
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Improve</div><h1 class="title">Prompt <em>fixes</em></h1>
      <p class="lede">Each fix is a small set of edits to Prompt A aimed at one root cause. It must clear the safety gate and beat Prompt A in a simulated replay before it reaches traffic.</p></div></div>
    ${fx.items.length ? html`<div class="card" style="padding:4px 22px">${fx.items.map((f) => {
      const r = rc[f.root_cause_id], p = cand[f.id], rp = f.replay;
      return html`<a class="xcard" href="#/fixes/${enc(f.id)}" style="grid-template-columns:minmax(0,1.6fr) minmax(0,1fr) 140px 120px">
        <div><div class="row"><span class="id">${f.id}</span><span class="mono muted" style="font-size:12px">${f.base_version} → ${p?.version || "B"}${f.created_ts ? ` · ${when(f.created_ts)}` : ""}</span>${p ? stateChip(p.status) : ""}</div>
          <div style="font:400 20px/1.25 var(--f-display);margin-top:8px">${f.rationale}</div></div>
        <div><div class="eyebrow">Root cause</div><div style="font-size:13px;margin-top:4px">${r?.name || f.root_cause_id}</div>${r ? html`<div style="margin-top:6px">${typeChip(r.type)}</div>` : ""}</div>
        <div><div class="eyebrow">Gate</div><div style="margin-top:6px">${f.gate ? html`<span class="chip ${f.gate.passed ? "win" : "harm"}"><i></i>${f.gate.passed ? "clear" : "blocked"}</span>` : html`<span class="chip">pending</span>`}</div></div>
        <div><div class="eyebrow">Replay, simulated</div><div class="mono" style="font-size:13px;margin-top:6px">${rp ? html`${pct(rp.fail_rate_a, 0)} → <b style="color:var(--win)">${pct(rp.fail_rate_b, 0)}</b>` : "—"}</div></div></a>`;
    })}</div>` : empty({ title: "No fixes yet", body: "The fixer writes Prompt B for the top root cause once the judge has run.", cmd: "make loop", glyph: "doc" })}`);
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
  if (n * m > 4e6) return html`<del>${a}</del> <ins>${b}</ins>`;
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
  return raw(s);
}

function hunkBody(h) {
  const paras = [];
  let aLine = h.a, k = 0;
  const L = h.lines;
  while (k < L.length) {
    if (L[k][0] === " ") { paras.push({ n: aLine++, cls: "ctx", body: clean(L[k][1]) }); k++; continue; }
    const del = [], ins = [];
    while (k < L.length && L[k][0] === "-") del.push(L[k++][1]);
    while (k < L.length && L[k][0] === "+") ins.push(L[k++][1]);
    const pairs = Math.min(del.length, ins.length);
    for (let i = 0; i < Math.max(del.length, ins.length); i++) {
      if (i < pairs) paras.push({ n: aLine++, body: wordDiff(del[i], ins[i]) });
      else if (i < del.length) paras.push({ n: aLine++, body: html`<del>${clean(del[i])}</del>` });
      else paras.push({ n: null, body: html`<ins>${clean(ins[i])}</ins>` });
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

export async function renderFix(el, { id }) {
  const [{ fix: f, root_cause: rc }, pr, ov] = await Promise.all([get(`/api/fixes/${enc(id)}`), get("/api/prompts").catch(() => ({ items: [] })), get("/api/overview").catch(() => null)]);
  const baseText = await text(`/api/prompts/${enc(f.base_version)}/text`).catch(() => null);
  const cand = pr.items.find((p) => p.fix_id === f.id);
  const cycle = ov?.cycles?.find((c) => c.fix_id === f.id);
  const hunks = parseDiff(f.diff);
  const notes = editsByHunk(hunks, f.edits);
  const aLines = baseText != null ? baseText.split("\n") : null;
  const gap = (from, to) => {  // 1-based inclusive line range of Prompt A left out of the diff
    if (to < from) return "";
    const n = to - from + 1;
    const w = aLines ? words(aLines.slice(from - 1, to).join(" ")) : null;
    return html`<div class="collapsed">… ${w != null ? `${int(w)} words` : `${int(n)} lines`} unchanged …</div>`;
  };
  const totalLines = aLines ? aLines.length - (aLines[aLines.length - 1] === "" ? 1 : 0) : null;
  const g = f.gate, rp = f.replay;
  const delta = rp ? rp.fail_rate_b - rp.fail_rate_a : null;

  mount(el, html`
    <div class="page-head"><div>
      <div class="eyebrow">Fix · ${f.id} · ${f.base_version} → ${cand?.version || "Prompt B"}${cycle ? ` · cycle ${String(cycle.n).padStart(2, "0")}` : ""}</div>
      <h1 class="title">Fix for <em>${rc?.name || f.root_cause_id}</em></h1>
      <p class="lede">${f.rationale}</p>
      <div class="meta" style="margin-top:14px"><span>${f.edits.length} edit${f.edits.length === 1 ? "" : "s"}</span><span>written by <b class="mono">${f.model}</b>${f.created_ts ? ` · ${when(f.created_ts)}` : ""}</span>
        <span>Prompt B <b class="mono">${short(f.prompt_b_sha256, 10)}</b></span>${cand ? html`<span>${stateChip(cand.status)}</span>` : ""}
        ${cycle?.experiment_id ? html`<a class="chip id" href="#/experiments/${enc(cycle.experiment_id)}">${cycle.experiment_id}</a>` : ""}</div></div></div>

    <div class="redline" aria-label="Redline from Prompt A to Prompt B">
      <div class="redline-head"><span class="eyebrow">Redline · ${f.base_version} to ${cand?.version || "Prompt B"}${baseText ? ` · ${int(words(baseText))} words in Prompt A` : ""}</span>
        <span class="legend"><span><del>struck</del> removed</span><span><ins>underlined</ins> added</span></span></div>
      ${hunks.length ? hunks.map((h, k) => html`
        ${k === 0 ? gap(1, h.a - 1) : gap(hunks[k - 1].a + hunks[k - 1].an, h.a - 1)}
        <div class="hunk">
          <div class="gut">L${h.a}</div>
          <div class="doc">${hunkBody(h).map((p) => html`<p class="${p.cls || ""}" title="${p.n ? `Prompt A line ${p.n}` : "new line"}">${p.body}</p>`)}</div>
          <aside class="margin">${notes[k].length ? notes[k].map((i) => html`
            <div class="eyebrow accent">Change ${i + 1} of ${f.edits.length} · ${f.edits[i].mode === "append" ? "appended" : "replaced"}</div>
            <div class="why">${f.edits[i].why}</div>`) : html`<div class="eyebrow">Context</div>`}
            ${rc ? html`<div>Targets <a href="#/issues?version=${enc(rc.prompt_version)}">${rc.name}</a>: ${int(rc.n_calls)} of ${int(rc.n_audited)} audited calls.</div>` : ""}</aside>
        </div>`) : empty({ title: "No textual change", body: "This fix's diff is empty.", compact: true })}
      ${hunks.length ? (totalLines ? gap(hunks[hunks.length - 1].a + hunks[hunks.length - 1].an, totalLines) : html`<div class="collapsed">… the rest of Prompt A unchanged …</div>`) : ""}
    </div>

    <div class="grid cols-2 section" style="margin-top:28px">
      <div class="card">
        <div class="card-head"><div><div class="eyebrow">Safety gate</div><h3 class="serif" style="margin-top:6px">${g ? (g.passed ? html`Clear to <em>test</em>` : "Blocked") : "Not run yet"}</h3></div>
          ${g ? html`<span class="chip ${g.passed ? "win" : "harm"}"><i></i>${g.passed ? "passed" : "blocked"}</span>` : ""}</div>
        ${g ? html`<p class="muted" style="font-size:12.5px;margin-bottom:6px">Each check is a probability from ${g.model}. A blocking check at or above its threshold stops the fix.</p>
          ${g.checks.map((c) => {
            const over = c.probability >= c.threshold, tone = over ? (c.blocking ? "var(--harm)" : "var(--warn)") : "var(--win)";
            return html`<div class="gate-row"><div class="q">${c.question}${c.reason ? html`<span class="why">${c.reason}</span>` : ""}<small>${c.blocking ? "blocking" : "advisory"} · ${c.id}</small></div>
              <div class="meter" role="img" aria-label="p ${fix(c.probability)} against threshold ${fix(c.threshold)}"><span class="fill" style="width:${(c.probability * 100).toFixed(1)}%;background:${raw(tone)}"></span><span class="tick" style="left:${(c.threshold * 100).toFixed(1)}%" data-label="${fix(c.threshold)}"></span></div>
              <div class="p" style="color:${raw(tone)}">p ${fix(c.probability)}</div></div>`;
          })}` : empty({ title: "The gate has not run", body: "The gate model scores each safety question before any replay.", compact: true, glyph: "lock" })}
      </div>
      <div class="card">
        <div class="card-head"><div><div class="sim-label">Simulated conversations</div><h3 class="serif" style="margin-top:6px">Replay, <em>before and after</em></h3></div>
          ${rp ? html`<span class="chip ${delta < 0 ? "win" : delta > 0 ? "harm" : ""}">${spp(delta)}</span>` : ""}</div>
        ${rp ? html`<p class="muted" style="font-size:12.5px;margin-bottom:16px">${int(rp.n_cases)} scenarios built from real calls, each played twice by a simulated buyer: once against Prompt A, once against Prompt B, then judged on <b class="ink">${rp.rubric_id}</b>. ${fix(rp.seconds, 1)} s.</p>
          <div class="ba">
            ${[["Real calls", "where the scenarios came from", rp.fail_rate_real, "var(--faint)"], ["Simulated A", "Prompt A, the fair before", rp.fail_rate_a, "var(--ink)"], ["Simulated B", "Prompt B, the after", rp.fail_rate_b, "var(--accent)"]].map(([l, s, v, c]) => html`
              <div class="ba-row"><div class="lbl">${l}<small>${s}</small></div><div class="track"><span style="width:${(v * 100).toFixed(1)}%;background:${raw(c)}"></span></div><div class="v">${pct(v, 0)}</div></div>`)}
          </div>
          <div style="margin-top:18px"><div class="eyebrow" style="margin-bottom:6px">Other rubrics, B minus A (fail rate)</div>
            <div class="row" style="gap:6px">${Object.entries(rp.regressions || {}).map(([r, d]) => html`<span class="chip ${d > 0.0001 ? "harm" : d < -0.0001 ? "win" : ""}" style="text-transform:none;letter-spacing:0">${r} ${spp(d, 0)}</span>`)}</div></div>
          <div style="margin-top:16px"><div class="eyebrow" style="margin-bottom:6px">Cases that changed</div>
            <div class="row" style="gap:6px">${rp.cases.filter((c) => c.grade_a !== c.grade_b).map((c) => html`<span class="row" style="gap:6px">${callLink(c.call_id)}<span class="mono muted" style="font-size:11px">${c.grade_a} → ${c.grade_b}</span></span>`)}
            <span class="muted" style="font-size:12px">${int(rp.cases.filter((c) => c.grade_a === c.grade_b).length)} unchanged</span></div></div>
          <p class="muted mono" style="font-size:10.5px;margin-top:14px">bot ${rp.models?.bot} · buyer ${rp.models?.buyer} · judge ${rp.models?.judge}</p>`
        : empty({ title: "No replay yet", body: "Replay runs after the gate passes.", compact: true, glyph: "flask" })}
      </div>
    </div>`);
}
