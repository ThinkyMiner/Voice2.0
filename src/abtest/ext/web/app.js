// Shell: boot (API or demo data), hash routing, breadcrumbs, theme, the Cmd/Ctrl+K palette.
import { boot, get, state } from "./api.js";
import { $, $$, html, mount, errorState, esc, enc } from "./ui.js";
import { brandRingPath } from "./viz.js";
import * as home from "./pages/home.js";
import * as audit from "./pages/audit.js";
import * as calls from "./pages/calls.js";
import * as issues from "./pages/issues.js";
import * as fixes from "./pages/fixes.js";
import * as experiments from "./pages/experiments.js";
import * as rubrics from "./pages/rubrics.js";
import * as prompts from "./pages/prompts.js";
import * as settings from "./pages/settings.js";

const ROUTES = [  // [pattern, nav key, section label, render]
  [/^$/, "loop", "Loop", home.render],
  [/^audit$/, "audit", "Audit", audit.render],
  [/^calls$/, "calls", "Calls", calls.renderList],
  [/^calls\/(.+)$/, "calls", "Calls", calls.renderCall],
  [/^issues$/, "issues", "Issues", issues.render],
  [/^fixes$/, "fixes", "Fixes", fixes.renderList],
  [/^fixes\/(.+)$/, "fixes", "Fixes", fixes.renderFix],
  [/^experiments$/, "experiments", "Experiments", experiments.renderList],
  [/^experiments\/(.+)$/, "experiments", "Experiments", experiments.renderExperiment],
  [/^rubrics$/, "rubrics", "Rubrics", rubrics.renderLibrary],
  [/^rubrics\/(.+)$/, "rubrics", "Rubrics", rubrics.renderStudio],
  [/^prompts$/, "prompts", "Prompts", prompts.render],
  [/^settings$/, "settings", "Settings", settings.render],
];

const view = $("#view");
let cleanup = null, seq = 0;

