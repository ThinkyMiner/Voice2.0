"""The loop's command line (`python -m abtest.loop`) and its dashboard page (PLAN_LOOP.md §L9).

The modules a command calls are swapped for small fakes through sys.modules (store_loop is the real one on a
temp SQLite DB), so these tests pass whatever the other loop modules are doing. No test touches the network.
"""

import os
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import yaml

from abtest.contracts import AppendPatch
from abtest.loop import cli, controller
from abtest.loop.contracts import (DEFAULT_LOOP_CONFIG, ROOT, Candidate, Generation, JudgeAnswer, LoopConfig,
                                   PersonaCard, Prices, Rubric, RubricScore, SpendRow, Transcript, Turn)

store = pytest.importorskip("abtest.loop.store_loop")

NOW = datetime(2026, 10, 10, 9, 30)


class FakeProvider:
    name = "fake"

    def __init__(self, chosen: str | None):
        self.chosen, self.attached, self.closed = chosen, None, False

    def attach(self, conn):
        self.attached = conn

    def close(self):
        self.closed = True


class Env:
    """Fake sibling modules and the records of what the CLI asked them to do."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self.tmp, self.mp = tmp_path, monkeypatch
        self.data = tmp_path / "data"
        monkeypatch.setattr(controller, "LOOP_DATA_DIR", self.data)
        monkeypatch.setattr(controller, "LOOP_REPORTS_DIR", tmp_path / "reports")
        monkeypatch.delenv("ABT_LOOP_PAUSE", raising=False)
        monkeypatch.delenv("LOOP_PROVIDER", raising=False)
        self.cfg = LoopConfig.model_validate(yaml.safe_load(DEFAULT_LOOP_CONFIG.read_text(encoding="utf-8")))
        self.provider: FakeProvider | None = None
        self.seen: dict[str, object] = {}
        self.already = set()  # call ids the fake transcriber finds already in the resumable file
        self.corpus = self._corpus()
        self.trusted = True
        self._install()

    @staticmethod
    def _corpus() -> pd.DataFrame:
        n = 12
        return pd.DataFrame({
            "call_id": [f"c{i:02d}" for i in range(n)], "lead_id": [f"L{i // 2}" for i in range(n)],
            "ts": pd.date_range("2026-10-06", periods=n, freq="h"), "answered": [i < 10 for i in range(n)],
            "disposition": None, "duration_s": [5.0 if i < 4 else 30.0 for i in range(n)], "attempt_no": None,
            "bot_version": "personalised_vani", "hotlead_type": "hot", "recording_url": "https://example.test/x.mp3"})

    def _install(self) -> None:
        def module(name: str, **attrs):
            mod = types.ModuleType(f"abtest.loop.{name}")
            for k, v in attrs.items():
                setattr(mod, k, v)
            self.mp.setitem(sys.modules, mod.__name__, mod)
            self.mp.setattr(sys.modules["abtest.loop"], name, mod, raising=False)

        class SpendCapExceeded(RuntimeError):
            pass

        class ProviderError(RuntimeError):
            pass

        self.SpendCapExceeded, self.ProviderError = SpendCapExceeded, ProviderError

        def get_provider(cfg, name=None):
            self.provider = FakeProvider(name)
            self.seen["provider_env"] = os.environ.get("LOOP_PROVIDER")
            return self.provider

        module("sarvam", SpendCapExceeded=SpendCapExceeded, ProviderError=ProviderError, get_provider=get_provider,
               load_loop_config=lambda path=DEFAULT_LOOP_CONFIG: self._load_cfg(path),
               load_prices=lambda path=None: Prices(currency="INR", llm={}, stt={}))
        module("rubric", load_rubric=lambda path: Rubric.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8"))))
        module("buyer_sim", load_seed_personas=lambda path: [
            PersonaCard(card_id=f"p{i}", source_call_id="s", requirement="r", render_vars={}, language="hi", tone="t")
            for i in range(5)])
        module("corpus", load_corpus=lambda path: self.corpus.copy())
        module("transcribe", download=self._download, transcribe=self._transcribe)
        module("scorer", score_calls=self._score_calls, scores_frame=self._scores_frame, per_lead=self._per_lead)
        module("report", rubric_report=self._report)
        module("audit", export_audit=self._export_audit, agreement=self._agreement)

    # --- fakes --------------------------------------------------------------------------------

    def _load_cfg(self, path) -> LoopConfig:
        self.seen["config_path"] = Path(path)
        return self.cfg

    def _download(self, calls, out_dir=None):
        self.seen["download"] = list(calls["call_id"])
        return calls.assign(audio_path="a.mp3", download_error=None)

    def _transcribe(self, calls, provider, rubric=None, min_duration_s=10.0, out=None):
        self.seen["transcribe"] = {"ids": list(calls["call_id"]), "min": min_duration_s, "out": out,
                                   "rubric": rubric.id if rubric else None, "provider": provider}
        made = [Transcript(call_id=cid, source="corpus", language="hi-IN", role_confidence=0.9, stt_model="m",
                           audio_seconds=30.0, turns=(Turn(role="bot", start_s=0, end_s=1, text="namaste"),))
                for cid in calls["call_id"]]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(t.model_dump_json() for t in made) + "\n", encoding="utf-8")
        return [t for t in made if t.call_id not in self.already]

    def _score_calls(self, transcripts, rubric, provider):
        answers = tuple(JudgeAnswer(criterion_id=c.id, value=0.2 if c.kind == "yes_no" else 4.0, provider="fake")
                        for c in rubric.criteria)
        return [RubricScore(call_id=t.call_id, rubric_hash="rh", judge_model="fake", answers=answers,
                            rubric_score=0.7, rubric_risk=0.2, failed=()) for t in transcripts]

    def _scores_frame(self, scores):
        return pd.DataFrame({"call_id": [s.call_id for s in scores], "rubric_score": [s.rubric_score for s in scores]})

    def _per_lead(self, scores, calls):
        self.seen["per_lead_calls"] = list(calls["call_id"])
        merged = scores.merge(calls[["call_id", "lead_id"]], on="call_id")
        return merged.groupby("lead_id", as_index=False)["rubric_score"].mean()

    def _report(self, per_lead, calls, rubric, corpus_name, out_dir=None):
        self.seen["report"] = {"leads": len(per_lead), "corpus_calls": len(calls), "name": corpus_name}
        path = self.tmp / "reports" / f"rubric_{corpus_name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("report", encoding="utf-8")
        return path

    def _export_audit(self, scores, calls, n, seed, out):
        self.seen["audit"] = {"n": n, "seed": seed, "out": out, "scored": len(scores)}
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("sheet", encoding="utf-8")
        return out

    def _agreement(self, sheet, rubric, min_rho, min_hard_agreement):
        self.seen["agreement"] = (sheet, rubric.id, min_rho, min_hard_agreement)
        return {"rho": 0.71, "rho_ci95": [0.5, 0.83], "n": 60, "role_accuracy": 0.95, "trusted": self.trusted}

    def seed_personas(self, monkeypatch) -> None:
        """Point the CLI at a seed-personas file (the real one is another agent's)."""
        seeds = self.tmp / "configs"
        seeds.mkdir(exist_ok=True)
        (seeds / "personas_seed.yaml").write_text("personas: []\n", encoding="utf-8")
        monkeypatch.setattr(cli, "LOOP_CONFIGS_DIR", seeds)

    def db(self) -> Path:
        return self.tmp / "loop.db"


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    return Env(tmp_path, monkeypatch)


def gen(n: int, state: str = "GEN_END", terminal: str | None = "PROMOTED", **kw) -> Generation:
    return Generation(generation=n, loop_id=kw.pop("loop_id", "vani-loop"), state=state, terminal=terminal,
                      base_prompt_hash="ab" * 32, rubric_hash="cd" * 32, started_ts=NOW, updated_ts=NOW, **kw)


def run(*argv: str) -> int:
    return cli.main(list(argv))


# --- pause, resume ----------------------------------------------------------------------------

def test_pause_and_resume_toggle_the_flag(env, capsys):
    assert run("pause") == 0
    assert (env.data / "PAUSE").exists() and controller.is_paused()
    assert "paused" in capsys.readouterr().out

    assert run("resume") == 0
    assert not (env.data / "PAUSE").exists() and not controller.is_paused()
    assert run("resume") == 0  # idempotent


def test_resume_warns_when_the_env_flag_still_pauses(env, capsys, monkeypatch):
    monkeypatch.setenv("ABT_LOOP_PAUSE", "1")
    assert run("resume") == 0
    assert "ABT_LOOP_PAUSE=1 is still set" in capsys.readouterr().err


# --- status -----------------------------------------------------------------------------------

def test_status_lists_every_loop_and_the_spend(env, capsys):
    conn = store.connect_loop(env.db())
    store.put_generation(conn, gen(1, terminal="NO_CANDIDATE"))
    store.put_generation(conn, gen(2, pick="g2-c0", experiment_id="vani-loop-g2"))
    store.put_generation(conn, gen(1, loop_id="vani-loop-demo"))
    store.reserve_spend(conn, SpendRow(spend_id="s", generation=1, step="gate", state="reserved", estimate_inr=12.5))
    conn.close()

    assert run("status", "--db", str(env.db())) == 0
    out = capsys.readouterr().out
    assert out.index("loop vani-loop\n") < out.index("loop vani-loop-demo")  # the configured loop first
    assert "NO_CANDIDATE" in out and "PROMOTED" in out and "g2-c0" in out and "vani-loop-g2" in out
    assert "spend: INR 12.50 of the INR 150.00 cap" in out
    assert out.rstrip().endswith("not paused")


def test_status_says_so_without_a_database(env, capsys):
    assert run("status", "--db", str(env.tmp / "missing.db")) == 2
    assert "no loop database" in capsys.readouterr().err


def test_status_defaults_to_the_loop_db_not_the_base_db(env, capsys):
    env.data.mkdir(parents=True)
    store.connect_loop(env.data / "loop.db").close()
    assert run("status") == 0
    assert "(no generations yet)" in capsys.readouterr().out


# --- run --------------------------------------------------------------------------------------

def test_run_prints_one_line_per_generation(env, monkeypatch, capsys):
    conn = store.connect_loop(env.db())
    ctx = types.SimpleNamespace(conn=conn, cfg=env.cfg, provider=FakeProvider("stub"))
    seen = {}

    def build(cfg, **kw):
        seen["build"] = kw
        return ctx

    def until_stop(c, n):
        seen["generations"] = n
        return [gen(1), gen(2, terminal="STOPPED_HARM", pick="g2-c0", experiment_id="e2")], "ran the 2 requested generation(s)"

    monkeypatch.setattr(cli, "build_context", build)
    monkeypatch.setattr(controller, "run_until_stop", until_stop)
    assert run("run", "--generations", "2", "--provider", "stub", "--db", "x.db", "--quiet") == 0

    assert seen == {"build": {"provider": "stub", "db": "x.db", "quiet": True}, "generations": 2}
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("generation 1: GEN_END, outcome PROMOTED")
    assert out[1].startswith("generation 2: GEN_END, outcome STOPPED_HARM, pick g2-c0, experiment e2")
    assert out[2] == "stopped: ran the 2 requested generation(s)" and out[3].startswith("spend: INR 0.00")
    assert ctx.provider.closed


def test_run_defaults_to_one_generation_and_closes_the_provider_on_error(env, monkeypatch):
    ctx = types.SimpleNamespace(conn=store.connect_loop(env.db()), cfg=env.cfg, provider=FakeProvider(None))
    monkeypatch.setattr(cli, "build_context", lambda cfg, **kw: ctx)
    calls = []

    def boom(c, n):
        calls.append(n)
        raise RuntimeError("boom")

    monkeypatch.setattr(controller, "run_until_stop", boom)
    with pytest.raises(RuntimeError):
        run("run")
    assert calls == [1] and ctx.provider.closed


def test_the_spend_cap_is_a_clean_exit(env, monkeypatch, capsys):
    def build(cfg, **kw):
        raise env.SpendCapExceeded("cap")

    monkeypatch.setattr(cli, "build_context", build)
    assert run("run") == 3
    assert "stopped at the spend cap: cap" in capsys.readouterr().err


def test_a_provider_error_is_a_clean_exit_and_never_the_spend_cap_code(env, monkeypatch, capsys):
    def build(cfg, **kw):
        raise env.ProviderError("SARVAM_API_KEY is not set")

    monkeypatch.setattr(cli, "build_context", build)
    assert run("run") == 4
    assert "provider error: SARVAM_API_KEY is not set" in capsys.readouterr().err


def test_generations_must_be_positive(env, capsys):
    assert run("run", "--generations", "0") == 2
    assert "at least 1" in capsys.readouterr().err


def test_a_module_that_is_not_built_yet_is_a_clean_error(env, monkeypatch, capsys):
    env.seed_personas(monkeypatch)
    monkeypatch.setitem(sys.modules, "abtest.loop.buyer_sim", None)  # import raises ModuleNotFoundError
    assert run("run") == 2
    assert "loop module buyer_sim is not built yet" in capsys.readouterr().err


# --- build_context ----------------------------------------------------------------------------

def test_build_context_assembles_everything_a_generation_needs(env, monkeypatch):
    env.seed_personas(monkeypatch)
    ctx = cli.build_context(env.cfg, provider="stub", db=env.db(), quiet=True)

    assert env.db().exists() and ctx.loop_id == env.cfg.loop.id and ctx.quiet
    assert ctx.base_prompt_path == "prompts/base_buyer_vani.txt"  # variant A's base in the template
    assert len(ctx.personas) == env.cfg.bot_tests.personas == 3  # 5 seeds, limited to the config
    assert env.provider.chosen == "stub" and env.provider.attached is ctx.conn  # spend lands in loop_spend
    assert ctx.rubric.id == "vani-call-quality-v1"


def test_the_default_db_is_the_loop_db(env, monkeypatch):
    env.seed_personas(monkeypatch)
    cli.build_context(env.cfg)
    assert (env.data / "loop.db").exists()


def test_missing_seed_personas_is_a_clean_error(env, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "LOOP_CONFIGS_DIR", tmp_path / "nowhere")
    assert run("run", "--db", str(env.db())) == 2
    assert "seed personas not found" in capsys.readouterr().err


# --- demo -------------------------------------------------------------------------------------

def _demo_fakes(env, monkeypatch, gens):
    seen = {}

    def build(cfg, **kw):
        seen.update(kw, loop_id=cfg.loop.id, env=os.environ.get("LOOP_PROVIDER"))
        return types.SimpleNamespace(conn=store.connect_loop(env.db()), cfg=cfg, provider=FakeProvider("stub"))

    def until_stop(ctx, n):
        seen["n"] = n
        return gens, "done"

    monkeypatch.setattr(cli, "build_context", build)
    monkeypatch.setattr(controller, "run_until_stop", until_stop)
    return seen


def test_demo_uses_a_fresh_db_and_the_stub_provider_whatever_the_environment_says(env, monkeypatch, capsys):
    env.data.mkdir(parents=True)
    for suffix in ("", "-wal", "-shm"):  # leftovers of an earlier demo: not a database at all
        (env.data / f"demo.db{suffix}").write_text("junk", encoding="utf-8")
    monkeypatch.setenv("LOOP_PROVIDER", "sarvam")
    seen = _demo_fakes(env, monkeypatch, [gen(1), gen(2, terminal="NO_CANDIDATE"), gen(3, terminal="NO_CANDIDATE")])

    assert run("demo") == 0

    assert not any((env.data / f"demo.db{s}").exists() for s in ("", "-wal", "-shm"))
    assert seen["provider"] == "stub" and seen["env"] == "stub" and seen["db"] == env.data / "demo.db"
    assert seen["loop_id"] == "vani-loop-demo" and seen["n"] == 3
    assert os.environ["LOOP_PROVIDER"] == "sarvam"  # restored
    out = capsys.readouterr().out
    assert "PROMOTED" in out and out.count("NO_CANDIDATE") == 2 and "generation_vani-loop-demo_<n>.md" in out


def test_demo_fails_loudly_when_nothing_was_promoted(env, monkeypatch, capsys):
    _demo_fakes(env, monkeypatch, [gen(1, terminal="NO_CANDIDATE")])
    assert run("demo") == 1
    assert "no generation was promoted" in capsys.readouterr().err


# --- observe ----------------------------------------------------------------------------------

def test_observe_samples_transcribes_scores_and_writes_the_report_and_audit_sheet(env, capsys, tmp_path):
    src = tmp_path / "squad_sample.csv"
    src.write_text("x", encoding="utf-8")
    env.already = {"c04"}  # one transcript is already in the resumable file, so transcribe() does not return it
    assert run("observe", "--src", str(src), "--limit", "4", "--seed", "7", "--db", str(env.db())) == 0

    kept = {f"c{i:02d}" for i in range(4, 10)}  # answered and >= 10 s: c10, c11 unanswered; c00-c03 short
    sample = env.seen["download"]
    assert len(sample) == 4 and set(sample) <= kept
    assert env.seen["transcribe"]["ids"] == sample and env.seen["transcribe"]["min"] == 10.0
    assert env.seen["transcribe"]["out"] == env.data / "transcripts.jsonl"
    assert env.seen["transcribe"]["rubric"] == "vani-call-quality-v1"
    assert env.provider.chosen is None and env.provider.attached is not None and env.provider.closed

    conn = store.connect_loop(env.db())
    assert sorted(t.call_id for t in store.get_transcripts(conn)) == sorted(sample)  # incl. the resumed one
    rows = conn.execute("SELECT call_id, lead_id FROM loop_rubric_scores ORDER BY call_id").fetchall()
    assert [(r[0], r[1]) for r in rows] == sorted((c, f"L{int(c[1:]) // 2}") for c in sample)  # lead from the corpus

    assert env.seen["per_lead_calls"] == sample
    assert env.seen["report"]["name"] == "squad_sample" and env.seen["report"]["corpus_calls"] == 12  # the whole corpus
    assert env.seen["audit"]["n"] == 4 and env.seen["audit"]["seed"] == 7  # capped by the sample (cap is 60)
    assert env.seen["audit"]["out"] == env.data / "audit" / "squad_sample.csv"
    out = capsys.readouterr().out
    assert "12 calls, 10 answered, 6 of at least 10 s (4 shorter, not scored); sample 4" in out
    assert "transcribed 4; scored 4" in out and "rubric_squad_sample.md" in out and "squad_sample.csv" in out
    assert "spend: INR 0.00 of the INR 150.00 cap" in out


def test_observe_sample_is_seeded(env, tmp_path):
    src = tmp_path / "c.csv"
    src.write_text("x", encoding="utf-8")
    picks = []
    for seed in ("1", "1", "2", "3", "4", "5"):
        run("observe", "--src", str(src), "--limit", "3", "--seed", seed, "--db", str(env.tmp / f"{len(picks)}.db"))
        picks.append(env.seen["download"])
    assert picks[0] == picks[1]
    assert len({tuple(p) for p in picks}) > 1  # another seed, another sample


def test_observe_without_a_limit_takes_every_kept_call_and_audit_caps_at_the_config(env, tmp_path):
    src = tmp_path / "c.csv"
    src.write_text("x", encoding="utf-8")
    env.cfg = env.cfg.model_copy(update={"audit": env.cfg.audit.model_copy(update={"corpus_calls": 2})})
    assert run("observe", "--src", str(src), "--db", str(env.db())) == 0
    assert len(env.seen["download"]) == 6 and env.seen["audit"]["n"] == 2


@pytest.mark.parametrize("what, message", [("src", "corpus not found"), ("config", "loop config not found"),
                                           ("nothing_kept", "no answered call of at least")])
def test_observe_errors_are_clean(env, tmp_path, capsys, what, message):
    src = tmp_path / "c.csv"
    src.write_text("x", encoding="utf-8")
    argv = ["observe", "--src", str(src), "--db", str(env.db())]
    if what == "src":
        argv[2] = str(tmp_path / "nope.csv")
    elif what == "config":
        argv += ["--config", str(tmp_path / "nope.yaml")]
    else:
        env.corpus["duration_s"] = 1.0
    assert run(*argv) == 2
    assert message in capsys.readouterr().err


def test_observe_reports_when_no_download_worked(env, tmp_path, monkeypatch, capsys):
    src = tmp_path / "c.csv"
    src.write_text("x", encoding="utf-8")
    fake = sys.modules["abtest.loop.transcribe"]
    monkeypatch.setattr(fake, "download", lambda calls, out_dir=None: calls.assign(audio_path=None, download_error="404"))
    monkeypatch.setattr(fake, "transcribe", lambda *a, **k: [])
    assert run("observe", "--src", str(src), "--limit", "2", "--db", str(env.db())) == 2
    assert "no call was transcribed (2 of 2 downloads failed)" in capsys.readouterr().err


def test_observe_on_the_real_modules_and_the_stub_provider(tmp_path, monkeypatch, capsys):
    """CSV -> corpus -> (cached) recordings -> stub STT -> roles -> stub judge -> report and audit sheet."""
    for name in ("corpus", "transcribe", "roles", "scorer", "report", "audit", "rubric", "sarvam"):
        pytest.importorskip(f"abtest.loop.{name}", reason=f"loop/{name}.py not built yet")
    ids = sorted(f.stem for f in (ROOT / "tests" / "loop" / "fixtures" / "stt").glob("*.json"))
    if not ids:
        pytest.skip("no stub STT fixtures yet")
    from abtest.loop import report, transcribe

    recordings = tmp_path / "rec"
    recordings.mkdir()
    for cid in ids:  # already "downloaded": the download step skips them, so no network
        (recordings / f"{cid}.mp3").write_bytes(b"x")
    src = tmp_path / "squad_sample.csv"
    pd.DataFrame({"glid": [f"G{i}" for i in range(len(ids))], "call_id": ids, "bot": "personalised_vani",
                  "call_status": "Answered", "duration_sec": 30, "call_start_ist": "2026-10-06 10:00:00",
                  "hotlead_type": "hot", "recording_url": "https://example.test/x.mp3"}).to_csv(src, index=False)
    download, rubric_report = transcribe.download, report.rubric_report
    monkeypatch.setattr(transcribe, "download", lambda calls, out_dir=None: download(calls, out_dir=recordings))
    monkeypatch.setattr(report, "rubric_report", lambda pl, calls, rubric, name, out_dir=None:
                        rubric_report(pl, calls, rubric, name, out_dir=tmp_path / "reports"))
    monkeypatch.setattr(controller, "LOOP_DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("LOOP_PROVIDER", "stub")

    assert run("observe", "--src", str(src), "--db", str(tmp_path / "loop.db")) == 0

    out = capsys.readouterr().out
    assert f"transcribed {len(ids)}" in out and "downloads failed 0" in out
    assert (tmp_path / "reports" / "rubric_squad_sample.md").exists()
    assert (tmp_path / "data" / "audit" / "squad_sample.csv").exists()
    conn = store.connect_loop(tmp_path / "loop.db")
    assert sorted(t.call_id for t in store.get_transcripts(conn)) == ids
    assert conn.execute("SELECT COUNT(*) FROM loop_rubric_scores").fetchone()[0] >= 1
    assert store.total_spend(conn) == 0.0


# --- agreement --------------------------------------------------------------------------------

@pytest.mark.parametrize("trusted, verdict", [(True, "TRUSTED"), (False, "NOT TRUSTED")])
def test_agreement_prints_the_result_and_the_verdict(env, tmp_path, capsys, trusted, verdict):
    sheet = tmp_path / "audit.csv"
    sheet.write_text("x", encoding="utf-8")
    env.trusted = trusted
    assert run("agreement", "--sheet", str(sheet)) == 0

    assert env.seen["agreement"] == (sheet, "vani-call-quality-v1", env.cfg.audit.min_rho,
                                     env.cfg.audit.min_hard_agreement)
    out = capsys.readouterr().out
    assert '"rho": 0.71' in out and out.rstrip().endswith(verdict)


def test_agreement_needs_the_sheet(env, tmp_path, capsys):
    assert run("agreement", "--sheet", str(tmp_path / "nope.csv")) == 2
    assert "audit sheet not found" in capsys.readouterr().err


# --- entry point ------------------------------------------------------------------------------

def test_python_dash_m_runs_the_cli():
    proc = subprocess.run([sys.executable, "-m", "abtest.loop", "--help"], cwd=ROOT, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0
    assert all(cmd in proc.stdout for cmd in ("observe", "agreement", "run", "demo", "status", "pause", "resume"))


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2 and "required" in capsys.readouterr().err


# --- dashboard page ---------------------------------------------------------------------------

PAGE = ROOT / "dashboard" / "pages" / "4_Loop.py"


def _page(monkeypatch, db: Path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.syspath_prepend(str(ROOT / "dashboard"))  # the page does `import common`
    monkeypatch.setenv("ABT_LOOP_DB", str(db))
    return AppTest.from_file(str(PAGE), default_timeout=60).run()


def _text(at) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_the_page_says_so_without_a_database(monkeypatch, tmp_path):
    at = _page(monkeypatch, tmp_path / "none.db")
    assert not at.exception
    assert "No loop database" in at.info[0].value
    assert "Self-improving loop: simulated traffic for the A/B; real Sarvam models for scoring, proposing and bot tests" in _text(at)


def test_the_page_says_so_with_an_empty_database(monkeypatch, tmp_path):
    store.connect_loop(tmp_path / "e.db").close()
    at = _page(monkeypatch, tmp_path / "e.db")
    assert not at.exception and "no generations yet" in at.info[0].value


def test_the_page_shows_generations_candidates_spend_and_the_guard_log(monkeypatch, tmp_path):
    path = tmp_path / "loop.db"
    conn = store.connect_loop(path)
    store.put_generation(conn, gen(1, pick="g1-c0", experiment_id="vani-loop-g1"))
    store.put_generation(conn, gen(2, state="GEN_END", terminal="STOPPED_HARM", pick="g2-c0",
                                   experiment_id="vani-loop-g2", spend_inr=3.5))
    ok = {"applies": True, "jinja_parses": True, "renders": True, "within_size": True}
    for g, status, score in ((1, "promoted", 0.12), (2, "not_promoted", 0.04)):
        store.put_candidate(conn, Candidate(
            candidate_id=f"g{g}-c0", generation=g, patch=AppendPatch(mode="append", text=f"Rule {g}: confirm the next step."),
            patch_hash=f"h{g}", rationale="why", targets=("closure",), prechecks=ok, gate={"alters_commitments": 0.07},
            bot_test_score=score, bot_test_ci=(score - 0.05, score + 0.05), status=status), "vani-loop")
    for look, (base, final, by) in enumerate([("CONTINUE", "CONTINUE", False), ("PROMOTE", "STOPPED_HARM", True)], 1):
        store.put_guard_log(conn, "vani-loop-g2", look, {
            "base_verdict": {"decision": base, "rule": 1, "reason": "r"},
            "final_verdict": {"decision": final, "rule": 2, "reason": "rubric_risk RCI beyond the margin"},
            "rubric_decided": by, "template_guardrail": {"delta": 0.001, "rci": [-0.01, 0.02]},
            "rubric_guardrail": {"delta": 0.06, "rci": [0.052, 0.07], "margin": 0.05}})
    store.reserve_spend(conn, SpendRow(spend_id="s", generation=1, step="gate", state="reserved", estimate_inr=12.5))
    conn.close()

    at = _page(monkeypatch, path)

    assert not at.exception and not at.error
    text = _text(at)
    assert "INR 12.50" in text and "of the INR 150.00 cap" in text and "1 of 2" in text  # spend, promoted
    gens, cands, guard = (at.dataframe[i].value for i in range(3))
    assert list(gens["Gen"]) == [1, 2] and list(gens["Outcome"]) == ["PROMOTED", "STOPPED_HARM"]
    assert list(cands["Candidate"]) == ["g2-c0"] and list(cands["Status"]) == ["not_promoted"]  # the latest generation's
    assert cands["Bot-test score"][0] == "+0.040" and cands["Gate (max p)"][0] == "0.07"
    assert list(guard["Decided by"]) == ["engine", "rubric guardrail"] and list(guard["Decision"]) == ["CONTINUE", "STOPPED_HARM"]
    assert "[+0.052, +0.070]" in list(guard["rubric_risk RCI"])
    assert [e.label for e in at.expander] == ["Patch of g2-c0 (not_promoted)"]


def test_the_page_never_writes_to_the_database(monkeypatch, tmp_path):
    path = tmp_path / "loop.db"
    conn = store.connect_loop(path)
    store.put_generation(conn, gen(1))
    conn.close()
    before = path.read_bytes()
    at = _page(monkeypatch, path)
    assert not at.exception and path.read_bytes() == before
