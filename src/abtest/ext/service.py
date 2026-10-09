"""The Vani Loop platform (docs/loop_api.md): the base app's routes (GET /assign, unchanged) plus the
loop's JSON routes, and the static site in ext/web/ at "/", mounted last so /api/* and /assign win.
make platform -> http://127.0.0.1:8600

Every read happens per request, at paths resolved then: the experiment store at loop.DB_PATH (the
store the loop runs its experiments in), loop artifacts under models.LOOP_DIR. The sibling modules
(listen, judge, rubrics, improve) are imported per request, so a missing or broken one fails its own
routes with 503 and the overview leaves its numbers empty; the app still serves. Real call content
stays on this machine: bind to 127.0.0.1, answer only loopback Host names (a DNS-rebinding page
sends its own); the web never sets allow_real; POST /api/rubrics/test grades a real transcript only
when the server was started with LOOP_ALLOW_REAL=1.
"""

import importlib
import math
import os
import re
import threading
from collections import Counter
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict
from starlette.middleware.trustedhost import TrustedHostMiddleware

from abtest import api, config, decision, store
from abtest.contracts import CONFIGS_DIR, ROOT, ExperimentConfig, ExperimentState, State
from abtest.ext import llm, loop, models
from abtest.ext.models import AgreementReport, CallAudit, Rubric, Stage, Transcript, load

WEB_DIR = Path(__file__).parent / "web"
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")  # ids that become file names
REAL = loop.REAL
DAY = timedelta(days=1)
FORMULA = "priority = 100 × (3·fatal + non-fatal) / (3·audited)"
HOSTS = ["127.0.0.1", "localhost"]
router = APIRouter(prefix="/api")
_advancing = threading.Lock()  # one web-started advance at a time; loop's file lock covers the CLI


# --- helpers -------------------------------------------------------------------------------

def _mod(name: str):
    """abtest.ext.<name>, imported per request: while it is missing or broken, only its routes fail."""
    try:
        return importlib.import_module(f"abtest.ext.{name}")
    except Exception as e:  # noqa: BLE001 - ImportError, or a SyntaxError while it is being written
        raise HTTPException(503, f"abtest.ext.{name} is not available: {type(e).__name__}: {e}") from None


def _maybe(fn, default=None):
    """fn(), or `default` when a sibling module it needs is unavailable (the overview degrades)."""
    try:
        return fn()
    except HTTPException as e:
        if e.status_code != 503:
            raise
        return default


