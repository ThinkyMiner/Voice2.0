"""Loop tables in the experiment store (PLAN_LOOP.md §L2.9, §L7.2, §L9).

`connect_loop` opens the base store (`abtest.store.connect`, so the base schema and its triggers
come along unchanged) and adds the loop's own tables with CREATE IF NOT EXISTS, tracked by its own
`loop_schema_version` table (the §E1.3 migrations table does not exist yet). Nothing here touches a
base table. Payloads are `model_dump_json`, so a model can grow a field without a migration; only
the columns the loop queries on (ids, hashes, scores, spend) are real columns.

Goodness is not stored. `put_score` keeps the judge's answers, and `get_scores` turns them into one
goodness column per criterion with the rubric whose hash matches the row (the same formulas as
rubric.goodness, PLAN_LOOP §L2.2). It finds that rubric in this order: `register_rubric`, the
rubric module's own memo (anything loaded or hashed in this process), then configs_loop/rubrics/*.yaml.
A hash that none of them knows raises LookupError, because a guessed polarity would flip a score.
"""

import importlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from abtest import store
from abtest.contracts import DB_PATH
from abtest.loop.contracts import (RUBRICS_DIR, Candidate, Criterion, Generation, JudgeAnswer, Rubric,
                                   RubricScore, SpendRow, Transcript, sha256_json)

SCHEMA_VERSION = 2   # 2: loop_candidates keyed by (loop_id, candidate_id); 1 keyed by candidate_id
                     # alone, so a new loop overwrote an older loop's candidates with the same id

LOOP_SCHEMA = """
CREATE TABLE IF NOT EXISTS loop_schema_version (
    version    INTEGER PRIMARY KEY,
    applied_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loop_transcripts (
    call_id TEXT PRIMARY KEY,
    source  TEXT NOT NULL,
    payload TEXT NOT NULL          -- Transcript.model_dump_json()
);
CREATE INDEX IF NOT EXISTS loop_transcripts_source ON loop_transcripts(source);

CREATE TABLE IF NOT EXISTS loop_rubric_scores (
    call_id      TEXT NOT NULL,
    rubric_hash  TEXT NOT NULL,
    lead_id      TEXT NOT NULL,
    prompt_hash  TEXT,
    rubric_score REAL NOT NULL,
    rubric_risk  REAL NOT NULL,
    payload      TEXT NOT NULL,    -- RubricScore.model_dump_json()
    PRIMARY KEY (call_id, rubric_hash)
);
CREATE INDEX IF NOT EXISTS loop_rubric_scores_hash ON loop_rubric_scores(rubric_hash, prompt_hash);

CREATE TABLE IF NOT EXISTS loop_generations (
    loop_id    TEXT NOT NULL,
    generation INTEGER NOT NULL,
    payload    TEXT NOT NULL,      -- Generation.model_dump_json()
    PRIMARY KEY (loop_id, generation)
);

CREATE TABLE IF NOT EXISTS loop_candidates (
    candidate_id TEXT NOT NULL,
    loop_id      TEXT NOT NULL,
    generation   INTEGER NOT NULL,
    payload      TEXT NOT NULL,    -- Candidate.model_dump_json()
    PRIMARY KEY (loop_id, candidate_id)   -- ids like g1-c1 repeat in every loop (schema 2)
);
CREATE INDEX IF NOT EXISTS loop_candidates_loop ON loop_candidates(loop_id, generation);

CREATE TABLE IF NOT EXISTS loop_spend (   -- reserved before a paid call, settled after (§L2.9)
    spend_id      TEXT PRIMARY KEY,
    generation    INTEGER NOT NULL,
    step          TEXT NOT NULL,
    state         TEXT NOT NULL CHECK (state IN ('reserved', 'settled')),
    estimate_inr  REAL NOT NULL,
    actual_inr    REAL,
    input_tokens  INTEGER,
    output_tokens INTEGER
);

CREATE TABLE IF NOT EXISTS loop_guard_log (
    experiment_id TEXT NOT NULL,
    look          INTEGER NOT NULL,
    payload       TEXT NOT NULL,
    PRIMARY KEY (experiment_id, look)
);
"""

