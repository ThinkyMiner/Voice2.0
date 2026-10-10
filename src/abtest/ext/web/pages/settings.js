// #/settings: what runs where. The privacy note, models per role, the model budget, where the data
// lives on this machine. GET /api/settings is optional; without it the page shows llm.py's defaults.
import { get, state } from "../api.js";
import { html, mount, pct, fix, int } from "../ui.js";

const DEFAULTS = {
  models: { transcribe: "google/gemini-3-flash-preview", judge: "anthropic/claude-sonnet-5.5", judge_fast: "anthropic/claude-haiku-5.5",
    fixer: "anthropic/claude-opus-5.5", gate: "typesafe/jev-router", replay: "anthropic/claude-haiku-5.5" },
  budget: null,
  paths: { loop: "data/derived/loop", recordings: "data/derived/loop/recordings", transcripts: "data/derived/loop/transcripts",
    audits: "data/derived/loop/audits/<version>", prompts: "data/derived/loop/prompts", rubrics: "src/abtest/ext/rubric_sets",
    custom_rubrics: "data/derived/loop/rubrics_custom.json", llm_cache: "data/derived/loop/llm_cache" },
  allow_real_rubric_tests: false,
};
const ROLE = { transcribe: "Listen: audio to transcript", judge: "Judge: grades every call", judge_fast: "Judge, fast pass", fixer: "Improve: writes Prompt B",
  gate: "Gate: safety checks", replay: "Replay: simulated bot and buyer" };
const home = (p) => String(p).replace(/^\/(?:Users|home)\/[^/]+(?=\/)/, "~");  // the absolute paths the platform reports, shorter

export async function render(el) {
  const live = await get("/api/settings").catch(() => null);
  const s = { ...DEFAULTS, ...(live || {}) };
  const b = s.budget, share = b?.cap_usd ? b.spent_usd / b.cap_usd : null;
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Platform</div><h1 class="title">Settings</h1>
      <p class="lede">What runs where. Models are set per role with <code>LLM_MODEL_&lt;ROLE&gt;</code> and the spend cap with <code>LLM_BUDGET_USD</code>, when the platform starts.</p></div></div>
    <div class="privacy"><svg viewBox="0 0 56 56" aria-hidden="true"><rect x="13" y="25" width="30" height="22" rx="4"/><path d="M19 25v-6a9 9 0 0 1 18 0v6M28 34v5"/></svg>
      <div><h3>Stored only on this machine</h3>
        <p>Recordings, transcripts and QA remarks are real customer calls. They live under <code>data/</code>, which git ignores, and the platform answers only on this computer (127.0.0.1).</p>
        <p>Audio and transcripts were sent to OpenRouter only to transcribe and grade them. Replies are cached, so reruns send nothing new. From this site, real call content is sent only when the platform was started with <code>LOOP_ALLOW_REAL=1</code>: ${(s.allow_real ?? s.allow_real_rubric_tests) ? "it was, so the loop's buttons and rubric tests can send it" : "it was not, so those steps run from a terminal with the go-ahead flag"}.${state.demo ? " This page is showing demo data: invented calls, real engine runs." : ""}</p></div></div>
    <div class="grid cols-2 section">
      <div class="card"><div class="card-head"><h3>Models per role</h3><span class="eyebrow">${state.demo ? "demo data" : live ? "from the platform" : "llm.py defaults"}</span></div>
        <dl class="kv">${Object.entries(s.models).map(([k, m]) => html`<dt>${ROLE[k] || k}</dt><dd>${m}</dd>`)}</dl></div>
      <div class="card"><div class="card-head"><h3>Model budget</h3><span class="eyebrow">OpenRouter</span></div>
        ${b ? html`<div class="big">${`$${fix(b.spent_usd)}`}<small>of $${fix(b.cap_usd, 0)}</small></div>
          <div class="bar tall" style="margin-top:14px" aria-hidden="true"><span class="s-a" style="flex-basis:${Math.min(100, 100 * (share || 0)).toFixed(1)}%;background:${share > 0.85 ? "var(--harm)" : "var(--accent)"}"></span></div>
          <dl class="kv budget-kv">
            <dt>Spent</dt><dd>${pct(share)} of the cap</dd>
            <dt>Left</dt><dd>$${fix(Math.max(0, b.cap_usd - b.spent_usd))}</dd>
            <dt>Paid model calls</dt><dd>${int(b.calls)}${b.calls ? ` · about $${fix(b.spent_usd / b.calls, 3)} each` : ""}</dd>
          </dl>
          <p class="muted" style="font-size:12.5px;margin-top:10px">Cached replies cost nothing. At the cap every model call stops, so nothing can overspend. The platform reports the total only, not spend per role.</p>`
          : html`<p class="muted" style="font-size:13px">The platform reports spend at <code>GET /api/settings</code>; it is also in <code>data/derived/loop/llm_spend.json</code>. Default cap: $40.</p>`}</div>
      <div class="card span-2"><div class="card-head"><h3>Data locations</h3><span class="eyebrow">on this machine</span></div>
        <dl class="kv">${Object.entries(s.paths).map(([k, p]) => html`<dt>${k.replaceAll("_", " ")}</dt><dd title="${p}">${home(p)}</dd>`)}
          <dt>listening on</dt><dd>${state.demo ? "a static server (demo data)" : location.host}</dd>
          <dt>rubric tests on real calls</dt><dd>${s.allow_real_rubric_tests ? "on (LOOP_ALLOW_REAL=1)" : "off · start the platform with LOOP_ALLOW_REAL=1 to allow them"}</dd></dl></div>
    </div>`);
}
