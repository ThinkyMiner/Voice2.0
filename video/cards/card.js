// Card timing. Every [data-step] element appears in document order. video/capture.py calls play(times) with
// one time in seconds per step, read off the narration (each step's data-cue is a phrase it says); opened by
// hand, a card plays at once, its steps spread over ?d= seconds. A card can hook window.onStep(i, el).
(() => {
const P = new URLSearchParams(location.search);
const D = Number(P.get("d") || 8);
const TAU = Math.PI * 2;
const wrap = (a) => ((a + Math.PI) % TAU + TAU) % TAU - Math.PI;

/** The platform's loop ring: a circle whose radius carries a few sines (src/abtest/ext/web/viz.js ringPath). */
function ringPath(cx, cy, R, phase, focus = -Math.PI / 2, amp = 1, steps = 360) {
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

// the brand mark wherever a card has <svg class="logo">
for (const s of document.querySelectorAll("svg.logo")) {
  s.setAttribute("viewBox", "0 0 32 32");
  s.innerHTML = `<path fill="none" stroke="var(--accent)" stroke-width="1.6" d="${ringPath(16, 16, 11.2, 1.1, -Math.PI / 2, 0.16, 120)}"/><circle cx="16" cy="4.2" r="2.6" fill="var(--accent-2)"/>`;
}

let t0 = null;
const now = () => (t0 == null ? 0 : (performance.now() - t0) / 1000);

window.play = (times) => {
  const steps = [...document.querySelectorAll("[data-step]")];
  times = times || steps.map((_, i) => (i / Math.max(1, steps.length)) * D * 0.75);
  t0 = performance.now();
  steps.forEach((el, i) => setTimeout(() => {
    el.classList.add("on");
    window.onStep?.(i, el, steps);
  }, Math.max(0, times[i] ?? 0) * 1000));
  window.onPlay?.();
};
window.cues = () => [...document.querySelectorAll("[data-step]")].map((el) => el.dataset.cue || null);
window.cardNow = now;
window.ringPath = ringPath;
if (P.get("auto") !== "0") addEventListener("load", () => document.fonts.ready.then(() => window.play()));
})();