function parse() {
  const raw = location.hash.replace(/^#\/?/, "");
  const [path, query = ""] = raw.split("?");
  return { path: path.replace(/\/$/, ""), params: new URLSearchParams(query) };
}

async function route(keepScroll = false) {
  const { path, params } = parse();
  const top = scrollY;
  const hit = ROUTES.find(([re]) => re.test(path));
  if (cleanup) { try { cleanup(); } catch { /* page already gone */ } cleanup = null; }
  const n = ++seq;
  if (!hit) return show(n, "Not found", null, (el) => mount(el, errorState({ status: 404, message: `There is no page at #/${path}.` })));
  const [re, key, label, render] = hit;
  let id = path.match(re)[1];
  try { if (id) id = decodeURIComponent(id); } catch {  // a malformed %-escape in the address
    return show(n, label, null, (el) => mount(el, errorState({ status: 404, message: `#/${path} is not a valid address.` })), key);
  }
  $$(".nav a").forEach((a) => (a.dataset.route === key ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  await show(n, label, id || null, (el) => render(el, { id, params }), key, keepScroll);
  if (keepScroll) scrollTo({ top });
}

async function show(n, label, id, render, key = "", keepScroll = false) {
  crumbs(label, id, key);
  const el = document.createElement("div");
  el.className = keepScroll ? "" : "page";
  if (keepScroll) el.style.minHeight = `${view.offsetHeight}px`;  // a re-render must not jump the page
  else el.innerHTML = '<div class="skeleton" aria-busy="true"><i style="width:22%"></i><i style="width:56%;height:46px"></i><i style="width:40%"></i></div>';
  view.replaceChildren(el);
  try {
    const c = await render(el);
    if (n !== seq) { if (typeof c === "function") c(); return; }
    cleanup = typeof c === "function" ? c : null;
  } catch (err) {
    if (n === seq) { if (err?.status !== 404) console.error(err); mount(el, errorState(err)); }
  }
  el.style.minHeight = "";
  document.title = id ? `${id} · Vani Loop` : label === "Loop" ? "Vani Loop" : `${label} · Vani Loop`;
}

function crumbs(label, id, key) {
  const items = [html`<li><a href="#/">Vani Loop</a></li>`];
  if (key && key !== "loop") items.push(id ? html`<li><a href="#/${key}">${label}</a></li>` : html`<li>${label}</li>`);
  else if (key === "loop") items.push(html`<li>The loop</li>`);
  if (id) items.push(html`<li class="mono">${id}</li>`);
  mount($("#crumbs"), items);
}

// ------------------------------------------------------------------------------------- theme
const themeBtn = $("#theme-toggle");
const isDark = () => document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
function syncTheme() {
  themeBtn.setAttribute("aria-label", isDark() ? "Switch to light theme" : "Switch to dark theme");
  window.dispatchEvent(new Event("vani:theme"));
}
themeBtn.addEventListener("click", () => {
  const next = isDark() ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("vani-theme", next); } catch { /* not persisted */ }
  syncTheme();
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", syncTheme);

// ----------------------------------------------------------------------------------- palette
const pal = $("#palette"), palQ = $("#pal-q"), palList = $("#pal-list");
const PAGES = [["Loop", "#/"], ["Calls", "#/calls"], ["Audit", "#/audit"], ["Rubrics", "#/rubrics"], ["Rubric studio: new rubric", "#/rubrics/new"],
  ["Issues", "#/issues"], ["Fixes", "#/fixes"], ["Prompts", "#/prompts"], ["Experiments", "#/experiments"], ["Settings", "#/settings"]];
let palItems = null, palSel = 0, palShown = [];

async function loadPalette() {
  const items = PAGES.map(([t, href]) => ({ k: "Page", t, href }));
  items.push({ k: "Action", t: "Toggle light / dark theme", run: () => themeBtn.click() });
  const [c, x, f, r] = await Promise.allSettled([get("/api/calls?limit=500"), get("/api/experiments"), get("/api/fixes"), get("/api/rubrics")]);
  if (c.status === "fulfilled") items.push(...c.value.items.map((i) => ({ k: "Call", t: i.call_id, s: i.summary || "", href: `#/calls/${enc(i.call_id)}` })));
  if (x.status === "fulfilled") items.push(...x.value.items.map((i) => ({ k: "Experiment", t: i.experiment_id, s: i.state, href: `#/experiments/${enc(i.experiment_id)}` })));
  if (f.status === "fulfilled") items.push(...f.value.items.map((i) => ({ k: "Fix", t: i.id, s: i.rationale || "", href: `#/fixes/${enc(i.id)}` })));
  if (r.status === "fulfilled") items.push(...r.value.items.map((i) => ({ k: "Rubric", t: i.name, s: i.category, href: `#/rubrics/${enc(i.id)}` })));
  return items;
}

function drawPalette() {
  const q = palQ.value.trim().toLowerCase();
  palShown = (palItems || []).filter((i) => !q || `${i.k} ${i.t} ${i.s || ""}`.toLowerCase().includes(q)).slice(0, 60);
  palSel = Math.min(palSel, Math.max(0, palShown.length - 1));
  palList.innerHTML = palShown.length
    ? palShown.map((i, n) => `<li class="pal-item" role="option" id="pal-${n}" aria-selected="${n === palSel}" data-n="${n}"><span class="k">${esc(i.k)}</span><span class="t">${esc(i.t)}</span>${i.s ? `<span class="s">${esc(i.s)}</span>` : ""}</li>`).join("")
    : `<li class="pal-group muted">${palItems ? "Nothing matches." : "Loading…"}</li>`;
  palQ.setAttribute("aria-activedescendant", palShown.length ? `pal-${palSel}` : "");
  palList.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" });
}

async function openPalette() {
  if (pal.open) return pal.close();
  palQ.value = "";
  palSel = 0;
  pal.showModal();
  drawPalette();
  palItems = await loadPalette();
  drawPalette();
}
function choose(i) {
  if (!i) return;
  pal.close();
  if (i.run) i.run(); else location.hash = i.href;
}
palQ.addEventListener("input", () => { palSel = 0; drawPalette(); });
palQ.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { palSel = Math.min(palShown.length - 1, palSel + 1); drawPalette(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { palSel = Math.max(0, palSel - 1); drawPalette(); e.preventDefault(); }
  else if (e.key === "Enter") { choose(palShown[palSel]); e.preventDefault(); }
});
palList.addEventListener("click", (e) => { const li = e.target.closest(".pal-item"); if (li) choose(palShown[Number(li.dataset.n)]); });
pal.addEventListener("click", (e) => { if (e.target === pal) pal.close(); });
$("#open-palette").addEventListener("click", openPalette);
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
});
if (!/Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)) $("#kbd-hint").textContent = "Ctrl K";

// -------------------------------------------------------------------------------------- boot
function foot() {
  const ov = state.overview;
  $("#foot-prod").textContent = ov?.production?.version || "—";
  $("#foot-mode").textContent = state.demo ? "demo" : "live";
  $("#foot-dot").className = `dot ${state.demo ? "demo" : "live"}`;
}
window.addEventListener("vani:changed", async () => {
  try { state.overview = await get("/api/overview"); } catch { /* keep the last one */ }
  foot();
  palItems = null;
});

(async () => {
  if (location.protocol === "file:") return;
  $("#brand-ring").setAttribute("d", brandRingPath());
  try {
    await boot();
  } catch (err) {
    mount(view, errorState(err));
    return;
  }
  if (state.demo) {
    $("#demo-ribbon").hidden = false;
    document.documentElement.classList.add("is-demo");
  }
  foot();
  syncTheme();
  addEventListener("hashchange", () => { route(); view.focus({ preventScroll: true }); scrollTo({ top: 0 }); });
  addEventListener("vani:rerender", () => route(true));
  route();
})();
