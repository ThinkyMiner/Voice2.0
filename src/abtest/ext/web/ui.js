// DOM, formatting and small shared components. Templates are tagged literals: every
// interpolated value is escaped unless it is already html``/raw() output.

class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
export const raw = (s) => new Raw(String(s));
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);
const val = (v) => (v instanceof Raw ? v.s : Array.isArray(v) ? v.map(val).join("") : v == null || v === false ? "" : esc(v));
export const html = (strs, ...vals) => new Raw(strs.reduce((o, s, i) => o + s + (i < vals.length ? val(vals[i]) : ""), ""));
export const $ = (sel, el = document) => el.querySelector(sel);
export const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
export const mount = (el, content) => { el.innerHTML = val(content); return el; };
export const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
export const enc = encodeURIComponent;

// ---------------------------------------------------------------------------------- format
const nf = new Intl.NumberFormat("en-IN");
const MINUS = "−";
export const int = (n) => (n == null || Number.isNaN(n) ? "—" : nf.format(Math.round(n)).replace("-", MINUS));
export const pct = (x, d = 1) => (x == null || Number.isNaN(x) ? "—" : `${(x * 100).toFixed(d)}%`.replace("-", MINUS));
export const spct = (x, d = 1) => (x == null || Number.isNaN(x) ? "—" : `${x >= 0 ? "+" : MINUS}${Math.abs(x * 100).toFixed(d)}%`);
export const spp = (x, d = 1) => (x == null || Number.isNaN(x) ? "—" : `${x >= 0 ? "+" : MINUS}${Math.abs(x * 100).toFixed(d)} pts`);
export const sint = (n) => (n == null ? "—" : Math.round(n) === 0 ? "0" : `${n > 0 ? "+" : MINUS}${nf.format(Math.abs(Math.round(n)))}`);
export const fix = (x, d = 2) => (x == null || Number.isNaN(x) ? "—" : Number(x).toFixed(d).replace("-", MINUS));
export const clock = (s) => (s == null ? "—" : `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`);
export const mmss = (s) => (s == null || !Number.isFinite(s) ? "—" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`);
export const dur = (s) => (s == null ? "—" : s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`);
const dt = (ts) => (ts ? new Date(ts) : null);
export const when = (ts) => (ts ? dt(ts).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) : "—");
export const day = (ts) => (ts ? dt(ts).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" }) : "—");
export const short = (sha, n = 8) => (sha ? String(sha).slice(0, n) : "—");
export const words = (s) => (s.match(/\S+/g) || []).length;
export const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

/** Counts or shares -> shares; `n` is the total when counts were given. */
export function shares(o) {
  if (!o) return null;
  const t = (o.pass || 0) + (o.non_fatal || 0) + (o.fatal || 0);
  if (!t) return { pass: 0, non_fatal: 0, fatal: 0, n: 0 };
  return t > 1.0001 ? { pass: o.pass / t, non_fatal: o.non_fatal / t, fatal: o.fatal / t, n: t } : { ...o, n: null };
}

// ------------------------------------------------------------------------------ vocabulary
export const GRADE = { pass: "Pass", non_fatal: "Non-fatal", fatal: "Fatal", na: "N/A" };
export const grade = (g, cls = "") => html`<span class="grade g-${g || "none"} ${cls}">${GRADE[g] ?? "Not graded"}</span>`;
const STATES = {
  PROMOTED: ["Promoted", "win"], PENDING_APPROVAL: ["Pending approval", "pending"], STOPPED_HARM: ["Stopped · harm", "harm"],
  BLOCKED_GUARDRAIL: ["Blocked · guardrail", "harm"], INCONCLUSIVE: ["Inconclusive", ""], HALTED_SRM: ["Halted · SRM", "warn"],
  RUNNING: ["Running", "info"], DRAFT: ["Draft", ""], REJECTED: ["Rejected", ""], GATE_BLOCKED: ["Gate blocked", "harm"],
  REPLAY_NO_GAIN: ["No replay gain", "warn"], production: ["Production", "accent"], candidate: ["Candidate", "pending"],
  retired: ["Retired", ""], rejected: ["Rejected", "harm"],
};
export const stateChip = (s) => { const [l, c] = STATES[s] || [cap(String(s || "—").toLowerCase().replaceAll("_", " ")), ""]; return html`<span class="chip ${c}"><i></i>${l}</span>`; };
export const typeChip = (t) => html`<span class="chip t-${t}" title="Root type: ${t}"><i></i>${t}</span>`;
export const FLAGS = { human_request: "Asked for a human", frustration: "Frustration", wrong_requirement: "Wrong requirement", loop: "Loop", low_confidence: "Low confidence" };
export const flagChip = (f) => html`<span class="chip ${f === "wrong_requirement" || f === "frustration" ? "harm" : f === "loop" ? "warn" : f === "human_request" ? "info" : ""}">${FLAGS[f] || f}</span>`;
const FLAG_AT = Object.keys(FLAGS);
/** A call's flags in one fixed order (FLAGS), so a list of calls scans down a column. */
export const flagChips = (fs) => [...fs].sort((a, b) => (FLAG_AT.indexOf(a) + 1 || 99) - (FLAG_AT.indexOf(b) + 1 || 99)).map(flagChip);
export const STAGES = [
  ["listen", "Listen", "Transcribe recordings"], ["judge", "Judge", "Grade every call"], ["improve", "Improve", "Rank causes, write Prompt B"],
  ["gate", "Gate", "Safety checks"], ["replay", "Replay", "Simulated before and after"], ["prove", "Prove", "10% A/B, simulated traffic"],
  ["approval", "Approval", "A human signs off"], ["rollout", "Roll out", "B becomes production"],
];
export const stageName = (s) => (STAGES.find((x) => x[0] === s)?.[1] || cap(s || ""));
export const weight = (w) => html`<span class="weight" title="Weight ${w} of 5">${[1, 2, 3, 4, 5].map((i) => html`<i class="${i <= w ? "on" : ""}"></i>`)}</span>`;
export const callLink = (id) => html`<a class="chip id" href="#/calls/${enc(id)}">${id}</a>`;

// ------------------------------------------------------------------------------ components
const GLYPH = {
  wave: '<svg viewBox="0 0 56 56"><path d="M6 28h4M14 20v16M20 14v28M26 22v12M32 10v36M38 18v20M44 24v8M50 28h0"/></svg>',
  doc: '<svg viewBox="0 0 56 56"><path d="M15 7h18l9 9v33H15z"/><path d="M33 7v9h9M21 27h15M21 34h15M21 41h9"/></svg>',
  ring: '<svg viewBox="0 0 56 56"><circle cx="28" cy="28" r="18" stroke-dasharray="3 4"/><circle cx="28" cy="10" r="3"/></svg>',
  flask: '<svg viewBox="0 0 56 56"><path d="M22 8h12M24 8v14L12 44a3 3 0 0 0 3 4h26a3 3 0 0 0 3-4L32 22V8"/><path d="M17 36h22"/></svg>',
  lock: '<svg viewBox="0 0 56 56"><rect x="13" y="25" width="30" height="22" rx="4"/><path d="M19 25v-6a9 9 0 0 1 18 0v6"/></svg>',
};
export const empty = ({ title, body = "", cmd = null, glyph = "ring", compact = false, action = null }) => html`
  <div class="empty ${compact ? "compact" : ""}">${raw(GLYPH[glyph] || GLYPH.ring)}<h3>${title}</h3>${body ? html`<p>${body}</p>` : ""}${cmd ? html`<code class="cmd">${cmd}</code>` : ""}${action || ""}</div>`;

export function errorState(err) {
  const status = err?.status;
  if (status === 404) return empty({ title: "Nothing here yet", body: err.message, glyph: "doc" });
  return empty({ title: "This page could not load", body: `${err?.message || err}. Is the platform running?`, cmd: "make platform", glyph: "lock" });
}

export function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.classList.add("out"), 4200);
  setTimeout(() => t.remove(), 4600);
}

