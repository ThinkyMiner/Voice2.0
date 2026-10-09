"""The Vani Loop driver: the prompt registry and one cycle per production prompt version.

Each stage persists the cycle to CYCLES before the next one runs, and runs only while the cycle is
at it, so re-running a finished stage does nothing:
  listen    count the production version's calls: v1's real production transcripts. A later version
            was never spoken by a live bot, so its calls are the replay transcripts of the test that
            proved it (source=replay), never presented as production calls.
  judge     grade them (judge.audit_many)
  improve   rank root causes; pick the top prompt/flow cause no cycle on this version has tried
  gate      draft Prompt B (a registry candidate) and safety-check it
  replay    simulated before/after on the root cause's calls
  prove     B on 10% of simulated traffic through the base engine (buyer template, approval promotion)
  approval  wait for a human while the engine says PENDING_APPROVAL
  rollout   PROMOTED: B becomes production and cycle n+1 audits it; otherwise A stays and cycle n+1
            tries the next root cause
A try that fails before the engine (GATE_BLOCKED, REPLAY_NO_GAIN, FIX_INVALID when the fixer's edits do
not apply, REPLAY_FAILED when the replay cannot run) rejects its candidate and tries the next root
cause, at most MAX_TRIES per cycle. Stages that send real call content to the model provider wait for
allow_real (CLI --yes-send-real-transcripts: the user's decision); the web server never sets it.

CLI: python -m abtest.ext.loop advance [--until STAGE] [--yes-send-real-transcripts]
     python -m abtest.ext.loop status
     python -m abtest.ext.loop approve <experiment_id> --actor NAME [--reject]
"""

import argparse
import fcntl
import hashlib
import os
import re
import sys
import threading
from collections import Counter, deque
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml

from abtest import config, contracts, decision, runner, store
from abtest.contracts import BASE_PROMPT, ROOT, ExperimentConfig, ExperimentState, Scenario, State
from abtest.ext import models
from abtest.ext.models import (CallAudit, Cycle, Fix, GateResult, PromptVersion, ReplayResult, RootCause,
                               Stage, Transcript, load, save)

TEMPLATE = "configs/experiments/vani-buyer-qty-asks-v1.yaml"  # buyer side: bl_success, abrupt_disconnect, 90/10, approval
DEFAULT_SIM = "configs/sims/buyer_win.yaml"                   # LOOP_DEMO_SIM overrides
SEED = 4100                                                    # cycle n simulates at seed SEED + n
MAX_TRIES = 3
EXPERIMENTS_DIR = models.LOOP_DIR / "experiments"  # vani-loop-c<n>.yaml (config), .json (scenario, assumed effect)
REPLAY_AUDITS = models.LOOP_DIR / "replay_audits"  # <version>.json: the judge stage's grades of a later version's calls
LOCK = models.LOOP_DIR / "loop.lock"
DB_PATH = contracts.DB_PATH  # read per call, so tests (and the service) can point it elsewhere
REAL = ("production", "audited")  # Transcript sources that are real customer calls
FIXABLE = ("prompt", "flow")      # root-cause types a prompt change can address
STAGES: tuple[Stage, ...] = ("listen", "judge", "improve", "gate", "replay", "prove", "approval", "rollout")
GO_AHEAD = ("waiting for the go-ahead to send real transcripts: this stage sends real call content to the "
            "model provider; once the user approves, run: make loop ARGS=--yes-send-real-transcripts")
FAILED_TRY = re.compile(r"^try \d+: (\S+) -> ")  # the note a failed try leaves; _tried() reads it back
LOG: deque[str] = deque(maxlen=200)  # recent progress lines, for GET /api/loop


