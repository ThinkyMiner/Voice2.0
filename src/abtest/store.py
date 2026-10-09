"""SQLite experiment store (WAL, so the dashboard reads while the runner writes).

Orchestrator-owned. Timestamps are ISO-8601 strings. Two invariants are enforced by triggers,
not app code: decision rows are never updated, and a terminal experiment_state never changes.
A scenario rerun calls reset_experiment(), which deletes that experiment's rows outright;
logs/decisions.jsonl keeps the full history.
"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd

from abtest.contracts import DB_PATH, TERMINAL, ExperimentConfig, ExperimentState, FrozenConfig, State

_TERMINAL_SQL = ", ".join(f"'{s}'" for s in sorted(TERMINAL))

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS experiments (
    experiment_id   TEXT PRIMARY KEY,
    config_json     TEXT NOT NULL,   -- FrozenConfig.canonical_json
    config_hash     TEXT NOT NULL,
    prompt_hash_A   TEXT NOT NULL,
    prompt_hash_B   TEXT NOT NULL,
    scenario        TEXT,            -- scenario name for simulated runs
    seed            INTEGER,
    created_ts_wall TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_state (
    experiment_id   TEXT PRIMARY KEY REFERENCES experiments(experiment_id),
    state           TEXT NOT NULL,
    alloc_A         REAL NOT NULL,   -- LIVE allocation; the router reads this
    alloc_B         REAL NOT NULL,
    looks_done      INTEGER NOT NULL DEFAULT 0,
    info_fractions  TEXT NOT NULL DEFAULT '[]',  -- JSON list of observed t_k
    n_b_max         REAL,
    started_ts_sim  TEXT,
    updated_ts_sim  TEXT,
    updated_ts_wall TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exposures (
    experiment_id TEXT NOT NULL,
    lead_id       TEXT NOT NULL,
    variant       TEXT NOT NULL CHECK (variant IN ('A', 'B')),
    bucket        INTEGER NOT NULL CHECK (bucket BETWEEN 0 AND 9999),
    ts            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS exposures_lead ON exposures(experiment_id, lead_id);

CREATE TABLE IF NOT EXISTS calls (     -- simulated call events, canonical schema
    experiment_id TEXT NOT NULL,
    call_id       TEXT NOT NULL,
    lead_id       TEXT NOT NULL,
    ts            TEXT NOT NULL,
    answered      INTEGER NOT NULL,
    disposition   TEXT NOT NULL,
    duration_s    REAL,
    attempt_no    INTEGER,
    PRIMARY KEY (experiment_id, call_id)
);
CREATE INDEX IF NOT EXISTS calls_lead ON calls(experiment_id, lead_id);

CREATE TABLE IF NOT EXISTS decisions (  -- §2.9, append-only
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id TEXT NOT NULL,
    ts_wall       TEXT NOT NULL,
    ts_sim        TEXT,
    kind          TEXT NOT NULL CHECK (kind IN ('look', 'state_change')),
    look          INTEGER,
    info_fraction REAL,
    decision      TEXT NOT NULL,
    reason        TEXT NOT NULL,
    actor         TEXT NOT NULL,
    payload       TEXT NOT NULL      -- Decision.model_dump_json()
);
CREATE INDEX IF NOT EXISTS decisions_exp ON decisions(experiment_id, id);

CREATE TABLE IF NOT EXISTS sim_truth (  -- written by the simulator; the engine never reads it
    experiment_id TEXT PRIMARY KEY,
    scenario      TEXT,
    payload       TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS decisions_append_only BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions is append-only'); END;

CREATE TRIGGER IF NOT EXISTS terminal_state_immutable BEFORE UPDATE ON experiment_state
WHEN OLD.state IN ({_TERMINAL_SQL})
BEGIN SELECT RAISE(ABORT, 'terminal state is immutable'); END;

CREATE TRIGGER IF NOT EXISTS config_frozen BEFORE UPDATE ON experiments
BEGIN SELECT RAISE(ABORT, 'experiment config is frozen'); END;
"""


def connect(path: str | Path = DB_PATH) -> sqlite3.Connection:
    """Open (creating if needed) the store. Pass ":memory:" for throwaway runs."""
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def _iso(ts: datetime | None) -> str | None:
    # Always microseconds: isoformat() drops zero microseconds, and mixed formats break parsing.
    return None if ts is None else ts.isoformat(timespec="microseconds")


