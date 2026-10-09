"""GET /assign?experiment_id=&lead_id= (PLAN.md §2.4). Run: make api."""

from datetime import datetime
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query

from abtest import router, store
from abtest.contracts import DB_PATH  # read per request, so tests can monkeypatch api.DB_PATH

app = FastAPI()

NonEmpty = Annotated[str, Query(min_length=1)]


@app.get("/assign")
def assign(experiment_id: NonEmpty, lead_id: NonEmpty) -> dict:
    conn = store.connect(DB_PATH)
    try:
        a = router.assign(conn, experiment_id, lead_id, datetime.now())
    except KeyError:
        raise HTTPException(404, f"unknown experiment {experiment_id!r}") from None
    finally:
        conn.close()
    return a.model_dump(mode="json", exclude={"bucket"})
