// Drawn elements: the loop ring, the z-path chart, the call waveform, the prompt lineage and the
// confusion matrix. SVG is built as markup; the ring and the waveform redraw on animation frames.
import { html, raw, esc, fix, pct, int, reducedMotion, day } from "./ui.js";

// ------------------------------------------------------------------------------- the loop ring
const TAU = Math.PI * 2;
export const STATIONS = ["Listen", "Judge", "Improve", "Prove", "Roll out"];
const stationAngle = (i) => -Math.PI / 2 + (i * TAU) / 5;
const wrap = (a) => ((a + Math.PI) % TAU + TAU) % TAU - Math.PI;

/** A circle whose radius carries a few sines: a waveform bent into a loop. `focus` is the angle
 *  where the signal is loudest (the comet), so the ring "speaks" at the current station. */
export function ringPath(cx, cy, R, phase, focus = -Math.PI / 2, amp = 1, steps = 300) {
  let d = "";
  for (let i = 0; i <= steps; i++) {
    const a = (i / steps) * TAU - Math.PI / 2;
    const env = 0.16 + 0.84 * Math.exp(-((wrap(a - focus) / 0.7) ** 2));
    const r = R + amp * env * 1.25 * (6.4 * Math.sin(9 * a + phase) + 3.6 * Math.sin(17 * a - 1.4 * phase)
      + 2.2 * Math.sin(31 * a + 2.3 * phase) + 1.3 * Math.sin(53 * a - 3.1 * phase));
    d += `${i ? "L" : "M"}${(cx + r * Math.cos(a)).toFixed(1)} ${(cy + r * Math.sin(a)).toFixed(1)}`;
  }
  return d + "Z";
}

export function mountRing(host, { station, halt = false, version, sub = "", sha = "", busy = false, start = 0 }) {
  const C = 250, R = 188;
  const st = STATIONS.map((name, i) => {
    const a = stationAngle(i), x = C + R * Math.cos(a), y = C + R * Math.sin(a);
    const lx = C + (R + 30) * Math.cos(a), ly = C + (R + 30) * Math.sin(a);
    const anchor = Math.cos(a) > 0.3 ? "start" : Math.cos(a) < -0.3 ? "end" : "middle";
    const dy = Math.sin(a) < -0.5 ? -6 : Math.sin(a) > 0.5 ? 20 : 6;
    const cls = i < station ? "done" : i === station ? (halt ? "halt" : "current") : "";
    return `<g class="st ${cls}"><circle class="halo" cx="${x}" cy="${y}" r="13"/><circle class="node" cx="${x}" cy="${y}" r="6"/>`
      + `<text class="n" x="${lx}" y="${ly + dy - 17}" text-anchor="${anchor}">0${i + 1}</text>`
      + `<text x="${lx}" y="${ly + dy}" text-anchor="${anchor}">${esc(name)}</text></g>`;
  }).join("");
  host.innerHTML = `<svg class="ring" viewBox="-24 -24 548 548" role="img" aria-label="The loop: ${STATIONS.map((s, i) => `${s}${i === station ? " (current)" : i < station ? " (done)" : ""}`).join(", ")}">
    <circle class="guide" cx="${C}" cy="${C}" r="${R - 30}"/>
    <path class="echo" d=""/><path class="wave" d=""/><g class="comet"></g>${st}</svg>
    <div class="ring-center"><div class="eyebrow">In production</div><div class="v">${esc(version)}</div><div class="sha">${esc(sha)}</div>${sub ? `<div class="eyebrow" style="margin-top:10px">${esc(sub)}</div>` : ""}</div>`;
  const wave = host.querySelector(".wave"), echo = host.querySelector(".echo"), comet = host.querySelector(".comet");
  const SEG = 26, TAIL = 0.95;  // the comet: a head with a glow and a tapering tail of short arcs
  comet.innerHTML = Array.from({ length: SEG }, (_, k) => `<path fill="none" stroke="var(--accent-2)" stroke-linecap="round" stroke-width="${(4.6 * (1 - k / SEG) + 0.4).toFixed(2)}" opacity="${((1 - k / SEG) ** 1.7 * 0.95).toFixed(3)}"/>`).join("")
    + `<circle r="15" fill="var(--accent-2)" opacity=".16"/><circle r="6.5" fill="var(--accent-2)"/>`;
  const segs = [...comet.querySelectorAll("path")], [glow, head] = comet.querySelectorAll("circle");
  const at = (a) => `${(C + R * Math.cos(a)).toFixed(1)} ${(C + R * Math.sin(a)).toFixed(1)}`;
  const dots = { place(angle) {
    segs.forEach((s, k) => {
      const a1 = angle - (k / SEG) * TAIL, a2 = angle - ((k + 1) / SEG) * TAIL;
      s.setAttribute("d", `M${at(a1)}A${R} ${R} 0 0 0 ${at(a2)}`);
    });
    for (const c of [glow, head]) { c.setAttribute("cx", (C + R * Math.cos(angle)).toFixed(1)); c.setAttribute("cy", (C + R * Math.sin(angle)).toFixed(1)); }
  } };
  const target = stationAngle(station);
  const from = stationAngle(start);
  let sweep = ((target - from) % TAU + TAU) % TAU;
  if (sweep < 0.01 && !busy) sweep = TAU;  // arriving at the same station: one full lap
  const t0 = performance.now(), still = reducedMotion();
  let raf = 0, angle = target, phase = 0.8;

  function frame(now) {
    if (!host.isConnected) return;
    const t = now - t0;
    if (still) angle = target;
    else if (ringState.busy) angle = target + (t / 7000) * TAU;
    else { const k = Math.min(1, t / 2200); angle = from + sweep * (1 - (1 - k) ** 3); }
    phase = still ? 0.8 : 0.8 + t * 0.0011;
    wave.setAttribute("d", ringPath(C, C, R, phase, angle));
    echo.setAttribute("d", ringPath(C, C, R - 11, -phase * 0.8 + 2, angle, 0.5));
    dots.place(angle);
    if (!still) raf = requestAnimationFrame(frame);
  }
  const ringState = { busy, stop: () => cancelAnimationFrame(raf), setBusy: (b) => { ringState.busy = b; } };
  frame(performance.now());
  return ringState;
}