_BASE_SCORE_COLUMNS = ["call_id", "lead_id", "prompt_hash", "rubric_score", "rubric_risk", "failed"]
_IN_CHUNK = 500  # stay well under SQLite's bound-variable limit


def _migrate_candidates(conn: sqlite3.Connection) -> None:
    """Schema 1 -> 2: rebuild loop_candidates with the (loop_id, candidate_id) key, keeping its rows
    and their order. Rows a newer loop already overwrote in schema 1 cannot be recovered; the
    generation reports under reports/loop/ keep those candidates."""
    cols = conn.execute("PRAGMA table_info(loop_candidates)").fetchall()
    if not cols:
        return  # fresh database: LOOP_SCHEMA creates the new table
    pk = [c[1] for c in sorted(cols, key=lambda c: c[5]) if c[5]]
    if pk == ["loop_id", "candidate_id"]:
        return
    conn.execute("ALTER TABLE loop_candidates RENAME TO loop_candidates_v1")
    conn.execute("DROP INDEX IF EXISTS loop_candidates_loop")
    conn.executescript(LOOP_SCHEMA)
    conn.execute("INSERT OR IGNORE INTO loop_candidates (candidate_id, loop_id, generation, payload) "
                 "SELECT candidate_id, loop_id, generation, payload FROM loop_candidates_v1 ORDER BY rowid")
    conn.execute("DROP TABLE loop_candidates_v1")


def connect_loop(path: str | Path | None = None) -> sqlite3.Connection:
    """store.connect(path or DB_PATH) plus the loop tables (CREATE IF NOT EXISTS, loop_schema_version)."""
    conn = store.connect(path or DB_PATH)
    with conn:
        _migrate_candidates(conn)
        conn.executescript(LOOP_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO loop_schema_version VALUES (?, ?)",
                     (SCHEMA_VERSION, datetime.now().isoformat(timespec="seconds")))
    return conn


# --- transcripts ---------------------------------------------------------------------------

def put_transcript(conn: sqlite3.Connection, t: Transcript) -> None:
    """Insert or replace by call_id (re-transcribing a call replaces its row)."""
    with conn:
        conn.execute(
            "INSERT INTO loop_transcripts (call_id, source, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(call_id) DO UPDATE SET source = excluded.source, payload = excluded.payload",
            (t.call_id, t.source, t.model_dump_json()))


def get_transcripts(conn: sqlite3.Connection, source: str | None = None,
                    call_ids: Sequence[str] | None = None) -> list[Transcript]:
    """Filtered by source and/or call_ids. With call_ids the order follows the argument (unknown ids
    are skipped); otherwise insertion order."""
    if call_ids is not None:
        wanted = list(dict.fromkeys(str(c) for c in call_ids))
        found: dict[str, Transcript] = {}
        for i in range(0, len(wanted), _IN_CHUNK):
            part = wanted[i:i + _IN_CHUNK]
            sql = f"SELECT call_id, payload FROM loop_transcripts WHERE call_id IN ({','.join('?' * len(part))})"
            params: list = list(part)
            if source is not None:
                sql += " AND source = ?"
                params.append(source)
            for cid, payload in conn.execute(sql, params):
                found[cid] = Transcript.model_validate_json(payload)
        return [found[c] for c in wanted if c in found]
    sql, params = "SELECT payload FROM loop_transcripts", []
    if source is not None:
        sql += " WHERE source = ?"
        params.append(source)
    return [Transcript.model_validate_json(r[0]) for r in conn.execute(sql + " ORDER BY rowid", params)]


# --- rubric scores -------------------------------------------------------------------------

_REGISTERED: dict[str, Rubric] = {}


def _local_hash(rubric: Rubric) -> str:
    """Same recipe as rubric.rubric_hash: sha256 of the canonical JSON of the parsed rubric."""
    return sha256_json(rubric.model_dump(mode="json"))


