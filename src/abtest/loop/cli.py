"""Command line for the loop: `python -m abtest.loop <command>` (PLAN_LOOP.md §L9).

    observe    transcribe and rubric-score a sample of a call corpus; write the rubric report and the audit sheet
    agreement  compare a reviewer's filled audit sheet with the judge (is the rubric trusted?)
    run        build the context and run N generations on the loop DB
    demo       three generations on the stub provider and a fresh demo DB, offline
    status     the generations table and total spend
    pause / resume   create / remove the PAUSE flag the controller checks before each generation

Every command is a thin shell around the modules the contracts name; the work and the state
machine live in controller.py. Personal data (recordings, transcripts, audit sheets, the loop DB)
stays under data/derived/loop/. The default DB is data/derived/loop/loop.db, never the base abtest.db.
"""

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

import pandas as pd

from abtest import config
from abtest.loop import controller
from abtest.loop.contracts import (DEFAULT_LOOP_CONFIG, LOOP_CONFIGS_DIR, ROOT, LoopConfig, Transcript)

DEFAULT_SEED = 20261010            # the repo-wide seed (configs_loop/default.yaml)
DEMO_LOOP_ID = "vani-loop-demo"    # keeps the demo's reports apart from a real loop's
DEMO_GENERATIONS = 3


class CliError(Exception):
    """A problem the user can fix; main() prints it without a traceback."""


def _mod(name: str):
    return controller.loop_module(name)


def _data_dir() -> Path:
    return controller.LOOP_DATA_DIR  # one place to redirect in tests


def _db_path(db: str | None) -> Path:
    return Path(db) if db else _data_dir() / "loop.db"


def _load_cfg(path: str | None) -> LoopConfig:
    p = Path(path) if path else DEFAULT_LOOP_CONFIG
    if not p.exists():
        raise CliError(f"loop config not found: {p}")
    return _mod("sarvam").load_loop_config(p)


@contextlib.contextmanager
def _env(name: str, value: str):
    """Set an environment variable for a block (the demo must stay offline whatever LOOP_PROVIDER says)."""
    old = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def _provider(cfg: LoopConfig, name: str | None, conn):
    """The provider, attached to the loop DB so its spend lands in loop_spend (and the cap is enforced on it)."""
    provider = _mod("sarvam").get_provider(cfg, name)
    if callable(attach := getattr(provider, "attach", None)):
        attach(conn)
    return provider


def build_context(cfg: LoopConfig, *, provider: str | None = None, db: str | Path | None = None,
                  quiet: bool = False) -> controller.LoopContext:
    """Everything a generation needs: DB, rubric, prices, seed personas, the production prompt's path."""
    sarvam, store, rubric = _mod("sarvam"), _mod("store_loop"), _mod("rubric")
    path = _db_path(str(db) if db else None)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = store.connect_loop(path)
    base = config.load_experiment(cfg.template).variants["A"].base
    if base is None:
        raise CliError(f"variant A of {cfg.template} has no base prompt (a full patch cannot be a loop's production prompt)")
    seeds = LOOP_CONFIGS_DIR / "personas_seed.yaml"
    if not seeds.exists():
        raise CliError(f"seed personas not found: {seeds}")
    return controller.LoopContext(
        cfg=cfg, conn=conn, provider=_provider(cfg, provider, conn),
        rubric=rubric.load_rubric(ROOT / cfg.rubric), prices=sarvam.load_prices(),
        personas=_mod("buyer_sim").load_seed_personas(seeds)[: cfg.bot_tests.personas],
        loop_id=cfg.loop.id, base_prompt_path=base, quiet=quiet)


def _close(provider, conn) -> None:
    """Close the provider, then the DB, so the WAL is checkpointed and the dashboard reads a tidy file."""
    if callable(close := getattr(provider, "close", None)):
        close()
    conn.close()


def _spend_line(conn, cfg: LoopConfig) -> str:
    return f"spend: INR {_mod('store_loop').total_spend(conn):.2f} of the INR {cfg.caps.max_spend_inr:.2f} cap"


# --- tables ------------------------------------------------------------------------------

def generations_table(gens) -> str:
    if not gens:
        return "(no generations yet)"
    return pd.DataFrame([{
        "gen": g.generation, "state": g.state, "outcome": g.terminal or "-", "base prompt": g.base_prompt_hash[:10],
        "pick": g.pick or "-", "experiment": g.experiment_id or "-", "spend INR": f"{g.spend_inr:.2f}",
        "updated": g.updated_ts.strftime("%Y-%m-%d %H:%M:%S")} for g in gens]).to_string(index=False)


def generation_line(g) -> str:
    return (f"generation {g.generation}: {g.state}, outcome {g.terminal or '-'}, pick {g.pick or '-'}, "
            f"experiment {g.experiment_id or '-'}, spend INR {g.spend_inr:.2f}")