export const brandRingPath = () => ringPath(16, 16, 11.2, 1.1, -Math.PI / 2, 0.16, 120);

// --------------------------------------------------------------------------------- z-path chart
const STATE_WORD = { PROMOTED: "promoted", PENDING_APPROVAL: "awaiting approval", REJECTED: "rejected", STOPPED_HARM: "stopped",
  BLOCKED_GUARDRAIL: "blocked", HALTED_SRM: "halted", INCONCLUSIVE: "inconclusive" };
export function zChart(allLooks, { width = 760, state = "RUNNING", started = null } = {}) {
  const ok = (x) => typeof x === "number" && Number.isFinite(x);  // a degenerate look can carry null (NaN upstream)
  const looks = allLooks.filter((l) => ok(l.z) && ok(l.info_fraction) && ok(l.win_bound) && ok(l.harm_bound));
  if (!looks.length) return "";
  const H = width < 560 ? 300 : 360, m = { l: 40, r: 16, t: 18, b: 46 };
  const iw = width - m.l - m.r, ih = H - m.t - m.b;
  const xs = looks.map((l) => l.info_fraction), zs = looks.map((l) => l.z);
  const win = looks.map((l) => l.win_bound), harm = looks.map((l) => -Math.abs(l.harm_bound));
  const xmax = Math.max(1.06, ...xs.map((x) => x + 0.05));
  const ymax = Math.max(4.5, ...win.map((w) => Math.min(w, 9) + 0.7), ...zs.map((z) => z + 1));
  const ymin = Math.min(-3.4, ...harm.map((h) => h - 0.7), ...zs.map((z) => z - 1));
  const X = (t) => m.l + (t / xmax) * iw, Y = (z) => m.t + ((ymax - z) / (ymax - ymin)) * ih;
  const pts = (arr) => looks.map((l, i) => `${X(l.info_fraction).toFixed(1)},${Y(arr[i]).toFixed(1)}`);
  const widen = looks.length === 1 ? 14 : 0;
  const x0 = X(xs[0]) - widen, x1 = X(xs[xs.length - 1]) + widen;
  const winPts = looks.length === 1 ? [`${x0},${Y(win[0])}`, `${x1},${Y(win[0])}`] : pts(win);
  const harmPts = looks.length === 1 ? [`${x0},${Y(harm[0])}`, `${x1},${Y(harm[0])}`] : pts(harm);
  const zoneWin = `${winPts.join(" ")} ${x1},${Y(ymax)} ${x0},${Y(ymax)}`;
  const zoneHarm = `${harmPts.join(" ")} ${x1},${Y(ymin)} ${x0},${Y(ymin)}`;
  const path = [[X(0), Y(0)], ...looks.map((l) => [X(l.info_fraction), Y(l.z)])];
  const len = path.slice(1).reduce((s, p, i) => s + Math.hypot(p[0] - path[i][0], p[1] - path[i][1]), 0);
  const yt = [];
  for (let z = Math.ceil(ymin); z <= Math.floor(ymax); z += (ymax - ymin > 10 ? 2 : 1)) yt.push(z);
  const xt = [0, 0.25, 0.5, 0.75, 1].filter((t) => t <= xmax);
  const lastAll = allLooks[allLooks.length - 1];  // the look that decided; no note if it could not be plotted
  const deciding = state !== "RUNNING" && state !== "DRAFT" && looks.includes(lastAll) ? lastAll : null;
  const dayOf = (ts) => (started && ts ? Math.round((new Date(ts) - new Date(started)) / 864e5) : null);
  let callout = "";
  if (deciding) {
    const px = X(deciding.info_fraction), py = Y(deciding.z);
    const crossedWin = deciding.z >= deciding.win_bound, crossedHarm = deciding.z <= -Math.abs(deciding.harm_bound);
    const d = dayOf(deciding.ts_sim);
    const won = state === "PROMOTED" || state === "PENDING_APPROVAL" || state === "REJECTED";
    const l1 = `Look ${deciding.look}${d != null ? ` · day ${d}` : ""} · ${STATE_WORD[state] || "decided here"}`;
    const zs = `z = ${fix(deciding.z)}`;
    const l2 = crossedHarm ? `${zs} ≤ harm ${fix(-Math.abs(deciding.harm_bound))}`
      : crossedWin && won ? `${zs} ≥ win ${fix(deciding.win_bound)}`
      : state === "STOPPED_HARM" || state === "BLOCKED_GUARDRAIL" ? `${zs} · the guardrail decided`
      : state === "HALTED_SRM" ? "the split failed its SRM check"
      : state === "INCONCLUSIVE" ? `${zs} · no boundary by the last look` : `${zs}, inside both boundaries`;
    const w = Math.max(l1.length, l2.length) * 6.9 + 24, h = 46;
    // First spot that fits the plot and keeps the z-path visible: right, left, above, below, then the zero band.
    const samples = [];
    for (let i = 1; i < path.length; i++) {
      const [ax, ay] = path[i - 1], [cx, cy] = path[i], n = Math.ceil(Math.hypot(cx - ax, cy - ay) / 4);
      for (let k = 0; k <= n; k++) samples.push([ax + ((cx - ax) * k) / n, ay + ((cy - ay) * k) / n]);
    }
    const fits = ([x, y]) => x >= m.l && x + w <= width - m.r && y >= m.t && y + h <= H - m.b;
    const clear = ([x, y]) => !samples.some(([sx, sy]) => sx > x - 4 && sx < x + w + 4 && sy > y - 4 && sy < y + h + 4);
    const spots = [[px + 18, py - h / 2], [px - w - 18, py - h / 2], [px - w + 20, py - h - 22], [px - w + 20, py + 22],
      [px - w / 2, py - h - 22], [px - w / 2, py + 22], [m.l + 6, py < Y(0) ? Y(0) + 8 : Y(0) - h - 8]];
    const [bx, by] = spots.find((p) => fits(p) && clear(p)) || spots.find(fits) || [m.l + 6, m.t];
    const tone = { PROMOTED: "var(--win)", PENDING_APPROVAL: "var(--accent-2)", STOPPED_HARM: "var(--harm)", BLOCKED_GUARDRAIL: "var(--harm)", HALTED_SRM: "var(--warn)" }[state] || "var(--faint)";
    const ax = Math.min(Math.max(px, bx), bx + w), ay = Math.min(Math.max(py, by), by + h);  // leader to the nearest edge
    callout = `<g><line x1="${px}" y1="${py}" x2="${ax}" y2="${ay}" stroke="${tone}" stroke-width="1"/>
      <rect class="call-b" x="${bx}" y="${by}" width="${w}" height="${h}" rx="8"/>
      <rect x="${bx}" y="${by}" width="3" height="${h}" rx="1.5" fill="${tone}"/>
      <text class="call" x="${bx + 14}" y="${by + 19}" style="font-weight:600">${esc(l1)}</text>
      <text class="call" x="${bx + 14}" y="${by + 36}" style="font-family:var(--f-mono);font-size:11.5px">${esc(l2)}</text></g>`;
  }
  return `<svg class="zchart" viewBox="0 0 ${width} ${H}" width="${width}" height="${H}" role="img" aria-label="z-path across ${looks.length} looks against the win and harm boundaries">
    <g class="grid">${yt.map((z) => `<line x1="${m.l}" x2="${width - m.r}" y1="${Y(z)}" y2="${Y(z)}"/>`).join("")}</g>
    <polygon class="zone-win" points="${zoneWin}"/><polygon class="zone-harm" points="${zoneHarm}"/>
    <line class="zero" x1="${m.l}" x2="${width - m.r}" y1="${Y(0)}" y2="${Y(0)}"/>
    ${xmax >= 1 ? `<line x1="${X(1)}" x2="${X(1)}" y1="${m.t}" y2="${H - m.b}" stroke="var(--hairline-2)" stroke-dasharray="2 4"/>${width >= 560 ? `<text class="lbl" x="${X(1) - 6}" y="${m.t + 10}" text-anchor="end">planned n_B,max</text>` : ""}` : ""}
    <polyline class="b-win" points="${winPts.join(" ")}"/><polyline class="b-harm" points="${harmPts.join(" ")}"/>
    <text class="zone-lbl" x="${x0 + 8}" y="${Y(ymax) + 16}" fill="var(--win)">Win zone · promote B</text>
    <text class="zone-lbl" x="${x0 + 8}" y="${Y(ymin) - 9}" fill="var(--harm)">Harm zone · stop B</text>
    <path class="zpath draw" style="--len:${len.toFixed(0)}" d="M${path.map((p) => p.map((v) => v.toFixed(1)).join(" ")).join("L")}"/>
    ${looks.map((l, i) => `<circle class="pt ${l === deciding ? "decide" : ""}" cx="${X(l.info_fraction)}" cy="${Y(l.z)}" r="${l === deciding ? 6 : 4.2}"><title>Look ${l.look}: z = ${fix(l.z)}; win ≥ ${fix(l.win_bound)}; harm ≤ ${fix(-Math.abs(l.harm_bound))}; lift ${pct(l.lift_rel)}</title></circle>`).join("")}
    ${looks.map((l, i) => `<circle cx="${X(l.info_fraction)}" cy="${Y(win[i])}" r="2.4" fill="var(--win)"/><circle cx="${X(l.info_fraction)}" cy="${Y(harm[i])}" r="2.4" fill="var(--harm)"/>`).join("")}
    <circle cx="${X(0)}" cy="${Y(0)}" r="2.5" fill="var(--faint)"/>
    ${callout}
    <g class="axis">${yt.map((z) => `<text x="${m.l - 10}" y="${Y(z) + 3.5}" text-anchor="end">${z > 0 ? "+" : ""}${z}</text>`).join("")}
    ${xt.map((t) => `<text x="${X(t)}" y="${H - m.b + 18}" text-anchor="middle">${Math.round(t * 100)}%</text>`).join("")}
    <text x="${m.l}" y="${H - 6}">${width >= 560 ? "Information fraction (matured B leads ÷ planned n_B,max)" : "Information fraction"}</text>
    <text x="${m.l - 10}" y="${m.t - 6}" text-anchor="end">z</text></g></svg>`;
}