def _finite(x):
    """NaN and inf -> None: a degenerate look carries them, and JSON has no spelling for them."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: _finite(v) for k, v in x.items()}
    if isinstance(x, list | tuple):
        return [_finite(v) for v in x]
    return x


def _dump(obj):
    if obj is None:
        return None
    if isinstance(obj, list | tuple):
        return [_dump(o) for o in obj]
    return _finite(obj.model_dump(mode="json"))


def _safe(value: str, what: str) -> str:
    if not SAFE_ID.fullmatch(value):
        raise HTTPException(404, f"unknown {what} {value!r}")
    return value


def _version(name: str):
    try:
        return loop.version(name)
    except KeyError:
        raise HTTPException(404, f"unknown prompt version {name!r}") from None


def _transcripts_of(name: str) -> list[Transcript]:
    """Every transcript on a version: v1's real calls of both sets; a later version's replay calls."""
    if _version(name).fix_id is not None:
        return loop.calls(name)
    return [t for t in _mod("listen").load_transcripts() if t.prompt_version == name]


def _audits_of(name: str) -> list[CallAudit]:
    """Every saved grade on a version (no model call): the judge's files, or the proving replay's."""
    if _version(name).fix_id is not None:
        return loop.audits(name)
    return _mod("judge").load_audits(name)


def _agreement() -> AgreementReport | None:
    return load(_mod("judge").AGREEMENT_JSON, AgreementReport)


def _rubrics() -> list[Rubric]:
    return _mod("rubrics").all_rubrics(include_archived=True)


def _human() -> dict[str, dict]:
    """IndiaMART's QA grades per audited call, keyed by rubric id (via each rubric's qa_column)."""
    try:
        rows = _mod("listen").audited_calls()
    except FileNotFoundError:
        return {}
    by_col = {r.qa_column: r.id for r in _rubrics() if r.qa_column}
    return {row["call_id"]: {"overall": row["human"]["overall"],
                             "grades": {by_col[c]: g for c, g in row["human"]["grades"].items() if c in by_col},
                             "remarks": {by_col[c]: t for c, t in row["human"]["remarks"].items() if c in by_col}}
            for row in rows}


def _find_call(call_id: str) -> tuple[Transcript, CallAudit | None]:
    """A transcript and its audit. A real call's audit is the judge's file. A replay transcript (and its
    audit) lives under improve's REPLAY_DIR/<fix id>/; the loop files a later version's copy, relabelled,
    in TRANSCRIPTS_DIR too."""
    t = load(models.TRANSCRIPTS_DIR / f"{_safe(call_id, 'call')}.json", Transcript)
    if t is not None and t.source != "replay":
        a = load(models.AUDITS_DIR / _safe(t.prompt_version, "version") / f"{call_id}.json", CallAudit)
        return t, None if a is None else _mod("judge").with_calibration([a])[0]  # as the calls list shows it
    replay_dir = _maybe(lambda: _mod("improve").REPLAY_DIR)
    hit = next(iter(sorted(replay_dir.glob(f"*/{call_id}.json"))), None) if replay_dir else None
    if t is None and hit is None:
        raise HTTPException(404, f"unknown call {call_id!r}")
    return t or load(hit, Transcript), load(hit.with_name(f"{call_id}.audit.json"), CallAudit) if hit else None


# --- experiments (base store; look-by-look numbers from the decision-log payloads) -------------

def _per_week(s: ExperimentState, cfg: ExperimentConfig, d) -> float | None:
    """decision.weekly_impact at a look: extra primary successes a week if B took all traffic."""
    if d is None or s.started_ts_sim is None:
        return None
    return decision.weekly_impact(d.primary.lift_abs, d.n_A + d.n_B, (d.ts_sim - s.started_ts_sim) / DAY, cfg)


def _assumed(eid: str, scenario: str | None, cfg: ExperimentConfig) -> str | None:
    """The sim's injected effect: from the loop's record of its run, else a base scenario's sim config."""
    if SAFE_ID.fullmatch(eid) and (meta := load(loop.EXPERIMENTS_DIR / f"{eid}.json")):
        return meta["assumed_effect"]
    if scenario and SAFE_ID.fullmatch(scenario) and (CONFIGS_DIR / "scenarios" / f"{scenario}.yaml").exists():
        try:
            return loop.assumed_effect(config.load_scenario(scenario).sim, cfg)
        except (OSError, ValueError):
            return None
    return None


def _experiment(conn, eid: str, scenario: str | None, cycles: dict[str, int], detail: bool) -> dict:
    s, cfg, ds = store.get_state(conn, eid), store.get_config(conn, eid), store.read_decisions(conn, eid)
    looks = [d for d in ds if d.kind == "look"]
    last = looks[-1] if looks else None
    out = {"experiment_id": eid, "state": s.state.value, "looks_done": s.looks_done, "n_looks": cfg.window.looks,
           "allocation": {"A": s.allocation.A, "B": s.allocation.B},
           "lift_rel": last.primary.lift_rel if last else None,
           "started": s.started_ts_sim.isoformat() if s.started_ts_sim else None,
           "updated": s.updated_ts_sim.isoformat() if s.updated_ts_sim else None,
           "cycle": cycles.get(eid), "simulated": True, "assumed_effect": _assumed(eid, scenario, cfg)}
    if detail:
        out |= {"config": cfg.model_dump(mode="json"), "n_b_max": s.n_b_max,
                "looks": [{"look": d.look, "info_fraction": d.info_fraction, "z": d.primary.z,
                           "win_bound": d.primary.win_boundary, "harm_bound": d.primary.harm_boundary,
                           "lift_rel": d.primary.lift_rel,
                           "ci95": [x / d.primary.rate_A if d.primary.rate_A else None for x in d.primary.ci95],
                           "n_a": d.n_A, "n_b": d.n_B, "decision": d.decision, "reason": d.reason,
                           "ts_sim": d.ts_sim.isoformat() if d.ts_sim else None} for d in looks],
                "decisions": [d.model_dump(mode="json") for d in ds],
                "impact": {"per_week": _per_week(s, cfg, last), "unit": " / ".join(cfg.primary.dispositions)}}
    return _finite(out)


