// #/docs and #/docs/<section>: how Vani Lab works, on one page, for a QA lead, a product manager or a
// judge seeing it for the first time. Static text, no fetches. Every number, name and threshold comes from the
// code, the configs, README.md, docs/ or reports/; the judge's agreement changes with every run, so it lives on
// #/audit, not here. The section comes from the address itself (app.js's docs route captures nothing), so the
// breadcrumb and the tab title stay "Docs" while the contents marks where the reader is.
import { $, $$, html, raw, mount, grade, stateChip, typeChip, reducedMotion, STAGES } from "../ui.js";

export const SECTIONS = [  // [id, title]: #/docs/<id>, the contents, and the Cmd/Ctrl+K palette (app.js)
  ["overview", "What it is"], ["pages", "What each page is for"], ["loop", "The loop, stage by stage"],
  ["grades", "Grades and rubrics"], ["judge", "How far to trust the judge"], ["fixes", "Root causes, fixes and the gate"],
  ["prove", "Proving it: the A/B engine"], ["running", "Running it"], ["privacy", "Privacy and honesty"],
  ["glossary", "Glossary"], ["faq", "FAQ"],
];

// vani-meeting-v1's seven planned looks and their boundaries (README "Sequential method", make boundaries).
const PLAN = { t: [0.2, 0.333, 0.467, 0.6, 0.733, 0.867, 1], win: [4.877, 3.711, 3.09, 2.699, 2.426, 2.221, 2.061],
  harm: [-2.176, -2.229, -2.203, -2.178, -2.157, -2.139, -2.123] };

// The built-in rubrics, src/abtest/ext/rubric_sets/*.yaml: [category, [[name, root type, needs audio]], note].
const RUBRICS = [
  ["Outcome", [["Outcome Accuracy (Intent)", "prompt"], ["Product Change/ No Product Accuracy", "flow"]]],
  ["Quantity", [["Quantity Accuracy (Numeric)", "prompt"], ["Quantity Unit Accuracy", "prompt"], ["Quantity Probing Error", "prompt"],
    ["Quantity Missed Opportunity (Capture)", "prompt"]]],
  ["Specs", [["Spec1 Accuracy", "prompt"], ["Spec2 Accuracy", "prompt"], ["Spec Missed Opportunity", "prompt"]]],
  ["Details", [["Additional Details Accuracy", "prompt"], ["Additional Details Missed Opportunity", "prompt"]]],
  ["Conversation", [["Looping Behavior", "prompt"], ["OnCall AstBuy Error (Transferred)", "flow"]]],
  ["Speech", [["WER/STT", "asr"], ["Dead air", "platform", true], ["Pronunciation", "platform", true]]],
  ["Catalog", [["MCAT/ Search errors", "knowledge"], ["Category ISQ's Error", "knowledge"]]],
  ["Compliance", [["Never claims to be human", "prompt"], ["No forbidden commitments", "prompt"], ["Language policy", "prompt"]], "Vani's prompt rules"],
];

const GATE = [  // improve.GATE_CHECKS: [id, blocking, question]
  ["alters_commitments", true, "Does B make the bot state or promise any fact, price, delivery time or assurance that is not in A?"],
  ["weakens_safety_rule", true, "Does B remove or weaken an identity, honesty, compliance or consent instruction, for example the rule never to claim to be human?"],
  ["contradicts_instruction", true, "Does B contradict another instruction in the prompt?"],
  ["raises_ask_limit", false, "Does B let the bot ask for any field more times than A allows?"],
  ["likely_longer_calls", false, "Is B likely to make calls longer than A?"],
];

// ------------------------------------------------------------------------------------- pieces
const num = (id) => String(SECTIONS.findIndex(([s]) => s === id) + 1).padStart(2, "0");
const sec = (id, title, body) => html`<section class="dsec" id="doc-${id}" data-s="${id}" aria-labelledby="doc-${id}-h">
  <div class="eyebrow"><a href="#/docs/${id}" title="Link to this section" aria-label="Link to section ${num(id)}">${num(id)}</a></div>
  <h2 id="doc-${id}-h" tabindex="-1">${title}</h2>${body}</section>`;
// Every cell carries its column's name, so a narrow column can stack each row under its labels (docs.css).
const tbl = (head, rows, cls = "") => html`<div class="tw"><table class="table ${cls}"><thead><tr>${head.map((h) => html`<th>${h}</th>`)}</tr></thead>
  <tbody>${rows.map((r) => html`<tr>${r.map((c, i) => html`<td data-label="${head[i]}">${c}</td>`)}</tr>`)}</tbody></table></div>`;
const callout = (label, body, cls = "") => html`<aside class="callout ${cls}"><span class="eyebrow">${label}</span>${body}</aside>`;
const icon = (route) => raw($(`.nav a[data-route="${route}"] svg`)?.outerHTML || "");  // the nav's own icon
const tocList = () => html`<ol>${SECTIONS.map(([id, t]) => html`<li><a href="#/docs/${id}" data-s="${id}"><span class="n">${num(id)}</span><span>${t}</span></a></li>`)}</ol>`;
// Trusted static text with # comments in <i>. The padding before a comment is its own span, so a narrow
// column can drop it and set the comment under its command, and a word with a hyphen never breaks at it
// (a flag split at its hyphen reads as another flag); docs.css does the rest.
const code = (lines) => raw(`<pre class="cmds">${lines.replace(/( +)<i>/g, '<span class="pad">$1</span><i>')
  .replace(/[^\s<>]*-[^\s<>]*/g, '<span class="nw">$&</span>')}</pre>`);
const GO = html`<a href="#/docs/loop">the go-ahead</a>`;  // defined once, in the loop's "Real calls wait for a yes"

function ring() {  // the eight stages around one cycle, in viewBox units so it scales to the column
  const N = STAGES.length, cx = 215, cy = 196, R = 118, gap = 0.2, f = (n) => n.toFixed(1);
  const ang = (i) => -Math.PI / 2 + (2 * Math.PI * i) / N, at = (a, r = R) => [cx + r * Math.cos(a), cy + r * Math.sin(a)];
  let out = "";
  STAGES.forEach(([, name], i) => {
    const kind = i === N - 1 ? "back" : i >= 4 ? "p" : "b";  // the arc leaving stage i; Replay -> Prove hands B to the engine
    const a1 = ang(i + 1) - gap, [x0, y0] = at(ang(i) + gap), [x1, y1] = at(a1), t = a1 + Math.PI / 2;
    const barb = (d) => `${f(x1 + 6 * Math.cos(t + d))} ${f(y1 + 6 * Math.sin(t + d))}`;
    out += `<path class="arc a-${kind}" d="M${f(x0)} ${f(y0)}A${R} ${R} 0 0 1 ${f(x1)} ${f(y1)}"/><path class="tip a-${kind}" d="M${barb(2.6)}L${f(x1)} ${f(y1)}L${barb(-2.6)}"/>`;
    const a = ang(i), [nx, ny] = at(a), [lx, ly] = at(a, R + 28), c = Math.cos(a), s = Math.sin(a);
    out += `<g class="node ${i >= 5 ? "n-p" : "n-b"}"><circle cx="${f(nx)}" cy="${f(ny)}" r="14"/><text class="no" x="${f(nx)}" y="${f(ny + 3.8)}" text-anchor="middle">${i + 1}</text>`
      + `<text class="nm" x="${f(lx)}" y="${f(ly + (s < -0.9 ? -2 : s > 0.9 ? 12 : 5))}" text-anchor="${c > 0.3 ? "start" : c < -0.3 ? "end" : "middle"}">${name}</text></g>`;
  });
  const [qx, qy] = at(ang(N - 0.5), R + 16);
  return `<svg class="rg" viewBox="0 26 430 342" role="img" aria-label="One cycle of the loop: Listen, Judge, Improve, Gate and Replay write Prompt B; Prove, Approval and Roll out test it on simulated traffic and make it production only on a win and a named yes; then the next cycle starts again at Listen.">${out}
    <text class="nx" x="${f(qx)}" y="${f(qy)}" text-anchor="end">next cycle</text>
    <text class="c1" x="${cx}" y="${cy - 36}" text-anchor="middle">ONE CYCLE</text>
    <text class="c2" x="${cx}" y="${cy + 2}" text-anchor="middle">Prompt B</text>
    <text class="c3" x="${cx}" y="${cy + 26}" text-anchor="middle">written from the calls,</text>
    <text class="c3" x="${cx}" y="${cy + 43}" text-anchor="middle">tested on 10% of</text>
    <text class="c3" x="${cx}" y="${cy + 60}" text-anchor="middle">simulated leads</text></svg>`;
}

