"""Rubrics the judge grades calls against: IndiaMART's QA checklist and the Vani prompt's own rules
(src/abtest/ext/rubric_sets/*.yaml, committed), plus rubrics users create in the Rubric Studio.

Custom rubrics live in models.CUSTOM_RUBRICS as every version ever saved. A save appends a version
and never edits one in place, because audits cite (rubric_id, rubric_version): an old audit must
still say what it was graded against. Editing or archiving a built-in stores a custom version with
the built-in's id, which then overrides the YAML.
"""

import fcntl
import hashlib
import json
import re
from contextlib import contextmanager

import yaml

from abtest.ext import models
from abtest.ext.models import Rubric

_ID = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_REQUIRED = ("name", "category", "question")
_SERVER_SET = {"version", "builtin", "author", "actor"}  # never taken from a request body
# What the judge prompt shows: a change to any of these changes the grading, so the hash too.
_HASHED = ("id", "version", "name", "question", "applies_when", "guidance", "fatal_when",
           "non_fatal_when", "needs_audio", "examples_pass", "examples_fail")


def snake(text: str) -> str:
    """'Category ISQ's Error' -> 'category_isqs_error' (how built-in ids derive from QA columns)."""
    return re.sub(r"[^a-z0-9]+", "_", text.lower().replace("'", "")).strip("_")


def _check(r: Rubric) -> Rubric:
    if not _ID.fullmatch(r.id):
        raise ValueError(f"rubric id {r.id!r} must be snake_case (a-z, 0-9, _), starting with a letter")
    for field in _REQUIRED:
        if not getattr(r, field).strip():
            raise ValueError(f"rubric {r.id}: {field} is required")
    return r


def builtin() -> list[Rubric]:
    """The committed rubrics, QA matrix first (files load in name order)."""
    out = []
    for path in sorted(models.RUBRICS_DIR.glob("*.yaml")):
        out += [_check(Rubric.model_validate(d)) for d in yaml.safe_load(path.read_text(encoding="utf-8")) or []]
    ids = [r.id for r in out]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate built-in rubric ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    return out


def _versions() -> list[Rubric]:
    return models.load(models.CUSTOM_RUBRICS, Rubric, default=[])


def all_rubrics(include_archived: bool = False) -> list[Rubric]:
    """Built-ins (each replaced by its latest custom version, if any), then custom rubrics in the
    order they were created; archived ones only on request."""
    latest: dict[str, Rubric] = {}
    for r in _versions():
        if r.id not in latest or r.version > latest[r.id].version:
            latest[r.id] = r
    out = [latest.pop(r.id, r) for r in builtin()] + list(latest.values())
    return [r for r in out if include_archived or r.active]


def get(rubric_id: str) -> Rubric:
    """Latest version, archived or not; KeyError for an unknown id."""
    for r in all_rubrics(include_archived=True):
        if r.id == rubric_id:
            return r
    raise KeyError(rubric_id)


@contextmanager
def _locked():
    """Serialise read-version-write across threads and processes (the platform and a CLI may both edit);
    flock is per open file, so threads that each open the lock file exclude each other too."""
    models.CUSTOM_RUBRICS.parent.mkdir(parents=True, exist_ok=True)
    with open(models.CUSTOM_RUBRICS.with_suffix(".lock"), "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def _append(r: Rubric) -> Rubric:
    models.save(models.CUSTOM_RUBRICS, [*_versions(), r])
    return r


def save(fields: dict, actor: str) -> Rubric:
    """New id -> version 1, builtin False, author = actor. Existing id -> the next version, the
    given fields merged over the latest one (old versions are kept). Saving an archived rubric
    restores it unless `active` is given. Without an id, the id is derived from the name."""
    actor = actor.strip()
    if not actor:
        raise ValueError("actor is required")
    fields = {k: v for k, v in fields.items() if k not in _SERVER_SET}
    rid = fields.get("id") or snake(fields.get("name") or "")
    with _locked():
        try:
            prev = get(rid)
        except KeyError:
            prev = None
        base = prev.model_dump() if prev else {"builtin": False, "category": "Custom"}
        new = Rubric.model_validate({**base, "active": True, **fields, "id": rid, "author": actor,
                                     "version": prev.version + 1 if prev else 1})
        return _append(_check(new))


def archive(rubric_id: str, actor: str) -> Rubric:
    """Archive as a new version (active False). Built-ins can be archived, never deleted."""
    with _locked():
        prev = get(rubric_id)
        if not prev.active:
            return prev
        return _append(prev.model_copy(update={"version": prev.version + 1, "active": False,
                                               "author": actor.strip() or prev.author}))


def set_hash(rubrics) -> str:
    """sha256 of the canonical JSON of the rubrics' ids, versions and texts, order-independent.
    Built-in YAML edits keep version 1, so the texts are hashed too."""
    rows = sorted(([getattr(r, f) for f in _HASHED] for r in rubrics), key=lambda row: row[0])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