def register_rubric(rubric: Rubric) -> str:
    """Make a rubric that is not a file in configs_loop/rubrics/ (a test's, a rubric built in code)
    resolvable by `get_scores`. Returns its hash."""
    h = _local_hash(rubric)
    _REGISTERED[h] = rubric
    return h


def _resolve_rubric(rubric_hash: str) -> Rubric:
    if rubric_hash in _REGISTERED:
        return _REGISTERED[rubric_hash]
    try:
        known = getattr(importlib.import_module("abtest.loop.rubric"), "known_rubric", None)
    except ImportError:
        known = None
    if known is not None and (r := known(rubric_hash)) is not None:
        return r
    for path in sorted(RUBRICS_DIR.glob("*.yaml")):
        try:
            r = Rubric.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if _local_hash(r) == rubric_hash:
            return r
    raise LookupError(f"no rubric with hash {rubric_hash[:12]}... in this process or in {RUBRICS_DIR}; "
                      "load it with rubric.load_rubric or store_loop.register_rubric first")


def _goodness(criterion: Criterion, answer: JudgeAnswer) -> float:
    """PLAN_LOOP §L2.2. Uses rubric.goodness when the module is importable, else the same formula."""
    try:
        fn = getattr(importlib.import_module("abtest.loop.rubric"), "goodness", None)
    except ImportError:
        fn = None
    if fn is not None:
        return float(fn(criterion, answer))
    x = min(1.0, max(0.0, answer.value if criterion.kind == "yes_no" else (answer.value - 1.0) / 4.0))
    return 1.0 - x if criterion.polarity == "bad" else x


def put_score(conn: sqlite3.Connection, s: RubricScore, lead_id: str, prompt_hash: str | None = None) -> None:
    """Insert or replace by (call_id, rubric_hash): a new rubric adds rows, it never overwrites old ones."""
    with conn:
        conn.execute(
            "INSERT INTO loop_rubric_scores (call_id, rubric_hash, lead_id, prompt_hash, rubric_score, "
            "rubric_risk, payload) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(call_id, rubric_hash) DO UPDATE SET lead_id = excluded.lead_id, "
            "prompt_hash = excluded.prompt_hash, rubric_score = excluded.rubric_score, "
            "rubric_risk = excluded.rubric_risk, payload = excluded.payload",
            (s.call_id, s.rubric_hash, str(lead_id), prompt_hash, s.rubric_score, s.rubric_risk,
             s.model_dump_json()))


def get_scores(conn: sqlite3.Connection, rubric_hash: str, prompt_hash: str | None = None) -> pd.DataFrame:
    """call_id, lead_id, prompt_hash, rubric_score, rubric_risk, failed (tuple), then one column per
    criterion holding its goodness in [0, 1] (higher is better). Rows are in insertion order. The
    rubric comes from `_resolve_rubric` (see the module docstring); LookupError if the hash is unknown
    and there is at least one row."""
    sql = ("SELECT call_id, lead_id, prompt_hash, rubric_score, rubric_risk, payload "
           "FROM loop_rubric_scores WHERE rubric_hash = ?")
    params: list = [rubric_hash]
    if prompt_hash is not None:
        sql += " AND prompt_hash = ?"
        params.append(prompt_hash)
    rows = conn.execute(sql + " ORDER BY rowid", params).fetchall()

    try:
        rubric = _resolve_rubric(rubric_hash)
    except LookupError:
        if rows:
            raise
        rubric = None
    criteria = list(rubric.criteria) if rubric is not None else []

    records = []
    for call_id, lead_id, p_hash, score, risk, payload in rows:
        s = RubricScore.model_validate_json(payload)
        by_id = {a.criterion_id: a for a in s.answers}
        rec = {"call_id": call_id, "lead_id": lead_id, "prompt_hash": p_hash,
               "rubric_score": score, "rubric_risk": risk, "failed": tuple(s.failed)}
        for c in criteria:  # a criterion skipped for this call (not applicable) has no goodness
            rec[c.id] = _goodness(c, by_id[c.id]) if c.id in by_id and c.id not in s.skipped else float("nan")
        records.append(rec)
    return pd.DataFrame(records, columns=_BASE_SCORE_COLUMNS + [c.id for c in criteria])