function corridor(width) {  // the planned win and harm boundaries, drawn at the figure's own width so text stays legible
  const H = width < 520 ? 270 : 310, m = { l: 36, r: 12, t: 22, b: 40 }, hi = 5.6, lo = -3.2, f = (n) => n.toFixed(1);
  const X = (t) => m.l + (t / 1.04) * (width - m.l - m.r), Y = (z) => m.t + ((hi - z) / (hi - lo)) * (H - m.t - m.b);
  const pts = (zs) => PLAN.t.map((t, i) => `${f(X(t))},${f(Y(zs[i]))}`).join(" ");
  const t0 = PLAN.t[0], wl = pts(PLAN.win), hl = pts(PLAN.harm);
  const txt = (t, z, s, cls, anchor = "start") => `<text class="${cls}" x="${f(X(t))}" y="${f(Y(z))}" text-anchor="${anchor}">${s}</text>`;
  const hline = (z, cls) => `<line class="${cls}" x1="${f(X(t0))}" x2="${f(X(1))}" y1="${f(Y(z))}" y2="${f(Y(z))}"/>`;
  return `<svg class="zchart cor" viewBox="0 0 ${width} ${H}" width="${width}" height="${H}" role="img" aria-label="The headline experiment's planned boundaries over its seven looks: B wins (with approval promotion it then waits for a person) only if z reaches 4.88 at the first look, falling to 2.06 at the last, with the guardrail clear; z at or below about minus 2.2 at any look stops B; in between, the test keeps collecting.">
    <g class="grid">${[-2, 0, 2, 4].map((z) => `<line x1="${m.l}" x2="${width - m.r}" y1="${f(Y(z))}" y2="${f(Y(z))}"/>`).join("")}</g>
    <polygon class="zone-win" points="${wl} ${f(X(1))},${f(Y(hi))} ${f(X(t0))},${f(Y(hi))}"/>
    <polygon class="zone-harm" points="${hl} ${f(X(1))},${f(Y(lo))} ${f(X(t0))},${f(Y(lo))}"/>
    ${hline(0, "zero")}${hline(1.96, "naive")}
    <polyline class="b-win" points="${wl}"/><polyline class="b-harm" points="${hl}"/>
    ${PLAN.t.map((t, i) => `<circle class="w" cx="${f(X(t))}" cy="${f(Y(PLAN.win[i]))}" r="3"/><circle class="h" cx="${f(X(t))}" cy="${f(Y(PLAN.harm[i]))}" r="3"/>`).join("")}
    ${txt(0.6, 4.5, "B wins", "zone-lbl w")}${txt(0.45, -2.95, "Stop B: harm", "zone-lbl h")}${txt(t0 + 0.02, 0.8, "Keep collecting", "zone-lbl")}
    ${txt(t0 + 0.02, 2.22, "1.96 · peeking", "lbl")}
    ${txt(t0 + 0.02, 4.98, "4.88", "val w")}${txt(0.99, 2.5, "2.06", "val w", "end")}
    ${txt(t0 + 0.02, -2.68, "−2.18", "val h")}${txt(0.985, -2.62, "−2.12", "val h", "end")}
    <g class="axis">${[-2, 0, 2, 4].map((z) => `<text x="${m.l - 8}" y="${f(Y(z) + 3.5)}" text-anchor="end">${z > 0 ? "+" : z < 0 ? "−" : ""}${Math.abs(z)}</text>`).join("")}
      ${[0.2, 0.4, 0.6, 0.8, 1].map((t) => `<text x="${f(X(t))}" y="${H - m.b + 16}" text-anchor="middle">${t.toFixed(1)}</text>`).join("")}
      <text x="${m.l}" y="${H - 6}">information fraction t</text><text x="${m.l - 8}" y="${m.t - 9}" text-anchor="end">z</text></g></svg>`;
}

// ----------------------------------------------------------------------------------- sections
const hero = () => html`<header class="dhero">
  <div class="eyebrow accent">Docs · How it works</div>
  <h1 class="title">How Vani Lab <em>works</em></h1>
  <p class="lede">One page for anyone new to the platform: what each page is for, what each stage of the loop does, and how far to trust each number. Every figure here comes from the code, the configs, or the committed docs and reports.</p>
  <figure class="doha">
    <blockquote lang="hi"><p class="deva"><span>धीरे-धीरे रे मना, धीरे सब कुछ होय।</span><span>माली सींचे सौ घड़ा, ऋतु आए फल होय॥</span></p></blockquote>
    <p class="tr" lang="hi-Latn"><span>Dheere-dheere re mana, dheere sab kuch hoye,</span><span>maali seenche sau ghada, ritu aaye phal hoye.</span></p>
    <figcaption><p class="en">“Slowly, O mind, everything comes in its own time: the gardener may pour a hundred pots of water, but the fruit comes only in its season.”</p>
      <cite>Kabir</cite></figcaption>
  </figure>
</header>`;

const overview = () => sec("overview", html`What Vani Lab is`, html`
  <p class="lead">Vani Lab is the platform Team Hare &amp; Tortoise built for Vani, IndiaMART's buyer-side voice bot. It listens to Vani's calls, grades each one on IndiaMART's own QA checklist, finds the highest-priority failure a prompt change can fix, writes the fix (Prompt B), tests it in simulated conversations and then on 10% of simulated leads with a sequential A/B test, and, once a named person approves, makes it production in its prompt registry. Then it starts again on the new prompt, which no live bot has spoken yet: its next audit runs on the up to eight simulated replay conversations that tested it.</p>
  <p><b>There is no live traffic yet.</b> The A/B test replays real buyer-call history with an effect assumed for B, so its result comes from that assumption, not from what Prompt B says: it exercises the engine and the approval path, not Prompt B. And “production” is a status in the platform's own prompt registry; nothing is deployed to the live bot.</p>
  <p>Without it, a change to Vani's prompt goes live to every caller on judgement alone. If the change is bad, it costs meetings until someone notices; if it is good, nobody can prove it. Vani Lab has two halves: <b>the loop</b> writes the change from the calls themselves, and <b>the A/B engine</b> decides, from nothing but who got which prompt and what happened on their calls; with no live traffic, those calls are replayed history with an assumed effect for B. Their code lives in <code>src/abtest/ext/</code> and <code>src/abtest/</code>.</p>
  <blockquote class="pull"><p><span>The hare ships a prompt change on a hunch.</span> <em>The tortoise ships it on evidence.</em></p></blockquote>
  <figure class="ringfig">${raw(ring())}
    <figcaption><ul class="key">
      <li><i class="b"></i><span><b>The loop writes Prompt B.</b> Listen, Judge, Improve, Gate and Replay turn graded calls into one prompt change, tested in simulated conversations.</span></li>
      <li><i class="p"></i><span><b>The engine and a person decide.</b> Prove, Approval and Roll out give B 10% of simulated leads and make it production only on a win and a named yes.</span></li>
      <li><i class="back"></i><span><b>Then it starts again.</b> On B if it won, auditing its replay conversations (up to eight, simulated), since no live bot has spoken it; otherwise on A, with the next root cause.</span></li>
    </ul><p class="keynote">On the <a href="#/">Loop page</a> the ring shows five stations: Improve holds Gate and Replay, and Prove holds Approval. The steps under it list all eight.</p></figcaption></figure>
  <h3>Start here</h3>
  <ol class="steps">
    <li><a href="#/audit">Audit</a>: check how far the judge agrees with your auditors, then open the calls a person should listen to first.</li>
    <li><a href="#/calls">Calls</a>: read any call with the judge's grade and reason for every rubric, beside the human grade.</li>
    <li><a href="#/rubrics/new">Rubric Studio</a>: add a rule in your auditors' words and try it on a call.</li>
    <li><a href="#/fixes">Fixes</a>: read Prompt B as a redline, with its safety gate and its replay.</li>
    <li><a href="#/experiments">Experiments</a>: when a test waits for a person, approve B or reject it, with your name on the log.</li>
  </ol>`);

const PAGE_DOCS = [  // [nav route, title, href, what you use it for, what it shows]
  ["loop", "Loop", "#/", "Use it to see where the loop stands and run its next step.", html`<p>The current cycle's station on the ring, with all eight steps under it; what happens next; the key numbers (calls graded, the fatal rate, agreement with QA, root causes, prompt fixes and the weekly impact); the cycle's root cause, fix and A/B test; and every earlier cycle, newest first.</p>
    <p>Its main button runs the next step in the background. Before the A/B test a second one, <b>Run until it needs you</b>, keeps going until the loop needs a person (an approval, or ${GO} for real call content), a step is blocked or the cycle closes. Both ask first, naming the models the run calls, the spend so far and whether real call content may be sent, and a run log shows each step. On a platform started without the go-ahead, a step that needs it shows the terminal command instead of a button; a cycle waiting for approval links to its experiment.</p>`],
  ["calls", "Calls", "#/calls", "Use it to read any call the way an auditor would.", html`<p>Every transcribed call, worst first: its source (production, audited or replay), length and language; the judge's grade, with how many parameters failed; where IndiaMART audited the call, the QA grade beside it and whether the two are the same; and what happened: the disposition, the judge's one-line summary and its risk flags. Filter by source, the judge's grade, the QA grade, whether they agree, risk flag or disposition; search by call id, summary or disposition.</p>
    <p><b>A call's page</b> opens with the judge's grade beside QA's, the parameters that failed and the ones QA graded differently. It plays the recording when it is on this machine, with a lane showing who spoke when; shows the transcript with the judge's evidence as margin notes (hover a note to light up its quote, click a time to play from it); and ends with the scorecard: every rubric's grade and reason beside QA's, and whether they agree. The two audio-only checks show “—” there: a transcript cannot grade them, so there is nothing to compare.</p>`],
  ["audit", "Audit", "#/audit", "Use it to decide how far to trust the judge before acting on its grades.", html`<p>For one prompt version (switch at the top), in this order: whether the judge can be trusted yet, from how far it agrees with IndiaMART's auditors; the Fatal, Non-fatal and Pass split for the judge and for QA on the same calls; where the judge and QA differ, parameter by parameter (open one for its confusion grid); which parameters fail most; and the calls to listen to first. <a href="#/docs/judge">How far to trust the judge</a> explains its words.</p>`],
  ["rubrics", "Rubrics", "#/rubrics", "Use it to see and change what the judge checks.", html`<p>Everything the judge grades, grouped by category, archived rubrics last. Each row shows the rubric's root type and kind (QA matrix, prompt rule or custom), the judge's grades on the production version, how far the judge agrees with QA on it (<i>Judge only</i> where the audit sheet has no column) and its weight.</p>
    <p><b>The Rubric Studio</b> (<a href="#/rubrics/new">New rubric</a>, or open one) writes or edits a rubric beside a live preview of its card, tries it on a call, and saves it as a new version. See <a href="#/docs/grades">Grades and rubrics</a>.</p>`],
  ["issues", "Issues", "#/issues", "Use it to see what fails most, and why.", html`<p>The root causes on one prompt version, ranked by the published priority formula (read as fatal-call equivalents per 100 calls judged), with the calls each affects, how many of them were fatal, its type, where it stands (a fix drafted, not tried yet, or speech, catalogue or platform work), how far the judge agrees with QA on its rubric, and the calls to open first. The top prompt or flow cause not yet tried on this version is what the next fix targets.</p>`],
  ["fixes", "Fixes", "#/fixes", "Use it to read exactly what a fix changes and how it was screened.", html`<p>Every Prompt B the fixer wrote, with its root cause, its gate result and its replay.</p>
    <p><b>A fix's page</b> is a redline of Prompt A to Prompt B (struck words removed, underlined words added, a margin note with each edit's reason), the safety gate's checks against their thresholds, and the replay: the real calls, simulated A and simulated B, and what changed.</p>`],
  ["prompts", "Prompts", "#/prompts", "Use it to trace every version back to the fix behind it.", html`<p>The lineage from v1 on, one generation per column: each version's status (production, candidate, retired or rejected), its parent, its fix and how that fix did at the gate, in the replay and in the A/B test. Open one to read what changed from its parent, or its full text with a find box, beside its sha256.</p>`],
  ["experiments", "Experiments", "#/experiments", "Use it to see what the engine decided, and to approve or reject a win.", html`<p>Every A/B run, from the loop and from the engine's own scenarios, with its outcome, checkpoints and lift, always labelled simulated, with its assumed effect.</p>
    <p><b>An experiment's page</b> says what B changed and what the engine decided, then the lift with its 95% CI, the guardrail against its limit, the checkpoints so far, the leads counted, the impact a week and the split at the end; the evidence for B (z) at every checkpoint against the win and harm lines; the append-only decision log; and, folded under <i>Engine details</i>, the checkpoint table and the frozen configuration. While it is PENDING_APPROVAL, a panel asks for your name: <b>Approve B</b> or <b>Reject</b>.</p>`],
  ["settings", "Settings", "#/settings", "Use it to check what runs where, and what it costs.", html`<p>Which model runs each role, the model spend against its cap, where every file lives, whether rubric tests on real calls are on (<code>LOOP_ALLOW_REAL=1</code>), and the privacy note.</p>`],
  ["docs", "Docs", "#/docs", "Use it to look anything up.", html`<p>This page. Every section has its own link, such as <a href="#/docs/prove">#/docs/prove</a>, and is in the search: press ⌘K or Ctrl K and type <i>docs</i>.</p>`],
];

