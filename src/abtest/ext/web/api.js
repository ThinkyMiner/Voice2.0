// The platform API (docs/loop_api.md). When /api/overview is unreachable (a plain static server,
// or no server) the app runs on ./demo.json instead: same shapes, invented calls, real engine runs.
// Demo writes stay in this tab, except custom rubrics, which persist in localStorage.

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

export const state = { demo: false, overview: null };
let demo = null;

export async function boot() {
  try {
    const r = await fetch("/api/overview", { headers: { accept: "application/json" } });
    if (!r.ok) throw new Error(String(r.status));
    state.overview = await r.json();
  } catch {
    const r = await fetch("demo.json");
    if (!r.ok) throw new ApiError(r.status, "The platform API is not running and demo.json is missing.");
    demo = await r.json();
    state.demo = true;
    state.overview = demo["/api/overview"];
  }
  return state;
}

async function fail(r) {
  let msg = `${r.status} ${r.statusText}`;
  try {
    const d = await r.json();
    msg = typeof d.detail === "string" ? d.detail : d.detail ? JSON.stringify(d.detail) : msg;
  } catch { /* not JSON */ }
  return new ApiError(r.status, msg);
}

export async function get(path) {
  if (demo) return structuredClone(demoGet(path));
  const r = await fetch(path, { headers: { accept: "application/json" } });
  if (!r.ok) throw await fail(r);
  return r.json();
}

export async function text(path) {
  if (demo) return demoText(path);
  const r = await fetch(path);
  if (!r.ok) throw await fail(r);
  return r.text();
}

