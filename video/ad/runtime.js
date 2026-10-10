// The ad's clock (video/ad). A scene page loads this, then calls scene({ render, sfx }):
//   render(t)  draws the frame at t seconds into the scene. Everything derives from t: no CSS transitions,
//              animations, timers or performance.now(), so render.py can film any frame in any order. It may
//              return a promise (e.g. an image decoding); the frame is taken after it settles.
//   sfx        optional [[t, name, gain?], ...]: sound effects at seconds into the scene (names: see music.py).
// window.AD is this scene's entry of video/out/ad/timeline.json: {id, kind, clip, voice, say, start, dur, frames,
// lead, narr, pad, cues: {phrase: s}, caps: [[from, to, text]], beats: [...], head, chapter}. render.py injects it
// before the page loads; opened by hand (http://127.0.0.1:<port>/ad/scenes/<id>.html, served from video/) the page
// fetches it from ../../out/ad/timeline.json by ?id= (default: the file name), then plays in real time, looping.
// ?t=12.5 freezes at 12.5 s (for screenshots). cue("phrase", off) is when the narration says phrase: call it in
// init(AD) or render(t), never at the top level (AD is not loaded yet). init(AD) may be async (build the DOM once).
(() => {
  const P = new URLSearchParams(location.search);
  const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
  const lerp = (a, b, k) => a + (b - a) * k;
  const E = {
    linear: (x) => x,
    inCubic: (x) => x ** 3,
    outCubic: (x) => 1 - (1 - x) ** 3,
    inOutCubic: (x) => (x < 0.5 ? 4 * x ** 3 : 1 - (-2 * x + 2) ** 3 / 2),
    outQuint: (x) => 1 - (1 - x) ** 5,
    inOutQuint: (x) => (x < 0.5 ? 16 * x ** 5 : 1 - (-2 * x + 2) ** 5 / 2),
    outExpo: (x) => (x >= 1 ? 1 : 1 - 2 ** (-10 * x)),
    inOutExpo: (x) => (x <= 0 ? 0 : x >= 1 ? 1 : x < 0.5 ? 2 ** (20 * x - 10) / 2 : (2 - 2 ** (-20 * x + 10)) / 2),
    outBack: (x, s = 1.70158) => 1 + (s + 1) * (x - 1) ** 3 + s * (x - 1) ** 2,
  };
  /** Progress of t through [a, a + d], 0..1, eased. */
  const prog = (t, a, d, ease = E.outCubic) => ease(clamp((t - a) / d));
  /** In at a over d, out at b over d2: 0..1..0. */
  const life = (t, a, d, b = Infinity, d2 = d, ease = E.outCubic) => Math.min(prog(t, a, d, ease), 1 - prog(t, b, d2, E.inOutCubic));
  /** Deterministic noise: a seeded hash in [0, 1), and smooth 1D value noise. */
  const hash = (n) => { const x = Math.sin(n * 127.1 + 311.7) * 43758.5453123; return x - Math.floor(x); };
  const noise = (x, seed = 0) => { const i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f); return lerp(hash(i + seed * 57), hash(i + 1 + seed * 57), u); };
  /** Seeded PRNG (mulberry32) for building a scene once, at load. */
  const rng = (seed) => () => { seed |= 0; seed = (seed + 0x6D2B79F5) | 0; let r = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    r = (r + Math.imul(r ^ (r >>> 7), 61 | r)) ^ r; return ((r ^ (r >>> 14)) >>> 0) / 4294967296; };

  let AD = window.AD || null;
  function cue(phrase, off = 0) {
    const c = AD?.cues?.[phrase];
    if (c == null) {
      // not timed (a phrase the narration does not say, or a page opened before timeline.py ran): spread by its
      // position in the text, so a preview still plays in order
      const k = AD?.say ? AD.say.indexOf(phrase) : -1;
      if (k < 0) console.warn(`cue not in the narration: ${phrase}`);
      return (AD ? AD.lead + AD.narr * Math.max(0, k) / Math.max(1, AD.say.length) : 0) + off;
    }
    return c + off;
  }

  let spec = null, ready = false;
  const ok = new Promise((res) => (window.__resolveReady = res));
  async function boot() {
    if (!AD) {
      const id = P.get("id") || location.pathname.split("/").pop().replace(/\.html$/, "");
      try {
        const tl = await (await fetch("../../out/ad/timeline.json")).json();
        AD = tl.scenes.find((s) => s.id === id) || tl.scenes.find((s) => s.clip === id) || null;
      } catch { /* no timeline yet */ }
      AD ||= { id, dur: Number(P.get("d") || 12), lead: 0, narr: Number(P.get("d") || 12), say: "", cues: {}, caps: [], beats: [] };
      window.AD = AD;
    }
    await document.fonts.ready;
    if (spec.init) await spec.init(AD);
    ready = true;
    window.__resolveReady();
    if (window.__RENDERER) return;  // render.py drives __frame(t)
    const T = P.get("t");
    if (T != null) return void spec.render(Number(T));
    const t0 = performance.now();
    const tick = async (now) => { await spec.render(((now - t0) / 1000) % AD.dur); requestAnimationFrame(tick); };
    requestAnimationFrame(tick);
  }

  window.scene = (s) => { spec = s; (document.readyState === "loading" ? new Promise((r) => addEventListener("DOMContentLoaded", r)) : Promise.resolve()).then(boot); };
  window.__ready = () => ok;
  window.__frame = async (t) => { await ok; await spec.render(t); };
  window.__sfx = () => (typeof spec?.sfx === "function" ? spec.sfx(AD) : spec?.sfx || []);
  Object.assign(window, { clamp, lerp, E, prog, life, hash, noise, rng, cue });
})();