# --- observe -----------------------------------------------------------------------------

def _collect_transcripts(out: Path, call_ids, fresh: list[Transcript]) -> list[Transcript]:
    """Transcripts of `call_ids` from the resumable file `out` plus the ones just made."""
    wanted, found = set(call_ids), {}
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            if line.strip():
                t = Transcript.model_validate_json(line)
                if t.call_id in wanted:
                    found[t.call_id] = t
    found.update({t.call_id: t for t in fresh if t.call_id in wanted})
    return [found[c] for c in call_ids if c in found]


def cmd_observe(args: argparse.Namespace) -> int:
    cfg, src = _load_cfg(args.config), Path(args.src)
    if not src.exists():
        raise CliError(f"corpus not found: {src}")
    conn = _mod("store_loop").connect_loop(_db_path(args.db))
    provider = _provider(cfg, args.provider, conn)
    try:
        rubric = _mod("rubric").load_rubric(ROOT / cfg.rubric)
        corpus = _mod("corpus").load_corpus(src)
        answered = corpus[corpus["answered"].astype(bool)]
        kept = answered[answered["duration_s"] >= cfg.observe.min_duration_s]
        if kept.empty:
            raise CliError(f"no answered call of at least {cfg.observe.min_duration_s:g} s in {src.name}")
        n = len(kept) if args.limit is None else min(args.limit, len(kept))
        sample = kept.sample(n=n, random_state=args.seed).sort_index()

        calls = _mod("transcribe").download(sample)
        out = _data_dir() / "transcripts.jsonl"
        fresh = _mod("transcribe").transcribe(calls, provider, rubric=rubric,
                                              min_duration_s=cfg.observe.min_duration_s, out=out)
        transcripts = _collect_transcripts(out, list(sample["call_id"]), fresh)
        failed = int(calls["audio_path"].isna().sum()) if "audio_path" in calls else 0
        if not transcripts:
            raise CliError(f"no call was transcribed ({failed} of {len(sample)} downloads failed)")
        store = _mod("store_loop")
        for t in transcripts:
            store.put_transcript(conn, t)

        scores = _mod("scorer").score_calls(transcripts, rubric, provider)
        lead_of = dict(zip(sample["call_id"], sample["lead_id"]))
        for s in scores:
            store.put_score(conn, s, str(lead_of[s.call_id]))
        frame = _mod("scorer").scores_frame(scores)
        per_lead = _mod("scorer").per_lead(frame, sample)

        name = src.stem
        report = _mod("report").rubric_report(per_lead, corpus, rubric, name)
        audit_path = _mod("audit").export_audit(frame, sample, min(cfg.audit.corpus_calls, len(frame)),
                                                args.seed, _data_dir() / "audit" / f"{name}.csv")
        print(f"corpus {src.name}: {len(corpus)} calls, {len(answered)} answered, {len(kept)} of at least "
              f"{cfg.observe.min_duration_s:g} s ({len(answered) - len(kept)} shorter, not scored); sample {len(sample)}")
        print(f"downloads failed {failed}; transcribed {len(transcripts)}; scored {len(scores)}")
        print(f"rubric report: {controller.rel_path(Path(report))}")
        print(f"audit sheet:   {controller.rel_path(Path(audit_path))}")
        print(_spend_line(conn, cfg))
        return 0
    finally:
        _close(provider, conn)


# --- agreement ---------------------------------------------------------------------------

def cmd_agreement(args: argparse.Namespace) -> int:
    cfg, sheet = _load_cfg(args.config), Path(args.sheet)
    if not sheet.exists():
        raise CliError(f"audit sheet not found: {sheet}")
    rubric = _mod("rubric").load_rubric(ROOT / cfg.rubric)
    result = _mod("audit").agreement(sheet, rubric, cfg.audit.min_rho, cfg.audit.min_hard_agreement)
    print(json.dumps(result, indent=2, default=str))
    print("TRUSTED" if result.get("trusted") else "NOT TRUSTED")
    return 0


# --- run, demo, status -------------------------------------------------------------------

def cmd_run(args: argparse.Namespace) -> int:
    if args.generations < 1:
        raise CliError("--generations must be at least 1")
    cfg = _load_cfg(args.config)
    ctx = build_context(cfg, provider=args.provider, db=args.db, quiet=args.quiet)
    try:
        gens, reason = controller.run_until_stop(ctx, args.generations)
        for g in gens:
            print(generation_line(g))
        print(f"stopped: {reason}")
        print(_spend_line(ctx.conn, cfg))
        return 0
    finally:
        _close(ctx.provider, ctx.conn)