def reset_experiment(conn: sqlite3.Connection, experiment_id: str) -> None:
    with conn:
        for table in ("decisions", "calls", "exposures", "sim_truth", "experiment_state", "experiments"):
            conn.execute(f"DELETE FROM {table} WHERE experiment_id = ?", (experiment_id,))


def create_experiment(conn: sqlite3.Connection, frozen: FrozenConfig,
                      scenario: str | None = None, seed: int | None = None) -> ExperimentState:
    """Insert the frozen config and a DRAFT state with the configured allocation."""
    cfg = frozen.config
    state = ExperimentState(experiment_id=cfg.id, state=State.DRAFT, allocation=cfg.allocation,
                            salt=cfg.salt, config_hash=frozen.config_hash, prompt_hash=frozen.prompt_hash)
    with conn:
        conn.execute(
            "INSERT INTO experiments VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cfg.id, frozen.canonical_json, frozen.config_hash, frozen.prompt_hash["A"],
             frozen.prompt_hash["B"], scenario, seed, _now()),
        )
        put_state(conn, state)
    return state


def put_state(conn: sqlite3.Connection, s: ExperimentState) -> None:
    with conn:
        conn.execute(
            """INSERT INTO experiment_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(experiment_id) DO UPDATE SET state=excluded.state, alloc_A=excluded.alloc_A,
                 alloc_B=excluded.alloc_B, looks_done=excluded.looks_done,
                 info_fractions=excluded.info_fractions, n_b_max=excluded.n_b_max,
                 started_ts_sim=excluded.started_ts_sim, updated_ts_sim=excluded.updated_ts_sim,
                 updated_ts_wall=excluded.updated_ts_wall""",
            (s.experiment_id, s.state.value, s.allocation.A, s.allocation.B, s.looks_done,
             json.dumps(list(s.info_fractions)), s.n_b_max, _iso(s.started_ts_sim),
             _iso(s.updated_ts_sim), _now()),
        )


def get_state(conn: sqlite3.Connection, experiment_id: str) -> ExperimentState | None:
    row = conn.execute(
        """SELECT s.*, e.config_hash, e.prompt_hash_A, e.prompt_hash_B, e.config_json
           FROM experiment_state s JOIN experiments e USING (experiment_id)
           WHERE experiment_id = ?""",
        (experiment_id,),
    ).fetchone()
    if row is None:
        return None
    return ExperimentState(
        experiment_id=row["experiment_id"], state=State(row["state"]),
        allocation={"A": row["alloc_A"], "B": row["alloc_B"]},
        salt=json.loads(row["config_json"])["salt"], config_hash=row["config_hash"],
        prompt_hash={"A": row["prompt_hash_A"], "B": row["prompt_hash_B"]},
        looks_done=row["looks_done"], info_fractions=tuple(json.loads(row["info_fractions"])),
        n_b_max=row["n_b_max"], started_ts_sim=row["started_ts_sim"], updated_ts_sim=row["updated_ts_sim"],
    )


def get_config(conn: sqlite3.Connection, experiment_id: str) -> ExperimentConfig:
    row = conn.execute("SELECT config_json FROM experiments WHERE experiment_id = ?",
                       (experiment_id,)).fetchone()
    if row is None:
        raise KeyError(experiment_id)
    return ExperimentConfig.model_validate_json(row["config_json"])


def insert_df(conn: sqlite3.Connection, table: str, df: pd.DataFrame) -> int:
    """Bulk insert; datetime columns are written as ISO strings, NaN/NA as NULL."""
    if df.empty:
        return 0
    out = df.copy()
    for col in out.columns:
        if pd.api.types.is_datetime64_any_dtype(out[col]):
            out[col] = out[col].map(lambda t: None if pd.isna(t) else _iso(t))
    out = out.astype(object).where(out.notna(), None)
    cols = ", ".join(out.columns)
    marks = ", ".join("?" * len(out.columns))
    with conn:
        conn.executemany(f"INSERT INTO {table} ({cols}) VALUES ({marks})",
                         out.itertuples(index=False, name=None))
    return len(out)


def read_df(conn: sqlite3.Connection, sql: str, params: tuple | dict = (),
            parse_dates: list[str] | None = None) -> pd.DataFrame:
    df = pd.read_sql_query(sql, conn, params=params)
    for col in parse_dates or ():
        df[col] = pd.to_datetime(df[col], format="ISO8601")
    return df