def _experiment_ids(conn) -> list[tuple[str, str | None]]:
    return [(r[0], r[1]) for r in conn.execute(
        "SELECT experiment_id, e.scenario FROM experiments e JOIN experiment_state s USING (experiment_id) "
        "ORDER BY s.updated_ts_wall DESC")]


def _loop_cycles() -> dict[str, int]:
    return {c.experiment_id: c.n for c in loop.cycles() if c.experiment_id}


def _business(conn, eids) -> tuple[float, float]:
    """(gained, protected) BL successes a week over the loop's experiments: PROMOTED ones gain, ones the
    harm boundary stopped protect. All loop experiments share the buyer template, so the units add up."""
    gained = protected = 0.0
    for eid in eids:
        if (s := store.get_state(conn, eid)) is None:
            continue
        looks = [d for d in store.read_decisions(conn, eid) if d.kind == "look"]
        w = _per_week(s, store.get_config(conn, eid), looks[-1] if looks else None)
        if w is None or not math.isfinite(w):
            continue
        if s.state == State.PROMOTED:
            gained += w
        elif s.state == State.STOPPED_HARM and looks[-1].primary.z <= looks[-1].primary.harm_boundary:
            protected += abs(w)
    return round(gained, 1), round(protected, 1)


# --- read routes ---------------------------------------------------------------------------------

@router.get("/overview")
def overview() -> dict:
    prod, cs = loop.production(), loop.cycles()
    audits = _maybe(lambda: loop.audits(prod.version), [])  # the calls the loop judges, not every set on disk
    split = Counter(a.overall for a in audits)
    rate = (lambda k: split[k] / len(audits)) if audits else (lambda k: None)
    agreement = _maybe(_agreement)
    with closing(store.connect(loop.DB_PATH)) as conn:
        gained, protected = _business(conn, _loop_cycles())
    fixes = _maybe(lambda: _mod("improve").load_fixes(), [])
    kpis = {"calls_total": _maybe(lambda: len(loop.calls(prod.version))), "calls_audited": len(audits),
            "fatal_rate": rate("fatal"), "non_fatal_rate": rate("non_fatal"), "pass_rate": rate("pass"),
            "agreement_overall": agreement.overall_accuracy if agreement else None,
            "agreement_kappa": agreement.overall_kappa if agreement else None,
            "fatal_vs_rest_accuracy": agreement.fatal_vs_rest_accuracy if agreement else None,
            "root_causes": _maybe(lambda: len(_mod("improve").load_root_causes(prod.version))),
            "fixes_tested": sum(f.gate is not None or f.replay is not None for f in fixes),
            "experiments": len(_loop_cycles()), "per_week_gained": gained, "per_week_protected": protected}
    return {"production": _dump(prod), "cycle": _dump(cs[-1] if cs else None), "cycles": _dump(cs[::-1]),
            "kpis": _finite(kpis), "corpus": loop.corpus() if prod.fix_id is None else "replay",
            "simulated_traffic": True}


@router.get("/calls")
def calls(source: str | None = None, overall: str | None = None, flag: str | None = None,
          q: str | None = None, limit: int = Query(200, ge=1, le=5000)) -> dict:
    ts = _mod("listen").load_transcripts()
    audits = {}
    for name in sorted({t.prompt_version for t in ts}):
        try:
            audits |= {(name, a.call_id): a for a in _audits_of(name)}
        except HTTPException:  # a version the registry does not know, or no judge module: no grades
            continue
    human = _human()
    items = []
    for t in ts:
        a, h = audits.get((t.prompt_version, t.call_id)), human.get(t.call_id)
        grades = Counter(v.grade for v in a.verdicts) if a else Counter()
        item = {"call_id": t.call_id, "source": t.source, "duration_s": t.duration_s, "language": t.language,
                "prompt_version": t.prompt_version, "overall": a.overall if a else None,
                "disposition": a.disposition if a else None, "risk_flags": list(a.risk_flags) if a else [],
                "summary": a.summary if a else "", "n_fatal": grades["fatal"], "n_non_fatal": grades["non_fatal"],
                "human_overall": h["overall"] if h else None}
        if ((source and t.source != source) or (overall and item["overall"] != overall)
                or (flag and flag not in item["risk_flags"])
                or (q and q.lower() not in f"{t.call_id} {item['summary']}".lower())):
            continue
        items.append(item)
    return {"total": len(items), "items": _finite(items[:limit])}