// ------------------------------------------------------------------------------------ waveform
export async function decodePeaks(url, n = 1600) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`recording ${r.status}`);
  const buf = await r.arrayBuffer();
  const audio = await new OfflineAudioContext(1, 1, 44100).decodeAudioData(buf);
  const data = audio.getChannelData(0), step = Math.max(1, Math.floor(data.length / n));
  const peaks = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let s = 0, mx = 0;
    for (let j = i * step, e = Math.min(data.length, j + step); j < e; j++) { const v = Math.abs(data[j]); s += v * v; if (v > mx) mx = v; }
    peaks[i] = 0.35 * mx + 0.65 * Math.sqrt(s / step);
  }
  const top = Float32Array.from(peaks).sort()[Math.floor(n * 0.985)] || 1e-6;  // a few loud clicks must not flatten the rest
  for (let i = 0; i < n; i++) peaks[i] = Math.min(1, Math.pow(peaks[i] / top, 0.9));
  return { peaks, duration: audio.duration };
}

export function drawWave(canvas, peaks, progress = 0, hover = null) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  if (!w || !h) return;
  if (canvas.width !== Math.round(w * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
  const g = canvas.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  const css = getComputedStyle(canvas);
  const played = css.getPropertyValue("--accent").trim(), rest = css.getPropertyValue("--muted").trim(), ink = css.getPropertyValue("--ink").trim();
  const bw = 2, gap = 1.2, n = Math.floor(w / (bw + gap));
  for (let i = 0; i < n; i++) {
    const a = Math.floor((i / n) * peaks.length), b = Math.max(a + 1, Math.floor(((i + 1) / n) * peaks.length));
    let mx = 0;
    for (let j = a; j < b; j++) if (peaks[j] > mx) mx = peaks[j];
    const bh = Math.max(1.5, mx * (h - 6));
    const x = i * (bw + gap);
    g.globalAlpha = (i + 0.5) / n <= progress ? 1 : hover != null && x <= hover ? 0.95 : 0.62;
    g.fillStyle = (i + 0.5) / n <= progress ? played : rest;
    g.beginPath();
    g.roundRect ? g.roundRect(x, (h - bh) / 2, bw, bh, 1) : g.rect(x, (h - bh) / 2, bw, bh);
    g.fill();
  }
  g.globalAlpha = 1;
  g.fillStyle = ink;
  g.fillRect(Math.round(progress * w) - 0.5, 0, 1.5, h);
}

// ------------------------------------------------------------------------------- prompt lineage
export function lineage(items, selected) {
  const by = Object.fromEntries(items.map((p) => [p.version, p]));
  const depth = (p) => (p.parent && by[p.parent] ? depth(by[p.parent]) + 1 : 0);
  const cols = {};
  const sorted = [...items].sort((a, b) => String(a.created_ts).localeCompare(String(b.created_ts)));
  const pos = {};
  for (const p of sorted) {
    const d = depth(p);
    cols[d] = (cols[d] || 0) + 1;
    pos[p.version] = { x: 24 + d * 230, y: 24 + (cols[d] - 1) * 116 };
  }
  const W = Math.max(...Object.values(pos).map((p) => p.x)) + 200, H = Math.max(...Object.values(pos).map((p) => p.y)) + 110;
  const NW = 168, NH = 86;
  const edges = sorted.filter((p) => p.parent && pos[p.parent]).map((p) => {
    const a = pos[p.parent], b = pos[p.version];
    const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2, mx = (x1 + x2) / 2;
    return `<path class="edge" d="M${x1} ${y1}C${mx} ${y1} ${mx} ${y2} ${x2} ${y2}"/><circle cx="${x2}" cy="${y2}" r="2.5" fill="var(--hairline-2)"/>`;
  }).join("");
  const nodes = sorted.map((p) => {
    const { x, y } = pos[p.version];
    return `<g class="node ${esc(p.status)} ${p.version === selected ? "sel" : ""}" data-v="${esc(p.version)}" tabindex="0" role="button" aria-label="Prompt ${esc(p.version)}, ${esc(p.status)}">
      <rect x="${x}" y="${y}" width="${NW}" height="${NH}" rx="12"/>
      <text class="v" x="${x + 16}" y="${y + 40}">${esc(p.version)}</text>
      <text class="s" x="${x + NW - 14}" y="${y + 22}" text-anchor="end">${esc(p.status)}</text>
      <text class="m" x="${x + 16}" y="${y + 64}">${esc(day(p.created_ts))}${p.fix_id ? ` · ${esc(p.fix_id)}` : ""}</text></g>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" style="max-width:${W}px" role="group" aria-label="Prompt lineage">${edges}${nodes}</svg>`;
}

// ------------------------------------------------------------------------------- confusion matrix
const CLS = [["pass", "Pass"], ["non_fatal", "Non-fatal"], ["fatal", "Fatal"], ["na", "N/A"]];
export function confusion(m) {
  const max = Math.max(1, ...CLS.flatMap(([a]) => CLS.map(([b]) => m?.[a]?.[b] || 0)));
  return html`<table class="cm" aria-label="Confusion matrix: human grade by row, judge grade by column">
    <thead><tr><th></th><th colspan="4">Judge</th></tr><tr><th class="side">Human ↓</th>${CLS.map(([, l]) => html`<th>${l}</th>`)}</tr></thead>
    <tbody>${CLS.map(([a, la]) => html`<tr><th class="side">${la}</th>${CLS.map(([b]) => {
      const v = m?.[a]?.[b] || 0, k = v / max;
      return html`<td class="${a === b ? "diag" : ""}" style="background:color-mix(in srgb, var(--accent) ${raw(Math.round(k * 85))}%, var(--ink-soft));${raw(k > 0.5 ? "color:#fff" : "")}">${int(v)}</td>`;
    })}</tr>`)}</tbody></table>`;
}
