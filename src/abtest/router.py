"""Hash-bucket routing on the live allocation, with exposure logging (PLAN.md §2.4)."""

import hashlib
import sqlite3
from datetime import datetime

import pandas as pd

from abtest import store
from abtest.contracts import Allocation, Assignment, ExperimentState, Exposure, Variant

BUCKETS = 10_000
HASH_HEX_DIGITS = 15  # leading hex digits of the SHA-256 digest that feed the bucket (60 bits)


def bucket(salt: str, lead_id: str) -> int:
    return int(hashlib.sha256(f"{salt}:{lead_id}".encode()).hexdigest()[:HASH_HEX_DIGITS], 16) % BUCKETS


def variant_for(bucket: int, allocation: Allocation) -> Variant:
    # Same as `bucket < B * BUCKETS`, but exact for shares on the 1/BUCKETS grid: 0.07 * 10000 is
    # 700.0000000000001 in floats (bucket 700 would go to B), while 700 / 10000 == 0.07 exactly.
    return "B" if bucket / BUCKETS < allocation.B else "A"


def route(state: ExperimentState, lead_id: str) -> Assignment:
    b = bucket(state.salt, lead_id)
    v = variant_for(b, state.allocation)
    return Assignment(variant=v, bucket=b, state=state.state, config_hash=state.config_hash,
                      prompt_hash=state.prompt_hash[v])


def assign(conn: sqlite3.Connection, experiment_id: str, lead_id: str,
           ts: datetime) -> Assignment:
    state = store.get_state(conn, experiment_id)
    if state is None:
        raise KeyError(experiment_id)
    a = route(state, lead_id)
    exposure = Exposure(experiment_id=experiment_id, lead_id=lead_id, variant=a.variant,
                        bucket=a.bucket, ts=ts)
    store.insert_df(conn, "exposures", pd.DataFrame([exposure.model_dump()]))
    return a
