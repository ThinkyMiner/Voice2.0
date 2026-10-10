"""Loop tables: connect, round trips, spend reserve/settle, rubric-resolved goodness (PLAN_LOOP §L2.9)."""

import sqlite3
from datetime import datetime

import pytest
import yaml

from abtest.contracts import AppendPatch, ReplaceTextPatch
from abtest.loop import store_loop
from abtest.loop.contracts import (RUBRICS_DIR, Candidate, Criterion, Generation,
                                   JudgeAnswer, JudgeSettings, Rubric, RubricScore, SpendRow, Transcript, Turn)

LOOP_TABLES = {"loop_schema_version", "loop_transcripts", "loop_rubric_scores", "loop_generations",
               "loop_candidates", "loop_spend", "loop_guard_log"}


@pytest.fixture
def conn():
    c = store_loop.connect_loop(":memory:")
    yield c
    c.close()


@pytest.fixture
def default_rubric():
    return Rubric.model_validate(yaml.safe_load((RUBRICS_DIR / "vani-call-quality-v1.yaml").read_text()))


def _transcript(call_id="c1", source="corpus", text="namaste"):
    return Transcript(call_id=call_id, source=source, language="hi-IN", role_confidence=0.9, stt_model="m",
                      audio_seconds=12.0,
                      turns=(Turn(role="bot", start_s=0, end_s=2, text=text), Turn(role="buyer", start_s=2, end_s=3, text="ji")))


def _answers(rubric, **values):
    """Answers for every criterion: yes_no -> 0.1, score_1_5 -> 3, with overrides."""
    return tuple(JudgeAnswer(criterion_id=c.id, provider="stub",
                             value=values.get(c.id, 0.1 if c.kind == "yes_no" else 3.0)) for c in rubric.criteria)


def _score(rubric, call_id, rubric_hash, score=0.7, risk=0.2, failed=(), **values):
    return RubricScore(call_id=call_id, rubric_hash=rubric_hash, judge_model="stub", answers=_answers(rubric, **values),
                       rubric_score=score, rubric_risk=risk, failed=failed)


def _generation(n=1, state="GEN_START", loop_id="L"):
    now = datetime(2026, 10, 10, 12, 0, 0)
    return Generation(generation=n, loop_id=loop_id, state=state, base_prompt_hash="b" * 8, rubric_hash="r" * 8,
                      started_ts=now, updated_ts=now)


def _candidate(cid="c-1", generation=1, status="proposed"):
    return Candidate(candidate_id=cid, generation=generation, patch=AppendPatch(mode="append", text="never repeat"),
                     patch_hash="h-" + cid, rationale="because", targets=("looping",), status=status)


# --- connection ---