@router.get("/calls/{call_id}")
def call(call_id: str) -> dict:
    t, a = _find_call(call_id)
    h = _human().get(call_id)
    mp3 = models.RECORDINGS_DIR / f"{call_id}.mp3"
    return {"transcript": _dump(t), "audit": _dump(a), "human": h,
            "audio_url": f"/api/recordings/{call_id}" if mp3.exists() else None,
            "rubrics": {r.id: {"name": r.name, "category": r.category, "root_type": r.root_type} for r in _rubrics()}}


@router.get("/recordings/{call_id}")
def recording(call_id: str) -> FileResponse:
    mp3 = models.RECORDINGS_DIR / f"{_safe(call_id, 'recording')}.mp3"
    if not mp3.is_file():
        raise HTTPException(404, f"no local recording of {call_id!r}")
    return FileResponse(mp3, media_type="audio/mpeg")


@router.get("/audit")
def audit(version: str = "v1", source: str | None = None) -> dict:
    """How the judge graded a version's calls: by default v1's are the loop's corpus (source=all: every set)."""
    v = _version(version)
    src = source or (loop.corpus() if v.fix_id is None else "all")
    ts = [t for t in _transcripts_of(version) if src == "all" or t.source == src]
    ids = {t.call_id for t in ts}
    audits = [a for a in _audits_of(version) if a.call_id in ids]
    human = _human() if v.fix_id is None else {}
    labelled = [human[t.call_id]["overall"] for t in ts if t.call_id in human and human[t.call_id]["overall"]]
    split = Counter(a.overall for a in audits)
    grades: dict[str, Counter] = {}
    for a in audits:
        for x in a.verdicts:
            grades.setdefault(x.rubric_id, Counter())[x.grade] += 1
    meta = {r.id: r for r in _rubrics()}
    by_rubric = [{"rubric_id": rid, "name": meta[rid].name if rid in meta else rid,
                  "category": meta[rid].category if rid in meta else None,
                  "root_type": meta[rid].root_type if rid in meta else None,
                  "n_applicable": g["pass"] + g["non_fatal"] + g["fatal"], **{k: g[k] for k in ("pass", "non_fatal", "fatal", "na")}}
                 for rid, g in sorted(grades.items(), key=lambda kv: list(meta).index(kv[0]) if kv[0] in meta else len(meta))]
    risky = sorted((a for a in audits if a.risk_flags), key=lambda a: (-models.SEVERITY[a.overall], a.call_id))
    return {"version": version, "source": src, "n_calls": len(ts), "n_audited": len(audits),
            "split": {k: split[k] for k in ("pass", "non_fatal", "fatal")},
            "human_split": {k: labelled.count(k) for k in ("pass", "non_fatal", "fatal")} if labelled else None,
            "agreement": _dump(_maybe(_agreement)) if v.fix_id is None else None,
            "by_rubric": by_rubric,
            "risky": [{"call_id": a.call_id, "flags": list(a.risk_flags), "overall": a.overall, "summary": a.summary}
                      for a in risky]}


@router.get("/issues")
def issues(version: str = "v1") -> dict:
    _version(version)
    rcs = sorted(_mod("improve").load_root_causes(version), key=lambda r: (-r.priority, r.id))
    return {"version": version, "formula": FORMULA, "items": _dump(rcs)}


@router.get("/fixes")
def fixes() -> dict:
    return {"items": _dump(_mod("improve").load_fixes())}