def cmd_demo(args: argparse.Namespace) -> int:
    cfg = _load_cfg(args.config)
    # Four bot turns: the stub bot only shows the closure fix from its fourth turn, so the demo can
    # promote two fixes in a row (looping, then closure) and show the improvement compounding.
    cfg = cfg.model_copy(update={"loop": cfg.loop.model_copy(update={"id": DEMO_LOOP_ID}),
                                 "bot_tests": cfg.bot_tests.model_copy(update={"max_turns": 4})})
    db = _data_dir() / "demo.db"
    for suffix in ("", "-wal", "-shm"):  # a fresh DB every time
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    with _env("LOOP_PROVIDER", "stub"):  # offline whatever the environment says
        ctx = build_context(cfg, provider="stub", db=db, quiet=args.quiet)
    try:
        gens, reason = controller.run_until_stop(ctx, DEMO_GENERATIONS)
        print(generations_table(gens))
        print(f"stopped: {reason}")
        print(_spend_line(ctx.conn, cfg))
        print(f"demo DB {controller.rel_path(db)}; reports data/derived/loop/reports/generation_{DEMO_LOOP_ID}_<n>.md")
        if not any(g.terminal == "PROMOTED" for g in gens):
            print("demo: no generation was promoted", file=sys.stderr)
            return 1
        return 0
    finally:
        _close(ctx.provider, ctx.conn)


def _loop_ids(conn, default: str) -> list[str]:
    ids = [r[0] for r in conn.execute("SELECT DISTINCT loop_id FROM loop_generations ORDER BY loop_id")]
    return sorted(ids, key=lambda i: (i != default, i)) or [default]


def cmd_status(args: argparse.Namespace) -> int:
    cfg, path = _load_cfg(args.config), _db_path(args.db)
    if not path.exists():
        raise CliError(f"no loop database at {path}")
    conn = _mod("store_loop").connect_loop(path)
    try:
        for loop_id in _loop_ids(conn, cfg.loop.id):
            print(f"loop {loop_id}")
            print(generations_table(_mod("store_loop").get_generations(conn, loop_id)))
        print(_spend_line(conn, cfg))
    finally:
        conn.close()
    print("PAUSED" if controller.is_paused() else "not paused")
    return 0


def cmd_pause(args: argparse.Namespace) -> int:
    flag = controller.pause_flag()
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.touch()
    print(f"paused: {controller.rel_path(flag)} created; no new generation starts until `resume`")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    controller.pause_flag().unlink(missing_ok=True)
    print("resumed: PAUSE flag removed")
    if os.environ.get("ABT_LOOP_PAUSE") == "1":
        print("warning: ABT_LOOP_PAUSE=1 is still set in this environment and also pauses the loop", file=sys.stderr)
    return 0


# --- parser ------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m abtest.loop", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, func, help: str, *, cfg: bool = True, db: bool = False, provider: bool = False):
        s = sub.add_parser(name, help=help)
        s.set_defaults(func=func)
        if cfg:
            s.add_argument("--config", help=f"loop config (default {controller.rel_path(DEFAULT_LOOP_CONFIG)})")
        if db:
            s.add_argument("--db", help="loop database (default data/derived/loop/loop.db)")
        if provider:
            s.add_argument("--provider", choices=["stub", "sarvam"], help="override the config's provider "
                           "(env LOOP_PROVIDER overrides both)")
        return s

    s = add("observe", cmd_observe, "transcribe and score a corpus sample; write the report and audit sheet",
            db=True, provider=True)
    s.add_argument("--src", required=True, help="corpus CSV of answered calls with recording URLs")
    s.add_argument("--limit", type=int, help="seeded sample size (default: every call of at least min_duration_s)")
    s.add_argument("--seed", type=int, default=DEFAULT_SEED)

    s = add("agreement", cmd_agreement, "judge-vs-human agreement on a filled audit sheet")
    s.add_argument("--sheet", required=True, help="the audit CSV the reviewer filled")

    s = add("run", cmd_run, "run generations on the loop DB", db=True, provider=True)
    s.add_argument("--generations", type=int, default=1)
    s.add_argument("--quiet", action="store_true", help="one line per generation only")

    s = add("demo", cmd_demo, "three generations, stub provider, fresh demo DB, offline")
    s.add_argument("--quiet", action="store_true")

    add("status", cmd_status, "generations and spend", db=True)
    add("pause", cmd_pause, "create the PAUSE flag", cfg=False)
    add("resume", cmd_resume, "remove the PAUSE flag", cfg=False)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except controller.spend_cap_exception() as e:
        print(f"stopped at the spend cap: {e}", file=sys.stderr)
        return 3
    except controller.provider_error() as e:  # a generation it cut short is re-run by the next `run`
        print(f"provider error: {e}", file=sys.stderr)
        return 4
    except ModuleNotFoundError as e:
        if e.name and e.name.startswith("abtest.loop."):
            print(f"error: loop module {e.name.rsplit('.', 1)[-1]} is not built yet", file=sys.stderr)
            return 2
        raise
