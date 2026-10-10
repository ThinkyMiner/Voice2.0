// Helpers for ad-loop, ad-split, ad-outcome and ad-close (until ad-kit.js exists). Load after ../runtime.js, which
// gives clamp, lerp, E, prog, life, hash, noise, rng and cue. Everything here is a pure function of t.
const TAU = Math.PI * 2;
const RGB = { ink: "237,234,226", teal: "63,184,169", tealHi: "150,228,216", amber: "217,145,43", amberHi: "255,214,150", harm: "196,85,61" };
const wrapA = (a) => ((a + Math.PI) % TAU + TAU) % TAU - Math.PI;
const sine = (x) => (1 - Math.cos(Math.PI * clamp(x))) / 2;

/** The platform's loop ring as a radius at angle a (cards/card.js ringPath, viz.js): a circle carrying a few sines,
 *  strongest around `focus`. */
function ringR(a, R, phase, focus = -Math.PI / 2, amp = 1) {
  const env = 0.16 + 0.84 * Math.exp(-((wrapA(a - focus) / 0.7) ** 2));
  return R + amp * env * 1.25 * (6.4 * Math.sin(9 * a + phase) + 3.6 * Math.sin(17 * a - 1.4 * phase)
    + 2.2 * Math.sin(31 * a + 2.3 * phase) + 1.3 * Math.sin(53 * a - 3.1 * phase));
}

/** The 2D context of a 1920x1080 canvas. */
function ctx2d(id) { const c = document.getElementById(id); c.width = 1920; c.height = 1080; return c.getContext("2d"); }

/** Per frame: the grain jumps (24 fps, like film) and #fx builds in from the stage over 0.3 s and resolves back to
 *  it over the last 0.4 s, so every hard cut lands on the plain stage. */
function stage(t) {
  const g = document.querySelector(".grain"), f = Math.floor(t * 24);
  if (g) g.style.backgroundPosition = `${Math.floor(hash(f) * 220)}px ${Math.floor(hash(f + 0.37) * 220)}px`;
  const fx = document.getElementById("fx");
  if (fx) fx.style.opacity = life(t, 0, 0.3, AD.dur - 0.4, 0.4).toFixed(3);
}

/** An element's entrance at a and exit at b: rises dy px and sharpens from blur in, drifts up and softens out. */
function show(el, t, a, b = Infinity, { dy = 26, blur = 10, d = 1.0, d2 = 0.6, dx = 0 } = {}) {
  const i = prog(t, a, d, E.outQuint), o = prog(t, b, d2, E.inOutCubic), k = i * (1 - o);
  el.style.opacity = k.toFixed(3);
  el.style.visibility = k > 0.002 ? "visible" : "hidden";
  el.style.transform = `translate3d(${((1 - i) * dx).toFixed(1)}px, ${((1 - i) * dy - o * dy * 0.7).toFixed(1)}px, 0)`;
  const bl = (1 - i) * blur + o * blur * 0.6;
  el.style.filter = bl > 0.06 ? `blur(${bl.toFixed(2)}px)` : "none";
  return k;
}

/** Stroke a path with a soft glow: wide faint passes under a fine core, added to the light already there. */
function glow(g, path, rgb, a = 1, core = 1.6, wide = 1) {
  if (a <= 0.002) return;
  g.save();
  g.globalCompositeOperation = "lighter";
  g.lineCap = g.lineJoin = "round";
  for (const [w, k] of [[22 * wide, 0.03], [10 * wide, 0.06], [4.5, 0.15], [core, 0.95]]) {
    g.strokeStyle = `rgba(${rgb},${(k * a).toFixed(4)})`;
    g.lineWidth = w;
    g.stroke(path);
  }
  g.restore();
}

/** A soft round light: radial falloff, additive. */
function bloom(g, x, y, r, rgb, a = 1) {
  if (a <= 0.002) return;
  const gr = g.createRadialGradient(x, y, 0, x, y, r);
  gr.addColorStop(0, `rgba(${rgb},${a})`);
  gr.addColorStop(0.35, `rgba(${rgb},${a * 0.35})`);
  gr.addColorStop(1, `rgba(${rgb},0)`);
  g.save(); g.globalCompositeOperation = "lighter"; g.fillStyle = gr;
  g.fillRect(x - r, y - r, 2 * r, 2 * r); g.restore();
}

/** A path through points [[x, y], ...]. */
function poly(pts, close = false) {
  const p = new Path2D();
  pts.forEach(([x, y], i) => (i ? p.lineTo(x, y) : p.moveTo(x, y)));
  if (close) p.closePath();
  return p;
}

/** Dust in the air: a few slow motes and soft out-of-focus discs, with parallax from (px, py). */
function makeDust(seed, n = 80) {
  const r = rng(seed);
  return Array.from({ length: n }, (_, i) => ({ x: r() * 2200 - 140, y: r() * 1300 - 110, z: 0.3 + r() * 1.7, s: r(),
    big: i < 7, hue: r() < 0.5 ? RGB.teal : RGB.amber }));
}
function drawDust(g, dust, t, px = 0, py = 0, a = 1) {
  for (const m of dust) {
    const x = m.x + (px + t * 6 + 30 * Math.sin(t * 0.13 + m.s * 9)) / m.z, y = m.y + (py - t * 3 + 20 * Math.cos(t * 0.11 + m.s * 7)) / m.z;
    if (m.big) { bloom(g, x, y, 60 + 90 * m.s, m.hue, 0.022 * a); continue; }
    g.fillStyle = `rgba(${RGB.ink},${((0.05 + 0.13 * m.s) * a * (0.7 + 0.3 * noise(t * 0.7, m.s * 40))).toFixed(3)})`;
    const s = 0.8 + 1.4 / m.z;
    g.fillRect(x, y, s, s);
  }
}