@router.get("/fixes/{fix_id}")
def fix(fix_id: str) -> dict:
    improve = _mod("improve")
    if (f := next((f for f in improve.load_fixes() if f.id == fix_id), None)) is None:
        raise HTTPException(404, f"unknown fix {fix_id!r}")
    rc = next((r for r in improve.load_root_causes(f.base_version) if r.id == f.root_cause_id), None)
    return {"fix": _dump(f), "root_cause": _dump(rc)}


@router.get("/prompts")
def prompts() -> dict:
    return {"production": loop.production().version, "items": _dump(loop.versions())}


@router.get("/prompts/{version}/text", response_class=PlainTextResponse)
def prompt_text(version: str) -> str:
    path = ROOT / _version(version).path
    if not path.is_file():
        raise HTTPException(404, f"the prompt file of {version} is missing")
    return path.read_text(encoding="utf-8")


@router.get("/experiments")
def experiments() -> dict:
    cycles = _loop_cycles()
    with closing(store.connect(loop.DB_PATH)) as conn:
        return {"items": [_experiment(conn, eid, sc, cycles, detail=False) for eid, sc in _experiment_ids(conn)]}


@router.get("/experiments/{experiment_id}")
def experiment(experiment_id: str) -> dict:
    with closing(store.connect(loop.DB_PATH)) as conn:
        hit = next(((e, sc) for e, sc in _experiment_ids(conn) if e == experiment_id), None)
        if hit is None:
            raise HTTPException(404, f"unknown experiment {experiment_id!r}")
        return _experiment(conn, *hit, _loop_cycles(), detail=True)


@router.get("/rubrics")
def rubrics() -> dict:
    return {"items": _dump(_rubrics())}


@router.get("/rubrics/{rubric_id}/versions")
def rubric_versions(rubric_id: str) -> dict:
    """Every version of one rubric, oldest first (the built-in YAML is version 1 of a built-in)."""
    lib = _mod("rubrics")
    out = [r for r in lib.builtin() if r.id == rubric_id] + \
          [r for r in load(models.CUSTOM_RUBRICS, Rubric, default=[]) if r.id == rubric_id]
    if not out:
        raise HTTPException(404, f"unknown rubric {rubric_id!r}")
    return {"items": _dump(sorted(out, key=lambda r: r.version))}


@router.get("/loop")
def loop_state() -> dict:
    c = loop.current_cycle()
    return {"cycle": _dump(c), "next_action": loop.next_action(c), "busy": _advancing.locked() or loop.busy(),
            "log": list(loop.LOG)}


@router.get("/settings")
def settings() -> dict:
    """What the platform runs with: model per role, spend against the cap, where data lives."""
    spend = load(llm.SPEND, default={}) or {}
    rel = lambda p: loop._rel(p)  # noqa: E731
    return {"models": {role: llm.model_for(role) for role in llm.MODELS},
            "budget": {"cap_usd": float(os.environ.get("LLM_BUDGET_USD", "40")),
                       "spent_usd": spend.get("total_usd", 0.0), "calls": spend.get("calls", 0)},
            "paths": {"loop": rel(models.LOOP_DIR), "recordings": rel(models.RECORDINGS_DIR),
                      "transcripts": rel(models.TRANSCRIPTS_DIR), "audits": rel(models.AUDITS_DIR),
                      "prompts": rel(models.PROMPTS_DIR), "rubrics": rel(models.RUBRICS_DIR),
                      "custom_rubrics": rel(models.CUSTOM_RUBRICS), "llm_cache": rel(models.LLM_CACHE_DIR)},
            "bind": "127.0.0.1:8600", "allow_real_rubric_tests": os.environ.get("LOOP_ALLOW_REAL") == "1"}


# --- write routes --------------------------------------------------------------------------------

class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionIn(_Body):
    actor: str
    approved: bool


class AdvanceIn(_Body):
    until: Stage | None = None


class RubricTestIn(_Body):
    rubric: dict[str, Any]
    call_id: str