class Busy(RuntimeError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def say(msg: str) -> None:
    LOG.append(f"{datetime.now():%H:%M:%S} {msg}")
    print(msg, flush=True)


@contextmanager
def _exclusive():
    """One loop writer at a time across threads and processes: the CLI and the web server can both
    drive the loop. flock is per open file, so a second open in the same process conflicts too."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Busy("the loop is already advancing (another thread or process holds it)") from None
        yield


def busy() -> bool:
    try:
        with _exclusive():
            return False
    except Busy:
        return True


# --- registry ----------------------------------------------------------------------------

def _rel(path: str | Path) -> str:
    """Relative to ROOT when under it (as configs write paths), else absolute."""
    p = Path(path).resolve()
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def _sha(path: str) -> str:
    """Of the bytes abtest.config reads for a variant prompt (a ROOT-relative or absolute path)."""
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def versions() -> list[PromptVersion]:
    """The registry, oldest first. First use bootstraps v1 = the committed production prompt."""
    vs = load(models.REGISTRY, PromptVersion, default=[])
    if not vs:  # readers (the web) bootstrap too, without the loop's lock: create-if-absent, never replace
        path = _rel(BASE_PROMPT)
        tmp = models.REGISTRY.with_name(f".registry.{os.getpid()}.{threading.get_ident()}.tmp")
        save(tmp, [PromptVersion(version="v1", path=path, sha256=_sha(path), status="production", created_ts=_now())])
        try:
            os.link(tmp, models.REGISTRY)  # atomic, and fails if the file exists
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
        vs = load(models.REGISTRY, PromptVersion)
    return vs


def production() -> PromptVersion:
    return next(v for v in versions() if v.status == "production")


def version(name: str) -> PromptVersion:
    if (v := next((v for v in versions() if v.version == name), None)) is None:
        raise KeyError(f"prompt version {name!r} is not in the registry")
    return v


def _verify(v: PromptVersion) -> None:
    """The file still holds the text the registry recorded, the one the gate and the replay reviewed."""
    if _sha(v.path) != v.sha256:
        raise ValueError(f"{v.version}'s prompt file {v.path} changed after it was registered (sha256 mismatch)")


def add_candidate(fix: Fix) -> PromptVersion:
    """Prompt B of `fix` as the next vN, child of production. Idempotent per fix."""
    vs = versions()
    if hit := next((v for v in vs if v.fix_id == fix.id), None):
        return hit
    sha = _sha(fix.prompt_b_path)
    if sha != fix.prompt_b_sha256:
        raise ValueError(f"{fix.prompt_b_path} changed after {fix.id} was drafted (sha256 mismatch)")
    v = PromptVersion(version=f"v{1 + max(int(x.version[1:]) for x in vs)}", path=fix.prompt_b_path, sha256=sha,
                      parent=production().version, fix_id=fix.id, status="candidate", created_ts=_now())
    save(models.REGISTRY, vs + [v])
    return v


def _set_status(name: str, status: str) -> PromptVersion:
    """candidate -> production (the old production retires) or rejected. Repeating it is a no-op, so a
    rollout interrupted after the registry write can simply run again."""
    vs = versions()
    target = version(name)
    if target.status == status:
        return target
    if target.status != "candidate":
        raise ValueError(f"{name} is {target.status}; only a candidate can become {status}")
    out = [v.model_copy(update={"status": status}) if v.version == name
           else v.model_copy(update={"status": "retired"}) if status == "production" and v.status == "production"
           else v for v in vs]
    save(models.REGISTRY, out)
    return version(name)


def promote(name: str) -> PromptVersion:
    return _set_status(name, "production")


def reject(name: str) -> PromptVersion:
    return _set_status(name, "rejected")


# --- seams: the sibling modules, imported on first use (tests replace these) ------------------

def calls(name: str) -> list[Transcript]:
    """The calls a cycle on this version judges. v1: real calls, by default the buyer-side calls
    IndiaMART's QA team audited (env LOOP_CORPUS=audited|production). They match the buyer-side prompt
    and the QA checklist; the 2026-10-06 production sample turned out to be the seller-side bot, whose
    prompt we do not have. A later version has no live calls (no bot ever spoke it), so its calls are
    Prompt B's replay transcripts from the test that proved it, relabelled with the version; their
    source stays replay."""
    v = version(name)
    if v.fix_id is None:
        from abtest.ext import listen
        corpus = os.environ.get("LOOP_CORPUS", "audited")
        return [t for t in listen.load_transcripts(corpus) if t.prompt_version == name]
    from abtest.ext import improve
    return [load(p, Transcript).model_copy(update={"prompt_version": name})
            for p in sorted((improve.REPLAY_DIR / v.fix_id).glob("*-B.json"))]


def audits(name: str) -> list[CallAudit]:
    """The saved grades of those calls, no model call: v1's from the judge's files; a later version's from
    the replay that proved it (the judge files no audit of a replay transcript: it is a simulation)."""
    v = version(name)
    if v.fix_id is None:
        from abtest.ext import judge
        ids = {t.call_id for t in calls(name)}
        return [a for a in judge.load_audits(name) if a.call_id in ids]
    if (graded := load(REPLAY_AUDITS / f"{name}.json", CallAudit)) is not None:  # this version's judge stage ran
        return graded
    from abtest.ext import improve
    return [load(p, CallAudit).model_copy(update={"prompt_version": name})
            for p in sorted((improve.REPLAY_DIR / v.fix_id).glob("*-B.audit.json"))]


def _judge(transcripts: list[Transcript], allow_real: bool) -> list[CallAudit]:
    """allow_real is checked by the stage before this runs (judge.audit_many itself never refuses)."""
    from abtest.ext import judge
    return judge.audit_many(transcripts)


def _rank(audits: list[CallAudit], name: str) -> list[RootCause]:
    from abtest.ext import improve, rubrics
    return improve.root_causes(audits, [r for r in rubrics.all_rubrics() if r.active], name)


def _ranked(name: str) -> list[RootCause]:
    from abtest.ext import improve
    return improve.load_root_causes(name)


def _draft(rc: RootCause, base: PromptVersion, audits: list[CallAudit]) -> Fix:
    from abtest.ext import improve
    history = [f for f in improve.load_fixes()  # tried fixes on this rubric: the fixer never repeats one
               if f.root_cause_id.startswith(f"rc-{rc.rubric_id}-") and (f.gate is not None or f.replay is not None)]
    return improve.draft_fix(rc, base, audits, history)


def _fix(fix_id: str) -> Fix:
    from abtest.ext import improve
    if (fix := next((f for f in improve.load_fixes() if f.id == fix_id), None)) is None:
        raise KeyError(f"fix {fix_id!r} is not on file")
    return fix


def _gate(fix: Fix) -> GateResult:
    from abtest.ext import improve
    return improve.gate(fix)


def _replay(fix: Fix, rc: RootCause, audits: list[CallAudit], allow_real: bool) -> ReplayResult:
    from abtest.ext import improve
    return improve.replay(fix, rc, audits, allow_real=allow_real)


# --- cycles ------------------------------------------------------------------------------

def cycles() -> list[Cycle]:
    return load(models.CYCLES, Cycle, default=[])


def current_cycle() -> Cycle | None:
    cs = cycles()
    return cs[-1] if cs else None


def _put(c: Cycle, note: str | None = None, **fields) -> Cycle:
    """Persist the cycle with `fields` changed and `note` appended (unless it repeats the last note)."""
    notes = c.notes + ((note,) if note and note not in c.notes[-1:] else ())
    c = Cycle.model_validate({**c.model_dump(), **fields, "notes": notes, "updated_ts": _now()})
    save(models.CYCLES, sorted([x for x in cycles() if x.n != c.n] + [c], key=lambda x: x.n))
    if note:
        say(f"cycle {c.n}: {note}")
    return c


def _wait(c: Cycle, note: str) -> None:
    """The cycle cannot move on now; record why and stop advancing."""
    _put(c, note)


def _open(n: int, production_version: str) -> Cycle:
    now = _now()
    return _put(Cycle(n=n, production_version=production_version, stage="listen", started_ts=now, updated_ts=now),
                f"cycle {n} opened on {production_version}")


def _real(c: Cycle) -> bool:
    return any(t.source in REAL for t in calls(c.production_version))


def _tried(name: str) -> set[str]:
    """Root causes any cycle on this production version has tried: its current one and failed tries."""
    out = set()
    for c in cycles():
        if c.production_version == name:
            out |= {c.root_cause_id} - {None}
            out |= {m.group(1) for note in c.notes if (m := FAILED_TRY.match(note))}
    return out


def _failures(c: Cycle) -> int:
    return sum(bool(FAILED_TRY.match(note)) for note in c.notes)


def _try_next(c: Cycle, rcs: list[RootCause]) -> Cycle:
    tried = _tried(c.production_version)
    rc = next((r for r in sorted(rcs, key=lambda r: (-r.priority, r.id)) if r.type in FIXABLE and r.id not in tried),
              None)
    if rc is None:
        return _put(c, f"blocked: no untried prompt or flow root cause left on {c.production_version} (the rest "
                       "need speech, knowledge or platform work, not a prompt change)", stage="blocked")
    return _put(c, f"improve: try {_failures(c) + 1} of {MAX_TRIES}: {rc.name} ({rc.id}, {rc.type}, priority "
                   f"{rc.priority:.1f}: {rc.n_fatal} fatal, {rc.n_non_fatal} non-fatal of {rc.n_audited} calls)",
                stage="gate", root_cause_id=rc.id, fix_id=None, candidate_version=None)


def _failed(c: Cycle, outcome: str, why: str) -> Cycle:
    """A try failed before the engine: reject its candidate, then the next root cause, or after
    MAX_TRIES the cycle is done and cycle n+1 starts over on the same production version."""
    if c.candidate_version:
        reject(c.candidate_version)
    note = f"try {_failures(c) + 1}: {c.root_cause_id} -> {outcome}: {why}"
    say(f"cycle {c.n}: {note}")
    # Persisted together with the next transition (below), so an interrupted failure is never counted twice.
    c = c.model_copy(update={"outcome": outcome, "notes": c.notes + (note,)})
    if _failures(c) < MAX_TRIES:
        return _try_next(c, _ranked(c.production_version))
    _put(c, f"done: {MAX_TRIES} tries failed; {c.production_version} stays production", stage="done")
    return _open(c.n + 1, c.production_version)


def _cause(c: Cycle) -> RootCause:
    if (rc := next((r for r in _ranked(c.production_version) if r.id == c.root_cause_id), None)) is None:
        raise KeyError(f"root cause {c.root_cause_id!r} of {c.production_version} is not on file")
    return rc


def _state(experiment_id: str) -> ExperimentState | None:
    conn = store.connect(DB_PATH)
    try:
        return store.get_state(conn, experiment_id)
    finally:
        conn.close()


def _label(metric: str) -> str:
    """bl_success -> "BL success", answered_duration_s -> "answered duration"."""
    return re.sub(r"\bbl\b", "BL", metric.removesuffix("_s").replace("_", " "))


def assumed_effect(sim: str, cfg: ExperimentConfig) -> str:
    """What the simulator injects into B, in words: every simulated result is shown with it."""
    t, sec = config.load_sim(sim).true_effect, cfg.secondary
    parts = [f"{t.primary_rel_lift * 100:+g}% {_label(cfg.primary.name)}"] if t.primary_rel_lift else []
    if t.secondary_change:
        unit = "%" if sec.type == "continuous" else " pp"
        parts.append(f"{t.secondary_change * 100:+g}{unit} {_label(sec.name)}")
    return f"{', '.join(parts) or 'no effect'} for B (assumed; simulated traffic)"


# --- stages: each takes the cycle at its stage, returns it persisted, or None to wait ------------

def _listen(c: Cycle, allow_real: bool) -> Cycle | None:
    v = version(c.production_version)
    found = calls(v.version)
    if not found:
        how = ("transcribe the production calls: make listen SOURCE=production ARGS=--yes-send-real-audio "
               "(sends real recordings to the model provider: the user's decision)" if v.fix_id is None
               else f"no replay transcripts of {v.fix_id} found")
        return _wait(c, f"waiting for transcripts of {v.version}: {how}")
    for t in found:  # where improve looks a call up by id (fix excerpts, replay personas)
        if t.source == "replay" and not (models.TRANSCRIPTS_DIR / f"{t.call_id}.json").exists():
            save(models.TRANSCRIPTS_DIR / f"{t.call_id}.json", t)
    if v.fix_id is None:
        note = f"listen: {len(found)} calls on {v.version}, {sum(t.source in REAL for t in found)} of them real"
    else:
        note = (f"listen: {v.version} has no live calls yet (no bot has spoken it), so this cycle judges its "
                f"{len(found)} replay transcripts: simulated conversations with Prompt B, labelled source=replay")
    return _put(c, note, stage="judge")


def _judge_stage(c: Cycle, allow_real: bool) -> Cycle | None:
    found = calls(c.production_version)
    if any(t.source in REAL for t in found) and not allow_real:
        return _wait(c, GO_AHEAD)
    graded = _judge(found, allow_real)
    if not graded:
        return _wait(c, f"waiting: none of the {len(found)} calls could be graded (the judge's errors are in the "
                        "log); advance again to retry them")
    if version(c.production_version).fix_id is not None:  # the judge files no audit of a replay call
        save(REPLAY_AUDITS / f"{c.production_version}.json", graded)
    n = Counter(a.overall for a in graded)
    left = f" ({len(found) - len(graded)} could not be graded and are left out)" if len(graded) < len(found) else ""
    return _put(c, f"judge: {len(graded)} of {len(found)} calls graded{left}: {n['fatal']} fatal, "
                   f"{n['non_fatal']} non-fatal, {n['pass']} pass", stage="improve")


def _improve(c: Cycle, allow_real: bool) -> Cycle | None:
    if _real(c) and not allow_real:  # root causes send judge reasons and quotes
        return _wait(c, GO_AHEAD)
    name = c.production_version
    # An earlier cycle on this version tried causes by id: keep that ranking, since a fresh grouping renumbers them.
    rcs = (_ranked(name) if _tried(name) else []) or _rank(audits(name), name)
    c = _put(c, f"improve: {len(rcs)} root causes ranked ({sum(r.type in FIXABLE for r in rcs)} of type prompt "
                "or flow)")
    return _try_next(c, rcs)


def _gate_stage(c: Cycle, allow_real: bool) -> Cycle | None:
    if c.fix_id is None:
        if _real(c) and not allow_real:  # the fixer sees call excerpts
            return _wait(c, GO_AHEAD)
        base = version(c.production_version)
        try:
            fix = _draft(_cause(c), base, audits(base.version))
        except ValueError as e:
            return _failed(c, "FIX_INVALID", f"the fixer's edits do not apply: {e}")
        cand = add_candidate(fix)
        c = _put(c, f"gate: drafted {fix.id} ({len(fix.edits)} edit(s)): candidate {cand.version}",
                 fix_id=fix.id, candidate_version=cand.version)
    else:
        fix = _fix(c.fix_id)
    g = _gate(fix)
    if not g.passed:
        why = ", ".join(f"{x.id} p={x.probability:.2f}" for x in g.checks if x.blocking and x.probability >= x.threshold)
        return _failed(c, "GATE_BLOCKED", f"the safety gate blocked {c.candidate_version} ({why})")
    return _put(c, f"gate: {c.candidate_version} passed the safety gate", stage="replay")


def _replay_stage(c: Cycle, allow_real: bool) -> Cycle | None:
    if _real(c) and not allow_real:  # personas are built from the real calls
        return _wait(c, GO_AHEAD)
    rc = _cause(c)
    try:
        r = _replay(_fix(c.fix_id), rc, audits(c.production_version), allow_real)
    except PermissionError:
        return _wait(c, GO_AHEAD)
    except ValueError as e:
        return _failed(c, "REPLAY_FAILED", str(e))
    what = (f"{rc.rubric_id} fail rate A {r.fail_rate_a:.0%} -> B {r.fail_rate_b:.0%} over {r.n_cases} "
            f"simulated conversations (the real calls: {r.fail_rate_real:.0%})")
    if r.fail_rate_b >= r.fail_rate_a:
        return _failed(c, "REPLAY_NO_GAIN", what)
    worse = ", ".join(f"{k} {d:+.0%}" for k, d in r.regressions.items() if d > 0)
    return _put(c, f"replay: {what}" + (f"; other rubrics worse in B: {worse}" if worse else ""), stage="prove")


def _prove(c: Cycle, allow_real: bool) -> Cycle:
    base, cand = version(c.production_version), version(c.candidate_version)
    _verify(base)
    _verify(cand)
    eid = f"vani-loop-c{c.n}"
    raw = yaml.safe_load((ROOT / TEMPLATE).read_text(encoding="utf-8"))
    raw.update(id=eid, salt=hashlib.sha256(c.fix_id.encode()).hexdigest()[:6],
               variants={"A": {"base": base.path}, "B": {"patch": {"mode": "full", "path": cand.path}}})
    path = EXPERIMENTS_DIR / f"{eid}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"# Vani Loop cycle {c.n}: A = {base.version} (production), B = {cand.version} ({c.fix_id}).\n"
                    f"# Written by abtest.ext.loop from {TEMPLATE}.\n"
                    + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    sim = os.environ.get("LOOP_DEMO_SIM", DEFAULT_SIM)
    # The runner names a scenario's experiment "<id>-<scenario name>": id "vani-loop" + "c<n>" = eid.
    sc = Scenario(name=f"c{c.n}", experiment=str(path), sim=sim, seed=SEED + c.n, overrides={"id": "vani-loop"})
    cfg = config.scenario_experiment(sc)
    effect = assumed_effect(sim, cfg)
    save(path.with_suffix(".json"), {"experiment_id": eid, "cycle": c.n, "assumed_effect": effect,
                                     "scenario": sc.model_dump(mode="json")})
    state = _state(eid) if c.experiment_id == eid else None
    if state is None or state.state in (State.DRAFT, State.RUNNING) or state.config_hash != config.freeze(cfg).config_hash:
        # The id is saved first: a prove resumed after the run reuses it (rerunning would reset its approval).
        c = _put(c, experiment_id=eid)
        conn = store.connect(DB_PATH)
        try:
            state = runner.run_scenario(sc, conn)
        finally:
            conn.close()
    return _put(c, f"prove: {eid} ran {cand.version} on {raw['allocation']['B']:.0%} of simulated traffic "
                   f"({effect}) -> {state.state.value}", stage="approval", experiment_id=eid, outcome=state.state.value)


def _approval(c: Cycle, allow_real: bool) -> Cycle | None:
    s = _state(c.experiment_id)
    if s is None or s.state in (State.DRAFT, State.RUNNING):  # the store lost or never finished the run
        return _put(c, f"prove again: {c.experiment_id} has no decided run in the store", stage="prove")
    if s.state == State.PENDING_APPROVAL:
        return _wait(c, f"waiting for approval: {c.experiment_id} won on simulated traffic; approve or reject it "
                        f"(POST /api/experiments/{c.experiment_id}/decision, or python -m abtest.ext.loop approve "
                        f"{c.experiment_id} --actor NAME [--reject])")
    return _put(c, f"approval: {c.experiment_id} is {s.state.value}", stage="rollout", outcome=s.state.value)


def _rollout(c: Cycle, allow_real: bool) -> Cycle:
    s = _state(c.experiment_id)
    if s is not None and s.state == State.PROMOTED:
        cand = version(c.candidate_version)
        if s.prompt_hash["B"] != cand.sha256:
            raise ValueError(f"{c.experiment_id} tested a different Prompt B than {cand.version}; not promoting it")
        _verify(cand)
        new = promote(cand.version)
        _put(c, f"rollout: {new.version} is production; {c.production_version} retired", stage="done",
             outcome=State.PROMOTED.value)
        return _open(c.n + 1, new.version)
    reject(c.candidate_version)
    outcome = s.state.value if s is not None else c.outcome
    _put(c, f"rollout: {c.experiment_id} ended {outcome}; {c.candidate_version} rejected, "
            f"{c.production_version} stays production", stage="done", outcome=outcome)
    return _open(c.n + 1, c.production_version)


_RUN = {"listen": _listen, "judge": _judge_stage, "improve": _improve, "gate": _gate_stage,
        "replay": _replay_stage, "prove": _prove, "approval": _approval, "rollout": _rollout}


def _advance(until: Stage | None, allow_real: bool) -> Cycle:
    c = current_cycle()
    if c is None or c.stage == "done":  # a cycle that ended before opening its successor (a crash in between)
        c = _open(c.n + 1 if c else 1, production().version)
    n, last = c.n, STAGES.index(until) if until in STAGES else len(STAGES)
    while c.n == n and c.stage in _RUN and STAGES.index(c.stage) <= last:
        stage = c.stage
        if (c := _RUN[stage](c, allow_real)) is None or stage == until:
            break
    return current_cycle()


def advance(until: Stage | None = None, allow_real: bool = False) -> Cycle:
    """Run the current cycle's stages in order until it is done (cycle n+1 then waits at listen), has to
    wait (for calls, the go-ahead or an approval), is blocked, or has run `until` once (a cycle already
    past `until` does nothing). Returns the current cycle."""
    with _exclusive():
        return _advance(until, allow_real)


def approve(experiment_id: str, actor: str, approved: bool) -> ExperimentState:
    """The base approval gate (decision.approve), then the loop continues if its cycle waits on this
    experiment. KeyError: unknown experiment. ValueError: not PENDING_APPROVAL, or no actor."""
    with _exclusive():
        conn = store.connect(DB_PATH)
        try:
            state = decision.approve(conn, experiment_id, actor, approved)
        finally:
            conn.close()
        c = current_cycle()
        if c is not None and c.experiment_id == experiment_id and c.stage == "approval":
            _advance(None, allow_real=False)
        return state


# --- status and CLI -----------------------------------------------------------------------

_NEXT = {"listen": "advance: count {v}'s calls", "judge": "advance: grade {v}'s calls with the rubrics",
         "improve": "advance: rank {v}'s root causes and pick the top untried prompt or flow cause",
         "gate": "advance: draft Prompt B for {rc} and run the safety gate",
         "replay": "advance: replay {rc}'s calls with Prompt A and Prompt B (simulated conversations)",
         "prove": "advance: run {cand} on 10% of simulated traffic", "approval": "advance: read {eid}'s state",
         "rollout": "advance: apply {eid}'s decision"}


def next_action(c: Cycle | None) -> str:
    if c is None:
        return "advance: open cycle 1 on the production prompt"
    if c.notes and c.notes[-1].startswith("waiting"):
        return c.notes[-1]
    if c.stage in ("done", "blocked"):
        return c.notes[-1] if c.notes else c.stage
    return _NEXT.get(c.stage, "advance").format(v=c.production_version, rc=c.root_cause_id,
                                                cand=c.candidate_version, eid=c.experiment_id)


def status() -> str:
    vs = versions()
    lines = ["registry: " + ", ".join(f"{v.version} {v.status}" + (f" ({v.fix_id})" if v.fix_id else "") for v in vs)]
    for c in cycles():
        fields = [f"{k} {getattr(c, k)}" for k in ("root_cause_id", "fix_id", "candidate_version", "experiment_id",
                                                    "outcome") if getattr(c, k)]
        lines.append(f"cycle {c.n} on {c.production_version}: {c.stage}" + (" | " + " | ".join(fields) if fields else ""))
    if (c := current_cycle()) is not None:
        lines += [f"  - {note}" for note in c.notes]
    lines.append(f"next: {next_action(c)}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m abtest.ext.loop", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    adv = sub.add_parser("advance", help="run the current cycle's next stages")
    adv.add_argument("--until", choices=STAGES, help="stop after running this stage")
    adv.add_argument("--yes-send-real-transcripts", dest="real", action="store_true",
                     help="the user approved sending real call content to the model provider")
    sub.add_parser("status", help="the registry, the cycles and the next action")
    ok = sub.add_parser("approve", help="approve (or --reject) a PENDING_APPROVAL experiment, then continue")
    ok.add_argument("experiment_id")
    ok.add_argument("--actor", required=True, help="who decides; recorded on the decision log row")
    ok.add_argument("--reject", action="store_true")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "advance":
            advance(a.until, a.real)
        elif a.cmd == "approve":
            approve(a.experiment_id, a.actor, not a.reject)
    except (Busy, KeyError, ValueError) as e:
        print(f"error: {e.args[0] if e.args else e}", file=sys.stderr)
        return 1
    print(status())
    return 0


if __name__ == "__main__":
    sys.exit(main())
