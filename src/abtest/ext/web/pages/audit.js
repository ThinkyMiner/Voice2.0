// #/audit: can the judge be trusted (the checks it agrees with IndiaMART's QA team on, then the overall grade
// beside trivial guesses), the grade split on the same calls, agreement by check (closest first), which parameters fail
// most, and the calls to listen to first.
import { get, state } from "../api.js";
import { $, html, mount, int, pct, fix, shares, stack, gradeStack, gradeLegend, grade, flagChips, FLAGS, GRADE, empty, enc, reducedMotion, kappaWord, goodChecks, audioIds } from "../ui.js";
import { confusion } from "../viz.js";
import { remember, restoreScroll, modelName, forEngineers } from "./calls.js";

const CLS = ["pass", "non_fatal", "fatal", "na"];
const SEV = { fatal: 2, non_fatal: 1, pass: 0 };
const SERIOUS = ["human_request", "frustration", "wrong_requirement"];
const FLAG_ORDER = Object.keys(FLAGS);  // the tiles and every row of chips share one order
const FLAG_NOTE = {  // judge.py's own definitions, shortened
  human_request: "The buyer wants a person or the original seller",
  frustration: "The buyer is annoyed, repeats or hangs up",
  wrong_requirement: "The lead would carry a wrong product, quantity or spec",
  loop: "The bot is stuck repeating itself",
  low_confidence: "Transcript too short or garbled to judge well; usually noise",
};
const band = (k) => [kappaWord(k), k == null ? "" : k < 0.2 ? "harm" : k < 0.6 ? "warn" : "win"];  // the words every page uses for κ
const plural = (n, w) => `${int(n)} ${w}${n === 1 ? "" : "s"}`;
const sum = (o) => Object.values(o || {}).reduce((s, x) => s + (x || 0), 0);