def _rubric_write(fields: dict[str, Any], rubric_id: str | None) -> Rubric:
    lib = _mod("rubrics")
    fields = dict(fields)
    actor = str(fields.pop("actor", "") or "").strip()
    if not actor:
        raise HTTPException(422, "actor is required: who wrote this rubric version")
    rid = rubric_id or fields.get("id") or lib.snake(str(fields.get("name") or ""))
    try:
        lib.get(rid)
        exists = True
    except KeyError:
        exists = False
    if rubric_id is None and exists:
        raise HTTPException(409, f"rubric {rid!r} exists; PUT /api/rubrics/{rid} saves a new version")
    if rubric_id is not None and not exists:
        raise HTTPException(404, f"unknown rubric {rubric_id!r}")
    try:
        return lib.save({**fields, "id": rid}, actor)
    except ValueError as e:  # pydantic's ValidationError included
        raise HTTPException(422, str(e)) from None


@router.post("/rubrics", status_code=201)
def rubric_create(fields: dict[str, Any] = Body(...)) -> dict:
    return _dump(_rubric_write(fields, None))


@router.put("/rubrics/{rubric_id}")
def rubric_update(rubric_id: str, fields: dict[str, Any] = Body(...)) -> dict:
    return _dump(_rubric_write(fields, rubric_id))


@router.delete("/rubrics/{rubric_id}")
def rubric_archive(rubric_id: str, actor: str = "web") -> dict:
    try:
        return _dump(_mod("rubrics").archive(rubric_id, actor))
    except KeyError:
        raise HTTPException(404, f"unknown rubric {rubric_id!r}") from None


@router.post("/rubrics/test")
def rubric_test(body: RubricTestIn) -> dict:
    """Grade one transcript with a draft rubric (one model call; nothing is saved). Real customer calls
    are refused unless the server was started with LOOP_ALLOW_REAL=1."""
    t, _ = _find_call(body.call_id)
    if t.source in REAL and os.environ.get("LOOP_ALLOW_REAL") != "1":
        raise HTTPException(403, f"{t.call_id} is a real customer call; testing a rubric on it sends it to the "
                                 "model provider. Test on a fixture or replay call, or start the server with "
                                 "LOOP_ALLOW_REAL=1 once that is approved.")
    try:
        return _dump(_mod("judge").test_rubric(body.rubric, t))
    except ValueError as e:  # a bad draft (validation) or an unusable judge reply
        raise HTTPException(422, str(e)) from None


@router.post("/experiments/{experiment_id}/decision")
def experiment_decision(experiment_id: str, body: DecisionIn) -> dict:
    if not body.actor.strip():
        raise HTTPException(422, "actor is required: the approver's name goes on the decision log")
    try:
        state = loop.approve(experiment_id, body.actor.strip(), body.approved)
    except loop.Busy as e:
        raise HTTPException(409, str(e)) from None
    except KeyError:
        raise HTTPException(404, f"unknown experiment {experiment_id!r}") from None
    except ValueError as e:
        raise HTTPException(409, str(e)) from None
    return {"experiment_id": experiment_id, "state": state.state.value, "cycle": _dump(loop.current_cycle())}


@router.post("/loop/advance", status_code=202)
def loop_advance(body: AdvanceIn | None = None) -> dict:
    """Runs loop.advance in a background thread, one at a time. allow_real stays False here: sending
    real transcripts is the user's decision, made on the CLI (--yes-send-real-transcripts)."""
    until = body.until if body else None
    if loop.busy() or not _advancing.acquire(blocking=False):
        raise HTTPException(409, "the loop is already advancing")

    def run() -> None:
        try:
            loop.say(f"advance requested from the platform (until {until or 'the cycle waits or ends'})")
            c = loop.advance(until=until)
            loop.say(f"advance finished: cycle {c.n} at {c.stage}")
        except Exception as e:  # noqa: BLE001 - the thread's only report is the log
            loop.say(f"advance failed: {type(e).__name__}: {e}")
        finally:
            _advancing.release()

    threading.Thread(target=run, name="loop-advance", daemon=True).start()
    return {"accepted": True, "until": until}


# A new app around the base app's routes rather than the base app object itself: the host check is
# middleware, which cannot join an app another importer may already have started.
app = FastAPI(title="Vani Loop")
app.router.routes.extend(r for r in api.app.routes if isinstance(r, APIRoute))
app.add_middleware(TrustedHostMiddleware, allowed_hosts=HOSTS)
app.include_router(router)
if WEB_DIR.is_dir():  # last: the site answers only what no route above does
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
