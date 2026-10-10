"""loop/ledger.py: patch hashes, tried hashes and the proposer's summary of past candidates."""

from datetime import datetime

import pytest

from abtest.contracts import AppendPatch, ReplaceSectionPatch, ReplaceTextPatch
from abtest.loop import ledger
from abtest.loop.contracts import Candidate, Generation, sha256_json

LOOP = "vani-loop"


def patch_a() -> AppendPatch:
    return AppendPatch(mode="append", text="Never repeat the same question.")


def cand(i: int, gen: int = 1, patch=None, **kw) -> Candidate:
    patch = patch or AppendPatch(mode="append", text=f"rule number {i}")
    fields = dict(candidate_id=f"g{gen}-c{i}", generation=gen, patch=patch, patch_hash=ledger.patch_hash(patch),
                  rationale=f"why {i}", targets=("looping",), status="bot_tested")
    return Candidate(**{**fields, **kw})


@pytest.fixture
def conn(tmp_path):
    store_loop = pytest.importorskip("abtest.loop.store_loop")
    c = store_loop.connect_loop(tmp_path / "loop.db")
    yield c
    c.close()


@pytest.fixture
def store_loop():
    return pytest.importorskip("abtest.loop.store_loop")


# --- patch_hash ---------------------------------------------------------------------------------------

def test_patch_hash_is_sha256_of_the_canonical_patch_json():
    patch = patch_a()
    assert ledger.patch_hash(patch) == sha256_json({"mode": "append", "text": "Never repeat the same question."})
    assert len(ledger.patch_hash(patch)) == 64


def test_patch_hash_depends_on_content_only():
    assert ledger.patch_hash(patch_a()) == ledger.patch_hash(patch_a())
    assert ledger.patch_hash(patch_a()) != ledger.patch_hash(AppendPatch(mode="append", text="Other."))
    find = ReplaceTextPatch(mode="replace_text", find="a", replace="b")
    assert ledger.patch_hash(find) != ledger.patch_hash(ReplaceTextPatch(mode="replace_text", find="b", replace="a"))
    section = ReplaceSectionPatch(mode="replace_section", start="a", end="b", text="c")
    assert len({ledger.patch_hash(p) for p in (patch_a(), find, section)}) == 3


# --- tried_hashes ---------------------------------------------------------------------------------------

def test_tried_hashes_round_trip(conn, store_loop):
    assert ledger.tried_hashes(conn, LOOP) == set()
    cs = [cand(1, status="promoted"), cand(2, status="precheck_failed"), cand(3, gen=2, status="gate_blocked"),
          cand(4, status="not_picked"), cand(5, status="bot_tested"), cand(6, status="not_promoted")]
    for c in cs:
        store_loop.put_candidate(conn, c, LOOP)
    # decided candidates are tried; outranked or pre-check failures are re-tested against a new prompt
    assert ledger.tried_hashes(conn, LOOP) == {cs[0].patch_hash, cs[2].patch_hash, cs[5].patch_hash}


def test_tried_hashes_are_per_loop(conn, store_loop):
    store_loop.put_candidate(conn, cand(1, status="live"), LOOP)
    store_loop.put_candidate(conn, cand(2, status="live"), "another-loop")
    assert ledger.tried_hashes(conn, LOOP) == {cand(1).patch_hash}
    assert ledger.tried_hashes(conn, "another-loop") == {cand(2).patch_hash}


def test_a_stored_patch_is_found_again_by_hash(conn, store_loop):
    store_loop.put_candidate(conn, cand(1, patch=patch_a(), status="picked"), LOOP)
    assert ledger.patch_hash(patch_a()) in ledger.tried_hashes(conn, LOOP)


# --- ledger_summary ---------------------------------------------------------------------------------------

def test_summary_records_what_the_proposer_needs(conn, store_loop):
    store_loop.put_candidate(conn, cand(1, bot_test_score=0.12, status="picked"), LOOP)
    now = datetime(2026, 10, 10, 9, 0)
    store_loop.put_generation(conn, Generation(generation=1, loop_id=LOOP, state="GEN_END", base_prompt_hash="b",
                                               rubric_hash="r", pick="g1-c1", terminal="STOPPED_HARM",
                                               started_ts=now, updated_ts=now))
    [rec] = ledger.ledger_summary(conn, LOOP)
    assert rec["generation"] == 1 and rec["candidate_id"] == "g1-c1" and rec["mode"] == "append"
    assert rec["targets"] == ["looping"] and rec["rationale"] == "why 1" and rec["status"] == "picked"
    assert rec["bot_test_score"] == pytest.approx(0.12) and rec["live_terminal"] == "STOPPED_HARM"
    assert rec["patch_preview"] == "rule number 1"


def test_summary_live_terminal_is_none_unless_picked(conn, store_loop):
    store_loop.put_candidate(conn, cand(1), LOOP)
    [rec] = ledger.ledger_summary(conn, LOOP)
    assert rec["live_terminal"] is None and rec["bot_test_score"] is None


def test_summary_keeps_the_latest_items_oldest_first(conn, store_loop):
    for gen in (1, 2):
        for i in range(1, 11):          # c10 must sort after c9
            store_loop.put_candidate(conn, cand(i, gen=gen), LOOP)
    ids = [r["candidate_id"] for r in ledger.ledger_summary(conn, LOOP, max_items=12)]
    assert ids == ["g1-c9", "g1-c10"] + [f"g2-c{i}" for i in range(1, 11)]
    assert len(ledger.ledger_summary(conn, LOOP)) == 20
    assert ledger.ledger_summary(conn, LOOP, max_items=0) == []


def test_summary_previews_the_text_a_patch_adds(conn, store_loop):
    long = AppendPatch(mode="append", text="word " * 100)
    swap = ReplaceTextPatch(mode="replace_text", find="old text", replace="new   text")
    section = ReplaceSectionPatch(mode="replace_section", start="a", end="b", text="between")
    for i, p in enumerate((long, swap, section), start=1):
        store_loop.put_candidate(conn, cand(i, patch=p), LOOP)
    previews = [r["patch_preview"] for r in ledger.ledger_summary(conn, LOOP)]
    assert len(previews[0]) == ledger.PREVIEW_CHARS and previews[0].startswith("word word")
    assert previews[1] == "new text" and previews[2] == "between"


def test_summary_of_an_empty_loop(conn):
    assert ledger.ledger_summary(conn, LOOP) == []
