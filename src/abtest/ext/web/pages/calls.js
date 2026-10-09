// #/calls (a filterable list) and #/calls/<id> (player, transcript record with margin notes,
// the judge's scorecard beside IndiaMART's human QA grades).
import { get, state } from "../api.js";
import { $, $$, html, raw, esc, mount, int, clock, mmss, dur, grade, flagChip, FLAGS, empty, enc, seg, toast } from "../ui.js";
import { decodePeaks, drawWave } from "../viz.js";

const SOURCES = [["", "All"], ["production", "Production"], ["audited", "Audited"], ["replay", "Replay"]];
const OVERALL = [["", "Any grade"], ["fatal", "Fatal"], ["non_fatal", "Non-fatal"], ["pass", "Pass"]];
const LANG = { hi: "Hindi", en: "English", te: "Telugu", mixed: "Hinglish" };

export async function renderList(el, { params }) {
  const f = { q: params.get("q") || "", source: params.get("source") || "", overall: params.get("overall") || "", flag: params.get("flag") || "" };
  mount(el, html`
    <div class="page-head"><div><div class="eyebrow">Listen</div><h1 class="title">Calls</h1>
      <p class="lede">Every transcribed call, with the judge's grade and, where IndiaMART audited it, the human grade beside it.</p></div></div>
    ${state.demo ? html`<div class="note" style="margin-bottom:18px"><b>Demo data.</b> These calls are invented samples: the names, products and grades are made up. Real calls stay on the machine running the platform.</div>` : ""}
    <div class="filters">
      <label class="search"><span class="sr-only">Search calls</span><svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="7" cy="7" r="4.6"/><path d="M10.5 10.5L14 14"/></svg>
        <input class="input" id="q" type="search" placeholder="Search id, product, disposition" value="${f.q}"></label>
      <span id="f-source">${seg(SOURCES, f.source)}</span>
      <span id="f-overall">${seg(OVERALL, f.overall)}</span>
      <label><span class="sr-only">Risk flag</span><select class="select" id="flag" style="width:auto"><option value="">Any flag</option>
        ${Object.entries(FLAGS).map(([k, l]) => html`<option value="${k}" ${k === f.flag ? "selected" : ""}>${l}</option>`)}</select></label>
    </div>
    <div id="list"></div>`);

  let t = 0, alive = true, ticket = 0;
  const load = async () => {
    const mine = ++ticket;  // a slow answer to an older filter must not overwrite a newer one
    const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v));
    history.replaceState(null, "", `#/calls${qs.toString() ? `?${qs}` : ""}`);
    qs.set("limit", "500");
    const host = $("#list", el);
    try {
      const r = await get(`/api/calls?${qs}`);
      if (!alive || mine !== ticket) return;
      const filtered = Object.values(f).some(Boolean);
      if (!r.items.length) {
        return mount(host, filtered ? empty({ title: "No calls match", body: "Loosen a filter or clear the search.", compact: true, glyph: "wave" })
          : empty({ title: "No transcripts yet", body: "Listen turns the call recordings into transcripts the judge can read.", cmd: "make listen", glyph: "wave" }));
      }
      mount(host, html`<p class="muted" style="font-size:12.5px;margin:0 0 10px">${int(r.total)} call${r.total === 1 ? "" : "s"}${r.total > r.items.length ? ` · showing the first ${int(r.items.length)}` : ""}</p>
        <div class="card flush"><div class="table-wrap"><table class="table">
        <thead><tr><th>Call</th><th>Source</th><th>Language</th><th class="r">Length</th><th>Prompt</th><th>Judge</th><th>Human QA</th><th>Summary</th></tr></thead>
        <tbody>${r.items.map((c) => html`<tr class="link" data-href="#/calls/${enc(c.call_id)}">
          <td class="id"><a href="#/calls/${enc(c.call_id)}" style="text-decoration:none">${c.call_id}</a></td>
          <td><span class="chip">${c.source}</span></td><td class="muted">${LANG[c.language] || c.language || "—"}</td>
          <td class="r mono">${dur(c.duration_s)}</td><td class="mono">${c.prompt_version}</td>
          <td>${c.overall ? grade(c.overall) : html`<span class="muted" style="font-size:12px">not judged</span>`}${c.n_fatal + c.n_non_fatal ? html`<div class="muted mono" style="font-size:10.5px;margin-top:4px">${c.n_fatal} F · ${c.n_non_fatal} NF</div>` : ""}</td>
          <td>${c.human_overall ? grade(c.human_overall) : html`<span class="muted">—</span>`}</td>
          <td><div class="sum">${c.summary || ""}</div>${c.risk_flags?.length ? html`<div class="row" style="gap:4px;margin-top:6px">${c.risk_flags.map(flagChip)}</div>` : ""}</td></tr>`)}</tbody>
        </table></div></div>`);
      $$("tr.link", host).forEach((tr) => tr.addEventListener("click", (e) => { if (!e.target.closest("a")) location.hash = tr.dataset.href; }));
    } catch (err) {
      if (alive) mount(host, empty({ title: "Calls could not load", body: err.message, glyph: "lock", compact: true }));
    }
  };
  $("#q", el).addEventListener("input", (e) => { f.q = e.target.value.trim(); clearTimeout(t); t = setTimeout(load, 220); });
  $("#flag", el).addEventListener("change", (e) => { f.flag = e.target.value; load(); });
  for (const [id, key] of [["#f-source", "source"], ["#f-overall", "overall"]]) {
    $(id, el).addEventListener("click", (e) => {
      const b = e.target.closest("button[data-v]");
      if (!b) return;
      f[key] = b.dataset.v;
      $$("button", $(id, el)).forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      load();
    });
  }
  await load();
  return () => { alive = false; clearTimeout(t); };
}

