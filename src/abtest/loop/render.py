"""Prompt rendering, patch application and pre-checks (PLAN_LOOP.md §L2.5, §L2.6, §L9).

The flow's prompt is a template with three kinds of markers:
  * `\\ufeff name \\ufeff` (a BOM character on each side of a name, spaces optional): a plain
    variable. `render` turns it into a Jinja expression `{{ name }}`.
  * `\\ufeff@ name\\ufeff`: a reference to an agent variable or tool. It is part of the instruction
    text the bot reads, not something to fill in, so it is left verbatim.
  * Jinja tags (`{% if ... %}`, over about two dozen variables), kept as they are.
Anything else Jinja would misread (a literal `{{` or `{#` in a patch's text, say) is escaped so it
renders as typed. Rendering uses `StrictUndefined`, in a sandbox: a missing variable is an error,
never a blank, and a proposed patch is untrusted text, so it must not be able to run code.

`apply_patch` is `config._resolve` on a string: the same newline handling, the same exactly-once
rule and the same errors, so a candidate that passes here resolves identically when the base engine
loads it as a variant (a test compares the two on the real prompt). `full` is rejected: a full
prompt is far beyond the proposer's output cap (PLAN_LOOP §L2.5).
"""

import re
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path

import jinja2
import jinja2.meta
import jinja2.sandbox
import yaml

from abtest.contracts import (AppendPatch, FullPatch, Patch, ReplaceSectionPatch,
                              ReplaceTextPatch)
from abtest.loop.contracts import LOOP_CONFIGS_DIR, ROOT

BOM = "﻿"
RENDER_VARS = ROOT / "tests" / "loop" / "fixtures" / "render_vars.yaml"
PERSONAS_SEED = LOOP_CONFIGS_DIR / "personas_seed.yaml"

# BOM, optional blanks, optional "@", an identifier, optional blanks, BOM. No newline inside, so one
# stray BOM can never swallow a paragraph; a BOM pair that is not a marker stays in the text as is.
_MARKER = re.compile(BOM + r"[ \t]*(@[ \t]*)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*" + BOM)
_TAG = re.compile(r"(\{%.*?%\})", re.S)       # a Jinja statement tag: left untouched
_OPENER = re.compile(r"\{[{#]")               # `{{` and `{#` outside a tag: literal text

# StrictUndefined fails on bool/==/str of a missing name; sandboxed because patches come from a model.
_ENV = jinja2.sandbox.SandboxedEnvironment(undefined=jinja2.StrictUndefined, autoescape=False,
                                           keep_trailing_newline=True)


# --- template -> Jinja ------------------------------------------------------------------------

def _escape_literal(text: str) -> str:
    """Make `{{` / `{#` in plain text render as typed; leave `{% ... %}` tags alone."""
    parts = _TAG.split(text)
    return "".join(p if i % 2 else _OPENER.sub(lambda m: "{{ '" + m.group(0) + "' }}", p)
                   for i, p in enumerate(parts))


def _to_jinja(template: str) -> str:
    out, pos = [], 0
    for m in _MARKER.finditer(template):
        out.append(_escape_literal(template[pos:m.start()]))
        out.append(m.group(0) if m.group(1) else "{{ " + m.group(2) + " }}")   # "@" marker: verbatim
        pos = m.end()
    out.append(_escape_literal(template[pos:]))
    return "".join(out)


def template_variables(template: str) -> set[str]:
    """Names used by plain interpolation markers and by Jinja tags (jinja2.meta undeclared names)."""
    names = jinja2.meta.find_undeclared_variables(_ENV.parse(_to_jinja(template)))
    return set(names) - set(_ENV.globals)


@lru_cache(maxsize=16)   # one prompt is rendered once per persona card; compiling 160k characters costs ~0.1 s
def _compile(template: str) -> tuple[frozenset[str], jinja2.Template]:
    """The variables the template needs, and the compiled template. Raises TemplateSyntaxError."""
    ast = _ENV.parse(_to_jinja(template))
    return frozenset(jinja2.meta.find_undeclared_variables(ast)) - set(_ENV.globals), _ENV.from_string(ast)


def _fill(needed: frozenset[str], compiled: jinja2.Template, variables: Mapping[str, str]) -> str:
    if missing := sorted(needed - set(variables)):
        raise jinja2.UndefinedError(f"missing template variables: {missing}")
    return compiled.render(**variables)


