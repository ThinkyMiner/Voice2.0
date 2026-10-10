// #/ The loop: where the current cycle stands and its one next step, the key numbers with their n,
// the cycle's five stations (eight steps), its root cause, fix and A/B test, and the closed cycles.
import { get, send, state } from "../api.js";
import { $, html, raw, esc, mount, int, pct, fix, sint, when, short, cap, stateChip, gradeStack, shares, toast, enc, kappaWord, confirmDialog } from "../ui.js";
import { mountRing } from "../viz.js";

// The ring's five stations and the loop's eight steps under them (loop.STAGES).
const GROUPS = [
  ["Listen", [["listen", "Count the calls"]]],
  ["Judge", [["judge", "Grade every call"]]],
  ["Improve", [["improve", "Rank root causes"], ["gate", "Write the fix, safety gate"], ["replay", "Replay A against B"]]],
  ["Prove", [["prove", "A/B test, simulated traffic"], ["approval", "A human approves"]]],
  ["Roll out", [["rollout", "B becomes production"]]],
];
const ORDER = GROUPS.flatMap(([, s]) => s.map(([k]) => k));
const STATION = { ...Object.fromEntries(GROUPS.flatMap(([, s], i) => s.map(([k]) => [k, i]))), done: 4, blocked: 2 };
const SUB = { improve: "rank causes", gate: "safety gate", replay: "replay", prove: "A/B test", approval: "approval" };
const HALT_AT = { GATE_BLOCKED: "gate", REPLAY_NO_GAIN: "replay", REPLAY_FAILED: "replay", FIX_INVALID: "gate", REJECTED: "approval",
  STOPPED_HARM: "prove", BLOCKED_GUARDRAIL: "prove", INCONCLUSIVE: "prove", HALTED_SRM: "prove" };
const OUTCOME = { PROMOTED: "B won and was promoted", PENDING_APPROVAL: "B won; waiting for a human", STOPPED_HARM: "stopped for harm",
  BLOCKED_GUARDRAIL: "blocked by the guardrail", INCONCLUSIVE: "no clear effect", HALTED_SRM: "halted: the split was off", REJECTED: "rejected by a human",
  RUNNING: "running", GATE_BLOCKED: "blocked by the safety gate", REPLAY_NO_GAIN: "no gain in the replay" };
// Steps that send real call content to the model provider (real calls, and replays built from them): the web
// runs them only when the platform was started with LOOP_ALLOW_REAL=1 (loop.py, models.is_real).
const NEEDS_REAL = (c) => ["judge", "improve", "replay"].includes(c.stage) || (c.stage === "gate" && !c.fix_id);
const STEP_NAME = { judge: "grading the calls", improve: "ranking the root causes", gate: "writing the fix", replay: "the replay" };
const PAID = { judge: ["judge"], improve: ["judge"], gate: ["fixer", "gate"], replay: ["replay", "judge"] };  // llm roles per step
const GO_AHEAD = "make loop ARGS=--yes-send-real-transcripts";

function steps(c) {
  if (!c) return ORDER.map(() => "");
  // Only an ended cycle shows where it stopped: a failed try leaves `outcome` set while the cycle moves on.
  const halt = c.stage === "blocked" ? "improve" : c.stage === "done" ? HALT_AT[c.outcome] : null;
  if (halt) { const h = ORDER.indexOf(halt); return ORDER.map((_, i) => (i < h ? "done" : i === h ? "halt" : "skip")); }
  if (c.stage === "done") return ORDER.map(() => "done");
  const k = ORDER.indexOf(c.stage);
  return ORDER.map((_, i) => (i < k ? "done" : i === k ? "current" : ""));
}
function station(c) {
  if (!c) return { station: 0, halt: false, detail: "" };
  const st = steps(c), h = st.indexOf("halt");
  if (h >= 0) return { station: STATION[ORDER[h]], halt: true, detail: SUB[ORDER[h]] ? `→ ${SUB[ORDER[h]]}` : "" };
  return { station: STATION[c.stage] ?? 0, halt: false, detail: SUB[c.stage] ? `→ ${SUB[c.stage]}` : "" };
}
const figs = (s) => raw(esc(s).replace(/\b(v\d+|\d[\d,.]*)\b/g, '<span class="fig">$1</span>'));  // the serif's 1 reads as l
const first = (s) => (String(s || "").match(/^.+?[.!?](?=\s|$)/)?.[0] || String(s || "")).trim();
const plain = (note) => cap(String(note || "").replace(/^(blocked|waiting|done|close):\s*/, ""));
const fixStatus = (f, x) => [
  `${f.edits.length} edit${f.edits.length === 1 ? "" : "s"}`,
  f.gate ? (f.gate.passed ? "gate passed" : "gate blocked") : "gate not run",
  f.replay ? `replay ${pct(f.replay.fail_rate_a, 0)} → ${pct(f.replay.fail_rate_b, 0)} failing, ${f.replay.n_cases} cases` : "replay not run",
  x ? `A/B ${OUTCOME[x.state] || x.state.toLowerCase()}` : "no A/B yet"].join(" · ");