export async function render(el, { params }) {
  const callsP = get("/api/calls?limit=5000").catch(() => null);  // per-call numbers: same-call split, fatal catches
  const pr = await get("/api/prompts").catch(() => ({ items: [], production: null }));
  const versions = pr.items.map((p) => p.version);
  const v = params.get("version") || (versions.includes("v1") ? "v1" : pr.production || "v1");
  const pv = pr.items.find((p) => p.version === v), sim = Boolean(pv?.fix_id);
  const chips = html`<div class="seg" role="group" aria-label="Prompt version">${(versions.length ? pr.items : [{ version: v }]).map((p) =>
    html`<a href="#/audit?version=${enc(p.version)}" aria-current="${p.version === v ? "true" : "false"}">${p.version}${p.version === pr.production ? " · production" : ""}${p.fix_id ? " · simulated" : ""}</a>`)}</div>`;
  const head = (eyebrow, title, lede = "", extra = "") => html`<div class="page-head"><div><div class="eyebrow">${eyebrow}</div><h1 class="title">${title}</h1>${lede ? html`<p class="lede">${lede}</p>` : ""}${extra}</div>${chips}</div>`;
  if (versions.length && !pv) {
    return mount(el, html`${head("Judge", html`There is no prompt <em>${v}</em>`)}
      ${empty({ title: `${v} doesn't exist`, body: `This platform has prompt version${versions.length === 1 ? "" : "s"} ${versions.join(", ")}: pick one above.`, compact: true, glyph: "doc" })}`);
  }
  const src = params.get("source") || "";  // empty: the server's default, the calls the loop judges
  const [a, cl, rb] = await Promise.all([get(`/api/audit?version=${enc(v)}${src ? `&source=${enc(src)}` : ""}`).catch((err) => err), callsP,
    get("/api/rubrics").catch(() => ({ items: [] }))]);
  if (a instanceof Error) {
    if (a.status !== 404) throw a;
    return mount(el, html`${head(`Judge · Prompt ${v}`, html`No audit for <em>${v}</em> yet`)}
      ${empty({ title: "Nothing judged for this prompt", body: "The judge grades every transcript of a prompt version with every active rubric; the results appear here.", glyph: "doc" })}${forEngineers("make judge")}`);
  }
  if (!a.n_audited) {
    return mount(el, html`${head(`Judge · Prompt ${v}`, html`Nothing judged for <em>${v}</em> yet`, `${plural(a.n_calls, "transcript")} on record for ${v}. Once the judge grades them, this page shows whether it can be trusted, which parameters fail and which calls to hear first.`)}
      ${empty({ title: a.n_calls ? "Transcripts are waiting for the judge" : "No transcripts yet", body: a.n_calls ? "The judge has not graded these transcripts yet." : "Listen turns the recordings into transcripts first; then the judge grades them.", glyph: "doc" })}${forEngineers(a.n_calls ? "make judge" : "make listen")}`);
  }
  const mine = sim ? [] : (cl?.items || []).filter((c) => c.prompt_version === v);
  const scope = a.source && a.source !== "all" ? a.source : null;  // older servers send no source: every call
  const scoped = scope ? mine.filter((c) => c.source === scope) : mine;
  const complete = scoped.length > 0 && scoped.length === a.n_calls;  // per-call numbers only when the list holds every call of this audit
  const audited = complete ? scoped.filter((c) => c.human_overall && c.overall) : [];
  const ag = a.agreement, audio = audioIds(rb.items);
  const names = Object.fromEntries([...rb.items.map((r) => [r.id, r.name]), ...(a.by_rubric || []).map((r) => [r.rubric_id, r.name])]);
  const rk = riskyCalls(a.risky || []);
  const inCalls = complete && !sim ? (qs) => `#/calls?${qs}${scope ? `&source=${enc(scope)}` : ""}` : null;  // the same calls, in the list

  mount(el, html`
    ${sim ? head(`Judge · Prompt ${v} · simulated replay`, html`Simulated replay: <em>${int(a.n_audited)}</em> conversation${a.n_audited === 1 ? "" : "s"}`,
        `Not real calls. In the replay test a simulated buyer talked to prompt ${v}, and the judge graded those conversations. Read it as a rough before-and-after against the real calls on ${pv.parent || "the production prompt"}, never as a measure of real customers.`)
      : head(`Judge · Prompt ${v} · real calls`, html`What the judge found in <em>${int(a.n_audited)}</em> calls`,
        "The judge grades every call on IndiaMART's QA checklist, Pass, Non-fatal or Fatal per parameter; a call takes its worst parameter's grade, the QA team's own rule. First check whether the judge can be trusted, then listen to the calls it flags.",
        html`<div class="row head-actions"><button type="button" class="btn sm" data-jump="risky">Calls to listen to ↓</button><a class="btn sm ghost" href="#/issues">Ranked issues →</a></div>`)}
    ${sim ? html`<div class="note sim-note"><b>Simulated conversations.</b> No real customer heard ${v} and IndiaMART QA graded none of these, so there is no agreement to show.</div>`
      : ag ? trust(a, ag, names, audio) : html`<div class="card"><div class="eyebrow">Can the judge be trusted yet?</div><p style="margin-top:10px">No agreement report yet. It compares the judge with IndiaMART's auditors on the calls they audited.</p>${forEngineers("make agreement")}</div>`}
    <div class="grid cols-3 audit-top">${coverage(a, mine, scoped, complete, sim)}${split(a, audited, complete)}</div>
    ${ag?.rows?.length ? byParameter(ag, names, audio) : ""}
    ${failMost(a)}
    ${risky(rk, inCalls)}`);

  el.addEventListener("click", (e) => {
    const jump = e.target.closest("[data-jump]");
    if (jump) return $(`#${jump.dataset.jump}`, el)?.scrollIntoView({ block: "start", behavior: reducedMotion() ? "auto" : "smooth" });
    if (e.target.closest("a.risk")) return remember(rk.top.map((r) => r.call_id), "Back to Audit");
    const tr = e.target.closest("tbody.ag-p tr.link");
    if (!tr) return;
    const b = $(".ag-toggle", tr), d = $(`#${b.getAttribute("aria-controls")}`, el), open = d.hidden;
    d.hidden = !open;
    tr.classList.toggle("open", open);
    b.setAttribute("aria-expanded", String(open));
  });
  restoreScroll();
}