def render(template: str, variables: Mapping[str, str]) -> str:
    """Interpolation markers -> values; Jinja tags evaluated with StrictUndefined; `@` markers verbatim.

    Every variable the template uses must be supplied, including ones in a branch that the given
    values would not take: a branch must not hide a missing variable until a different buyer hits it.
    Extra variables are ignored. Raises `jinja2.UndefinedError` (missing variable) or
    `jinja2.TemplateSyntaxError` (a tag that does not parse).
    """
    return _fill(*_compile(template), variables)


# --- patches ----------------------------------------------------------------------------------

def _count(text: str, sub: str) -> int:
    """Occurrences of `sub` in `text`, overlapping ones included (as config._count)."""
    return len(re.findall(f"(?={re.escape(sub)})", text))


def _once(base: str, what: str, sub: str) -> None:
    if (n := _count(base, sub)) != 1:
        raise ValueError(f"patch: {what} {sub!r} occurs {n} times in the base, need exactly 1")


def apply_patch(template: str, patch: Patch) -> str:
    """The base config.py patch semantics on a string (append, replace_text exactly once,
    replace_section); `full` is rejected."""
    match patch:
        case AppendPatch():
            return template + ("" if template.endswith("\n") else "\n") + patch.text + "\n"
        case ReplaceTextPatch():
            _once(template, "replace_text find", patch.find)
            return template.replace(patch.find, patch.replace, 1)
        case ReplaceSectionPatch():
            _once(template, "replace_section start marker", patch.start)
            _once(template, "replace_section end marker", patch.end)
            s, e = template.index(patch.start) + len(patch.start), template.index(patch.end)
            if e < s:
                raise ValueError(f"patch: replace_section end marker {patch.end!r} comes before "
                                 f"the end of start marker {patch.start!r}")
            return template[:s] + patch.text + template[e:]
        case FullPatch():
            raise ValueError("patch: mode `full` is not allowed in the loop (a full prompt is "
                             "beyond the proposer's output cap)")
        case _:
            raise ValueError(f"patch: unsupported patch {patch!r}")


def patch_chars(patch: Patch) -> int:
    """Total characters of the patch's text fields (text, find, replace, start, end)."""
    return sum(len(v) for k, v in patch.model_dump().items() if k != "mode" and isinstance(v, str))


def precheck(template: str, patch: Patch, fixture_vars: Sequence[Mapping[str, str]],
             max_patch_chars: int) -> dict[str, bool]:
    """{applies, jinja_parses, renders, within_size}. Later checks need earlier ones, so they are
    False when an earlier one fails. `renders` means the patched template renders for every
    fixture variable set (an empty list is checked against no variables, which fails for any
    template that uses one: a missing fixture is loud, not a silent pass)."""
    out = {"applies": False, "jinja_parses": False, "renders": False,
           "within_size": patch_chars(patch) <= max_patch_chars}
    try:
        patched = apply_patch(template, patch)
    except ValueError:
        return out
    out["applies"] = True
    try:
        needed, compiled = _compile(patched)
    except jinja2.TemplateSyntaxError:
        return out
    out["jinja_parses"] = True
    try:
        for variables in list(fixture_vars) or [{}]:
            _fill(needed, compiled, variables)
    except Exception:  # noqa: BLE001  undefined name, bad filter input, sandbox refusal: all "does not render"
        return out
    out["renders"] = True
    return out


# --- fixture variable sets --------------------------------------------------------------------

def fixture_variable_sets(path: str | Path | None = None) -> list[dict[str, str]]:
    """Variable sets the pre-checks render against: `tests/loop/fixtures/render_vars.yaml`
    (`sets: [{name, vars}]`), or, when that file is absent (an install without tests/), the seed
    personas' `render_vars`. An explicit `path` must exist."""
    if path is None:
        path = RENDER_VARS if RENDER_VARS.exists() else PERSONAS_SEED
        if not Path(path).exists():
            return []
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    entries = raw.get("sets", raw.get("personas", [])) if isinstance(raw, dict) else raw
    return [{str(k): str(v) for k, v in (e.get("vars") or e.get("render_vars") or e).items()}
            for e in entries]