/** The headline and the one next step for where the loop stands. */
function plan(c, lp, k) {
  const A = c?.production_version || k.prodV, B = c?.candidate_version || "Prompt B", n = c?.n;
  const wait = /^waiting/.test(lp.next_action || "") ? lp.next_action : "";
  if (lp.busy) return { title: `Cycle ${n ?? 1} is running`, body: "The loop is working in the background. The run log below shows each step as it happens; this page refreshes when it stops." };
  if (!c || c.stage === "done") {
    const m = c ? c.n + 1 : 1;
    return { title: `Ready to start cycle ${m}`, body: `Cycle ${m} works on ${A}, the prompt in production. Starting opens the cycle and counts ${A}'s calls. No model is called.`,
      run: { until: "listen", label: `Start cycle ${m}` } };
  }
  if (c.stage === "blocked") return { title: `Cycle ${n} is blocked`, tone: "harm", body: plain(c.notes.at(-1)) || "No root cause a prompt change can fix is left on this version." };
  if (c.stage === "approval" && (k.x?.state === "PENDING_APPROVAL" || /^waiting for approval/.test(wait)))
    return { title: `Cycle ${n} needs your decision`, tone: "pending", body: `${B} beat ${A} on simulated traffic. It keeps 10% of that traffic until someone approves or rejects it on the experiment page.`,
      link: ["Review and decide", `#/experiments/${enc(c.experiment_id)}`] };
  if (k.real && !k.allowReal && (NEEDS_REAL(c) || /^waiting for the go-ahead/.test(wait)))
    return { title: `Cycle ${n} is waiting for your go-ahead`, tone: "pending",
      body: `${c.stage === "judge" && k.graded ? "The numbers below are from the judge's last run. Grading again for this cycle" : `The next step, ${STEP_NAME[c.stage] || c.stage},`} sends real call content to the model provider (OpenRouter), so it needs your go-ahead. Run it from a terminal, or start the platform with LOOP_ALLOW_REAL=1 to run it from here:`, cli: GO_AHEAD };
  if (/^waiting for transcripts/.test(wait)) return { title: `Cycle ${n} is waiting for calls`, tone: "pending", body: plain(wait.replace(/^waiting for transcripts of \S+:\s*/, "")) };
  if (/^waiting: none of the/.test(wait)) return { title: `Cycle ${n}: the judge could not grade any call`, tone: "harm", body: "Its errors are in the run log. Grading again retries them.", run: { until: "judge", label: "Grade again", paid: ["judge"] } };
  const rc = k.rc ? ` “${k.rc.name}”` : "";
  const S = {
    listen: [`Cycle ${n}: count ${A}'s calls`, "Counts the calls this cycle works on. No model is called.", "Count the calls"],
    judge: [`Cycle ${n}: grade ${A}'s calls`, "The judge model grades every call against the active rubrics. Calls graded before are cached and cost nothing.", "Grade the calls"],
    improve: [`Cycle ${n}: rank the root causes`, "Groups the failed grades into root causes, ranks them by priority and picks the top one a prompt change can fix.", "Rank root causes"],
    gate: c.fix_id ? [`Cycle ${n}: safety-check ${B}`, `The gate model checks ${B} against the safety rules before any replay.`, "Run the safety gate"]
      : [`Cycle ${n}: write the fix${rc ? ` for${rc}` : ""}`, `The fixer drafts Prompt B as a few edits to ${A}, then the safety gate checks it.`, "Write the fix"],
    // up to 8 calls: improve.replay(n=8)
    replay: [`Cycle ${n}: replay ${B} against ${A}`, `Up to 8 of the calls behind${rc || " the root cause"} become simulated buyers. Each one talks to ${A} and to ${B}, and the judge grades both conversations.`, "Run the replay"],
    prove: [`Cycle ${n}: A/B test ${B}`, `The A/B engine replays call history with ${B} on 10% of leads and an effect assumed for B, deciding at each checkpoint. Simulated traffic; no model is called.`, "Run the A/B test"],
    approval: [`Cycle ${n}: read the A/B decision`, `${c.experiment_id || "The A/B test"} has ended${k.x ? `: ${OUTCOME[k.x.state] || k.x.state}` : ""}. This moves the cycle to roll-out.`, "Read the decision"],
    rollout: k.x?.state === "PROMOTED" ? [`Cycle ${n}: roll out ${B}`, `${B} becomes production, ${A} is retired, and cycle ${n + 1} starts on ${B}.`, `Roll out ${B}`]
      : [`Cycle ${n}: close the cycle`, `${B} is rejected and ${A} stays in production. Cycle ${n + 1} then starts on ${A}.`, "Close the cycle"],
  }[c.stage];
  if (!S) return { title: `Cycle ${n} is at ${c.stage}`, body: lp.next_action || "" };
  const rest = ORDER.slice(ORDER.indexOf(c.stage), ORDER.indexOf("approval"));  // the steps before a person decides
  return { title: S[0], body: S[1], run: { until: c.stage, label: S[2], body: S[1], paid: PAID[c.stage] || [] },
    all: rest.length > 1 ? { label: "Run until it needs you", paid: [...new Set(rest.flatMap((s) => PAID[s] || []))],
      body: `Runs every step from ${STEP_NAME[c.stage] || c.stage} on, until the loop needs a person: the A/B decision, a step that is blocked, or a step that waits for your go-ahead.` } : null };
}