/** Can the judge be trusted: what it gets right first (the checks in good agreement), then its overall grade and
 *  why that lags. The trivial-guess baselines are on the same calls when the report keeps the overall confusion;
 *  an older report has only QA's own overall grade of every call it graded, and the tiles name that base. */
function trust(a, ag, names, audio) {
  const k = ag.overall_kappa, n = ag.n_calls, matches = Math.round(ag.overall_accuracy * n);
  const base = plural(n, ag.held_out ? "held-out call" : "call"), nm = (r) => names[r.rubric_id] || r.rubric_id;
  const { text, good } = goodChecks(ag, audio);
  const fair = text.filter((r) => r.kappa != null && r.kappa >= 0.4 && r.kappa < 0.6).length;
  const deaf = ag.rows.filter((r) => audio.has(r.rubric_id)).map(nm);
  const oc = ag.overall_confusion || {}, same = Object.keys(oc).length > 0;
  const qa = same ? Object.fromEntries(["pass", "non_fatal", "fatal"].map((g) => [g, sum(oc[g])])) : a.human_split, qaN = sum(qa);
  const top = qaN ? ["pass", "non_fatal", "fatal"].reduce((x, y) => (qa[y] > qa[x] ? y : x)) : null;
  const byCheck = k != null && k < 0.4 && good.length > 0;
  const [word, kTone] = band(k), tone = byCheck ? "warn" : kTone;
  const verdict = k == null ? "Agreement can't be measured yet." : k < 0.4 ? "Not reliable yet." : k < 0.6 ? "Use with care." : "Reliable on the overall grade.";
  const held = ag.calibrated ? `Measured on ${plural(n, "audited call")} held out from tuning; the judge's grades were calibrated on ${plural(ag.n_tuning || 0, "other audited call")}.`
    : ag.held_out ? `Measured on ${plural(n, "audited call")} that were not used to tune the rubrics.`
      : "Caution: the rubrics were tuned on these same calls, which flatters the judge.";
  const kRange = good.length ? `Cohen's κ ${fix(good.at(-1).kappa)}${good.length > 1 ? ` to ${fix(good[0].kappa)}` : ""}` : "";
  const chips = good.length ? html`<ul class="trust-checks">${good.map((r) => html`<li class="chip win">${nm(r)}<span class="mono">κ ${fix(r.kappa)}</span></li>`)}</ul>` : "";
  const why = k != null && k < 0.4 ? html`<p class="trust-why"><b>Why not the overall grade yet:</b> a call takes its worst check's grade, so a miss on any one of its ${plural(text.length, "check")} can change it, and QA's grade also covers the lead record the bot saved, which the judge cannot see. On the overall grade it matches QA on ${int(matches)} of ${base}, ${k < 0.2 ? "no better than chance" : `${word} agreement`} (Cohen's κ ${fix(k)}).</p>` : "";
  const qaBase = same ? `on the same ${base}` : `on QA's own grade of all ${plural(qaN, "call")} it graded`;
  return html`<section class="trust t-${tone || "none"}" aria-labelledby="trust-h">
    <div class="trust-say"><div class="eyebrow">Can the judge be trusted yet?</div>
      ${byCheck ? html`<h2 id="trust-h"><b>Reliable on ${good.length} of ${text.length} checks.</b> Not yet on a call's overall grade.</h2>
        <p>On these ${good.length === 1 ? "one" : int(good.length)} its grades agree with IndiaMART QA's well beyond chance (${kRange}; 0 is guessing, 1 is perfect), on the same ${base}:</p>${chips}${why}`
      : html`<h2 id="trust-h"><b>${verdict}</b> It matches IndiaMART QA's overall grade on ${int(matches)} of ${plural(n, "call")}.</h2>
        <p>${k != null ? `${k < 0.2 ? "That is no better than chance" : `Agreement beyond chance is ${word}`} (Cohen's κ ${fix(k)}; 0 is guessing, 1 is perfect). ` : ""}${top ? `Answering “${GRADE[top]}” on every call would match ${pct(qa[top] / qaN)} of QA's grades, ${same ? `on the same ${base}` : `on all ${plural(qaN, "call")} it graded`}.` : ""}</p>
        ${good.length ? html`<p>Check by check, it agrees well (κ 0.6 or more) on ${good.length} of ${text.length}:</p>${chips}` : ""}${why}`}</div>
    <dl class="trust-stats">
      ${text.length ? html`<div><dt>Checks in good agreement</dt><dd>${int(good.length)}<small> of ${int(text.length)}</small></dd><small>κ 0.6 or more${fair ? `; ${fair} more moderate` : ""}, on the ${base}</small></div>` : ""}
      <div><dt>Overall grade matches</dt><dd>${pct(ag.overall_accuracy)}</dd><small>${int(matches)} of ${base} · κ ${fix(k)}</small></div>
      ${top ? html`<div><dt>Always “${GRADE[top]}”</dt><dd>${pct(qa[top] / qaN)}</dd><small>a trivial guess, ${qaBase}</small></div>` : ""}
      <div><dt>Fatal vs not fatal</dt><dd>${pct(ag.fatal_vs_rest_accuracy)}</dd><small>on the ${base}${qaN ? `; always “not fatal” gives ${pct(1 - qa.fatal / qaN)}${same ? "" : ` on QA's ${int(qaN)}`}` : ""}</small></div>
    </dl>
    <p class="trust-foot">${held}${deaf.length ? ` ${deaf.length > 1 ? `${deaf.slice(0, -1).join(", ")} and ${deaf.at(-1)} are` : `${deaf[0]} is`} left out: ${deaf.length > 1 ? "they need" : "it needs"} the audio.` : ""} Judge: ${modelName(ag.model)}.</p>
  </section>`;
}

const SCOPE = { audited: "Audited by QA", production: "Production" };
function coverage(a, mine, scoped, complete, sim) {
  const nA = scoped.filter((c) => c.source === "audited").length, nP = scoped.filter((c) => c.source === "production").length;
  const waiting = a.n_calls - a.n_audited, w = (n) => (100 * n / Math.max(1, a.n_calls)).toFixed(1);
  const corpus = state.overview?.corpus && state.overview.corpus !== "replay" ? state.overview.corpus : "audited";
  const outside = mine.filter((c) => !scoped.includes(c)), outP = outside.filter((c) => c.source === "production").length;
  const both = !sim && new Set(mine.map((c) => c.source)).size > 1;
  return html`<div class="card cov"><div class="eyebrow">${sim ? "Conversations on record" : "Calls in this view"}</div>
    <div class="big md">${int(a.n_audited)}<small>judged</small></div>
    ${complete && nA && nP ? html`<div class="bar tall" role="img" aria-label="${nA} audited, ${nP} production"><span class="s-a" style="flex-basis:${w(nA)}%"></span><span class="s-b" style="flex-basis:${w(nP)}%"></span></div>` : ""}
    <ul class="cov-list">${complete ? html`${nA ? html`<li><i class="s-a"></i><span><b>${int(nA)}</b> audited by IndiaMART QA</span></li>` : ""}${nP ? html`<li><i class="s-b"></i><span><b>${int(nP)}</b> production, no QA grade${nA ? "" : " (the seller-side bot)"}</span></li>` : ""}`
      : sim ? html`<li><span>Simulated replay conversations: no real customer, no QA grade.</span></li>` : a.human_split ? html`<li><span><b>${int(sum(a.human_split))}</b> graded by IndiaMART QA</span></li>` : ""}
      ${waiting ? html`<li><span><b>${int(waiting)}</b> of ${int(a.n_calls)} waiting for the judge</span></li>` : ""}</ul>
    ${both ? html`<p class="cov-note">${outside.length ? `The loop judges these. ${outP === outside.length ? `The ${plural(outP, "production call")} are left out: they come from the seller-side bot, which this checklist and prompt do not cover.` : `${plural(outside.length, "other call")} are left out.`}`
        : "Production calls come from the seller-side bot, which this checklist was not written for: read their grades with care."}</p>
      <div class="seg cov-scope" role="group" aria-label="Which calls"><a href="#/audit?version=${enc(a.version)}" aria-current="${a.source !== "all" ? "true" : "false"}">${SCOPE[corpus] || corpus} · ${int(mine.filter((c) => c.source === corpus).length)}</a><a href="#/audit?version=${enc(a.version)}&source=all" aria-current="${a.source === "all" ? "true" : "false"}">All calls · ${int(mine.length)}</a></div>` : ""}</div>`;
}

function split(a, audited, complete) {
  const row = (who, sub, s) => html`<div class="split-row"><div class="who"><b>${who}</b>${sub}</div><div>${gradeStack(s, "xl")}
    <div class="split-nums">${["pass", "non_fatal", "fatal"].map((g) => html`<span>${pct(s[g], 0)} ${GRADE[g].toLowerCase()}${s.n ? ` (${int(s[g] * s.n)})` : ""}</span>`)}</div></div></div>`;
  const all = shares(a.split);
  if (complete && audited.length) {
    const count = (k) => shares(Object.fromEntries(["pass", "non_fatal", "fatal"].map((g) => [g, audited.filter((c) => c[k] === g).length])));
    const j = count("overall"), q = count("human_overall"), n = audited.length;
    const qf = audited.filter((c) => c.human_overall === "fatal"), cf = qf.filter((c) => c.overall === "fatal").length;
    const say = Math.abs(j.pass - q.pass) < 0.05 && Math.abs(j.fatal - q.fatal) < 0.05 ? "On the same calls the judge is about as strict as QA."
      : `On the same calls the judge is ${j.pass < q.pass ? "harsher" : "more lenient"} than QA: it passes ${int(j.pass * n)} where QA passes ${int(q.pass * n)}, and calls ${int(j.fatal * n)} Fatal where QA marks ${int(q.fatal * n)}.`;
    return html`<div class="card span-2"><div class="card-head"><h3>Judge vs IndiaMART QA, same ${plural(n, "audited call")}</h3>${gradeLegend()}</div>
      ${row("Judge", "", j)}${row("IndiaMART QA", "", q)}<p class="split-say">${say}${qf.length ? ` Of the ${plural(qf.length, "call")} QA marked Fatal, the judge also called ${int(cf)} Fatal.` : ""}</p>
      ${a.n_audited > n ? html`<div class="split-all">${row("Judge", `all ${int(a.n_audited)} calls`, all)}</div>` : ""}</div>`;
  }
  const human = shares(a.human_split);
  return html`<div class="card span-2"><div class="card-head"><h3>Fatal · Non-fatal · Pass</h3>${gradeLegend()}</div>
    ${row("Judge", `${int(a.n_audited)} calls`, all)}
    ${human?.n ? html`${row("IndiaMART QA", `${int(human.n)} calls`, human)}<p class="split-say muted">The bars cover different calls: QA graded only the ones it audited.</p>`
      : html`<p class="muted" style="font-size:12.5px;margin-top:12px">IndiaMART QA graded none of ${a.version}'s calls.</p>`}</div>`;
}

function byParameter(ag, names, audio) {  // closest agreement first; the audio-only checks, which the judge cannot read, last
  const rows = ag.rows.map((r, i) => ({ r, i })).sort((x, y) => audio.has(x.r.rubric_id) - audio.has(y.r.rubric_id) || (y.r.kappa ?? -9) - (x.r.kappa ?? -9));
  return html`<section class="section" id="by-param">
    <div class="section-head"><h2>Check by check: where the judge <em>agrees</em> with QA</h2><p>Closest agreement first, on the ${plural(ag.n_calls, "call")} both graded. Click a row for the full grid.</p></div>
    <p class="sec-note">“Matches QA” flatters rare failures: where QA almost always says Pass, a judge that always says Pass matches nearly every call. Agreement beyond chance (Cohen's κ) corrects for that: 0 is guessing, 1 is perfect. N/A, a parameter that did not apply, counts as its own grade.</p>
    <div class="card ag-card"><table class="table ag-table" id="agree">
      <thead><tr><th>Parameter</th><th>Agreement beyond chance</th><th class="r">Matches QA</th><th class="r">QA-fatal caught</th><th>Biggest mismatch</th></tr></thead>
      ${rows.map(({ r, i }) => paramRow(r, i, names[r.rubric_id] || r.rubric_id, audio.has(r.rubric_id)))}
    </table></div></section>`;
}

function paramRow(r, i, nm, deaf) {  // deaf: an audio-only check, graded N/A from a transcript, so not compared
  const [word, tone] = deaf ? ["needs audio, not graded", ""] : band(r.kappa), cm = r.confusion || {};
  const qaF = sum(cm.fatal), usual = CLS.reduce((x, y) => (sum(cm[y]) > sum(cm[x]) ? y : x));
  let miss = null;
  for (const q of CLS) for (const j of CLS) if (q !== j && (cm[q]?.[j] || 0) > (miss?.n || 0)) miss = { q, j, n: cm[q][j] };
  return html`<tbody class="ag-p t-${tone || "none"}">
    <tr class="link">
      <td class="ag-name"><button type="button" class="ag-toggle" aria-expanded="false" aria-controls="cm-${i}"><i aria-hidden="true">›</i>${nm}</button></td>
      <td class="ag-band"><span class="chip ${tone}">${word}</span>${deaf ? "" : html` <span class="mono muted">κ ${fix(r.kappa)}</span>`}</td>
      <td class="r ag-acc"><span class="mono">${pct(r.accuracy)}</span><small>always “${GRADE[usual]}”: ${pct(r.n ? sum(cm[usual]) / r.n : null)}</small></td>
      <td class="r ag-fatal">${qaF ? html`<span class="mono">${int(cm.fatal.fatal || 0)} of ${int(qaF)}</span>` : html`<span class="muted">QA marked none</span>`}</td>
      <td class="ag-miss">${miss ? html`Judge says <b>${GRADE[miss.j]}</b> on ${plural(miss.n, "call")} QA ${miss.q === "na" ? "marked N/A" : `graded ${GRADE[miss.q]}`}` : html`<span class="muted">none</span>`}</td>
    </tr>
    <tr class="detail" id="cm-${i}" hidden><td colspan="5"><div class="cm-wrap">${confusion(cm)}
      <div class="cm-explain"><b>${nm}</b><span>Rows are QA's grades, columns the judge's. The outlined diagonal is where they agree.</span>
        ${qaF ? html`<span>Of the ${plural(qaF, "call")} QA graded Fatal, the judge called ${int(cm.fatal.fatal || 0)} Fatal.</span>` : ""}</div></div></td></tr>
  </tbody>`;
}

function failMost(a) {
  const N = Math.max(1, a.n_audited);
  const graded = (a.by_rubric || []).filter((r) => r.n_applicable), never = (a.by_rubric || []).filter((r) => !r.n_applicable);
  const rows = [...graded].sort((x, y) => (y.fatal + y.non_fatal) - (x.fatal + x.non_fatal) || y.fatal - x.fatal);
  return html`<section class="section">
    <div class="section-head"><h2>Which parameters <em>fail</em> most</h2><div class="legend"><span><i class="s-fatal"></i>Fatal</span><span><i class="s-non_fatal"></i>Non-fatal</span><span><i class="s-pass"></i>Pass</span><span><i class="s-na"></i>Did not apply</span></div></div>
    <p class="sec-note">Each bar spans all ${plural(a.n_audited, "judged call")}, so a long red bar means many calls failed. Cause: <b>prompt</b>, the bot's instructions; <b>asr</b>, speech recognition misheard the buyer; <b>knowledge</b>, missing product or catalogue facts; <b>flow</b>, call-flow or tool design; <b>platform</b>, telephony or system failure.</p>
    <div class="card fails">${rows.map((r) => html`<a class="fail-row" href="#/rubrics/${enc(r.rubric_id)}">
      <div class="fr-name">${r.name}<small>${r.category || ""}${r.root_type ? ` · cause: ${r.root_type}` : ""}</small></div>
      <div class="fr-bar">${stack([{ v: r.fatal / N, cls: "s-fatal", label: "Fatal", n: r.fatal }, { v: r.non_fatal / N, cls: "s-non_fatal", label: "Non-fatal", n: r.non_fatal }, { v: r.pass / N, cls: "s-pass", label: "Pass", n: r.pass }], "tall")}</div>
      <div class="fr-nums"><b>${int(r.fatal + r.non_fatal)}</b> failed<small>${int(r.fatal)} fatal · applied to ${int(r.n_applicable)}</small></div></a>`)}
      ${never.length ? html`<p class="fail-never">Never graded (needs audio, or never applied): ${never.map((r) => r.name).join(", ")}.</p>` : ""}</div>
  </section>`;
}

function riskyCalls(list) {  // the calls a QA lead acts on, worst first: Fatal, then frustration / human / wrong need, then loops
  const act = list.filter((r) => r.flags.some((f) => f !== "low_confidence"));
  const score = (r) => [SEV[r.overall] ?? -1, r.flags.filter((f) => SERIOUS.includes(f)).length, r.flags.includes("loop") ? 1 : 0];
  const top = [...act].sort((x, y) => { const p = score(x), q = score(y); return q[0] - p[0] || q[1] - p[1] || q[2] - p[2] || x.call_id.localeCompare(y.call_id); }).slice(0, 10);
  return { act, top, counts: Object.fromEntries(FLAG_ORDER.map((f) => [f, list.filter((r) => r.flags.includes(f)).length])) };
}

function risky({ act, top, counts }, link) {
  const tile = (f) => {
    const body = html`<span class="ft-n">${int(counts[f])}</span><span class="ft-l">${FLAGS[f]}</span><small>${FLAG_NOTE[f]}</small>`;
    return link && counts[f] ? html`<a class="flag-tile f-${f}" href="${link(`flag=${f}`)}">${body}</a>` : html`<div class="flag-tile f-${f}">${body}</div>`;
  };
  return html`<section class="section" id="risky">
    <div class="section-head"><h2>Calls to <em>listen to</em> first</h2><p>${act.length ? `${plural(act.length, "call")} carry a flag a QA lead acts on; the worst ${int(top.length)} are below.` : "The judge flagged nothing a QA lead acts on."}</p></div>
    <div class="flag-tiles">${FLAG_ORDER.map(tile)}</div>
    ${top.length ? html`<div class="card risk-list">${top.map((r) => html`<a class="risk" href="#/calls/${enc(r.call_id)}"><span><span class="mono" style="font-size:12px">${r.call_id}</span><br>${grade(r.overall)}</span>
        <span class="sum">${r.summary}</span><span class="flags">${flagChips(r.flags.filter((f) => f !== "low_confidence"))}</span></a>`)}</div>
      ${link && act.length > top.length ? html`<a class="btn sm see-all" href="${link("flag=action")}">See all ${int(act.length)} in Calls →</a>` : ""}`
      : empty({ title: "Nothing flagged", body: "No call raised a frustration, human-request, wrong-requirement or loop flag.", compact: true })}
  </section>`;
}
