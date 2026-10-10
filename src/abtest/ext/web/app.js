// Shell: boot (the platform's API, or demo data only when there is no platform), hash routing,
// breadcrumbs, the phone menu, theme, the Cmd/Ctrl+K palette.
import { boot, get, state } from "./api.js";
import { $, $$, html, mount, esc, enc, failState } from "./ui.js";
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

const view = $("#view"), rail = $(".rail"), menuBtn = $("#menu-btn");
let cleanup = null, seq = 0, bootFailed = false;

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
  const toLoop = ["Go to the loop", "#/"];
  if (!hit) return show(n, "Not found", null, (el) => mount(el, failState({ status: 404, message: `There is no page at #/${path}.` }, toLoop)));
  const [re, key, label, render] = hit;
  let id = path.match(re)[1];
  try { if (id) id = decodeURIComponent(id); } catch {  // a malformed %-escape in the address
    return show(n, label, null, (el) => mount(el, failState({ status: 404, message: `#/${path} is not a valid address.` }, toLoop)), key);
  }
  $$(".nav a").forEach((a) => (a.dataset.route === key ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  $("#menu-label").textContent = label;
  $("#sim-pill").hidden = key !== "experiments";  // the calls are real; only the A/B traffic is simulated
  const back = id ? [`All ${label.toLowerCase()}`, `#/${key}`] : key === "loop" ? null : toLoop;
  await show(n, label, id || null, (el) => render(el, { id, params }), key, keepScroll, back);
  if (keepScroll) scrollTo({ top });
}

async function show(n, label, id, render, key = "", keepScroll = false, back = null) {
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
    if (n === seq) { if (err?.status !== 404) console.error(err); mount(el, failState(err, back)); }
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

view.addEventListener("click", (e) => {
  if (!e.target.closest("[data-retry]")) return;
  if (bootFailed) location.reload(); else route(true);
});

// ------------------------------------------------------------------------- phone menu (≤ 900px)
const setMenu = (open) => { rail.classList.toggle("open", open); menuBtn.setAttribute("aria-expanded", String(open)); };
menuBtn.addEventListener("click", () => setMenu(!rail.classList.contains("open")));
document.addEventListener("click", (e) => { if (rail.classList.contains("open") && !rail.contains(e.target)) setMenu(false); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && rail.classList.contains("open")) { setMenu(false); menuBtn.focus(); } });

// ------------------------------------------------------------------------------------- theme
const themeBtn = $("#theme-toggle");
const isDark = () => document.documentElement.dataset.theme === "dark" || (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
function syncTheme() {
  themeBtn.dataset.mode = isDark() ? "dark" : "light";  // CSS shows the icon of the theme a click switches to
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

// --------------------------------------------------------------------------- figures in serif text
// In Instrument Serif the digit 1 reads as l ("v1" looked like "vl", "160" like "l60"). Whatever page
// writes them, digits and version labels that land in serif text are set in Geist (.fig).
const SERIF = /Instrument Serif/, FIGURE = /(\bv\d+\b|\d(?:[\d,.]*\d)?%?)/;
function figures(root) {
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, { acceptNode: (t) => {
    const p = t.parentElement;
    return p && /\d/.test(t.data) && !p.closest(".fig, pre, code, kbd, .mono, svg, textarea, script, style")
      && SERIF.test(getComputedStyle(p).fontFamily) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
  } });
  const hits = [];
  while (walk.nextNode()) hits.push(walk.currentNode);
  for (const t of hits) {  // odd pieces of the split are the figures
    t.replaceWith(...t.data.split(FIGURE).map((s, i) => (i % 2 ? Object.assign(document.createElement("span"), { className: "fig", textContent: s }) : s)).filter(Boolean));
  }
}
new MutationObserver((records) => {
  for (const r of records) for (const n of r.addedNodes) {
    const host = n.nodeType === 3 ? (/\d/.test(n.data) ? n.parentElement : null) : n.nodeType === 1 ? n : null;
    if (host?.isConnected) figures(host);
  }
}).observe(document.body, { childList: true, subtree: true });

// ----------------------------------------------------------------------------------- palette
const pal = $("#palette"), palQ = $("#pal-q"), palList = $("#pal-list");
const PAGES = [  // [title, href, words people search for]
  ["Loop", "#/", "cycle stage next step advance start replay gate approval roll out home"],
  ["Calls", "#/calls", "transcripts recordings listen production audited"],
  ["Audit", "#/audit", "judge grades agreement QA fatal risky"],
  ["Rubrics", "#/rubrics", "QA matrix checklist judge"],
  ["Rubric studio: new rubric", "#/rubrics/new", "create write custom rubric"],
  ["Issues", "#/issues", "root causes priority"],
  ["Fixes", "#/fixes", "prompt B edits safety gate replay"],
  ["Prompts", "#/prompts", "versions lineage text v1 production candidate"],
  ["Experiments", "#/experiments", "A/B test prove approve reject engine scenarios"],
  ["Settings", "#/settings", "models budget spend privacy data locations"],
];
let palLoad = null, palItems = null, palSel = 0, palShown = [];

async function loadPalette() {
  const items = PAGES.map(([t, href, w]) => ({ k: "Page", t, w, href }));
  items.push({ k: "Action", t: "Switch light / dark theme", w: "theme mode", run: () => themeBtn.click() });
  const prod = state.overview?.production?.version || "v1";
  const got = await Promise.allSettled([get("/api/prompts"), get(`/api/issues?version=${enc(prod)}`), get("/api/experiments"),
    get("/api/fixes"), get("/api/rubrics"), get("/api/calls?limit=500")]);
  const [p, rc, x, f, r, c] = got.map((s) => (s.status === "fulfilled" ? s.value.items : []));
  items.push(...p.map((i) => ({ k: "Prompt", t: `${i.version} · ${i.status}`, s: i.fix_id ? `from ${i.parent} · ${i.fix_id}` : "original prompt", href: `#/prompts?v=${enc(i.version)}` })));
  items.push(...rc.map((i) => ({ k: "Root cause", t: i.name, s: `${i.n_calls} calls · priority ${Number(i.priority).toFixed(1)}`, w: i.description, href: `#/issues?version=${enc(i.prompt_version)}` })));
  items.push(...x.map((i) => ({ k: "Experiment", t: i.experiment_id, s: i.state, w: i.assumed_effect, href: `#/experiments/${enc(i.experiment_id)}` })));
  items.push(...f.map((i) => ({ k: "Fix", t: i.id, s: i.rationale || "", href: `#/fixes/${enc(i.id)}` })));
  items.push(...r.map((i) => ({ k: "Rubric", t: i.name, s: i.category, href: `#/rubrics/${enc(i.id)}` })));
  items.push(...c.map((i) => ({ k: "Call", t: i.call_id, s: i.summary || "", href: `#/calls/${enc(i.call_id)}` })));  // last: there are hundreds
  return items;
}

function drawPalette() {
  const q = palQ.value.trim().toLowerCase();
  palShown = (palItems || []).filter((i) => !q || `${i.k} ${i.t} ${i.s || ""} ${i.w || ""}`.toLowerCase().includes(q)).slice(0, 60);
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
  const load = (palLoad ||= loadPalette());  // kept until the data changes (vani:changed)
  const items = await load;
  if (load === palLoad) { palItems = items; drawPalette(); }
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
  $("#foot-prod").textContent = state.overview?.production?.version || "—";
  $("#foot-mode").textContent = state.demo ? "demo" : "live";
  $("#foot-dot").className = `dot ${state.demo ? "demo" : "live"}`;
}
window.addEventListener("vani:changed", async () => {
  try { state.overview = await get("/api/overview"); } catch { /* keep the last one */ }
  foot();
  palLoad = palItems = null;
});

(async () => {
  if (location.protocol === "file:") return;
  $("#brand-ring").setAttribute("d", brandRingPath());
  try {
    await boot();
  } catch (err) {
    bootFailed = true;
    mount(view, failState(err));
    return;
  }
  if (state.demo) {
    $("#demo-banner").hidden = false;
    document.documentElement.classList.add("is-demo");
  }
  state.overviewFresh = Boolean(state.overview);  // the home page's first render reuses it
  foot();
  syncTheme();
  // Back and Forward land where the reader was: each history entry keeps its own scroll position
  // (a new page has none, so it opens at the top).
  history.scrollRestoration = "manual";
  let saveY = 0;
  addEventListener("scroll", () => { clearTimeout(saveY); saveY = setTimeout(() => history.replaceState({ ...history.state, y: Math.round(scrollY) }, ""), 150); }, { passive: true });
  addEventListener("hashchange", async () => {
    clearTimeout(saveY);
    setMenu(false);
    const y = history.state?.y, at = location.hash;
    view.focus({ preventScroll: true });
    scrollTo({ top: 0 });
    await route();
    if (y && location.hash === at) scrollTo({ top: y });
  });
  addEventListener("vani:rerender", () => route(true));
  route();
})();