function ask(run, settings) {
  const roles = run.paid || [], b = settings?.budget;
  const models = [...new Set(roles.map((r) => settings?.models?.[r]).filter(Boolean))];
  return confirmDialog({
    title: `${run.label}?`, ok: run.label,
    body: html`<p>${run.body}</p>
      <p><b>Model calls:</b> ${roles.length ? html`${models.join(" and ") || "the platform's models"}, through OpenRouter. ${b ? `$${fix(b.spent_usd)} of the $${fix(b.cap_usd, 0)} budget is spent so far. ` : ""}Replies are cached, so anything graded before costs nothing.` : "none, so nothing is spent."}</p>
      <p>${settings?.allow_real ? html`<b>Real calls:</b> this platform was started with LOOP_ALLOW_REAL=1, so a step that needs real call content sends it to OpenRouter.` : html`<b>Real calls</b> are not sent from here: a step that needs them stops and waits for your go-ahead.`}</p>
      <p class="muted">${state.demo ? "Demo mode: a scripted replay from the cache; nothing is written." : "It runs in the background and updates the loop's files on this machine."}</p>`,
  });
}

const kpi = (href, label, body) => html`<a class="kpi" href="${href}" aria-label="${label}">${body}<span class="go" aria-hidden="true">→</span></a>`;