// ------------------------------------------------------------------------------------- one call
function marked(text, quotes) {  // wrap each verdict's quote in <mark data-v>, skipping overlaps
  const spans = [];
  for (const { q, v } of quotes) {
    let at = text.indexOf(q);
    if (at < 0) at = text.toLowerCase().indexOf(q.toLowerCase());
    if (at >= 0 && q) spans.push([at, at + q.length, v]);
  }
  spans.sort((a, b) => a[0] - b[0]);
  let out = "", i = 0;
  for (const [s, e, v] of spans) {
    if (s < i) continue;
    out += esc(text.slice(i, s)) + `<mark data-v="${v}">${esc(text.slice(s, e))}</mark>`;
    i = e;
  }
  return raw(out + esc(text.slice(i)));
}

export async function renderCall(el, { id }) {
  const d = await get(`/api/calls/${enc(id)}`);
  const t = d.transcript, a = d.audit, h = d.human, meta = d.rubrics || {};
  const turns = t.turns || [];
  const verdicts = (a?.verdicts || []).map((v, i) => ({ ...v, i }));
  const SEV = { fatal: 0, non_fatal: 1, pass: 2, na: 3 };
  const noted = verdicts.filter((v) => v.evidence?.length && v.grade !== "na").sort((x, y) => SEV[x.grade] - SEV[y.grade]);
  const notesAt = {}, quotesAt = {};
  for (const v of noted) {
    const first = Math.min(...v.evidence.map((e) => e.turn));
    (notesAt[first] ||= []).push(v);
    for (const e of v.evidence) (quotesAt[e.turn] ||= []).push({ q: e.quote, v: v.i });
  }
  const name = (rid) => meta[rid]?.name || rid;
  const agree = h && a ? (h.overall === a.overall) : null;
  const rows = [...new Set([...verdicts.map((v) => v.rubric_id), ...Object.keys(h?.grades || {})])]
    .map((rid) => ({ rid, v: verdicts.find((x) => x.rubric_id === rid), hg: h?.grades?.[rid], remark: h?.remarks?.[rid] }))
    .sort((x, y) => (SEV[x.v?.grade ?? x.hg] ?? 4) - (SEV[y.v?.grade ?? y.hg] ?? 4));
  const total = t.duration_s || (turns.length ? turns[turns.length - 1].end_s : 0) || 0;

  mount(el, html`
    <div class="call-head">
      <div class="eyebrow">Call · ${t.source}${t.vendor ? ` · ${t.vendor}` : ""}${t.language ? ` · ${LANG[t.language] || t.language}` : ""}</div>
      <h1 class="title">${a?.summary || `Call ${t.call_id}`}</h1>
      <div class="meta"><span class="mono">${t.call_id}</span><span><b>${dur(total)}</b></span><span>Prompt <b class="mono">${t.prompt_version}</b>${t.variant ? ` · variant ${t.variant}` : ""}</span>
        ${a?.disposition ? html`<span>Disposition <b>${a.disposition}</b></span>` : ""}<span>Transcribed by <b class="mono">${t.provider}</b></span>
        ${Object.entries(t.meta || {}).filter(([k]) => k !== "sample").map(([k, v]) => html`<span>${k} <b>${v}</b></span>`)}</div>
      <div class="verdict-pair">
        <span class="vbox">Judge ${a ? grade(a.overall) : html`<span>not judged</span>`}</span>
        <span class="vbox">Human QA ${h ? grade(h.overall) : html`<span>not audited</span>`}</span>
        ${agree != null ? html`<span class="agree ${agree ? "y" : "n"}">${agree ? "✓ agree on the overall grade" : "✕ disagree on the overall grade"}</span>` : ""}
        ${(a?.risk_flags || []).map(flagChip)}
      </div>
    </div>

    <div class="card flush" id="player"></div>

    <section class="section" style="margin-top:34px">
      <div class="section-head"><h2>Transcript, <em>as recorded</em></h2><p>${turns.length} turns · notes in the margin are the judge's evidence; hover one to see its quote.</p></div>
      ${turns.length ? html`<div class="record" id="record">${turns.map((tu, k) => html`
        <div class="turn ${tu.speaker}" data-k="${k}" data-s="${tu.start_s ?? ""}">
          <span class="ln">${String(k + 1).padStart(2, "0")}</span>
          ${tu.start_s != null ? html`<button class="ts" type="button" data-seek="${tu.start_s}" aria-label="Play from ${clock(tu.start_s)}">${clock(tu.start_s)}</button>` : html`<span class="ts">—</span>`}
          <span class="sp">${tu.speaker}</span>
          <div class="tx">${marked(tu.text, quotesAt[k] || [])}${tu.confidence != null && tu.confidence < 0.7 ? html`<span class="conf" title="Speech recognition confidence">conf ${tu.confidence.toFixed(2)}</span>` : ""}</div>
          <div class="notes-col">${(notesAt[k] || []).map((v) => html`<div class="mnote g-${v.grade}" tabindex="0" data-v="${v.i}">
            <div class="h">${name(v.rubric_id)} ${grade(v.grade)}</div>${v.reason}</div>`)}</div>
        </div>`)}</div>` : empty({ title: "No turns in this transcript", body: "The recording may be silent or the transcriber returned nothing.", compact: true, glyph: "wave" })}
    </section>

    <section class="section">
      <div class="section-head"><h2>Scorecard</h2><p>${a ? `${verdicts.length} rubrics · ${a.model} · rubric set ${a.rubric_set_hash}` : "Not judged yet"}</p></div>
      ${rows.length ? html`<div class="card flush"><div class="table-wrap"><table class="table scorecard">
        <thead><tr><th>Rubric</th><th>Judge</th><th class="r hide-sm">Conf.</th><th>Human QA</th><th></th><th class="hide-sm">Why</th></tr></thead>
        <tbody>${rows.map(({ rid, v, hg, remark }) => html`<tr>
          <td><div>${name(rid)}</div><div class="muted mono" style="font-size:10.5px;margin-top:2px">${meta[rid]?.category || ""}${meta[rid]?.root_type ? ` · ${meta[rid].root_type}` : ""}</div></td>
          <td>${v ? grade(v.grade) : html`<span class="muted">—</span>`}</td><td class="r mono hide-sm">${v ? v.confidence.toFixed(2) : ""}</td>
          <td>${hg ? grade(hg) : html`<span class="muted">—</span>`}</td>
          <td>${v && hg ? html`<span class="agree ${v.grade === hg ? "y" : "n"}">${v.grade === hg ? "✓" : "✕"}</span>` : ""}</td>
          <td class="why hide-sm">${v?.reason || ""}${remark ? html`<div style="margin-top:4px;font-style:italic">QA: ${remark}</div>` : ""}</td></tr>`)}</tbody>
      </table></div></div>` : empty({ title: "Not judged yet", body: "Run the judge to grade this call with every active rubric.", cmd: "make judge", compact: true, glyph: "doc" })}
    </section>`);

  // margin note <-> quote highlighting
  const hl = (v, on) => $$(`mark[data-v="${v}"]`, el).forEach((m) => m.classList.toggle("on", on));
  $$(".mnote", el).forEach((n) => {
    for (const [ev, on] of [["mouseenter", true], ["mouseleave", false], ["focus", true], ["blur", false]]) n.addEventListener(ev, () => hl(n.dataset.v, on));
  });
  $$("mark[data-v]", el).forEach((m) => {
    m.addEventListener("mouseenter", () => { hl(m.dataset.v, true); $(`.mnote[data-v="${m.dataset.v}"]`, el)?.classList.add("hover"); });
    m.addEventListener("mouseleave", () => hl(m.dataset.v, false));
  });
  return mountPlayer($("#player", el), el, { url: d.audio_url, turns, total });
}