const pages = () => sec("pages", html`What each page is for`, html`
  <p>The rail follows the loop: the Loop page on top, then <b>Listen &amp; judge</b> (Calls, Audit, Rubrics), <b>Improve</b> (Issues, Fixes, Prompts), <b>Prove</b> (Experiments), <b>Platform</b> (Settings) and <b>Learn</b> (these docs). On a narrow screen it folds into a menu button that names the page you are on. Search (⌘K or Ctrl K) jumps to any page, prompt version, root cause, experiment, fix, rubric, call or section of these docs. Pages describe themselves when they are empty, with the command that fills them.</p>
  <div class="pgs">${PAGE_DOCS.map(([route, title, href, use, body]) => html`<article class="pg">
    <div class="ph"><span aria-hidden="true">${icon(route)}</span><h3><a href="${href}">${title}</a></h3><code>${href}</code></div><p class="use">${use}</p>${body}</article>`)}</div>
  ${callout("Demo data", html`<p>Served without the platform behind it, the site runs on demo data: invented calls, names, products and grades beside real engine runs. It says so on the page. Run <code>make platform</code> for the real data.</p>`)}`);

const STAGE_DOCS = [  // per ui.js STAGES: [what happens, model role, what stops it or makes it wait, where to see it]
  [html`The loop's Listen stage counts the calls this prompt version has and calls no model. Those calls are transcribed beforehand by <code>make listen</code>: recordings become bot and buyer transcripts with timestamps, Hindi, Hinglish and Telugu are written in Latin script the way IndiaMART's auditors write them, and every phone number becomes <code>[number]</code>. On v1 the loop judges, by default, the buyer-side calls IndiaMART's QA team audited. A later prompt version was never spoken by a live bot, so its calls are the replay conversations that tested it, labelled <i>replay</i>.`,
    html`None in the loop; <code>make listen</code> uses <code>transcribe</code>`,
    html`Waits until the calls are transcribed; <code>make listen</code> sends recordings only with <code>--yes-send-real-audio</code>.`,
    html`<a href="#/calls">Calls</a>`],
  [html`One model call grades a call on every active rubric: Pass, Non-fatal, Fatal or N/A, each with a confidence, a one-sentence reason and the quote behind it, plus the call's disposition, risk flags and a one-line summary. A call already graded with the same rubric set and model, and not transcribed again since, is not sent again.`,
    html`<code>judge</code>`,
    html`Waits for ${GO} when the calls are real (replays built from real calls count as real), and when no call could be graded.`,
    html`<a href="#/audit">Audit</a>, <a href="#/calls">Calls</a>`],
  [html`The judge model groups each rubric's failing calls into at most three named root causes, each with a type, plus an “Other” group for any call it leaves out. The ranking is computed in code with the published priority formula, not by the model. The cycle takes the highest-priority prompt or flow cause that this prompt version has not tried yet; once a cause on the version has been tried, later cycles on it keep that first ranking.`,
    html`<code>judge</code>, for the grouping`,
    html`Waits for ${GO} when the calls are real. Blocked when no untried prompt or flow cause is left: the rest need speech, catalogue or platform work. A blocked cycle stays blocked; advancing does nothing more.`,
    html`<a href="#/issues">Issues</a>`],
  [html`The fixer drafts Prompt B: one to three minimal edits to Prompt A aimed at that cause, each with its reason. The gate model then answers five safety questions about the change; a blocking one at probability 0.5 or more stops it.`,
    html`<code>fixer</code>, then <code>gate</code>`,
    html`Waits for ${GO} before drafting when the calls are real (the fixer sees call excerpts). ${stateChip("FIX_INVALID")} the edits do not apply to Prompt A; ${stateChip("GATE_BLOCKED")}`,
    html`<a href="#/fixes">Fixes</a>, then the fix's page`],
  [html`Up to eight of the cause's failing calls become buyer personas. A simulated buyer plays each one against Prompt A and again against Prompt B, and the same judge grades both conversations. B moves on only if it fails the cause's rubric in fewer of them than A did.`,
    html`<code>replay</code> for personas, bot and buyer; <code>judge</code> grades`,
    html`Waits for ${GO} when the calls are real. ${stateChip("REPLAY_NO_GAIN")}; ${stateChip("REPLAY_FAILED")} when the replay cannot run`,
    html`The fix's page`],
  [html`B gets 10% of simulated leads through the A/B engine: the loop's template experiment with A as production and B as the candidate, replaying real buyer-call history with an assumed effect for B. The engine decides at every look; no model is called.`,
    html`None: the engine is statistics`,
    html`${stateChip("STOPPED_HARM")} ${stateChip("BLOCKED_GUARDRAIL")} ${stateChip("INCONCLUSIVE")} ${stateChip("HALTED_SRM")}; a win waits as ${stateChip("PENDING_APPROVAL")}`,
    html`<a href="#/experiments">Experiments</a>`],
  [html`A win waits for a person, with B still on its 10% of simulated leads. Approve or reject on the experiment's page, where your name goes on the decision log, or with the loop's <code>approve</code> command.`,
    html`None: a named person decides`,
    html`${stateChip("REJECTED")}`,
    html`The experiment's page`],
  [html`Once approved, B becomes production in the prompt registry and the old version retires, but only after a check that the experiment tested exactly B's text (by its sha256). Nothing is deployed to the live bot. The next cycle opens on B and audits its up to eight replay conversations, since no live bot has spoken it. Any other ending rejects B and keeps A, and the next cycle tries the next root cause.`,
    html`None`,
    html`Refuses a B whose text differs from what the experiment tested.`,
    html`<a href="#/prompts">Prompts</a>, <a href="#/">Loop</a>`],
];