# --- generations and candidates ------------------------------------------------------------

def put_generation(conn: sqlite3.Connection, g: Generation) -> None:
    with conn:
        conn.execute(
            "INSERT INTO loop_generations (loop_id, generation, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(loop_id, generation) DO UPDATE SET payload = excluded.payload",
            (g.loop_id, g.generation, g.model_dump_json()))


def get_generations(conn: sqlite3.Connection, loop_id: str) -> list[Generation]:
    rows = conn.execute("SELECT payload FROM loop_generations WHERE loop_id = ? ORDER BY generation", (loop_id,))
    return [Generation.model_validate_json(r[0]) for r in rows]


def put_candidate(conn: sqlite3.Connection, c: Candidate, loop_id: str) -> None:
    """Upsert by (loop_id, candidate_id); the row keeps its position when a later stage updates it."""
    with conn:
        conn.execute(
            "INSERT INTO loop_candidates (candidate_id, loop_id, generation, payload) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(loop_id, candidate_id) DO UPDATE SET "
            "generation = excluded.generation, payload = excluded.payload",
            (c.candidate_id, loop_id, c.generation, c.model_dump_json()))


def get_candidates(conn: sqlite3.Connection, loop_id: str, generation: int | None = None) -> list[Candidate]:
    sql, params = "SELECT payload FROM loop_candidates WHERE loop_id = ?", [loop_id]
    if generation is not None:
        sql += " AND generation = ?"
        params.append(generation)
    return [Candidate.model_validate_json(r[0]) for r in conn.execute(sql + " ORDER BY generation, rowid", params)]


# --- spend ---------------------------------------------------------------------------------

def reserve_spend(conn: sqlite3.Connection, row: SpendRow) -> bool:
    """Insert a reserved row; False if spend_id already exists (a retry: do not double-count)."""
    with conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO loop_spend (spend_id, generation, step, state, estimate_inr, actual_inr, "
            "input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (row.spend_id, row.generation, row.step, row.state, row.estimate_inr, row.actual_inr,
             row.input_tokens, row.output_tokens))
    return cur.rowcount == 1


def settle_spend(conn: sqlite3.Connection, spend_id: str, actual_inr: float,
                 input_tokens: int | None, output_tokens: int | None) -> None:
    """Mark a reserved row settled with the actual cost. Settling twice just rewrites the same row,
    so a retry never counts a call twice. KeyError for an unknown id."""
    with conn:
        cur = conn.execute(
            "UPDATE loop_spend SET state = 'settled', actual_inr = ?, input_tokens = ?, output_tokens = ? "
            "WHERE spend_id = ?", (actual_inr, input_tokens, output_tokens, spend_id))
    if cur.rowcount == 0:
        raise KeyError(f"no reserved spend row {spend_id[:12]}...")


def total_spend(conn: sqlite3.Connection) -> float:
    """Settled actuals plus reserved estimates."""
    row = conn.execute("SELECT COALESCE(SUM(CASE WHEN state = 'settled' THEN COALESCE(actual_inr, estimate_inr) "
                       "ELSE estimate_inr END), 0.0) FROM loop_spend").fetchone()
    return float(row[0])


# --- guard log -----------------------------------------------------------------------------

def put_guard_log(conn: sqlite3.Connection, experiment_id: str, look: int, payload: dict) -> None:
    with conn:
        conn.execute(
            "INSERT INTO loop_guard_log (experiment_id, look, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(experiment_id, look) DO UPDATE SET payload = excluded.payload",
            (experiment_id, look, json.dumps(payload, sort_keys=True, default=str)))