export async function send(method, path, body) {
  let out;
  if (demo) out = structuredClone(await demoSend(method, path, body));
  else {
    const r = await fetch(path, {
      method, headers: { "content-type": "application/json", accept: "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!r.ok) throw await fail(r);
    out = r.status === 204 ? null : await r.json().catch(() => null);
  }
  window.dispatchEvent(new CustomEvent("vani:changed", { detail: { method, path } }));
  return out;
}

/** Prompt B's text is Prompt A with the fix's edits applied (mirrors the fixer's patching). */
export function applyEdits(text, edits) {
  for (const e of edits || []) {
    text = e.mode === "append" ? text + (text.endsWith("\n") ? "" : "\n") + (e.text ?? "") + "\n"  // improve._apply
      : text.replace(e.find ?? "", () => e.replace ?? "");
  }
  return text;
}

// ------------------------------------------------------------------------------- demo mode
const RUBRIC_KEY = "vani-demo-rubrics";
const notFound = (what) => new ApiError(404, `${what} is not in the demo data.`);
const productionVersion = () => demo["/api/overview"].production.version;

function stored() {
  try { return JSON.parse(localStorage.getItem(RUBRIC_KEY)) || {}; } catch { return {}; }
}
function store(all) {
  try { localStorage.setItem(RUBRIC_KEY, JSON.stringify(all)); } catch { /* private mode: this tab only */ }
}

function demoRubrics() {
  const mine = stored();
  const base = demo["/api/rubrics"].items.filter((r) => !mine[r.id]);
  return [...base, ...Object.values(mine).map((versions) => versions[versions.length - 1])];
}

function demoGet(path) {
  const [p, q = ""] = path.split("?");
  const params = new URLSearchParams(q);
  if (p === "/api/calls") return demoCalls(params);
  if (p === "/api/rubrics") return { items: demoRubrics() };
  if (p === "/api/audit" || p === "/api/issues") {
    const v = params.get("version") || productionVersion();
    const hit = demo[`${p}?version=${v}`];
    if (!hit) throw notFound(`The ${p.slice(5)} for ${v}`);
    return hit;
  }
  const versions = p.match(/^\/api\/rubrics\/([^/]+)\/versions$/);
  if (versions && stored()[versions[1]]) return { items: stored()[versions[1]] };
  if (p in demo) return demo[p];
  throw notFound(p);
}

function demoCalls(params) {
  const q = (params.get("q") || "").toLowerCase();
  const items = demo["/api/calls"].items.filter((c) =>
    (!params.get("source") || c.source === params.get("source")) &&
    (!params.get("overall") || c.overall === params.get("overall")) &&
    (!params.get("flag") || c.risk_flags.includes(params.get("flag"))) &&
    (!q || `${c.call_id} ${c.summary} ${c.disposition}`.toLowerCase().includes(q)));
  return { total: items.length, items: items.slice(0, Number(params.get("limit") || 200)) };
}

function demoText(path) {
  const m = path.match(/^\/api\/prompts\/([^/]+)\/text$/);
  if (!m) throw notFound(path);
  return promptText(m[1]);
}

function promptText(v) {
  const t = demo[`/api/prompts/${v}/text`];
  if (typeof t === "string") return t;
  const pv = demo["/api/prompts"].items.find((x) => x.version === v);
  const fix = pv?.fix_id && demo[`/api/fixes/${pv.fix_id}`]?.fix;
  if (!fix) throw notFound(`The text of ${v}`);
  return applyEdits(promptText(fix.base_version), fix.edits);
}

const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));
const now = () => new Date().toISOString().slice(0, 19);
const clock = () => new Date().toTimeString().slice(0, 8);

async function demoSend(method, path, body) {
  await sleep(220);  // let the UI show its pending state
  const exp = path.match(/^\/api\/experiments\/([^/]+)\/decision$/);
  if (method === "POST" && exp) return demoDecide(decodeURIComponent(exp[1]), body);
  if (method === "POST" && path === "/api/loop/advance") return demoAdvance();
  if (method === "POST" && path === "/api/rubrics/test") return demoTest(body);
  if (path.startsWith("/api/rubrics")) return demoSaveRubric(method, path, body);
  throw new ApiError(405, "Demo mode cannot do that.");
}

function demoDecide(id, { actor, approved }) {
  const d = demo[`/api/experiments/${id}`];
  if (!d) throw notFound(id);
  if (d.state !== "PENDING_APPROVAL") throw new ApiError(409, `${id} is ${d.state}, not PENDING_APPROVAL.`);
  if (!actor) throw new ApiError(422, "An approver name is required.");
  const to = approved ? "PROMOTED" : "REJECTED";
  const last = d.decisions[d.decisions.length - 1] || {};
  const impact = d.impact?.per_week;
  d.decisions.push({
    ...Object.fromEntries(Object.keys(last).map((k) => [k, null])),
    ts_wall: now(), ts_sim: last.ts_sim, experiment_id: id, config_hash: last.config_hash,
    prompt_hash_A: last.prompt_hash_A, prompt_hash_B: last.prompt_hash_B, kind: "state_change",
    decision: to, actor, from_state: "PENDING_APPROVAL", to_state: to,
    reason: approved
      ? `Approved by ${actor}: B now gets 100% of traffic${impact ? `, worth ≈ +${impact} ${d.impact.unit} a week` : ""}.`
      : `Rejected by ${actor}: all traffic returns to A.`,
  });
  d.state = to;
  d.allocation = approved ? { A: 0, B: 1 } : { A: 1, B: 0 };
  const item = demo["/api/experiments"].items.find((x) => x.experiment_id === id);
  if (item) Object.assign(item, { state: to, allocation: d.allocation });
  const loop = demo["/api/loop"];
  if (loop.cycle?.experiment_id === id) {
    loop.next_action = approved ? `Roll out ${loop.cycle.candidate_version}: it becomes production and cycle ${loop.cycle.n + 1} starts on it.`
      : `Close cycle ${loop.cycle.n}: ${loop.cycle.candidate_version} is rejected and ${loop.cycle.production_version} stays in production.`;
  }
  return d.decisions[d.decisions.length - 1];
}

function setCycle(patch) {
  const ov = demo["/api/overview"], loop = demo["/api/loop"];
  const c = { ...loop.cycle, ...patch, updated_ts: now() };
  loop.cycle = c;
  ov.cycle = c;
  ov.cycles = [c, ...ov.cycles.filter((x) => x.n !== c.n)];
}

async function demoAdvance() {
  const loop = demo["/api/loop"];
  if (loop.busy) throw new ApiError(409, "The loop is already running.");
  const c = loop.cycle;
  const exp = c.experiment_id && demo[`/api/experiments/${c.experiment_id}`];
  loop.busy = true;
  const say = (line) => loop.log.push(`${clock()} ${line}`);
  (async () => {
    if (c.stage === "approval" && exp?.state === "PENDING_APPROVAL") {
      say(`demo: replaying cycle ${c.n} from the cache to show the stages`);
      for (const [stage, line] of [["listen", "listen: transcripts on disk, 0 new"], ["judge", "judge: every call graded (cache hits)"],
        ["improve", `improve: ${c.root_cause_id} is the top untried root cause`], ["gate", `gate: ${c.fix_id} clears every blocking check`],
        ["replay", "replay: simulated A vs simulated B, fail rate down"], ["prove", `prove: ${c.experiment_id} on 10% (simulated traffic)`],
        ["approval", "approval: waiting for a human to approve or reject"]]) {
        await sleep(950); setCycle({ stage }); say(line);
      }
    } else if (c.stage === "approval" && exp) {
      const promoted = exp.state === "PROMOTED";
      const reg = demo["/api/prompts"].items;
      await sleep(800);
      if (promoted) {
        setCycle({ stage: "rollout" }); say(`rollout: ${c.candidate_version} becomes production`);
        await sleep(1100);
        for (const p of reg) {
          if (p.version === c.production_version) p.status = "retired";
          if (p.version === c.candidate_version) p.status = "production";
        }
        demo["/api/prompts"].production = c.candidate_version;
        demo["/api/overview"].production = reg.find((p) => p.version === c.candidate_version);
        demo["/api/overview"].kpis.per_week_gained += exp.impact?.per_week || 0;
      } else {
        reg.find((p) => p.version === c.candidate_version).status = "rejected";
        say(`close: ${c.candidate_version} rejected, ${c.production_version} stays in production`);
      }
      setCycle({ stage: "done", outcome: exp.state });
      await sleep(900);
      const prod = demo["/api/overview"].production.version;
      const next = { n: c.n + 1, production_version: prod, stage: "listen", root_cause_id: null, fix_id: null, candidate_version: null,
        experiment_id: null, outcome: null, notes: [`Started on ${prod}. The demo stops here: no calls exist under ${prod} yet.`],
        started_ts: now(), updated_ts: now() };
      demo["/api/loop"].cycle = next;
      demo["/api/overview"].cycle = next;
      demo["/api/overview"].cycles = [next, ...demo["/api/overview"].cycles];
      say(`cycle ${next.n} starts on ${prod}`);
      loop.next_action = `Listen: transcribe calls made with ${prod}. In the demo there are none yet; run make platform for the real loop.`;
    } else {
      await sleep(700);
      say(`demo: cycle ${c.n} is at ${c.stage}; the scripted demo has nothing further to run`);
    }
    loop.busy = false;
    window.dispatchEvent(new CustomEvent("vani:changed", { detail: { path: "/api/loop" } }));
  })();
  return { accepted: true };
}

function demoTest({ rubric, call_id }) {
  const d = demo[`/api/calls/${call_id}`];
  if (!d) throw notFound(`Call ${call_id}`);
  const turns = d.transcript.turns;
  const find = (phrases) => {
    for (const ph of phrases || []) {
      const needle = ph.trim().toLowerCase();
      if (!needle) continue;
      const k = turns.findIndex((t) => t.text.toLowerCase().includes(needle));
      if (k >= 0) {
        const at = turns[k].text.toLowerCase().indexOf(needle);
        return { turn: k, quote: turns[k].text.slice(at, at + needle.length) };
      }
    }
    return null;
  };
  const bad = find(rubric.examples_fail), good = find(rubric.examples_pass);
  const base = { rubric_id: rubric.id || "draft", rubric_version: rubric.version || 1 };
  if (bad) return { ...base, grade: "fatal", confidence: 0.6, reason: "Demo mode, phrase match only: a fail example occurs in this call.", evidence: [bad] };
  if (good) return { ...base, grade: "pass", confidence: 0.6, reason: "Demo mode, phrase match only: a pass example occurs in this call.", evidence: [good] };
  return { ...base, grade: "na", confidence: 0.3, reason: "Demo mode has no judge model: none of your example phrases occur in this call. On the platform the judge model grades it.", evidence: [] };
}

function demoSaveRubric(method, path, body) {
  const all = stored();
  const id = decodeURIComponent(path.split("?")[0].split("/")[3] || "");
  const known = demoRubrics();
  if (method === "DELETE") {
    const cur = known.find((r) => r.id === id);
    if (!cur) throw notFound(`Rubric ${id}`);
    all[id] = [...(all[id] || [cur]), { ...cur, active: false }];
    store(all);
    return { ...cur, active: false };
  }
  const { actor, ...fields } = body;
  if (method === "POST") {
    if (known.some((r) => r.id === fields.id)) throw new ApiError(409, `A rubric with id ${fields.id} exists; edit it instead.`);
    all[fields.id] = [{ ...fields, version: 1, builtin: false, author: fields.author || actor }];
  } else {
    const cur = known.find((r) => r.id === id);
    if (!cur) throw notFound(`Rubric ${id}`);
    all[id] = [...(all[id] || [cur]), { ...cur, ...fields, id, builtin: cur.builtin, version: cur.version + 1 }];
  }
  store(all);
  const versions = all[fields.id || id];
  return versions[versions.length - 1];
}
