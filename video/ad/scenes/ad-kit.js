// The ad's shared helpers, on window.K (load after runtime.js; pages link ad.css). All pure functions of what they
// are given, so render(t) stays deterministic:
//   K.stage()            builds the layers once (bg, #world, grain, vignette) and returns #world, where a scene draws
//   K.frame(t)           call first in render(t): moves the grain, fades #world in over 0.3 s and out over the last
//                        0.4 s of AD.dur (every scene starts and ends on the bare stage, so hard cuts are seamless)
//   K.canvas(parent)     a 1920x1080 2D canvas in parent (default #world)
//   K.dots(ctx, ...)     many points of light, fast (squares when small, discs when large: depth-of-field bokeh)
//   K.bloom(ctx, k)      a soft glow of the canvas added onto itself (a cheap blur pyramid; no CSS filter)
//   K.textPts(...)       where a line of text's pixels are, for particles that form or dissolve into type
//   K.ringPts / ringPath the platform's loop ring (cards/card.js, src/abtest/ext/web/viz.js)
//   K.chars(el), K.rise(el, k)  per-letter reveals: rise from blur, eased by the caller
(() => {
  const W = 1920, H = 1080, TAU = Math.PI * 2;
  const wrap = (a) => ((a + Math.PI) % TAU + TAU) % TAU - Math.PI;
  const smooth = (a, b, x) => { const k = clamp((x - a) / (b - a)); return k * k * (3 - 2 * k); };
  const C = { ink: "#EDEAE2", teal: "#3FB8A9", tealHi: "#7FE0D3", amber: "#D9912B", amberHi: "#F0B455", harm: "#C4553D", stage: "#0B0B0C" };
  const rgb = (c) => (c[0] === "#" ? [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)) : c.match(/[\d.]+/g).slice(0, 3).map(Number));
  const mix = (a, b, k) => { const x = rgb(a), y = rgb(b); return `rgb(${x.map((v, i) => Math.round(lerp(v, y[i], k))).join(",")})`; };

  let world = null, grain = null;
  function stage() {
    if (world) return world;
    const div = (cls, id) => { const d = document.createElement("div"); if (cls) d.className = cls; if (id) d.id = id; return d; };
    world = document.getElementById("world") || div(null, "world");
    document.body.prepend(div("ad-bg"), world);
    grain = div("ad-grain");
    document.body.append(grain, div("ad-vignette"));
    return world;
  }
  const edge = (t, dur = window.AD?.dur ?? 10, a = 0.3, b = 0.4) => Math.min(prog(t, 0, a, E.inOutCubic), 1 - prog(t, dur - b, b, E.inOutCubic));
  function frame(t, dur) {
    stage();
    const f = Math.floor(t * 24);  // film grain at 24 fps
    grain.style.transform = `translate(${Math.round(hash(f) * 256)}px, ${Math.round(hash(f + 0.37) * 256)}px)`;
    const k = edge(t, dur);
    world.style.opacity = k.toFixed(4);
    return k;
  }
  function canvas(parent) {
    const c = document.createElement("canvas");
    c.width = W; c.height = H; c.className = "ad-c";
    (parent || stage()).append(c);
    return c.getContext("2d");
  }

  /** Points [from, to) in one colour: X, Y centres, S sizes (px), A alphas. Large points are soft-edged discs. */
  function dots(ctx, X, Y, S, A, from, to, style) {
    ctx.fillStyle = style;
    for (let i = from; i < to; i++) {
      const a = A[i], s = S[i], x = X[i], y = Y[i];
      if (a < 0.004 || x < -s || y < -s || x > W + s || y > H + s) continue;
      ctx.globalAlpha = a > 1 ? 1 : a;
      if (s <= 3) ctx.fillRect(x - s / 2, y - s / 2, s, s);
      else { ctx.beginPath(); ctx.arc(x, y, s / 2, 0, TAU); ctx.fill(); }
    }
    ctx.globalAlpha = 1;
  }

  // a 2x box pyramid (each halving averages 2x2 exactly), added back at 1/4 and 1/8 scale
  let pyr = null;
  function bloom(ctx, k = 0.6, wide = 0.5) {
    if (!pyr) pyr = [2, 4, 8].map((d) => { const c = document.createElement("canvas"); c.width = W / d; c.height = H / d; return c; });
    let src = ctx.canvas;
    for (const c of pyr) { const g = c.getContext("2d"); g.clearRect(0, 0, c.width, c.height); g.drawImage(src, 0, 0, c.width, c.height); src = c; }
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.globalCompositeOperation = "lighter";
    ctx.globalAlpha = k; ctx.drawImage(pyr[1], 0, 0, W, H);
    ctx.globalAlpha = k * wide; ctx.drawImage(pyr[2], 0, 0, W, H);
    ctx.restore();
  }

  /** Pixel positions of text drawn at (x, y) in font: a Float32Array [x0, y0, x1, y1, ...], one per step px. */
  function textPts(text, font, x, y, { align = "center", baseline = "alphabetic", step = 2, seed = 7, spacing = "0px" } = {}) {
    const c = document.createElement("canvas"); c.width = W; c.height = H;
    const g = c.getContext("2d", { willReadFrequently: true });
    g.font = font; g.textAlign = align; g.textBaseline = baseline; g.letterSpacing = spacing; g.fillStyle = "#fff";
    g.fillText(text, x, y);
    const d = g.getImageData(0, 0, W, H).data, r = rng(seed), out = [];
    for (let py = 0; py < H; py += step) for (let px = 0; px < W; px += step) {
      if (d[(py * W + px) * 4 + 3] > 110) out.push(px + (r() - 0.5) * step, py + (r() - 0.5) * step);
    }
    return Float32Array.from(out);
  }

  /** The loop ring's outline as points [[x, y], ...] (closed: the last is the first). */
  function ringPts(cx, cy, R, phase, focus = -Math.PI / 2, amp = 1, steps = 360) {
    const pts = [];
    for (let i = 0; i <= steps; i++) {
      const a = (i / steps) * TAU - Math.PI / 2;
      const env = 0.16 + 0.84 * Math.exp(-((wrap(a - focus) / 0.7) ** 2));
      const r = R + amp * env * 1.25 * (6.4 * Math.sin(9 * a + phase) + 3.6 * Math.sin(17 * a - 1.4 * phase)
        + 2.2 * Math.sin(31 * a + 2.3 * phase) + 1.3 * Math.sin(53 * a - 3.1 * phase));
      pts.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]);
    }
    return pts;
  }
  const ringPath = (...a) => ringPts(...a).map(([x, y], i) => `${i ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join("") + "Z";

  /** Wraps every character of el (keeping nested <em> etc.) in a span.ch; returns the spans in reading order. */
  function chars(el) {
    const out = [];
    for (const n of [...el.childNodes]) {
      if (n.nodeType === 3) {
        const frag = document.createDocumentFragment();
        for (const c of n.textContent) { const s = document.createElement("span"); s.className = "ch"; s.textContent = c; frag.append(s); out.push(s); }
        n.replaceWith(frag);
      } else if (n.nodeType === 1) out.push(...chars(n));
    }
    return out;
  }
  /** Style an element at reveal progress k (0 hidden .. 1 settled): rises y px out of a blur. */
  function rise(el, k, { y = 26, blur = 12, x = 0, s = 0 } = {}) {
    const st = el.style;
    st.opacity = k.toFixed(4);
    st.transform = k >= 1 ? "none" : `translate(${((1 - k) * x).toFixed(2)}px, ${((1 - k) * y).toFixed(2)}px)${s ? ` scale(${1 + (1 - k) * s})` : ""}`;
    st.filter = k > 0.001 && k < 0.999 && blur ? `blur(${((1 - k) * blur).toFixed(2)}px)` : "none";
  }
  const fmt = (n) => Math.round(n).toLocaleString("en-US");

  window.K = { W, H, TAU, C, wrap, smooth, mix, rgb, stage, edge, frame, canvas, dots, bloom, textPts, ringPts, ringPath, chars, rise, fmt };
})();
