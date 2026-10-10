// Injected into every page video/capture.py films (before the page's own scripts): the blur list, burned-in
// captions, a soft accent spotlight, a fake cursor, a paper curtain for page changes, a terminal card and a badge
// that labels what is on screen (e.g. an offline rehearsal).
// capture.py prepends `window.VL_CFG = {blur: "<selectors>"}`. Everything lives in one manual popover so it
// stays above the page's own dialogs (vl.raise() re-shows it after a dialog opens).
(() => {
  const CFG = window.VL_CFG || {};
  const CSS = `
    ${CFG.blur ? `${CFG.blur} { filter: blur(9px) !important; }` : ""}
    html { scroll-behavior: auto !important; scrollbar-width: none; }
    #toasts { transform: translate(-50%, -120px) !important; }  /* above the captions */
    ::-webkit-scrollbar { display: none; }
    #vl-top { position: fixed; inset: 0; width: 100vw; height: 100vh; max-width: none; max-height: none; margin: 0; padding: 0;
      border: 0; background: transparent; overflow: visible; pointer-events: none; color: inherit; }
    #vl-top * { box-sizing: border-box; }
    .vl-curtain { position: fixed; inset: 0; background: #F5F2EA; opacity: 0; transition: opacity 300ms ease; }
    .vl-curtain.on { opacity: 1; }
    .vl-spot { position: fixed; left: 0; top: 0; border-radius: 14px; border: 2.5px solid #0F6B66; opacity: 0;
      box-shadow: 0 0 0 7px rgba(15, 107, 102, .12), 0 0 34px 6px rgba(15, 107, 102, .16), 0 0 0 200vmax rgba(22, 20, 15, .075);
      transition: opacity 450ms ease; }
    .vl-spot.on { opacity: 1; }
    .vl-cursor { position: fixed; left: 0; top: 0; width: 26px; height: 26px; opacity: 0; transition: opacity 300ms ease;
      filter: drop-shadow(0 3px 5px rgba(0, 0, 0, .28)); }
    .vl-cursor.on { opacity: 1; }
    .vl-tap { position: fixed; width: 44px; height: 44px; margin: -22px 0 0 -22px; border-radius: 50%; border: 2px solid #0F6B66;
      animation: vl-tap 600ms ease-out forwards; }
    @keyframes vl-tap { from { transform: scale(.3); opacity: .9; } to { transform: scale(1.5); opacity: 0; } }
    .vl-cap { position: fixed; left: 50%; bottom: calc(var(--u) * 46px); transform: translateX(-50%); max-width: calc(var(--u) * 1480px);
      padding: calc(var(--u) * 14px) calc(var(--u) * 30px) calc(var(--u) * 16px); border-radius: calc(var(--u) * 14px);
      background: rgba(18, 17, 14, .74); backdrop-filter: blur(6px); color: #fff; text-align: center; text-wrap: balance;
      font: 500 calc(var(--u) * 34px)/1.32 "Geist", ui-sans-serif, system-ui, sans-serif; letter-spacing: -.005em;
      opacity: 0; transition: opacity 220ms ease; }
    .vl-cap.on { opacity: 1; }
    .vl-scrim { position: fixed; inset: 0; background: rgba(245, 242, 234, .62); backdrop-filter: blur(7px); opacity: 0; transition: opacity 500ms ease; }
    .vl-scrim.on { opacity: 1; }
    .vl-term { position: fixed; left: 50%; top: 45%; width: min(1300px, 92vw); transform: translate(-50%, -50%) translateY(16px); opacity: 0;
      transition: opacity 500ms ease, transform 700ms cubic-bezier(.2, .7, .2, 1); border-radius: 16px; overflow: hidden;
      background: #16140F; color: #EDEAE2; box-shadow: 0 2px 6px rgba(22, 20, 15, .1), 0 40px 90px -30px rgba(22, 20, 15, .55); }
    .vl-term.on { opacity: 1; transform: translate(-50%, -50%); }
    .vl-term .bar { display: flex; align-items: center; gap: 8px; padding: 13px 16px; background: #22201A; font: 500 12px "Geist Mono", monospace; color: #9C978C; }
    .vl-term .bar i { width: 11px; height: 11px; border-radius: 50%; background: #3A372F; }
    .vl-term .bar span { margin-left: 10px; letter-spacing: .06em; }
    .vl-term pre { margin: 0; padding: 22px 26px 26px; font: 400 17px/1.75 "Geist Mono", ui-monospace, Menlo, monospace; white-space: pre-wrap; min-height: 150px; }
    .vl-term pre b { color: #3FB8A9; font-weight: 500; }
    .vl-term pre .caret { display: inline-block; width: .6em; height: 1.1em; vertical-align: -.2em; background: #F0B455; animation: vl-blink 1s steps(1) infinite; }
    @keyframes vl-blink { 50% { opacity: 0; } }
    .vl-badge { position: fixed; right: 24px; top: 84px; padding: 9px 16px 10px; border-radius: 999px; background: #F3D9AE; color: #5E3D08;
      border: 1px solid rgba(149, 96, 15, .35); font: 600 14px/1 "Geist", ui-sans-serif, system-ui, sans-serif; letter-spacing: .01em;
      box-shadow: 0 6px 18px -8px rgba(22, 20, 15, .35); }
    .vl-badge:empty { display: none; }
  `;
  const addCss = () => {
    if (document.getElementById("vl-css")) return;
    const s = document.createElement("style");
    s.id = "vl-css";
    s.textContent = CSS;
    (document.head || document.documentElement).appendChild(s);
  };
  if (document.documentElement) addCss();
  else new MutationObserver((_, o) => { if (document.documentElement) { addCss(); o.disconnect(); } }).observe(document, { childList: true });

  const $ = (s) => document.querySelector(s);
  const ease = (k) => (k < 0.5 ? 4 * k * k * k : 1 - (-2 * k + 2) ** 3 / 2);
  let top = null, curtain, spot, cursor, cap, scrim, term;
  function host() {
    if (top) return top;
    addCss();
    top = document.createElement("div");
    top.id = "vl-top";
    top.setAttribute("popover", "manual");
    top.innerHTML = '<div class="vl-spot"></div>'
      + '<svg class="vl-cursor" viewBox="0 0 26 26"><path d="M5 3l15.5 9.2-6.6 1.4 3.9 7.3-3.1 1.6-3.9-7.3L5.6 20z" fill="#fff" stroke="#16140F" stroke-width="1.6" stroke-linejoin="round"/></svg>'
      + '<div class="vl-curtain"></div><div class="vl-scrim"></div><div class="vl-term"></div><div class="vl-cap"></div><div class="vl-badge"></div>';
    document.body.append(top);
    top.style.setProperty("--u", String(1 / devicePixelRatio));
    [curtain, scrim, term, spot, cursor, cap] = ["curtain", "scrim", "term", "spot", "cursor", "cap"].map((k) => top.querySelector(`.vl-${k}`));
    top.showPopover();
    requestAnimationFrame(loop);
    return top;
  }

  // ---- the spotlight, tracked in document coordinates so a scroll never makes it lag
  let spotSel = null, spotPad = 14, cur = null, from = null, to = null, t0 = 0;
  const rectOf = (sel, pad) => {
    const rs = [...document.querySelectorAll(sel)].map((e) => e.getBoundingClientRect()).filter((r) => r.width && r.height);
    if (!rs.length) return null;
    const l = Math.min(...rs.map((r) => r.left)), t = Math.min(...rs.map((r) => r.top));
    const r = Math.max(...rs.map((r) => r.right)), b = Math.max(...rs.map((r) => r.bottom));
    return { x: l - pad + scrollX, y: t - pad + scrollY, w: r - l + 2 * pad, h: b - t + 2 * pad };
  };
  function loop(now) {
    if (spotSel) {
      const want = rectOf(spotSel, spotPad);
      if (want) {
        if (!cur) cur = want;
        const k = Math.min(1, (now - t0) / 650), e = ease(k);
        const a = from || want;
        cur = { x: a.x + (want.x - a.x) * e, y: a.y + (want.y - a.y) * e, w: a.w + (want.w - a.w) * e, h: a.h + (want.h - a.h) * e };
        spot.style.transform = `translate(${cur.x - scrollX}px, ${cur.y - scrollY}px)`;
        spot.style.width = `${cur.w}px`;
        spot.style.height = `${cur.h}px`;
        spot.classList.add("on");
      }
    }
    requestAnimationFrame(loop);
  }

  // ---- the cursor
  let cx = null, cy = null, hideT = 0;
  function moveCursor(sel, ms = 650) {
    host();
    clearTimeout(hideT);
    const el = $(sel);
    if (!el) return 0;
    const r = el.getBoundingClientRect(), tx = r.left + Math.min(r.width / 2, 60), ty = r.top + r.height / 2;
    if (cx == null) { cx = tx + 180; cy = ty + 140; }
    const sx = cx, sy = cy, s0 = performance.now();
    cursor.classList.add("on");
    const step = (now) => {
      const k = Math.min(1, (now - s0) / ms), e = ease(k);
      cx = sx + (tx - sx) * e; cy = sy + (ty - sy) * e;
      cursor.style.transform = `translate(${cx - 5}px, ${cy - 3}px)`;
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    return ms;
  }

  // ---- captions: [[from, to, text], ...] in seconds from start()
  function captions(list) {
    host();
    if (!list.length) return;  // a clean take (VIDEO_CLEAN=1)
    const s0 = performance.now();
    let shown = -1;
    const tick = () => {
      const t = (performance.now() - s0) / 1000;
      const i = list.findIndex(([a, b]) => t >= a && t < b);
      if (i !== shown) {
        shown = i;
        if (i < 0) cap.classList.remove("on");
        else { cap.textContent = list[i][2]; cap.classList.add("on"); }
      }
      if (t < list[list.length - 1][1] + 1) setTimeout(tick, 30);
      else cap.classList.remove("on");
    };
    tick();
  }

  function scrollToY(y, ms) {
    const max = document.documentElement.scrollHeight - innerHeight;
    y = Math.max(0, Math.min(max, y));
    const y0 = scrollY, s0 = performance.now();
    if (!ms) return scrollTo(0, y);
    const step = (now) => {
      const k = Math.min(1, (now - s0) / ms);
      scrollTo(0, y0 + (y - y0) * ease(k));
      if (k < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
  const headH = () => Math.max(0, ...[...document.querySelectorAll(".topbar, .demo-banner")].map((e) => e.getBoundingClientRect().bottom));

  window.vl = {
    start: (caps) => captions(caps),
    spot: (sel, pad = 14) => {
      host();
      if (!document.querySelector(sel)) return false;
      from = cur; to = sel; spotSel = sel; spotPad = pad; t0 = performance.now();
      return true;
    },
    unspot: () => { spotSel = null; cur = null; spot?.classList.remove("on"); },
    scroll: (sel, block = "start", ms = 1100) => {  // block: start (under the header), center, or end (above the captions)
      const u = rectOf(sel, 0);
      if (!u) return false;
      const h = headH(), top = u.y - scrollY, bottom = top + u.h;
      const y = block === "center" ? scrollY + top + u.h / 2 - (innerHeight + h) / 2
        : block === "end" ? scrollY + bottom - innerHeight + 150 : scrollY + top - h - 28;
      scrollToY(y, ms);
      return true;
    },
    scrollY: (y, ms = 1100) => scrollToY(y, ms),
    cursor: moveCursor,
    tap: () => {
      const t = document.createElement("div");
      t.className = "vl-tap";
      t.style.left = `${cx}px`; t.style.top = `${cy}px`;
      top.append(t);
      setTimeout(() => t.remove(), 700);
      hideT = setTimeout(() => cursor.classList.remove("on"), 1600);  // a cursor that stopped working fades away
    },
    curtain: (on) => { host(); curtain.classList.toggle("on", on); },
    badge: (text) => { host(); top.querySelector(".vl-badge").textContent = text; },
    raise: () => { host(); top.hidePopover(); top.showPopover(); },
    stickyBanner: () => {  // keep "Demo data: invented calls" on screen while the page scrolls
      const b = document.getElementById("demo-banner"), bar = document.querySelector(".topbar");
      if (!b || b.hidden || !bar) return;
      Object.assign(b.style, { position: "sticky", top: "0", zIndex: "25", background: "color-mix(in srgb, var(--accent-2) 22%, var(--bg))" });
      bar.style.top = `${b.offsetHeight}px`;
    },
    terminal: (text, ms = 2600) => {
      host();
      scrim.classList.add("on");
      term.innerHTML = '<div class="bar"><i></i><i></i><i></i><span>vani lab · terminal</span></div><pre></pre>';
      requestAnimationFrame(() => term.classList.add("on"));
      const pre = term.querySelector("pre"), lines = text.replace(/\s+$/, "").split("\n");
      const cmd = lines[0], rest = lines.slice(1), typeMs = Math.min(ms * 0.45, cmd.length * 42);
      const esc = (s) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
      const s0 = performance.now() + 450;
      const tick = (now) => {
        const t = now - s0;
        const typed = cmd.slice(0, Math.max(0, Math.floor((t / typeMs) * cmd.length)));
        const shownLines = t < typeMs + 250 ? 0 : Math.min(rest.length, 1 + Math.floor((t - typeMs - 250) / 160));
        pre.innerHTML = `<b>${esc(typed.slice(0, 2))}</b>${esc(typed.slice(2))}${shownLines ? "\n" + rest.slice(0, shownLines).map(esc).join("\n") : ""}<span class="caret"></span>`;
        if (shownLines < rest.length) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    },
  };
})();
