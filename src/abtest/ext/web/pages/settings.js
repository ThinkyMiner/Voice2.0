// #/settings: models per role, model budget, where the data lives, and the privacy note.
// GET /api/settings is optional (see the result notes); without it the page shows llm.py's defaults.
import { get, state } from "../api.js";
import { html, mount, pct, fix, int } from "../ui.js";

const DEFAULTS = {
  models: { transcribe: "google/gemini-2.5-flash", judge: "anthropic/claude-sonnet-5.5", judge_fast: "anthropic/claude-haiku-5.5",
    fixer: "anthropic/claude-opus-5.5", gate: "anthropic/claude-sonnet-5.5", replay: "anthropic/claude-haiku-5.5" },
  budget: null,
  paths: { loop: "data/derived/loop", recordings: "data/derived/loop/recordings", transcripts: "data/derived/loop/transcripts",
    audits: "data/derived/loop/audits/<version>", prompts: "data/derived/loop/prompts", rubrics: "configs/rubrics",
    custom_rubrics: "data/derived/loop/rubrics_custom.json", llm_cache: "data/derived/loop/llm_cache" },
  bind: "127.0.0.1:8600",
};
const ROLE = { transcribe: "Listen: audio to transcript", judge: "Judge: grades every call", judge_fast: "Judge, fast pass", fixer: "Improve: writes Prompt B",
  gate: "Gate: safety checks", replay: "Replay: simulated bot and buyer" };

export async function render(el) {
  const live = await get("/api/settings").catch(() => null);
  const s = { ...DEFAULTS, ...(live || {}) };
  const b = s.budget;
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Platform</div><h1 class="title">Settings</h1>
      <p class="lede">What runs where. Models are set per role with <code>LLM_MODEL_&lt;ROLE&gt;</code>; the spend cap with <code>LLM_BUDGET_USD</code>.</p></div></div>
    <div class="privacy"><svg viewBox="0 0 56 56" aria-hidden="true"><rect x="13" y="25" width="30" height="22" rx="4"/><path d="M19 25v-6a9 9 0 0 1 18 0v6M28 34v5"/></svg>
      <div><h3>Calls stay on this machine</h3>
        <p>Recordings, transcripts and QA remarks are real customer calls. They live under <code>data/</code>, which git ignores, and the platform binds to <b>127.0.0.1</b> only, so nothing here is reachable from the network.</p>
        <p>Model calls go to OpenRouter and are cached on disk by request hash, so a rerun sends nothing new.${state.demo ? " This page is showing demo data: invented calls, real engine runs." : ""}</p></div></div>
    <div class="grid cols-2 section">
      <div class="card"><div class="card-head"><h3>Models per role</h3><span class="eyebrow">${state.demo ? "demo data" : live ? "from the platform" : "llm.py defaults"}</span></div>
        <dl class="kv">${Object.entries(s.models).map(([k, m]) => html`<dt>${ROLE[k] || k}</dt><dd>${m}</dd>`)}</dl></div>
      <div class="card"><div class="card-head"><h3>Model budget</h3><span class="eyebrow">OpenRouter</span></div>
        ${b ? html`<div class="big">${`$${fix(b.spent_usd)}`}<small>of $${fix(b.cap_usd, 0)}</small></div>
          <div class="bar tall" style="margin-top:14px"><span class="s-a" style="flex-basis:${Math.min(100, 100 * b.spent_usd / b.cap_usd).toFixed(1)}%;background:${b.spent_usd / b.cap_usd > 0.85 ? "var(--harm)" : "var(--accent)"}"></span></div>
          <p class="muted" style="font-size:12.5px;margin-top:10px">${pct(b.spent_usd / b.cap_usd)} of the cap · ${int(b.calls)} paid calls. Cached replays cost nothing. At the cap, every model call stops with BudgetExceeded.</p>`
          : html`<p class="muted" style="font-size:13px">The platform reports spend at <code>GET /api/settings</code>; it is also in <code>data/derived/loop/llm_spend.json</code>. Default cap: $40.</p>`}</div>
      <div class="card span-2"><div class="card-head"><h3>Data locations</h3><span class="eyebrow">relative to the repo</span></div>
        <dl class="kv">${Object.entries(s.paths).map(([k, p]) => html`<dt>${k.replaceAll("_", " ")}</dt><dd>${p}</dd>`)}<dt>listening on</dt><dd>${s.bind}</dd></dl></div>
    </div>`);
}