export const stack = (parts, cls = "") => html`<div class="bar ${cls}" role="img" aria-label="${parts.map((p) => `${p.label} ${pct(p.v)}`).join(", ")}">${parts.filter((p) => p.v > 0).map((p) => html`<span class="${p.cls}" style="flex-basis:${(p.v * 100).toFixed(3)}%" title="${p.label}: ${pct(p.v)}${p.n != null ? ` (${int(p.n)})` : ""}"></span>`)}</div>`;
export const gradeStack = (s, cls = "") => stack([
  { v: s.pass, cls: "s-pass", label: "Pass", n: s.n != null ? s.pass * s.n : null },
  { v: s.non_fatal, cls: "s-non_fatal", label: "Non-fatal", n: s.n != null ? s.non_fatal * s.n : null },
  { v: s.fatal, cls: "s-fatal", label: "Fatal", n: s.n != null ? s.fatal * s.n : null },
], cls);
export const gradeLegend = () => html`<div class="legend"><span><i class="s-pass"></i>Pass</span><span><i class="s-non_fatal"></i>Non-fatal</span><span><i class="s-fatal"></i>Fatal</span></div>`;

export function seg(options, current, attr = "data-v") {
  return html`<div class="seg" role="group">${options.map(([v, label]) => html`<button type="button" ${raw(attr)}="${v}" aria-pressed="${String(v) === String(current)}">${label}</button>`)}</div>`;
}