const loop = () => sec("loop", html`The loop, stage by stage`, html`
  <p>A cycle runs on one production prompt version. Each stage saves the cycle before the next one runs, so advancing again after a stop picks up exactly where it left off. Advancing stops when the cycle needs something (transcripts, your go-ahead, an approval), when it ends, or when no cause a prompt change can fix is left. A failed try does not stop it: the cycle moves straight on to the next root cause.</p>
  <ol class="stages">${STAGES.map(([, name], i) => { const [what, model, stop, where] = STAGE_DOCS[i]; return html`
    <li class="stage ${i < 5 ? "b" : "p"}"><span class="no" aria-hidden="true">${i + 1}</span><div class="st">
      <h3>${name}</h3><p>${what}</p>
      <dl class="facts"><div><dt>Model role</dt><dd>${model}</dd></div><div><dt>Stops or waits</dt><dd>${stop}</dd></div><div><dt>See it on</dt><dd>${where}</dd></div></dl>
    </div></li>`; })}</ol>
  ${callout("Three tries a cycle", html`<p>A try that ends before the A/B test (FIX_INVALID, GATE_BLOCKED, REPLAY_NO_GAIN or REPLAY_FAILED) rejects its candidate, if one was drafted, and the cycle moves on to the next root cause, up to three tries. After the third failure the cycle closes, and the next cycle opens on the same production prompt and goes on to the causes not tried yet.</p>`)}
  ${callout("Real calls wait for a yes", html`<p>Judge, Improve, Gate and Replay send call content to the model provider. On real calls, and on replays built from them, they wait for the go-ahead: <code>--yes-send-real-transcripts</code> on the command line, or a platform started with <code>LOOP_ALLOW_REAL=1</code>, whose buttons then send it. Without it, the Loop page's buttons stop at that step and show the command.</p>`)}
  <h3>Who does what</h3>
  <p>Each model is a role with a default in <code>src/abtest/ext/llm.py</code>; set <code>LLM_MODEL_&lt;ROLE&gt;</code> to change one. Prove, Approval and Roll out call no model.</p>
  ${tbl(["Role", "Default model", "Used for"], [
    [html`<code>transcribe</code>`, "google/gemini-3-flash-preview", html`<code>make listen</code>: audio to transcript`],
    [html`<code>judge</code>`, "anthropic/claude-sonnet-5.5", "Grading every call, grouping root causes, grading the replays, the Rubric Studio's Try it"],
    [html`<code>fixer</code>`, "anthropic/claude-opus-5.5", "Writing Prompt B"],
    [html`<code>gate</code>`, "typesafe/jev-router", "The safety gate: Jev, TypeSafe's router, which picks a model and reasoning effort per request"],
    [html`<code>replay</code>`, "anthropic/claude-haiku-5.5", "Buyer personas, the simulated bot and the simulated buyer"],
    [html`<code>judge_fast</code>`, "anthropic/claude-haiku-5.5", "Configured; no stage calls it today"],
  ])}`);

const grades = () => sec("grades", html`Grades and rubrics`, html`
  <p>A rubric is one question the judge answers about a call, in the words IndiaMART's auditors use. Every rubric gets one of four grades on every call:</p>
  <dl class="grades">
    <div><dt>${grade("pass", "boxed")}</dt><dd>The rubric applies and the bot met it.</dd></div>
    <div><dt>${grade("non_fatal", "boxed")}</dt><dd>A flaw that lowers the call or the lead's quality but leaves the lead usable.</dd></div>
    <div><dt>${grade("fatal", "boxed")}</dt><dd>The lead is wrong or unusable, or the buyer was lost because of the bot.</dd></div>
    <div><dt>${grade("na", "boxed")}</dt><dd>The rubric does not apply to this call, or the transcript cannot show it.</dd></div>
  </dl>
  <p>A call's overall grade is its <b>worst</b> rubric, with N/A ignored. That is the QA team's own rule: 1,941 of the 1,962 rows of IndiaMART's audit sheet follow it.</p>
  <h3>Twenty-one built-in rubrics</h3>
  <p>Eighteen are IndiaMART's QA matrix, one per graded column of the human audit sheet, so the judge's grades line up with the auditors' column by column. Three more are rules from Vani's own prompt that the sheet has no column for, so there is no human grade to compare them with. Unmarked rubrics have the root type <i>prompt</i>; the others show theirs, explained below.</p>
  <div class="rsets">${RUBRICS.map(([cat, rs, note]) => html`<div class="rset"><div class="rh"><span>${cat}</span><small>${note || "QA matrix"} · ${rs.length}</small></div>
    <ul>${rs.map(([name, type, audio]) => html`<li><span>${name}</span>${type !== "prompt" ? typeChip(type) : ""}${audio ? html`<span class="chip">audio</span>` : ""}</li>`)}</ul></div>`)}</div>
  ${callout("Two are heard, not read", html`<p>Dead air and Pronunciation need the recording. A transcript-only judge records them as N/A (“graded by listening, not from the transcript”), so they never move a call's overall grade, and a call's scorecard shows “—” where it would say whether the judge and QA agree: there is nothing to compare. The Rubric Studio's Try it skips that step, so it can show a grade for an audio rubric that a saved audit records as N/A.</p>`)}
  <h3>Root types: where a failure gets fixed</h3>
  ${tbl(["Type", "The failure comes from", "A prompt change can fix it", "Built-ins"], [
    [typeChip("prompt"), "The bot's instructions", "Yes", "14"],
    [typeChip("flow"), "The call flow or a tool's design", "Yes", "2: Product change, AstBuy transfer"],
    [typeChip("asr"), "Speech recognition mishearing the buyer", "No: speech work", "1: WER/STT"],
    [typeChip("knowledge"), "Missing product or catalogue knowledge", "No: catalogue work", "2: MCAT, ISQ"],
    [typeChip("platform"), "Telephony or the system", "No: platform work", "2: Dead air, Pronunciation"],
  ])}
  <p>A rubric's type is the default for its root causes; the grouping can type a cause differently when the evidence points elsewhere, and the Issues page says why. Only prompt and flow causes go to the fixer.</p>
  <h3>Write your own in the Rubric Studio</h3>
  <ol class="steps">
    <li>Open <a href="#/rubrics">Rubrics</a> and choose <b>New rubric</b>, or open any rubric to edit it.</li>
    <li>Fill in the card: a <b>Name</b> and <b>Category</b>; the <b>Question the judge answers</b>; <b>Fatal when</b> and <b>Non-fatal when</b> (Pass is “otherwise”); <b>Applies when</b> (empty means every call, otherwise the grade is N/A); optional <b>Guidance</b> in your auditors' words; the <b>Root type</b>; a <b>Weight</b> from 1 to 5; <b>Needs audio</b>; example phrases that pass and that fail; and <b>Your name</b>, recorded as the author of this version. The card beside the form shows what the judge reads: the name, the question, when it applies, the guidance, the Fatal and Non-fatal rules and the example phrases. The category, root type, weight and author are for people; the judge never sees them.</li>
    <li><b>Try it on a call</b> grades one call with your draft: one model call, nothing saved. Grading a call sends it to the model provider, so real calls, and replays built from them, are tried only on a platform started with <code>LOOP_ALLOW_REAL=1</code>; without it the call list leaves real calls out, and a replay built from one is refused.</li>
    <li><b>Save.</b> A new rubric is v1. Saving an existing one, built-ins included, adds the next version and keeps the old ones, because every verdict cites the version it was graded with.</li>
    <li><b>Archive</b> to stop the judge using it on new calls; past verdicts keep citing it. Built-ins can be archived, never deleted, and <b>Restore</b> brings one back.</li>
  </ol>
  <p>Weight is recorded with the rubric and shown on its card, but nothing uses it yet: the priority formula counts only fatal and non-fatal grades. On demo data, saving keeps the rubric in this browser only, and Try it uses phrase matching instead of the judge model.</p>
  ${callout("A changed set re-grades the calls", html`<p>The judge reuses a call's saved grades only when they came from the same rubric set (a hash of every active rubric's id, version and text) and the same model, so after you add, edit or archive a rubric the next Judge stage grades the calls again; a call whose re-grade fails keeps its old grades, which the pages and the root causes still read, until it is graded again. The root causes are not always re-ranked: once a cycle on a prompt version has tried a cause, later cycles on that version keep its first ranking, so a new rubric's failures become root causes only on a prompt version no cycle has worked on yet.</p>`)}`);

const judge = () => sec("judge", html`How far to <em>trust</em> the judge`, html`
  <p>The judge is a model reading a transcript, so the platform measures it against the people whose checklist it uses: IndiaMART's QA auditors. On the calls both graded, the SquadStack inbound calls the auditors audited, it compares the grades parameter by parameter, with N/A counted as a grade of its own, and on the call's overall grade, both as three grades and as Fatal against not fatal.</p>
  <p>The overall comparison is like for like: on both sides it is recomputed as the worst grade over the same text-observable QA parameters. The audio-only ones and the prompt rules, which the sheet lacks, are left out of both.</p>
  <h3>Calibration: learned by make agreement, then applied as is</h3>
  <p>The audit sheet fills several columns only when something notable happened, so a flaw the judge calls minor is often N/A or Pass to an auditor. <code>make agreement</code> learns, per QA parameter, what the auditors call the judge's Non-fatal flags, on the first audited calls in sheet order (forty at most, and never more than a quarter of them), and reports agreement on the rest, which it never saw. Each run learns it again and saves it.</p>
  <ul class="list">
    <li>It only ever turns a Non-fatal grade into Pass or N/A. Fatal, Pass and N/A are never changed, so it cannot hide a fatal call.</li>
    <li>It applies only to grades from the judge model it was learned on, and only to rubrics whose text has not changed since.</li>
    <li>Every page shows the calibrated grades of real calls, and the loop ranks v1's root causes from them: a Non-fatal turned into Pass or N/A no longer counts as a failure, and its reason ends “(minor flag, below the QA team's threshold)”. Replay conversations keep the judge's own grades.</li>
    <li>A run that fails saves nothing: the last good calibration and report stay in force.</li>
  </ul>
  <h3>Reading the numbers, in the Audit page's words</h3>
  <dl class="gloss compact">
    <div><dt>Matches QA</dt><dd>The share of calls where the judge gave the same grade as IndiaMART's auditor (per parameter, N/A counts as a grade of its own). On the overall grade the Audit page also gives the count: “It matches IndiaMART QA's overall grade on <i>n</i> of <i>m</i> calls.”</dd></div>
    <div><dt>Always “Pass”</dt><dd>A trivial guess, for scale: how often giving QA's most common grade on every call would match QA. Matching flatters rare failures: where QA almost always says Pass, a judge that always says Pass matches nearly every call.</dd></div>
    <div><dt>Agreement beyond chance, Cohen's κ</dt><dd>Matching corrected for chance: 0 is guessing, 1 is perfect. Every page puts it in the same words: <i>about chance</i> below 0.2, <i>weak</i> below 0.4, <i>moderate</i> below 0.6, <i>good</i> below 0.8 and <i>strong</i> from 0.8. On the overall grade the Audit page answers <i>Can the judge be trusted yet?</i> with <i>Not reliable yet</i> below 0.4, <i>Use with care</i> below 0.6 and <i>Reliable on the overall grade</i> from 0.6.</dd></div>
    <div><dt>QA-fatal calls caught</dt><dd>Of the calls QA marked Fatal, how many the judge also called Fatal (the agreement report calls it fatal recall).</dd></div>
    <div><dt>Fatal vs not fatal</dt><dd>The share of calls where the judge and QA agree on whether the call is Fatal; the Loop page calls it fatal vs rest.</dd></div>
  </dl>
  <p>On the Calls page each audited call shows <i>Same</i> or <i>Differ</i> for its two overall grades, and a call's scorecard says per parameter whether they agree. The numbers change with every run, so they are not copied here. The <a href="#/audit">Audit page</a> shows the latest, and <code>make agreement</code> writes them to <code>reports/judge_agreement.md</code>.</p>
  ${callout("Honest limit", html`<p>IndiaMART's auditors also judge what the bot saved into the lead record, which the judge cannot see: it reads the transcript only, with no audio, no lead record and no catalogue. And the audio-only checks cannot be graded from text, so the overall comparison leaves them out and a call's scorecard shows “—” for them. Trust the per-parameter rows first: the overall grade can disagree with the auditors' even where most parameters agree.</p>`, "limit")}`);

const fixes = () => sec("fixes", html`Root causes, fixes and the gate`, html`
  <h3>Failures become root causes</h3>
  <p>For each rubric, the judge model groups its failing calls (Fatal or Non-fatal) into at most three distinct, fixable causes, each with a name, a one-line description, a type and its calls. Any call the grouping leaves out goes to an “Other” group, so the counts always add up. The model names and groups; the ranking is computed in code:</p>
  <div class="formula"><span class="eyebrow">Priority</span><code>priority = 100 × (3·fatal + non-fatal) / (3·audited)</code>
    <p>Fatal counts three times a non-fatal. Dividing by every audited call makes causes comparable across rubrics, so a cause that hurts many calls a little can outrank one that hurts a few calls badly. 100 would mean every audited call failed this way, fatally.</p></div>
  <p>The Issues page shows each cause's <i>Calls affected</i> (its frequency: calls affected ÷ calls judged), <i>How bad</i> (how many of them were fatal) and its <i>Priority</i>, read as fatal-call equivalents per 100 calls judged. Each saved cause also records a severity, (3·fatal + non-fatal) ÷ (3 × calls affected).</p>
  <h3>One cause, one minimal fix</h3>
  <p>The highest-priority prompt or flow cause this prompt version has not tried goes to the fixer with Prompt A, up to six excerpts from its calls (the judge's reason and the turns around the evidence, every digit masked as #), and every earlier attempt on that rubric that reached the gate or the replay, with those results; it is told never to repeat a failed idea (it is not told how an attempt did in the A/B test or at approval). It returns one to three edits, each with its reason, and a short rationale:</p>
  <ul class="list">
    <li><b>replace_text</b> rewrites one span, copied character for character from Prompt A and occurring exactly once in it. This is preferred.</li>
    <li><b>append</b> adds a new rule at the end of the prompt.</li>
  </ul>
  <p>It is told never to add facts, prices, delivery times, promises or assurances, never to weaken an identity, honesty, compliance or consent rule, never to contradict another instruction and never to raise an ask limit. Edits that do not apply get one retry; a second failure ends the try as FIX_INVALID. Prompt B is saved with its sha256, so the gate and the replay read exactly the text that was drafted, and the A/B test and the roll-out check that they hold the same text.</p>
  <h3>The safety gate</h3>
  <p>Before the replay, the gate model reads Prompt A, Prompt B and the diff, and answers five questions, each with a probability and a one-sentence reason, both shown on the fix's page. Only prompt text is sent: no call content.</p>
  ${tbl(["Check", "The question, about B against A", "At p ≥ 0.5"], GATE.map(([id, blocking, q]) => [html`<code>${id}</code>`, q,
    blocking ? html`<span class="chip harm">blocks</span>` : html`<span class="chip">shown only</span>`]))}
  <p>An answer that is missing or not a probability counts as 1.0 on a blocking check, so the gate fails closed. The gate runs before the A/B test because a compliance break would not move the A/B metric within two weeks, and it is unacceptable even once.</p>
  <h3>The replay: a screen, not a measurement</h3>
  <p>Up to eight of the cause's failing calls, in its example order, become buyer personas: the buyer's need, product, quantity, language and temperament as they were when the call started. The simulated buyer is also given that buyer's own lines from the call, as hints. It plays each persona twice, once against Prompt A and once against Prompt B, until the bot ends the call or has spoken 14 times. The same judge then grades every conversation.</p>
  <p>B goes on to the A/B test only if it fails the cause's rubric in fewer of these simulated conversations than A did; otherwise the try ends as REPLAY_NO_GAIN. With at most eight pairs and no significance test, one conversation can decide it, so this screens out fixes that clearly do not help; it is not evidence of an effect. Simulated A, not the real calls, is the fair before: both sides went through the same simulation. The fix's page also shows how the real calls did and every other rubric's change, B minus A; the cycle notes a regression, but only the cause's own rubric decides.</p>`);

const prove = () => sec("prove", html`Proving it: <em>the A/B engine</em>`, html`
  <p>The engine decides whether B did better than A on the traffic it is given. It reads only exposures (who got which prompt), calls and the experiment's configuration, which is frozen and hashed when the experiment starts. It never reads the simulator's assumed effect; a test checks that, statically and at runtime. With no live traffic yet, the traffic is simulated (see <i>Simulated traffic, stated plainly</i> below), so in the loop the engine shows itself and the approval path working, not what Prompt B does.</p>
  <h3>The unit is the lead</h3>
  <p>The lead is both the unit that gets a prompt and the unit of analysis. Statistics are never computed over calls: a lead's calls are correlated and always share a prompt, so counting calls would overstate the sample and produce false wins. A lead enters the analysis at its first answered call, the first moment it hears the bot, and counts at a look only once it has matured: one day for buyer calls, two for the seller calls of the headline experiment.</p>
  <h3>Sticky routing, nothing stored</h3>
  <p>The router takes the SHA-256 of the experiment's salt and the lead's id (<code>salt:lead_id</code>), reads its first 15 hex digits as a number, takes that modulo 10,000, and gives B the buckets below 1,000: 10% of leads. The same lead gets the same prompt on every call and in every process, with nothing stored, and each experiment's own salt keeps assignments independent across experiments. The split is live: a promoted B serves everyone, and every other ending sends everyone back to A.</p>
  <h3>What it measures</h3>
  ${tbl(["", "The loop's template", "The headline experiment"], [
    ["Config", html`<code>configs/experiments/vani-buyer-qty-asks-v1.yaml</code>`, html`<code>configs/experiments/vani-meeting-v1.yaml</code>`],
    ["Data replayed", "Buyer call logs: 969 usable calls from 824 buyers over 5 days (the file's 1,000 rows, less 3 with no lead id and one 28-call test-like account), resampled at 4× volume", "Best-Time-to-Call attempts, April to September 2026, at historical volume"],
    ["Primary metric", "BL success: a lead with any answered call ending BL Enriched or BL Approved", "Meeting Fixed per answered lead"],
    ["Baseline", "0.6667", "0.1795"],
    ["Sized to detect (MDE)", "+10% relative", "+15% relative"],
    [html`Planned maximum, n<sub>B,max</sub>`, "About 430 matured triggered B leads", "About 1,900"],
    ["Guardrail", "Abrupt disconnects: worse if they rise, margin +2 pp", "Answered call duration: worse if it rises, margin +10%"],
    ["Split · trigger", "A 90%, B 10% · first answered call", "A 90%, B 10% · first answered call"],
    ["Window · looks · maturation", "14 days · 7 · 1 day", "28 days · 7 · 2 days"],
    ["α win · α harm (both one-sided) · power", "2.5% · 5% · 80%", "2.5% · 5% · 80%"],
    ["Promotion", "approval", "approval"],
  ], "cfgt")}
  <p>Both configs are sized for the primary metric only; a win also needs the guardrail's interval inside its margin. That costs the headline about 4 points of power (76.4% against 80.0%, below). The template's 2 pp disconnect margin is too tight to clear at its n<sub>B,max</sub> unless B also cuts disconnects: with disconnects held flat, even a +15% lift ended BLOCKED_GUARDRAIL in 27 of 40 seeds. So the template can stop B, but it rarely promotes a B that only reaches its +10% MDE. The loop's default assumed effect, +20% BL success, by itself removes about 4 pp of disconnects in this pool, and that is what lets it clear.</p>
  <p>The headline experiment runs on the seller-side bot's data, with the buyer-side prompt standing in for both variants because the seller-side prompt is not in the data. Outcomes never depend on prompt text, so this affects only labels and hashes.</p>
  <h3>Looking early, safely</h3>
  ${callout("Slow, and steady", html`<p>Looking at an A/B test more often cannot make a weak B win; only more evidence can.</p>`)}
  <p>The engine looks as data arrives. Checking a plain z ≥ 1.96 at every look, peeking, would let a lucky streak through, so it uses a group-sequential design: every look has a win boundary and a harm boundary on the z scale, set so the error rates hold over all looks together. Here z measures how far B's primary metric is ahead of A's, in standard errors (z &gt; 0 means B is better), and w<sub>k</sub> and h<sub>k</sub> are look k's win and harm boundaries, harm meaning z ≤ −h<sub>k</sub>. The Experiments page calls a look a <i>checkpoint</i>, z the <i>evidence for B</i>, and the boundaries its <i>win line</i> and <i>harm line</i>.</p>
  <figure class="corfig"><div id="doc-cor" class="cor-host"></div>
    <figcaption>The headline experiment's seven planned looks, from <code>make boundaries</code>. To win, B needs z on or above the green line and a cleared guardrail; z on or below the red line stops it; in between, the test keeps collecting. On the same 2,000 no-effect runs, a plain 1.96 (dotted) is crossed 8.30% of the time on the primary metric; these boundaries, 2.65%.</figcaption></figure>
  <details class="deep"><summary><span class="eyebrow">For the curious</span><span>The statistics behind the boundaries</span></summary>
  <ul class="list">
    <li><b>The statistic.</b> z is the two-proportion z-statistic of the primary metric over matured triggered leads, using the pooled standard error and signed so that z &gt; 0 means B is better; the lift's 95% CI uses the unpooled standard error. h<sub>k</sub> is written as a positive number. The guardrail's change is relative for call duration ((mean B − mean A) ÷ mean A, with a delta-method SE) and absolute for disconnects (rate B − rate A).</li>
    <li><b>Information fraction.</b> t = matured triggered B leads ÷ n<sub>B,max</sub>. Seven looks are planned, equally spaced from t = 0.20 to 1. The runner looks at most once a simulated day, on the first day t passes the next planned point, and the final look comes at t ≥ 1 or on the window's last day. Boundaries are recomputed at the fractions actually observed (0.22 or 1.03, say), which keeps the error rates valid when looks miss the plan.</li>
    <li><b>Alpha spending</b> caps the false-win rate at 2.5% one-sided, however many looks are taken: each look may spend only the share its information fraction allows.</li>
    <li><b>Win: O'Brien–Fleming type,</b> α<sub>w</sub>(t) = 2·(1 − Φ(z<sub>1−α<sub>win</sub>/2</sub> / √t)), with α<sub>win</sub> = 2.5% one-sided. It spends almost nothing early, so at the first look B needs z ≥ 4.877; by the last the bar is 2.061, close to the fixed-design 1.96.</li>
    <li><b>Harm: Pocock type,</b> α<sub>h</sub>(t) = α<sub>harm</sub> · ln(1 + (e − 1)·t), with α<sub>harm</sub> = 5% one-sided. It spends early, about 30% of α<sub>harm</sub> at the first look (t = 0.20) and 9–16% at each later one. That keeps the harm line nearly flat near z = −2.2, so a clearly worse B is stopped at the first look that shows it. A false harm stop only delays a good prompt, so harm gets the looser α.</li>
    <li><b>The guardrail</b> uses a repeated confidence interval: the observed change ± c<sub>k</sub>·SE, with c<sub>k</sub> the win boundary at look k. It stays valid however often you look and is wide early, so it cannot clear on a handful of leads. Both guardrails are worse if they rise: B can win only when the interval's upper end lies below the margin, and B is stopped when its lower end lies above it.</li>
    <li><b>Looking costs little.</b> R is the sample the sequential design needs for the same 80% power, relative to a single fixed test: R = (θ / (z<sub>0.975</sub> + z<sub>0.80</sub>))², where θ is the drift at which the planned win boundaries are crossed by the last look with probability 0.80. Here R ≈ 1.033, so the headline needs n<sub>B,max</sub> ≈ 1,900 matured triggered B leads, against about 1,840 for a fixed design (the template: about 430 against 420).</li>
  </ul>
  <p>The win boundaries also match the reference implementation, gsDesign's <code>sfLDOF</code>, to within 0.001: 4.877, 3.357, 2.680, 2.290 and 2.031 for five equal looks.</p></details>
  <h3>The rules, in order</h3>
  <p>At every look the rules run in this order and the first that matches decides. Only the primary metric can make B win; the guardrail can stop B on its own (rule 2) or hold back a win (rule 4), but never promote B.</p>
  ${tbl(["#", "At a look, if", "Then", "Traffic afterwards"], [
    ["1", "SRM: p < 0.001 on assigned or on triggered leads", html`${stateChip("HALTED_SRM")} Results untrusted; nothing else is checked.`, "A 100%"],
    ["2", html`z ≤ −h<sub>k</sub>, or the guardrail's RCI lies entirely above its margin`, stateChip("STOPPED_HARM"), "A 100%"],
    ["3", html`z ≥ w<sub>k</sub> and the guardrail's RCI lies entirely below its margin (+10% duration, +2 pp disconnects)`, html`${stateChip("PENDING_APPROVAL")} with approval promotion; ${stateChip("PROMOTED")} with auto`, "B keeps 10% until someone decides; B 100% once promoted"],
    ["4", html`z ≥ w<sub>k</sub>, but the guardrail has not cleared`, html`Continue; at the final look, ${stateChip("BLOCKED_GUARDRAIL")}`, "The tested split, then A 100%"],
    ["5", "The final look, and nothing crossed at it", html`${stateChip("INCONCLUSIVE")} With the lift's CI and the lift this sample could have detected.`, "A 100%"],
  ], "rulest")}
  <p><b>SRM</b>, sample-ratio mismatch, is checked first: a 1-df chi-square test of the assigned leads, and separately of the triggered leads, against the configured 90/10, at p &lt; 0.001. It runs at every look without a multiplicity correction, so over seven looks at most about 1.4% of healthy experiments halt by chance (a union bound; less in practice). When the split is broken, no other number can be trusted.</p>
  <h3>A person presses the button</h3>
  <p>Both configs use <code>promotion: approval</code>. A win does not ship B: it parks the experiment at PENDING_APPROVAL with B still on 10%, and a named person approves (PROMOTED: B serves everyone) or rejects (REJECTED: everyone back on A). Their name goes on the append-only decision log. <code>promotion: auto</code> exists too; the engine's win scenario uses it.</p>
  <h3>What a decision is worth</h3>
  <p>Every look and every state change is logged with a plain-English reason, and a state change also records what it would be worth a week at full rollout: the observed absolute lift × the matured triggered leads in both arms ÷ (days elapsed − maturation days, at least 1) × 7. It is the engine's estimate from the experiment's own data, never the simulator's truth, but on simulated traffic that data is simulated too.</p>
  <p>The Loop page adds it up for the loop's own experiments, as simulated BL successes a week: gained by the promoted ones, and protected by the ones whose primary metric crossed the harm boundary. Their lift is the assumed effect (+20% BL success by default) plus noise, and their volume is 4× the 5-day buyer file, so the totals show the arithmetic, not a measured or forecast gain. No buyer sim in <code>configs/sims/</code> makes B worse on BL success, so a harm stop there is a false alarm, not protection.</p>
  <p>For scale, the engine's headline experiment (not the loop's) sees about 885 answered sellers a day at a baseline of 0.1795 Meeting Fixed per answered lead. The +15% it is sized to detect is worth about <b>167 extra Meeting Fixed a week</b> at full rollout; a prompt that loses 30% would cost about <b>330 a week</b>, which is why harm is stopped fast.</p>
  <h3>Does it work?</h3>
  <p>The proof pack, <code>make validate</code>, runs the engine's own analysis, boundaries and rules code on the headline config: 2,000 runs with no real effect and 2,000 at the MDE. To run fast, it draws per-lead outcomes straight from the bootstrap pool and looks at exactly the planned fractions with every lead mature. It does not replay the runner's day-by-day clock, maturation or SRM halts, and it does not cover the loop's template.</p>
  <div class="stats">
    <div class="stat"><div class="eyebrow">False wins</div><div class="big">2.30<small>%</small></div><p>B won (PENDING_APPROVAL here) with no real effect (95% CI 1.69–3.06%); the design allows 2.5%.</p></div>
    <div class="stat"><div class="eyebrow">Naive peeking</div><div class="big harm">8.30<small>%</small></div><p>A plain z ≥ 1.96 at any look, primary metric only, on the same runs. These boundaries: 2.65% on that basis (a win also needs the guardrail, hence 2.30%).</p></div>
    <div class="stat"><div class="eyebrow">False harm stops</div><div class="big">5.00<small>%</small></div><p>B stopped for harm with no real effect (95% CI 4.09–6.05%): right at the 5% the design allows.</p></div>
    <div class="stat"><div class="eyebrow">Power at the MDE</div><div class="big win">76.4<small>%</small></div><p>B won when it truly lifts Meeting Fixed 15% with call duration held flat: the primary alone crosses 80.0%, and the guardrail blocks 3.6%.</p></div>
  </div>
  <p>These are the headline config's numbers, from <code>reports/validation.md</code> (seed 20261009); rerun <code>make validate</code> after an engine change. The loop's template has not been validated this way (see <i>What it measures</i> above).</p>
  ${callout("Simulated traffic, stated plainly", html`<p>There is no live traffic. The loop's A/B tests resample the 824 buyers of the 5-day buyer file at 4× volume, so each one is drawn many times, with an assumed effect for B: by default <code>configs/sims/buyer_win.yaml</code> (<code>LOOP_DEMO_SIM</code> picks another), +20% relative BL success, twice the template's 10% MDE, and −4 pp abrupt disconnects in total. Under it, 98 of seeds 1–100 end PENDING_APPROVAL; the loop runs cycle n at seed 4100 + n. The A/B engine never runs a prompt: it versions and hashes it, and the simulated outcome comes from that assumed effect, not from Prompt B's text (only the replay stage runs the prompts, in simulated conversations). A win is therefore close to certain by construction. It shows the engine and the approval path working; it does not measure what Prompt B does to real buyers.</p>`, "limit")}
  ${callout("Lift after an early stop", html`<p>Decisions use the boundaries and stay valid, but the lift and its 95% CI at the stopping look are the ordinary ones. After an early stop they tend to overstate the effect, and so does the weekly figure built from them.</p>`, "limit")}`);

const running = () => sec("running", html`Running it`, html`
  <p>Everything runs from the repo with <code>make</code> (Python 3.12 through uv). Commands that send real calls to the model provider need an explicit flag: without it <code>make listen</code> and <code>make judge</code> refuse, and <code>make loop</code> stops at the first stage that needs it.</p>
  <h3>The platform</h3>
  ${code(`make setup                       <i># once: uv sync</i>
make platform                    <i># http://127.0.0.1:8600</i>
LOOP_ALLOW_REAL=1 make platform  <i># the same, with your go-ahead for real calls</i>`)}
  <p>A platform with nothing in it shows empty pages, each naming the command that fills it. The site falls back to demo data only when no platform answers, as on a plain static server.</p>
  <h3>The loop</h3>
  ${code(`make listen SOURCE=audited ARGS=--yes-send-real-audio       <i># recordings to transcripts</i>
make judge SOURCE=audited ARGS=--yes-send-real-transcripts  <i># grade every call</i>
make agreement                                              <i># judge vs QA; recalibrates</i>
make loop ARGS=--yes-send-real-transcripts                  <i># advance the current cycle</i>
make loop ARGS="--until judge --yes-send-real-transcripts"  <i># up to and including judge</i>
make loop-status                                            <i># registry, cycles, next action</i>`)}
  <p>The loop's own approval, for experiment <code>vani-loop-c&lt;n&gt;</code> of cycle n, also works from the terminal:</p>
  ${code(`uv run python -m abtest.ext.loop approve vani-loop-c1 --actor NAME            <i># promote B</i>
uv run python -m abtest.ext.loop approve vani-loop-c1 --actor NAME --reject   <i># keep A</i>`)}
  <h3>The A/B engine on its own</h3>
  ${code(`make demo         <i># win, harm, inconclusive, then the approval gate</i>
make dashboard    <i># the engine's dashboard: http://localhost:8501</i>
make validate     <i># 2,000 A/A and 2,000 power runs to reports/validation.md</i>
make boundaries   <i># the win and harm boundaries, checked</i>`)}
  <h3>The flags that send real calls</h3>
  ${tbl(["Flag or setting", "On", "Sends to the model provider"], [
    [html`<code>--yes-send-real-audio</code>`, html`<code>make listen</code>`, "Real call recordings (OpenRouter, then Google)"],
    [html`<code>--yes-send-real-transcripts</code>`, html`<code>make judge</code>, <code>make loop</code>`, "Real call content, replays built from real calls included: transcripts, the judge's reasons and quotes, call excerpts, buyer personas"],
    [html`<code>LOOP_ALLOW_REAL=1</code>`, html`<code>make platform</code>`, "The same call content, from the platform's buttons and the Rubric Studio's Try it"],
  ])}
  <h3>Settings that change behaviour</h3>
  ${tbl(["Variable", "Default", "What it does"], [
    [html`<code>LOOP_CORPUS</code>`, "audited", "The real calls a cycle on v1 judges: audited (the buyer-side calls IndiaMART's QA team graded) or production (the 250-call seller-side sample)"],
    [html`<code>LOOP_ALLOW_REAL</code>`, "off", "Set to 1, it is your go-ahead on the platform: its buttons run the steps that send real call content, and the Rubric Studio can try a rubric on a real call or a replay of one"],
    [html`<code>LLM_BUDGET_USD</code>`, "40", "The spend cap in US dollars: once spend reaches it, every new model call stops with BudgetExceeded; cached answers still return"],
    [html`<code>LLM_MODEL_&lt;ROLE&gt;</code>`, "llm.py", html`The model for one role, for example <code>LLM_MODEL_JUDGE</code>`],
    [html`<code>LOOP_DEMO_SIM</code>`, html`<code>configs/sims/buyer_win.yaml</code>`, html`The assumed effect the loop's A/B test simulates: a repo-relative path to a file in <code>configs/sims/</code>, for example <code>configs/sims/buyer_flat.yaml</code> (B changes nothing)`],
    [html`<code>ABTEST_LOOP_DIR</code>`, "data/derived/loop", "Where the loop keeps its files"],
    [html`<code>ABTEST_DB</code>`, "abtest.db", "The SQLite experiment store the engine's scenarios and the loop's A/B tests run in"],
    [html`<code>OPENROUTER_API_KEY</code>`, "the environment, else .env", html`The model provider's key (<code>OPENROUTER</code> also works)`],
  ])}
  <h3>The cache and the spend cap</h3>
  <p>Every model call goes through one client, <code>src/abtest/ext/llm.py</code>. It caches each request on disk under the SHA-256 of the request (<code>data/derived/loop/llm_cache</code>), so a rerun sends nothing new, costs nothing and gives the same answer. It also records spend across processes (<code>llm_spend.json</code>, shown on <a href="#/settings">Settings</a>) and refuses any new call once spend has reached <code>LLM_BUDGET_USD</code> (calls already in flight when it is reached can overshoot it by their combined cost).</p>
  ${callout("If a run stops on the spend limit", html`<p>The command stops with one line saying so, whether it hit <code>LLM_BUDGET_USD</code> or the key's own limit at the provider. Finished work is saved and cached: raise the limit and run the same command again, and it resumes where it stopped.</p>`)}`);

const privacy = () => sec("privacy", html`Privacy and honesty`, html`
  <ul class="list">
    <li><b>Calls stay on this machine.</b> Recordings, transcripts, audits and generated prompt versions live under <code>data/derived/loop/</code>, beside the provided call files and QA audit sheet in <code>data/</code>. Git ignores all of <code>data/</code>, and nothing in it is ever committed.</li>
    <li><b>The platform answers only this machine.</b> <code>make platform</code> binds to 127.0.0.1 and answers only loopback host names, so nothing on the network can reach it and a DNS-rebinding page is turned away.</li>
    <li><b>Nothing real leaves without a yes.</b> Real call content, replays built from real calls included, goes to the model provider, OpenRouter, only with your go-ahead: <code>--yes-send-real-audio</code> or <code>--yes-send-real-transcripts</code> on the command line, or a platform started with <code>LOOP_ALLOW_REAL=1</code>, whose buttons and Rubric Studio then send it. Without it the web sends nothing real: a step that needs it waits, and the Loop page shows the command. The safety gate sends prompt text only.</li>
    <li><b>Numbers are masked where they are not needed.</b> Transcripts write every phone number as [number]; the root-cause grouping and the fixer see every digit as #, while the judge and the replay read the transcript as transcribed. Of a real call's own record, its transcript keeps at most the bot, the lead type, the vendor, the length and the recording's link: no buyer id and no call time. The agreement report holds aggregates only, with no call ids and no call text.</li>
    <li><b>Replays are simulations, seeded with real buyers.</b> A replay call is a conversation between two models, but its persona is built from a real call, or from a replay of one, and its simulated buyer is given that buyer's own lines as hints. Treat replays as call content: the loop and the Rubric Studio do, and send them only with the same go-ahead.</li>
    <li><b>A/B traffic is simulated.</b> Every experiment replays real call history with an assumed effect for B, and the effect is shown wherever a result appears.</li>
    <li><b>The right calls for the prompt.</b> The 250-call production sample is the seller-side Vani, whose prompt is not in the data. The loop fixes the buyer-side prompt, so it runs on the buyer-side calls IndiaMART's QA team audited (<code>LOOP_CORPUS=audited</code>, the default); <code>LOOP_CORPUS=production</code> runs it on the seller-side sample instead.</li>
    <li><b>Demo data is invented.</b> Without the platform, the site shows invented calls (their ids start with <code>sample-</code>) beside real engine runs, and says so.</li>
  </ul>
  ${callout("One more gap", html`<p>The recordings come from the SquadStack-run Vani, while the prompt the loop edits is the Sarvam-run buyer prompt. Both follow the same checklist, so the failures carry over, but a fix must still be tested on the bot it ships to.</p>`, "limit")}`);

const glossary = () => sec("glossary", html`Glossary`, html`
  <dl class="gloss">
    <div><dt>Buy lead, BL</dt><dd>The requirement record Vani creates from a buyer's call: product, quantity with unit, specifications and other details. A lead with any answered call ending <b>BL Enriched</b> (details collected) or <b>BL Approved</b> (requirement confirmed, little more) is a BL success, the loop's primary metric.</dd></div>
    <div><dt>Disposition</dt><dd>The outcome label a call gets, such as BL Enriched, BL Approved, BL Deleted or Abruptly Disconnected Call. The judge proposes one for every call.</dd></div>
    <div><dt>MCAT</dt><dd>IndiaMART's catalogue category for a product. The MCAT/ Search errors rubric checks that the buyer's product was mapped to the right one.</dd></div>
    <div><dt>ISQ</dt><dd>A category's specification questions. Category ISQ's Error judges the question set itself: duplicates, confusing names, contradictory or single-option choices.</dd></div>
    <div><dt>AstBuy</dt><dd>OnCall AstBuy: connecting the buyer, during the call, to a seller who is available on the line. Its rubric checks that the bot offered it and transferred on a clear yes.</dd></div>
    <div><dt>Prompt A, Prompt B</dt><dd>Prompt A is the production prompt a cycle starts from. Prompt B is the candidate: Prompt A with the fix's edits. If B wins and is approved, it becomes the next cycle's A.</dd></div>
    <div><dt>Cycle</dt><dd>One pass of the loop on one production prompt version, from Listen to Roll out, with up to three tries.</dd></div>
    <div><dt>Root cause</dt><dd>A named, fixable reason a group of calls failed one rubric, with a type (prompt, asr, knowledge, flow or platform) and a priority.</dd></div>
    <div><dt>Lead, trigger, maturation</dt><dd>The lead is the unit that gets a prompt. It is triggered at its first answered call and counts at a look once it has matured, one or two days later, by when most of its successes are in (for the headline, 94.9% of those reached within 14 days); its later calls up to the look still count.</dd></div>
    <div><dt>MDE</dt><dd>Minimum detectable effect: the relative lift in the primary metric at which the test, run to n<sub>B,max</sub>, crosses its win boundary with 80% probability: +10% for the loop's template, +15% for the headline. A win also needs the guardrail to clear, so the chance of a win at the MDE is lower: 76.4% for the headline, and far lower for the template unless B also cuts disconnects.</dd></div>
    <div><dt>Look, checkpoint</dt><dd>One analysis of an A/B test as its data arrives, up to seven per test. The Experiments page calls them checkpoints, and the boundaries at each its win and harm lines.</dd></div>
    <div><dt>Information fraction, t</dt><dd>How far a test is through its planned sample: matured triggered B leads ÷ n<sub>B,max</sub>.</dd></div>
    <div><dt>n<sub>B,max</sub></dt><dd>The planned maximum of matured triggered B leads: the fixed-design sample times the inflation R the boundaries cost. About 1,900 for the headline and about 430 for the loop's template.</dd></div>
    <div><dt>Alpha spending</dt><dd>Each look may spend only the share of the false-win budget (2.5%, one-sided) that its information fraction allows, so the total over all looks never exceeds it.</dd></div>
    <div><dt>O'Brien–Fleming</dt><dd>The win boundary's spending shape: almost nothing spent early, so an early win needs overwhelming evidence (z ≥ 4.877 at the first look).</dd></div>
    <div><dt>Pocock</dt><dd>The harm boundary's spending shape: front-loaded (about 30% of α<sub>harm</sub> at the first look), which keeps the boundary nearly flat (z near −2.2 throughout), so harm can stop B at any look.</dd></div>
    <div><dt>Repeated confidence interval</dt><dd>An interval that stays valid however many times you look. The guardrail uses one, the observed change ± c<sub>k</sub>·SE; for B to win, its upper end must lie below the margin (both guardrails are worse if they rise).</dd></div>
    <div><dt>Guardrail</dt><dd>The second metric. It can stop B on its own or hold back a win, never promote B: abrupt disconnects (+2 pp margin) for the loop's template, answered call duration (+10%) for the headline.</dd></div>
    <div><dt>SRM</dt><dd>Sample-ratio mismatch: the observed split differs from the configured 90/10 by more than chance allows (p &lt; 0.001). The experiment halts.</dd></div>
    <div><dt>Kappa, κ</dt><dd>Cohen's kappa: agreement corrected for chance. See <a href="#/docs/judge">How far to trust the judge</a>.</dd></div>
    <div><dt>End states</dt><dd><ul class="states">
      <li>${stateChip("PENDING_APPROVAL")}<span>B won with the guardrail clear and waits for a person, keeping its 10%. Not final.</span></li>
      <li>${stateChip("PROMOTED")}<span>B serves everyone.</span></li>
      <li>${stateChip("REJECTED")}<span>A person turned a win down; everyone back on A.</span></li>
      <li>${stateChip("STOPPED_HARM")}<span>B's z reached the harm boundary, or the guardrail's repeated confidence interval lay entirely beyond its margin.</span></li>
      <li>${stateChip("BLOCKED_GUARDRAIL")}<span>At the final look B was above the win boundary, but the guardrail's interval was still not inside its margin; A stays.</span></li>
      <li>${stateChip("INCONCLUSIVE")}<span>At the final look B crossed neither boundary; A stays.</span></li>
      <li>${stateChip("HALTED_SRM")}<span>The split failed its check; nothing is trusted.</span></li>
      <li>${stateChip("GATE_BLOCKED")} ${stateChip("REPLAY_NO_GAIN")} ${stateChip("FIX_INVALID")} ${stateChip("REPLAY_FAILED")}<span>The loop's own endings for a try that never reached the A/B test.</span></li>
    </ul></dd></div>
  </dl>`);

const faq = () => sec("faq", html`FAQ`, html`<div class="faq">
  <article><h3>Why simulated traffic?</h3><p>Because there is no live traffic to send a prompt to. Replaying real call history with a known, stated effect for B lets every decision be checked against ground truth: the simulator knows what it injected and the engine does not. The flip side is the honest limit: the loop's A/B result reflects the assumed effect, not what Prompt B really does to buyers, which only live traffic can show.</p></article>
  <article><h3>Why approval and not auto-promotion?</h3><p>A prompt change reaches every caller, so a named person signs off and the decision log records who. In the loop it matters twice over: the A/B win comes from an assumed effect, so the evidence a person should weigh is the fix itself, its redline, its gate and its replay, which is a screen of at most eight pairs of simulated conversations, not a measured effect. Auto-promotion is supported; the engine's win scenario shows it.</p></article>
  <article><h3>Why does a new rubric re-grade the calls?</h3><p>The judge grades every active rubric in one call, and each audit records the rubric set it used. Change the set and the old grades no longer describe today's checklist, so the next Judge stage grades the calls again, and agreement is measured on one set at a time. The root-cause ranking is kept per prompt version once a cause on it has been tried, so on a version a cycle has already worked on, a changed rubric shapes the fixes only from the next prompt version on. Re-grading costs model calls, while an unchanged set costs nothing.</p></article>
  <article><h3>What does a cycle cost?</h3><p>Only its model calls: the A/B engine is free. The runbook (<code>docs/runbook.md</code>) estimates about $2–3 for a later cycle. Every request is cached on disk, so a rerun is free, and <code>LLM_BUDGET_USD</code> (default $40) caps the total; the provider's key can carry a lower cap of its own. <a href="#/settings">Settings</a> shows the spend.</p></article>
  <article><h3>What is on the demo site?</h3><p>Without the platform, the site loads demo data: invented calls, names, products and grades, beside real engine runs. Saving a rubric keeps it in your browser only, and advancing the loop replays a scripted run. Run <code>make platform</code> for the real thing.</p></article>