export async function render(el) {
  const ov = state.overviewFresh && state.overview ? state.overview : await get("/api/overview");
  state.overviewFresh = false;
  const prodV = ov.production?.version;
  const versions = [...new Set([prodV, ...(ov.cycles || []).map((c) => c.production_version)].filter(Boolean))];
  const [lp, pr, fx, xs, iss, settings] = await Promise.all([
    get("/api/loop"),
    get("/api/prompts").catch(() => ({ items: [] })),
    get("/api/fixes").catch(() => ({ items: [] })),
    get("/api/experiments").catch(() => ({ items: [] })),
    Promise.allSettled(versions.map((v) => get(`/api/issues?version=${enc(v)}`))),
    get("/api/settings").catch(() => null),
  ]);
  const rcBy = Object.fromEntries(iss.flatMap((r) => (r.status === "fulfilled" ? r.value.items.map((i) => [i.id, i]) : [])));
  const fixBy = Object.fromEntries(fx.items.map((f) => [f.id, f]));
  const xBy = Object.fromEntries(xs.items.map((x) => [x.experiment_id, x]));
  const verBy = Object.fromEntries(pr.items.map((p) => [p.version, p]));
  const cur = lp.cycle || ov.cycle;
  const k = ov.kpis || {};
  const ctx = (c) => ({ prodV, rc: rcBy[c?.root_cause_id], f: fixBy[c?.fix_id], x: xBy[c?.experiment_id], graded: k.calls_audited || 0,
    real: !state.demo && Boolean(c || prodV),  // v1's calls are real; a later version's are replays built from them
    allowReal: Boolean(settings?.allow_real) });
  const p = plan(cur, lp, ctx(cur));
  const sp = shares({ pass: k.pass_rate, non_fatal: k.non_fatal_rate, fatal: k.fatal_rate });
  const loopX = xs.items.filter((x) => x.cycle != null), engineX = xs.items.length - loopX.length;
  const finished = loopX.filter((x) => !["RUNNING", "DRAFT", "PENDING_APPROVAL"].includes(x.state));
  const latest = fx.items[0];
  const acc = k.agreement_overall, kap = k.agreement_kappa;
  const agreeTone = acc == null ? "" : kap != null ? (kap < 0.2 ? "harm" : kap < 0.6 ? "warn" : "win") : acc < 0.6 ? "harm" : acc < 0.8 ? "warn" : "win";
  const st = station(cur);
  const closed = (ov.cycles || []).filter((c) => c.n !== cur?.n);

  mount(el, html`
    <section class="status">
      <div class="status-main">
        <div class="eyebrow accent">The loop · ${cur ? html`cycle ${cur.n} · ${GROUPS[st.station][0]}${st.detail ? ` ${st.detail}` : ""}` : "not started yet"}</div>
        <h1 class="status-title ${p.tone || ""}">${figs(p.title)}</h1>
        <p class="status-body">${p.body}</p>
        ${p.cli ? html`<code class="cmd">${p.cli}</code>` : ""}
        <div class="row status-actions">
          ${p.run ? html`<button class="btn primary" id="adv" type="button"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 3l8 5-8 5z"/></svg><span>${p.run.label}</span></button>` : ""}
          ${p.run && p.all ? html`<button class="btn" id="adv-all" type="button"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 3l6 5-6 5zM9 3l6 5-6 5z"/></svg><span>${p.all.label}</span></button>` : ""}
          ${p.link ? html`<a class="btn primary" href="${p.link[1]}">${p.link[0]}</a>` : ""}
          ${!p.run && !p.link && !lp.busy ? html`<button class="btn" type="button" data-retry>Refresh</button>` : ""}
          ${lp.busy ? html`<button class="btn" id="adv" type="button" disabled><span>Running…</span></button>` : ""}
        </div>
        <details class="runlog" id="runlog" ${lp.busy ? "open" : ""} ${lp.log?.length ? "" : "hidden"}><summary><span class="eyebrow">Run log</span><span class="last-line" id="lastline"></span></summary><pre class="log" id="log"></pre></details>
        <p class="status-intro">Vani Lab listens to Vani's buyer calls, grades each one against IndiaMART's QA checklist, writes a prompt fix for the costliest failure and tests it on simulated traffic before it ships.</p>
      </div>
      <div class="ring-wrap" id="ring"></div>
    </section>

    <section class="kpis home-kpis" aria-label="Key numbers">
      ${kpi("#/calls", `${int(k.calls_audited)} calls graded by the judge. Open the calls.`, html`<div class="eyebrow">Calls graded</div><div class="big">${int(k.calls_audited)}</div>
        <div class="sub">by the judge, on ${prodV || "—"}${k.calls_total != null && k.calls_total !== k.calls_audited ? ` (of ${int(k.calls_total)})` : ""}<span class="sub-line" id="qa-n"></span></div>`)}
      ${kpi("#/audit", `Fatal rate ${pct(k.fatal_rate)}, judge's grade. Open the audit.`, html`<div class="eyebrow">Fatal rate <span class="tag">judge</span></div><div class="big harm">${pct(k.fatal_rate)}</div>
        <div class="sub">of ${int(k.calls_audited)} graded calls · non‑fatal ${pct(k.non_fatal_rate, 0)} · pass ${pct(k.pass_rate, 0)}</div><div aria-hidden="true">${sp ? gradeStack(sp, "minibar") : ""}</div>`)}
      ${kpi("#/audit", `Judge versus IndiaMART QA: ${pct(acc, 0)} agreement. Open the audit.`, html`<div class="eyebrow">Judge vs QA</div><div class="big ${agreeTone}">${pct(acc, 0)}<small>agree</small></div>
        <div class="sub">with IndiaMART QA on the overall grade<span id="agree-n"></span> · κ ${fix(kap)}, ${kappaWord(kap)} · fatal vs rest ${pct(k.fatal_vs_rest_accuracy, 0)}</div>`)}
      ${kpi("#/issues", `${int(k.root_causes)} root causes. Open the issues.`, html`<div class="eyebrow">Root causes <span class="tag">judge</span></div><div class="big ${k.root_causes ? "" : "none"}">${k.root_causes ? int(k.root_causes) : "—"}</div>
        <div class="sub">${k.root_causes ? "ranked by priority; the loop fixes the top one a prompt can fix" : "not ranked yet: Improve ranks them after the judge"}</div>`)}
      ${kpi("#/fixes", `${fx.items.length} prompt fixes. Open the fixes.`, html`<div class="eyebrow">Prompt fixes</div><div class="big ${fx.items.length ? "" : "none"}">${fx.items.length ? int(fx.items.length) : "—"}</div>
        <div class="sub">${latest ? `latest: ${fixStatus(latest, xBy[(ov.cycles || []).find((c) => c.fix_id === latest.id)?.experiment_id])}` : "none written yet: the fixer writes one for the top root cause"}</div>`)}
      ${kpi("#/experiments", "Weekly impact from loop experiments. Open the experiments.", html`<div class="eyebrow">Weekly impact</div>
        ${finished.length ? html`<div class="big ${k.per_week_gained > 0 ? "win" : ""}">${sint(k.per_week_gained)}</div><div class="sub">simulated successes a week gained ·${int(k.per_week_protected)} protected · from ${finished.length} loop A/B test${finished.length === 1 ? "" : "s"}${engineX ? `, plus ${engineX} engine scenarios` : ""}</div>`
          : html`<div class="big none">—</div><div class="sub">no loop A/B test has finished yet${engineX ? ` · ${engineX} engine scenarios ran, changing nothing` : ""}</div>`}`)}
    </section>
    ${acc != null && (kap != null ? kap <= 0.4 : acc < 0.7) ? html`<p class="kpi-note"><i class="dot demo" aria-hidden="true"></i><span>Judge grades are directional until they agree better with IndiaMART QA (${pct(acc, 0)} today). <a href="#/audit">Compare them call by call</a></span></p>` : ""}

    <section class="section" id="current"></section>

    ${closed.length ? html`<section class="section">
      <div class="section-head"><h2>Earlier <em>cycles</em></h2><p>Newest first. Each cycle starts on the prompt the one before left in production.</p></div>
      <div class="timeline">${closed.map((c) => {
        const r = rcBy[c.root_cause_id], x = xBy[c.experiment_id];
        return html`<div class="tl ${c.outcome === "PROMOTED" ? "win" : "harm"}">
          <div class="when"><div class="c">Cycle <span class="fig">${c.n}</span></div><div class="d">${when(c.started_ts)}</div></div>
          <div class="spine"><i></i></div>
          <div class="body">
            <div class="head"><span class="v">${c.production_version}${c.candidate_version ? html` → ${c.candidate_version}` : ""}</span>${c.outcome ? stateChip(c.outcome) : stateChip(c.stage)}</div>
            ${r ? html`<div class="rc"><a href="#/issues?version=${enc(r.prompt_version)}&rc=${enc(r.id)}">${r.name}</a></div>` : c.root_cause_id ? html`<div class="rc">${c.root_cause_id}</div>` : ""}
            <div class="row">
              ${c.fix_id ? html`<a class="chip id" href="#/fixes/${enc(c.fix_id)}">${c.fix_id}</a>` : ""}
              ${c.experiment_id ? html`<a class="chip id" href="#/experiments/${enc(c.experiment_id)}">${c.experiment_id}</a>` : ""}
              ${x && !c.outcome ? stateChip(x.state) : ""}
            </div>
            ${c.notes?.length ? html`<details class="enginelog"><summary>Engine notes · ${c.notes.length}</summary><ul class="notes">${c.notes.map((t) => html`<li>${t}</li>`)}</ul></details>` : ""}
          </div></div>`;
      })}</div>
    </section>` : ""}`);

  // --- the ring, the current cycle and the run log; redrawn while a run is polled
  const prod = ov.production;
  let ring = null, shown = null;
  const drawRing = (c, busy, start = 0) => {
    const s = station(c);
    if (ring && shown && shown.station === s.station && shown.halt === s.halt && shown.detail === s.detail) { ring.setBusy(busy); return; }
    ring?.stop();
    ring = mountRing($("#ring", el), { ...s, version: prod?.version || "—", sha: prod?.sha256 ? `sha ${short(prod.sha256)}` : "", sub: c ? `Cycle ${c.n}` : "", busy, start });
    shown = s;
  };
  const drawCurrent = (c) => {
    const host = $("#current", el), kc = ctx(c), stp = steps(c);
    const notes = c?.notes || [];
    const calls = notes.map((t) => /^listen: (\d+) calls on \S+, (\d+) of them real/.exec(t)).find(Boolean);
    const idx = (key) => ORDER.indexOf(key);
    mount(host, html`
      <div class="section-head"><h2>${c ? html`Cycle <span class="fig">${c.n}</span>, <em>${c.stage === "done" ? "closed" : c.stage === "blocked" ? "blocked" : `${GROUPS[station(c).station][0].toLowerCase()}`}</em>` : html`How a cycle <em>runs</em>`}</h2>
        <p>${c ? html`Prompt A is ${c.production_version}${c.candidate_version ? html`; Prompt B is ${c.candidate_version}` : ""}${calls ? ` · ${int(calls[1])} calls${calls[2] === calls[1] ? ", all real" : `, ${int(calls[2])} real`}` : ""} · started ${when(c.started_ts)}` : "Five stations, eight steps; nothing ships without the A/B test and a human."}</p></div>
      <ol class="stations">${GROUPS.map(([name, subs]) => {
        const states = subs.map(([key]) => stp[idx(key)]);
        const s = states.includes("halt") ? "halt" : states.includes("current") ? "current" : states.every((x) => x === "done") ? "done" : states.every((x) => x === "skip") ? "skip" : "";
        return html`<li class="station ${s}"><span class="node" aria-hidden="true"></span><div class="name">${name}</div>
          <ul>${subs.map(([key, what], j) => html`<li class="${states[j]}" ${states[j] === "current" ? html`aria-current="step"` : ""}>${what}</li>`)}</ul></li>`;
      })}</ol>
      ${c ? html`<div class="links3">
        ${kc.rc ? html`<a class="linkcard" href="#/issues?version=${enc(kc.rc.prompt_version)}&rc=${enc(kc.rc.id)}" aria-label="Root cause: ${kc.rc.name}"><span class="eyebrow">Root cause</span><span class="t">${kc.rc.name}</span>
            <span class="s">${int(kc.rc.n_calls)} of ${int(kc.rc.n_audited)} graded calls · priority ${fix(kc.rc.priority, 1)}</span></a>`
          : html`<a class="linkcard" href="#/issues" aria-label="Root causes"><span class="eyebrow">Root cause</span><span class="t muted">${c.root_cause_id || "Not chosen yet"}</span><span class="s">Chosen once the judge has graded the calls</span></a>`}
        ${kc.f ? html`<a class="linkcard" href="#/fixes/${enc(kc.f.id)}" aria-label="Prompt fix ${kc.f.id}"><span class="eyebrow">Prompt fix · ${kc.f.id}</span><span class="t">${first(kc.f.rationale)}</span>
            <span class="s">${fixStatus(kc.f, kc.x)}</span></a>`
          : html`<a class="linkcard" href="#/fixes" aria-label="Prompt fixes"><span class="eyebrow">Prompt fix</span><span class="t muted">${c.fix_id || "Not written yet"}</span><span class="s">Written for the top root cause a prompt can fix</span></a>`}
        ${kc.x ? html`<a class="linkcard" href="#/experiments/${enc(kc.x.experiment_id)}" aria-label="A/B test ${kc.x.experiment_id}"><span class="eyebrow">A/B test · simulated</span><span class="t">${cap(OUTCOME[kc.x.state] || kc.x.state.toLowerCase())}</span>
            <span class="s">${kc.x.looks_done} of ${kc.x.n_looks} checkpoints · lift ${kc.x.lift_rel == null ? "—" : `${kc.x.lift_rel >= 0 ? "+" : "−"}${pct(Math.abs(kc.x.lift_rel))}`}</span></a>`
          : html`<a class="linkcard" href="#/experiments" aria-label="Experiments"><span class="eyebrow">A/B test</span><span class="t muted">Not started</span><span class="s">${c.candidate_version ? `${c.candidate_version} gets one after the replay` : "Runs once a fix passes the gate and the replay"}</span></a>`}
      </div>` : ""}
      ${notes.length ? html`<details class="enginelog"><summary>Engine notes for this cycle · ${notes.length}</summary><ul class="notes">${notes.map((t) => html`<li>${t}</li>`)}</ul></details>` : ""}`);
  };
  const drawLog = (log) => {
    const pre = $("#log", el);
    if (!log?.length) return;
    $("#runlog", el).hidden = false;
    const lines = log.slice(-9);
    mount(pre, lines.map((l, i) => html`<span class="${i === lines.length - 1 ? "last" : ""}">${l}</span>\n`));
    $("#lastline", el).textContent = lines[lines.length - 1];
    pre.scrollTop = pre.scrollHeight;
  };
  drawRing(cur, lp.busy);
  drawCurrent(cur);
  drawLog(lp.log);

  // The judge's agreement with QA, with its n, and the QA-audited share: /api/audit is heavier, so it fills in after.
  get("/api/audit?version=v1").then((a) => {
    if (!el.isConnected) return;
    const g = a.agreement, h = a.human_split;
    if (g) $("#agree-n", el).textContent = `, ${int(g.n_calls)} ${g.held_out ? "held-out " : ""}calls${g.calibrated && g.n_tuning ? ` (calibrated on ${int(g.n_tuning)} others)` : ""}`;
    const qa = h ? h.pass + h.non_fatal + h.fatal : 0;
    if (qa && !verBy[prodV]?.fix_id) $("#qa-n", el).textContent = qa === k.calls_audited ? "all also audited by IndiaMART QA" : `${int(qa)} also audited by IndiaMART QA`;
  }).catch(() => { /* the tiles stand without it */ });

  // --- one step at a time, confirmed first, with live progress from GET /api/loop
  const btn = $("#adv", el);
  let timer = 0, alive = true;
  const busyUi = (b) => {
    if (!btn) return;
    btn.disabled = b;
    btn.lastElementChild.textContent = b ? "Running…" : p.run?.label || "Running…";
    const all = $("#adv-all", el);
    if (all) all.disabled = b;
  };
  const poll = async () => {
    if (!alive) return;
    try {
      const now = await get("/api/loop");
      if (!alive) return;
      drawLog(now.log);
      if (now.cycle) { const prev = shown?.station ?? 0; if (station(now.cycle).station !== prev) { ring?.stop(); ring = null; } drawRing(now.cycle, now.busy, prev); drawCurrent(now.cycle); }
      if (now.busy) timer = setTimeout(poll, 900);
      else { busyUi(false); window.dispatchEvent(new Event("vani:rerender")); }
    } catch (err) { busyUi(false); toast(`Loop status failed: ${err.message}`, "harm"); }
  };
  const go = async (run) => {
    const now = await get("/api/settings").catch(() => settings);
    if (!alive || !(await ask({ body: p.body, ...run }, now))) return;  // a run without its own text explains with the plan's
    busyUi(true);
    try {
      await send("POST", "/api/loop/advance", run.until ? { until: run.until } : {});
      $("#runlog", el).hidden = false;
      $("#runlog", el).open = true;
      mount($(".status-title", el), `Cycle ${cur?.n ?? 1} is running`);
      ring?.setBusy(true);
      timer = setTimeout(poll, 500);
      if (state.demo) toast("Demo mode: a scripted run from the cache, nothing is written.");
    } catch (err) { busyUi(false); toast(err.status === 409 ? err.message : `Could not start: ${err.message}`, "harm"); }
  };
  if (p.run && btn) btn.addEventListener("click", () => go(p.run));
  if (p.all) $("#adv-all", el)?.addEventListener("click", () => go(p.all));
  if (lp.busy) timer = setTimeout(poll, 900);
  return () => { alive = false; clearTimeout(timer); ring?.stop(); };
}