// ------------------------------------------- agreement with IndiaMART's QA auditors, per rubric
/** Plain words for Cohen's kappa, the same on every page, for a reader who has never met kappa. */
export const kappaWord = (k) => (k == null ? "not measurable" : k < 0.2 ? "about chance" : k < 0.4 ? "weak"
  : k < 0.6 ? "moderate" : k < 0.8 ? "good" : "strong");
/** One rubric's AgreementReport row as words; no row means no QA column, so only the judge grades it. */
export function agreement(row) {
  if (!row) return { weak: true, text: "No QA column: only the judge checks this", short: "Judge only" };
  return { weak: row.kappa == null || row.kappa < 0.4 || row.accuracy < 0.6, short: `${pct(row.accuracy, 0)} · κ ${fix(row.kappa)}`,
    text: `Matches QA auditors on ${pct(row.accuracy, 0)} of ${int(row.n)} calls · κ ${fix(row.kappa)}, ${kappaWord(row.kappa)}` };
}
export const agreeLine = (row) => {
  const a = agreement(row);
  return html`<div class="agree-line ${a.weak ? "weak" : "ok"}" title="Cohen's κ discounts agreement by chance: 0 is chance level, 1 is perfect.">${a.text}</div>`;
};

// ------------------------------------------------ failed loads and confirmations (shell, loop pages)
/** A load that failed, in the server's own words, with a way on: Retry (app.js re-runs the route on
 *  [data-retry]) and an optional way back, e.g. ["All experiments", "#/experiments"]. */
export function failState(err, back = null) {
  const s = err?.status;
  const title = s === 404 ? "Not found" : s ? `The platform answered with an error (${s})` : "The platform is not answering";
  const body = !s ? `${err?.message || err}. Start it with make platform, then retry.`
    : s >= 500 ? `The platform is running, but this request failed on the server: ${err.message}. Its log has the cause.` : String(err.message || "");
  return html`<div class="empty" role="alert">${raw(s === 404 ? GLYPH.doc : GLYPH.lock)}<h3>${title}</h3>${body ? html`<p>${body}</p>` : ""}
    <div class="row" style="justify-content:center">${s === 404 ? "" : html`<button class="btn" type="button" data-retry>Retry</button>`}${back ? html`<a class="btn ${s === 404 ? "" : "ghost"}" href="${back[1]}">${back[0]}</a>` : ""}</div></div>`;
}

/** Ask before a step that changes real state. Resolves true only on the confirm button; Esc, Cancel
 *  and a click outside resolve false. `body` is html``. */
export function confirmDialog({ title, body, ok = "Continue", tone = "primary" }) {
  const d = document.createElement("dialog");
  d.className = "confirm";
  d.setAttribute("aria-labelledby", "confirm-t");
  mount(d, html`<form method="dialog"><h3 id="confirm-t">${title}</h3><div class="confirm-body">${body}</div>
    <div class="row confirm-actions"><button class="btn" value="cancel" autofocus>Cancel</button><button class="btn ${tone}" value="ok">${ok}</button></div></form>`);
  document.body.append(d);
  return new Promise((done) => {
    d.addEventListener("close", () => { done(d.returnValue === "ok"); d.remove(); });
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
    d.showModal();
  });
}