function mountPlayer(host, page, { url, turns, total }) {
  const lane = total ? turns.filter((t) => t.start_s != null).map((t) =>
    `<span class="${esc(t.speaker)}" style="left:${(100 * t.start_s / total).toFixed(3)}%;width:${Math.max(0.3, 100 * ((t.end_s ?? t.start_s + 1) - t.start_s) / total).toFixed(3)}%" title="${esc(t.speaker)} ${clock(t.start_s)}"></span>`).join("") : "";
  const legend = html`<span class="legend"><span><i class="lane-bot" style="background:var(--accent)"></i>Bot</span><span><i style="background:var(--accent-2)"></i>Buyer</span></span>`;
  const noAudio = (title, body) => mount(host, html`<div class="player"><button class="play" disabled aria-label="No recording"><svg viewBox="0 0 16 16"><path d="M4 2.5l9 5.5-9 5.5z"/></svg></button>
    <div class="wave-area no-audio"><div class="wave-top"><span>${title}</span>${legend}<span>${mmss(total)}</span></div>
      <div class="flat"></div><div class="lane">${raw(lane)}</div><p>${body}</p></div></div>`);
  if (!url) {
    noAudio("No recording", state.demo ? "Invented sample calls have no recordings, except sample-0001's synthetic placeholder." : "No recording on this machine for this call (Sarvam recordings are not downloadable).");
    return () => {};
  }
  mount(host, html`<div class="player"><button class="play" id="play" type="button" aria-label="Play"><svg viewBox="0 0 16 16"><path d="M4 2.5l9 5.5-9 5.5z"/></svg></button>
    <div class="wave-area"><div class="wave-top"><span class="now" id="now">0:00</span>${legend}<span id="len">${mmss(total)}</span></div>
      <div class="wave-box" id="wave" role="slider" tabindex="0" aria-label="Seek in the recording" aria-valuemin="0" aria-valuemax="${Math.round(total)}" aria-valuenow="0"><canvas></canvas><span class="hover-t" hidden></span></div>
      <div class="lane">${raw(lane)}</div>
      ${state.demo ? html`<p class="muted" style="font-size:11.5px">Synthetic placeholder audio for an invented call: a murmur timed to the transcript, not speech.</p>` : ""}</div></div>`);
  const audio = new Audio();
  audio.preload = "auto";
  audio.src = url;
  const btn = $("#play", host), wave = $("#wave", host), canvas = $("canvas", wave), tip = $(".hover-t", wave);
  let peaks = new Float32Array(400).fill(0.04), raf = 0, hover = null, alive = true, cur = -1;
  const length = () => (Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : total || 1);
  const rows = $$(".turn", page);
  const draw = () => {
    if (!alive) return;
    const p = Math.min(1, audio.currentTime / length());
    drawWave(canvas, peaks, p, hover);
    $("#now", host).textContent = mmss(audio.currentTime);
    wave.setAttribute("aria-valuenow", String(Math.round(audio.currentTime)));
    wave.setAttribute("aria-valuetext", `${mmss(audio.currentTime)} of ${mmss(length())}`);
    let k = -1;
    for (let i = 0; i < turns.length; i++) if (turns[i].start_s != null && turns[i].start_s <= audio.currentTime + 0.05) k = i;
    if (k !== cur) {
      rows[cur]?.classList.remove("current");
      if (k >= 0 && (audio.currentTime > 0 || !audio.paused)) {
        rows[k]?.classList.add("current");
        if (!audio.paused) {
          const r = rows[k]?.getBoundingClientRect(), top = Math.max(70, host.getBoundingClientRect().bottom + 8);
          if (r && (r.top < top || r.bottom > innerHeight - 20)) scrollBy({ top: r.top - (top + innerHeight) / 2 + r.height / 2, behavior: "smooth" });
        }
      }
      cur = k;
    }
  };
  const loop = () => { draw(); if (!audio.paused) raf = requestAnimationFrame(loop); };
  const icon = (playing) => { btn.innerHTML = playing ? '<svg viewBox="0 0 16 16"><path d="M4 3h3v10H4zM9 3h3v10H9z"/></svg>' : '<svg viewBox="0 0 16 16"><path d="M4 2.5l9 5.5-9 5.5z"/></svg>'; btn.setAttribute("aria-label", playing ? "Pause" : "Play"); };
  const toggle = () => (audio.paused ? audio.play().catch((e) => toast(`Playback failed: ${e.message}`, "harm")) : audio.pause());
  btn.addEventListener("click", toggle);
  audio.addEventListener("play", () => { icon(true); cancelAnimationFrame(raf); loop(); });
  audio.addEventListener("pause", () => { icon(false); draw(); });
  audio.addEventListener("seeked", draw);
  audio.addEventListener("loadedmetadata", () => { $("#len", host).textContent = mmss(length()); draw(); });
  audio.addEventListener("error", () => {
    if (!alive) return;
    dispose();  // the controls are about to go: stop everything that draws into them
    noAudio("Recording unavailable", "The recording could not be loaded: the file is missing on this machine.");
  });
  const seekTo = (clientX) => { const r = wave.getBoundingClientRect(); audio.currentTime = Math.max(0, Math.min(1, (clientX - r.left) / r.width)) * length(); draw(); };
  let dragging = false;
  wave.addEventListener("pointerdown", (e) => { dragging = true; wave.setPointerCapture(e.pointerId); seekTo(e.clientX); });
  wave.addEventListener("pointermove", (e) => {
    const r = wave.getBoundingClientRect(), x = e.clientX - r.left;
    hover = x; tip.hidden = false; tip.style.left = `${x}px`; tip.textContent = mmss((x / r.width) * length());
    if (dragging) seekTo(e.clientX); else if (audio.paused) draw();
  });
  wave.addEventListener("pointerup", () => { dragging = false; });
  wave.addEventListener("pointerleave", () => { hover = null; tip.hidden = true; if (audio.paused) draw(); });
  wave.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -5, ArrowRight: 5 }[e.key];
    if (step) { audio.currentTime = Math.max(0, Math.min(length(), audio.currentTime + step)); e.preventDefault(); }
    else if (e.key === "Home") { audio.currentTime = 0; e.preventDefault(); }
    else if (e.key === "End") { audio.currentTime = length(); e.preventDefault(); }
    else if (e.key === " " || e.key === "Enter") { toggle(); e.preventDefault(); }
  });
  page.addEventListener("click", (e) => {
    const b = e.target.closest("[data-seek]");
    if (!b) return;
    audio.currentTime = Number(b.dataset.seek);
    if (audio.paused) audio.play().catch(() => {});
  });
  const ro = new ResizeObserver(() => draw());
  ro.observe(wave);
  const onTheme = () => draw();
  window.addEventListener("vani:theme", onTheme);
  decodePeaks(url).then((r) => { peaks = r.peaks; draw(); }).catch(() => {
    if (!alive) return;
    const note = document.createElement("p");
    note.className = "muted";
    note.style.fontSize = "11.5px";
    note.textContent = "Waveform unavailable for this recording (it plays from its original link); the lane below shows who spoke when.";
    $(".wave-area", host)?.append(note);
  });
  draw();
  function dispose() { alive = false; cancelAnimationFrame(raf); ro.disconnect(); window.removeEventListener("vani:theme", onTheme); }
  return () => { dispose(); audio.pause(); audio.removeAttribute("src"); audio.load(); };
}