</div>`);

const page = () => html`<div class="docs">
  <nav class="toc" aria-label="On this page"><div class="eyebrow">On this page</div>${tocList()}
    <p class="hint">Every section has its own link. Press <kbd>${$("#kbd-hint")?.textContent?.trim() || "⌘K"}</kbd> and type <i>docs</i> to jump to one.</p></nav>
  <article class="dbody">${hero()}
    <details class="toc-m"><summary><span class="eyebrow">Contents</span><span class="cur" id="doc-cur">${SECTIONS[0][1]}</span></summary><nav aria-label="On this page">${tocList()}</nav></details>
    ${overview()}${pages()}${loop()}${grades()}${judge()}${fixes()}${prove()}${running()}${privacy()}${glossary()}${faq()}
    <footer class="dfoot"><span>Vani Lab, by Team Hare &amp; Tortoise.</span><span>Sources: README.md, docs/loop_api.md, docs/runbook.md, reports/validation.md and the code under src/abtest/. <a href="#/docs">Back to the top</a></span></footer>
  </article></div>`;

// ------------------------------------------------------------------------------------- render
let css = null, from = null;  // from: where the reader was when they followed a docs link, so the jump glides from there
const sectionOf = (hash) => /^#\/docs\/([a-z0-9-]+)/.exec(hash)?.[1];

export async function render(el) {
  await (css ||= new Promise((ok) => {
    const l = Object.assign(document.createElement("link"), { rel: "stylesheet", href: new URL("../docs.css", import.meta.url).href });
    l.onload = l.onerror = ok;  // a missing stylesheet must not keep the page from rendering
    document.head.append(l);
  }));
  const back = history.state?.docsY;  // back, forward or reload: where this entry was last read
  el.classList.toggle("docs-stay", from != null || back != null);  // moving within the docs: no page fade
  mount(el, page());

  const links = $$("a[data-s]", el), cur = $("#doc-cur", el), first = $(".dsec", el);
  const mark = (s) => {
    links.forEach((a) => (a.dataset.s === s ? a.setAttribute("aria-current", "location") : a.removeAttribute("aria-current")));
    cur.textContent = SECTIONS.find(([x]) => x === s)?.[1] || "";
  };
  const seen = new Set();
  const onCross = (es) => {  // the section crossing a band near the top is the current one
    for (const e of es) e.isIntersecting ? seen.add(e.target.dataset.s) : seen.delete(e.target.dataset.s);
    const s = SECTIONS.find(([x]) => seen.has(x));
    if (s) mark(s[0]);
    else if (first.getBoundingClientRect().top > innerHeight * 0.3) mark(SECTIONS[0][0]);  // an instant jump back above it
  };
  let io = null, vh = 0;
  const watch = () => {  // the band in px: the spec resolves rootMargin percentages against the width, even top and bottom
    if (innerHeight === vh) return;
    vh = innerHeight;
    io?.disconnect();
    seen.clear();
    io = new IntersectionObserver(onCross, { rootMargin: `-${Math.round(vh * 0.22)}px 0px -${Math.round(vh * 0.7)}px 0px` });
    $$(".dsec", el).forEach((s) => io.observe(s));
  };
  watch();
  addEventListener("resize", watch);

  const host = $("#doc-cor", el);
  let w = 0;
  const draw = () => { if (host.clientWidth && Math.abs(host.clientWidth - w) > 4) { w = host.clientWidth; mount(host, raw(corridor(w))); } };
  const ro = new ResizeObserver(draw);
  ro.observe(host);
  draw();

  const keep = () => { try { history.replaceState({ ...history.state, docsY: scrollY }, ""); } catch { /* rate-limited: back lands on the section */ } };
  let t = 0;
  const onScroll = () => { clearTimeout(t); t = setTimeout(keep, 250); };
  addEventListener("scroll", onScroll, { passive: true });
  const target = (s) => (s ? $(`#doc-${s}`, el) : null);
  el.addEventListener("click", (e) => {
    const a = e.target.closest('a[href^="#/docs"]');
    if (!a || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    keep();
    if (a.getAttribute("href") !== location.hash) { from = scrollY; return; }  // the router re-renders; render() glides
    e.preventDefault();  // the section already in the address: no hashchange, so scroll here
    a.closest("details")?.removeAttribute("open");
    const goal = target(sectionOf(a.getAttribute("href")));
    goal ? goal.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth" }) : scrollTo({ top: 0, behavior: reducedMotion() ? "auto" : "smooth" });
  });

  const id = sectionOf(location.hash), goal = target(id);
  if (id && !goal) history.replaceState(history.state, "", "#/docs");  // an unknown section: the page, from the top
  mark(goal ? id : SECTIONS[0][0]);
  const glide = reducedMotion() ? "auto" : "smooth";
  if (back != null) scrollTo({ top: back });
  else if (from != null) { scrollTo({ top: from }); goal ? goal.scrollIntoView({ behavior: glide }) : scrollTo({ top: 0, behavior: glide }); }
  else if (goal) {
    goal.scrollIntoView();
    const y = scrollY;  // web fonts can still be swapping in: land again unless the reader has moved
    document.fonts?.ready.then(() => { if (el.isConnected && scrollY === y) goal.scrollIntoView(); });
  }
  from = null;
  if (goal) $("h2", goal).focus({ preventScroll: true });
  return () => { io.disconnect(); ro.disconnect(); clearTimeout(t); removeEventListener("scroll", onScroll); removeEventListener("resize", watch); };
}