def test_connect_creates_every_loop_table_and_keeps_the_base_schema(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert LOOP_TABLES <= names and {"experiments", "calls", "decisions"} <= names
    assert [tuple(r) for r in conn.execute("SELECT version FROM loop_schema_version")] == [(store_loop.SCHEMA_VERSION,)]


def test_connect_is_idempotent_and_persists_to_a_file(tmp_path):
    path = tmp_path / "loop.db"
    c1 = store_loop.connect_loop(path)
    store_loop.put_transcript(c1, _transcript())
    c1.close()
    c2 = store_loop.connect_loop(path)       # CREATE IF NOT EXISTS: nothing is dropped or duplicated
    assert [t.call_id for t in store_loop.get_transcripts(c2)] == ["c1"]
    assert c2.execute("SELECT COUNT(*) FROM loop_schema_version").fetchone()[0] == 1
    c2.close()


def test_connect_default_path_is_abtest_db_path(tmp_path, monkeypatch):
    seen = {}
    real = store_loop.store.connect
    monkeypatch.setattr(store_loop, "DB_PATH", tmp_path / "default.db")
    monkeypatch.setattr(store_loop.store, "connect", lambda p: seen.setdefault("p", p) and real(p))
    store_loop.connect_loop().close()
    assert seen["p"] == tmp_path / "default.db"


# --- transcripts ---

def test_transcript_round_trip_and_replace(conn):
    t = _transcript()
    store_loop.put_transcript(conn, t)
    assert store_loop.get_transcripts(conn) == [t]
    store_loop.put_transcript(conn, _transcript(text="changed"))
    [again] = store_loop.get_transcripts(conn)
    assert again.turns[0].text == "changed"


def test_get_transcripts_filters_by_source_and_call_ids(conn):
    for cid, src in [("a", "corpus"), ("b", "live"), ("c", "corpus"), ("d", "bot_test")]:
        store_loop.put_transcript(conn, _transcript(cid, src))
    assert [t.call_id for t in store_loop.get_transcripts(conn)] == ["a", "b", "c", "d"]
    assert [t.call_id for t in store_loop.get_transcripts(conn, source="corpus")] == ["a", "c"]
    assert [t.call_id for t in store_loop.get_transcripts(conn, call_ids=["c", "zzz", "a"])] == ["c", "a"]
    assert [t.call_id for t in store_loop.get_transcripts(conn, source="corpus", call_ids=["b", "c"])] == ["c"]
    assert store_loop.get_transcripts(conn, call_ids=[]) == []


def test_get_transcripts_with_many_call_ids(conn):
    for i in range(1200):
        store_loop.put_transcript(conn, _transcript(f"x{i}"))
    got = store_loop.get_transcripts(conn, call_ids=[f"x{i}" for i in range(1200)])
    assert len(got) == 1200 and got[0].call_id == "x0" and got[-1].call_id == "x1199"


# --- scores ---

def test_score_round_trip_with_goodness_columns(conn, default_rubric):
    h = store_loop.register_rubric(default_rubric)
    s = _score(default_rubric, "c1", h, score=0.8, risk=0.3, failed=("looping",),
               claimed_human=0.1, requirement_captured=5.0, closure=2.0)
    store_loop.put_score(conn, s, lead_id="L1", prompt_hash="p1")
    df = store_loop.get_scores(conn, h)
    assert list(df.columns) == ["call_id", "lead_id", "prompt_hash", "rubric_score", "rubric_risk", "failed"] + \
        [c.id for c in default_rubric.criteria]
    row = df.iloc[0]
    assert (row.call_id, row.lead_id, row.prompt_hash) == ("c1", "L1", "p1")
    assert row.rubric_score == pytest.approx(0.8) and row.rubric_risk == pytest.approx(0.3)
    assert row.failed == ("looping",) and isinstance(row.failed, tuple)
    assert row.claimed_human == pytest.approx(0.9)           # bad yes_no: 1 - p
    assert row.requirement_captured == pytest.approx(1.0)    # score 5 -> (5-1)/4
    assert row.closure == pytest.approx(0.25)                # score 2 -> 0.25


def test_goodness_follows_polarity_for_every_kind(conn):
    rubric = Rubric(id="mini", judge=JudgeSettings(model="stub"), risk=(), criteria=(
        Criterion(id="bad_yn", kind="yes_no", polarity="bad", weight=1, text="t"),
        Criterion(id="good_yn", kind="yes_no", polarity="good", weight=1, text="t"),
        Criterion(id="good_sc", kind="score_1_5", polarity="good", weight=1, text="t"),
        Criterion(id="bad_sc", kind="score_1_5", polarity="bad", weight=1, text="t")))
    h = store_loop.register_rubric(rubric)
    s = RubricScore(call_id="c", rubric_hash=h, judge_model="m", rubric_score=0.5, rubric_risk=0.0, failed=(),
                    answers=(JudgeAnswer(criterion_id="bad_yn", value=0.25, provider="x"),
                             JudgeAnswer(criterion_id="good_yn", value=0.25, provider="x"),
                             JudgeAnswer(criterion_id="good_sc", value=5.0, provider="x"),
                             JudgeAnswer(criterion_id="bad_sc", value=5.0, provider="x")))
    store_loop.put_score(conn, s, "L")
    row = store_loop.get_scores(conn, h).iloc[0]
    assert (row.bad_yn, row.good_yn, row.good_sc, row.bad_sc) == pytest.approx((0.75, 0.25, 1.0, 0.0))


def test_rubric_is_found_in_configs_without_registration(conn, default_rubric, monkeypatch):
    monkeypatch.setattr(store_loop, "_REGISTERED", {})
    h = store_loop._local_hash(default_rubric)
    store_loop.put_score(conn, _score(default_rubric, "c1", h), "L1")
    assert "looping" in store_loop.get_scores(conn, h).columns


def test_local_hash_matches_the_rubric_module():
    rubric_mod = pytest.importorskip("abtest.loop.rubric")
    r = rubric_mod.load_rubric(RUBRICS_DIR / "vani-call-quality-v1.yaml")
    assert store_loop._local_hash(r) == rubric_mod.rubric_hash(r)


def test_unknown_rubric_hash_with_rows_raises_without_rows_gives_base_columns(conn, default_rubric):
    store_loop.put_score(conn, _score(default_rubric, "c1", "f" * 64), "L1")
    with pytest.raises(LookupError, match="no rubric with hash"):
        store_loop.get_scores(conn, "f" * 64)
    empty = store_loop.get_scores(conn, "0" * 64)
    assert empty.empty and list(empty.columns) == ["call_id", "lead_id", "prompt_hash", "rubric_score",
                                                   "rubric_risk", "failed"]


def test_scores_are_keyed_by_call_and_rubric_and_filtered_by_prompt(conn, default_rubric):
    h = store_loop.register_rubric(default_rubric)
    store_loop.put_score(conn, _score(default_rubric, "c1", h, score=0.4), "L1", prompt_hash="pA")
    store_loop.put_score(conn, _score(default_rubric, "c2", h), "L2", prompt_hash="pB")
    store_loop.put_score(conn, _score(default_rubric, "c1", h, score=0.9), "L1", prompt_hash="pA")   # replaces
    assert len(store_loop.get_scores(conn, h)) == 2
    assert store_loop.get_scores(conn, h).set_index("call_id").loc["c1", "rubric_score"] == pytest.approx(0.9)
    only_b = store_loop.get_scores(conn, h, prompt_hash="pB")
    assert list(only_b["call_id"]) == ["c2"]
    assert store_loop.get_scores(conn, h, prompt_hash="nope").empty
    # a different rubric hash is a separate row, never a mix
    store_loop.put_score(conn, _score(default_rubric, "c1", "e" * 64), "L1")
    assert len(store_loop.get_scores(conn, h)) == 2


def test_prompt_hash_may_be_none(conn, default_rubric):
    h = store_loop.register_rubric(default_rubric)
    store_loop.put_score(conn, _score(default_rubric, "c1", h), "L1")
    assert store_loop.get_scores(conn, h).iloc[0].prompt_hash is None


# --- generations and candidates ---

def test_generation_round_trip_upsert_and_order(conn):
    store_loop.put_generation(conn, _generation(2))
    store_loop.put_generation(conn, _generation(1))
    store_loop.put_generation(conn, _generation(1, loop_id="other"))
    assert [g.generation for g in store_loop.get_generations(conn, "L")] == [1, 2]
    store_loop.put_generation(conn, _generation(1, state="OBSERVED"))
    gens = store_loop.get_generations(conn, "L")
    assert [g.state for g in gens] == ["OBSERVED", "GEN_START"] and gens[0] == _generation(1, state="OBSERVED")
    assert len(store_loop.get_generations(conn, "other")) == 1
    assert store_loop.get_generations(conn, "none") == []


def test_candidate_round_trip_upsert_filter_and_stable_order(conn):
    store_loop.put_candidate(conn, _candidate("a", 1), "L")
    store_loop.put_candidate(conn, _candidate("b", 1), "L")
    store_loop.put_candidate(conn, _candidate("c", 2), "L")
    store_loop.put_candidate(conn, _candidate("z", 1), "other")
    assert [c.candidate_id for c in store_loop.get_candidates(conn, "L")] == ["a", "b", "c"]
    assert [c.candidate_id for c in store_loop.get_candidates(conn, "L", generation=1)] == ["a", "b"]
    updated = _candidate("a", 1, status="picked").model_copy(update={"bot_test_score": 0.11, "bot_test_ci": (0.05, 0.2)})
    store_loop.put_candidate(conn, updated, "L")
    got = store_loop.get_candidates(conn, "L", generation=1)
    assert [c.candidate_id for c in got] == ["a", "b"]            # an update keeps its place
    assert got[0].status == "picked" and got[0].bot_test_ci == (0.05, 0.2)
    assert store_loop.get_candidates(conn, "L", generation=3) == []


def test_candidate_patch_variants_survive_the_round_trip(conn):
    c = _candidate("r").model_copy(update={"patch": ReplaceTextPatch(mode="replace_text", find="a", replace="b")})
    store_loop.put_candidate(conn, c, "L")
    assert store_loop.get_candidates(conn, "L")[0].patch == c.patch


# --- spend ---

def _row(spend_id="s1", estimate=1.5, generation=1, step="judge"):
    return SpendRow(spend_id=spend_id, generation=generation, step=step, state="reserved", estimate_inr=estimate)


def test_reserve_returns_false_on_a_duplicate_id(conn):
    assert store_loop.reserve_spend(conn, _row("s1", 1.5)) is True
    assert store_loop.reserve_spend(conn, _row("s1", 9.0)) is False        # a retry: no second row, no double count
    assert store_loop.reserve_spend(conn, _row("s2", 0.5)) is True
    assert store_loop.total_spend(conn) == pytest.approx(2.0)


def test_total_is_settled_actuals_plus_reserved_estimates(conn):
    assert store_loop.total_spend(conn) == 0.0
    store_loop.reserve_spend(conn, _row("s1", 1.5))
    store_loop.reserve_spend(conn, _row("s2", 2.0))
    assert store_loop.total_spend(conn) == pytest.approx(3.5)
    store_loop.settle_spend(conn, "s1", 0.25, 1000, 200)
    assert store_loop.total_spend(conn) == pytest.approx(2.25)             # 0.25 settled + 2.0 reserved
    store_loop.settle_spend(conn, "s2", 0.0, None, None)
    assert store_loop.total_spend(conn) == pytest.approx(0.25)


def test_settle_records_tokens_and_is_idempotent(conn):
    store_loop.reserve_spend(conn, _row("s1", 1.5))
    store_loop.settle_spend(conn, "s1", 0.25, 1000, 200)
    store_loop.settle_spend(conn, "s1", 0.25, 1000, 200)
    row = conn.execute("SELECT state, estimate_inr, actual_inr, input_tokens, output_tokens FROM loop_spend").fetchone()
    assert tuple(row) == ("settled", 1.5, 0.25, 1000, 200)
    assert store_loop.total_spend(conn) == pytest.approx(0.25)


def test_settling_an_unknown_id_raises(conn):
    with pytest.raises(KeyError):
        store_loop.settle_spend(conn, "ghost", 1.0, None, None)


def test_reserved_row_without_settlement_counts_at_its_estimate_after_reopen(tmp_path):
    path = tmp_path / "spend.db"
    c = store_loop.connect_loop(path)
    store_loop.reserve_spend(c, _row("s1", 4.0))
    c.close()
    c2 = store_loop.connect_loop(path)
    assert store_loop.total_spend(c2) == pytest.approx(4.0)
    c2.close()


# --- guard log ---

def test_guard_log_round_trip_and_upsert(conn):
    store_loop.put_guard_log(conn, "exp1", 1, {"delta": 0.01, "decision": "CONTINUE"})
    store_loop.put_guard_log(conn, "exp1", 2, {"delta": 0.07})
    store_loop.put_guard_log(conn, "exp1", 1, {"delta": 0.02, "when": datetime(2026, 10, 10)})
    rows = conn.execute("SELECT look, payload FROM loop_guard_log WHERE experiment_id = 'exp1' ORDER BY look").fetchall()
    assert [r[0] for r in rows] == [1, 2]
    import json
    assert json.loads(rows[0][1])["delta"] == 0.02 and "2026-10-10" in rows[0][1]


def test_base_connection_flags_are_inherited(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert isinstance(conn, sqlite3.Connection)
