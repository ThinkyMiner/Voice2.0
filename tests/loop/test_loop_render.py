"""loop/render.py: markers, Jinja, strict variables, patches against config._resolve, pre-checks."""

import re

import jinja2
import pytest
from pydantic import TypeAdapter

from abtest import config
from abtest.contracts import (AppendPatch, FullPatch, Patch, ReplaceSectionPatch, ReplaceTextPatch,
                              VariantSpec)
from abtest.loop import render
from abtest.loop.contracts import ROOT

BOM = "﻿"
PROMPT_PATH = "prompts/base_buyer_vani.txt"
RENDER_VARS = ROOT / "tests" / "loop" / "fixtures" / "render_vars.yaml"


@pytest.fixture(scope="module")
def real_prompt() -> str:
    return (ROOT / PROMPT_PATH).read_bytes().decode("utf-8")


@pytest.fixture(scope="module")
def var_sets() -> list[dict[str, str]]:
    return render.fixture_variable_sets(RENDER_VARS)


def marker(name: str) -> str:
    return f"{BOM} {name} {BOM}"


def agent(name: str) -> str:
    return f"{BOM}@ {name}{BOM}"


# --- the real prompt ----------------------------------------------------------------------------

def test_template_variables_of_the_real_prompt(real_prompt):
    names = render.template_variables(real_prompt)
    assert len(names) == 29
    assert {"bot_name", "initial_message", "turn_intent"} <= names        # marker-only variables
    assert {"is_enrich", "lead_type", "language_name", "pcd_pitch"} <= names   # tag-only variables
    # agent/tool references are text the bot reads, not variables to fill
    assert not names & {"call_outcome", "buyer_disposition", "update_ast_buy_variables_by_city_tool",
                        "additional_details"}


def test_fixture_sets_cover_every_variable(real_prompt, var_sets):
    assert len(var_sets) == 3
    needed = render.template_variables(real_prompt)
    for vs in var_sets:
        assert needed <= set(vs), needed - set(vs)
        assert all(isinstance(v, str) for v in vs.values())


def test_fixture_sets_take_both_sides_of_the_switches(var_sets):
    assert {vs["is_enrich"] for vs in var_sets} == {"1", "0"}
    pns = [vs["ast_seller_pns"] for vs in var_sets]
    assert "" in pns and any(p not in ("", "0", "00") for p in pns)
    assert {vs["quantity_unit_options"] for vs in var_sets} >= {"[]"}
    assert any(vs["quantity_unit_options"] not in ("[]", "") for vs in var_sets)
    assert {vs["language_name"] for vs in var_sets} >= {"Hindi", "English", "telugu"}


@pytest.mark.parametrize("i", [0, 1, 2])
def test_real_prompt_renders_for_every_fixture_set(real_prompt, var_sets, i):
    out = render.render(real_prompt, var_sets[i])
    assert "{%" not in out and "{{" not in out and "%}" not in out
    assert f"You are {var_sets[i]['bot_name']} ," in out                    # a plain marker, filled in
    assert len(out) < len(real_prompt)                                      # untaken branches are gone
    # every BOM left in the output belongs to an agent/tool reference, none to a plain marker
    leftover = re.findall(BOM + r"[^" + BOM + r"\n]*" + BOM, out)
    assert leftover and all(m.strip(BOM).lstrip().startswith("@") for m in leftover)


def test_agent_markers_are_kept_verbatim(real_prompt, var_sets):
    out = render.render(real_prompt, var_sets[0])
    assert f"{BOM}@ call_outcome{BOM}" in out
    assert f"{BOM}@ update_ast_buy_variables_by_city_tool{BOM}" in out
    assert f"{BOM}@ buyer_disposition{BOM}" in out


def test_branches_follow_the_values(real_prompt, var_sets):
    live, plain, telugu = (render.render(real_prompt, vs) for vs in var_sets)
    assert "Live Seller Connection - Highest Priority" in live          # is_enrich == '1' and a pns
    assert "Live Seller Connection - Highest Priority" not in plain
    assert "prathyaksha vikretha" in telugu                             # language_name == 'telugu'
    assert "prathyaksha vikretha" not in live and "prathyaksha vikretha" not in plain
    assert "Quantity Collection" in live and "Quantity Collection" not in plain   # "[]" hides it


def test_a_missing_variable_is_an_error_never_a_blank(real_prompt, var_sets):
    for name in ("bot_name", "off_topic_counter", "specification_options"):
        broken = {k: v for k, v in var_sets[0].items() if k != name}
        with pytest.raises(jinja2.UndefinedError, match=name):
            render.render(real_prompt, broken)


def test_extra_variables_are_ignored(real_prompt, var_sets):
    assert render.render(real_prompt, {**var_sets[0], "unused": "x"}) == render.render(real_prompt, var_sets[0])


# --- markers and Jinja on small templates -----------------------------------------------------------

def test_marker_spacing_variants_all_fill():
    t = f"a {BOM}x{BOM} b {BOM} x {BOM} c {BOM}\tx {BOM}."
    assert render.render(t, {"x": "1"}) == "a 1 b 1 c 1."


def test_template_variables_reads_markers_and_tags():
    t = f"hi {marker('who')} {{% if flag == '1' and n|int > 2 %}}{agent('tool')}{{% endif %}}"
    assert render.template_variables(t) == {"who", "flag", "n"}


def test_strict_undefined_for_a_tag_variable():
    with pytest.raises(jinja2.UndefinedError, match="flag"):
        render.render("{% if flag == '1' %}x{% endif %}", {})


def test_literal_braces_render_as_typed_and_do_not_become_variables():
    t = "Say {{ not_a_variable }} and {# not a comment #} and {{ '{{' }}"
    assert render.template_variables(t) == set()
    assert render.render(t, {}) == t


def test_a_value_is_not_evaluated_again():
    assert render.render(f"Hi {marker('who')}", {"who": "{{ 7 * 7 }}{% if 1 %}"}) == "Hi {{ 7 * 7 }}{% if 1 %}"


def test_a_bom_pair_that_is_not_a_marker_stays_text():
    t = f"a {BOM}two words{BOM} c, {BOM}@ not a name{BOM}, and a lone one: {BOM}"
    assert render.template_variables(t) == set()
    assert render.render(t, {}) == t


def test_newlines_and_the_trailing_newline_are_kept():
    t = f"line one\n{{% if x == '1' %}}\nyes\n{{% endif %}}\nend {marker('y')}\n"
    assert render.render(t, {"x": "0", "y": "Y"}) == "line one\n\nend Y\n"


def test_a_tag_that_does_not_parse_is_a_syntax_error():
    with pytest.raises(jinja2.TemplateSyntaxError):
        render.render("{% if x %}no end", {"x": "1"})


def test_the_sandbox_refuses_attribute_tricks():
    t = "{% if ''.__class__.__mro__ %}x{% endif %}"
    with pytest.raises(jinja2.TemplateError):
        render.render(t, {})


# --- apply_patch == config._resolve -------------------------------------------------------------------

def resolve(patch: Patch | None, base: str = PROMPT_PATH) -> str:
    return config._resolve("B", VariantSpec(base=base, patch=patch))


def real_patches() -> list[Patch]:
    return [
        AppendPatch(mode="append", text="Never repeat the same question."),
        ReplaceTextPatch(mode="replace_text", find="Lead Quality Mandate:", replace="Lead rules:"),
        ReplaceTextPatch(mode="replace_text", find="Role & Persona:\n", replace=""),
        ReplaceSectionPatch(mode="replace_section", start="Lead Quality Mandate:\n",
                            end="Avoid incomplete, inaccurate", text="A lead needs a product and a city.\n"),
    ]


@pytest.mark.parametrize("patch", real_patches(), ids=lambda p: p.mode)
def test_apply_patch_equals_config_resolve_on_the_real_prompt(real_prompt, patch):
    assert render.apply_patch(real_prompt, patch) == resolve(patch)


@pytest.mark.parametrize("ends_with_newline", [True, False])
def test_append_newline_handling_matches_config(tmp_path, ends_with_newline):
    base = tmp_path / "p.txt"
    base.write_bytes(("line one\nline two" + ("\n" if ends_with_newline else "")).encode())
    patch = AppendPatch(mode="append", text="added")
    assert render.apply_patch(base.read_text(), patch) == resolve(patch, str(base))
    assert render.apply_patch("x\n", patch) == "x\nadded\n" and render.apply_patch("x", patch) == "x\nadded\n"


@pytest.mark.parametrize("patch", [
    ReplaceTextPatch(mode="replace_text", find="this sentence is nowhere", replace="x"),   # 0 times
    ReplaceTextPatch(mode="replace_text", find="buyer", replace="x"),                      # many times
    ReplaceSectionPatch(mode="replace_section", start="Avoid incomplete, inaccurate",
                        end="Lead Quality Mandate:", text="x"),                            # end before start
    ReplaceSectionPatch(mode="replace_section", start="nowhere at all", end="Lead Quality Mandate:", text="x"),
], ids=["absent", "ambiguous", "reversed", "start-absent"])
def test_apply_patch_fails_where_config_fails(real_prompt, patch):
    with pytest.raises(ValueError):
        resolve(patch)
    with pytest.raises(ValueError):
        render.apply_patch(real_prompt, patch)


def test_find_must_occur_exactly_once_overlaps_included():
    patch = ReplaceTextPatch(mode="replace_text", find="aa", replace="b")
    with pytest.raises(ValueError, match="2 times"):
        render.apply_patch("aaa", patch)           # overlapping: positions 0 and 1
    assert render.apply_patch("aab", patch) == "bb"


def test_full_patches_are_rejected():
    with pytest.raises(ValueError, match="full"):
        render.apply_patch("base", FullPatch(mode="full", text="a whole new prompt"))


# --- precheck -----------------------------------------------------------------------------------------

def check(real_prompt, var_sets, patch, max_chars=1500):
    return render.precheck(real_prompt, patch, var_sets, max_chars)


def test_precheck_passes_a_good_patch(real_prompt, var_sets):
    patch = AppendPatch(mode="append", text="Never repeat the same question.")
    assert check(real_prompt, var_sets, patch) == {"applies": True, "jinja_parses": True, "renders": True,
                                                   "within_size": True}


def test_precheck_catches_a_patch_that_breaks_an_if_block(real_prompt, var_sets):
    # drops the opening tag of the Telugu block: its {%endif%} is now unmatched
    patch = ReplaceTextPatch(mode="replace_text", find="{%if language_name == 'telugu'%}", replace="")
    assert check(real_prompt, var_sets, patch) == {"applies": True, "jinja_parses": False, "renders": False,
                                                   "within_size": True}
    unclosed = AppendPatch(mode="append", text="{% if is_enrich == '1' %} only for enrichment calls")
    assert check(real_prompt, var_sets, unclosed)["jinja_parses"] is False


def test_precheck_catches_an_oversize_patch(real_prompt, var_sets):
    patch = AppendPatch(mode="append", text="x" * 1501)
    got = check(real_prompt, var_sets, patch)
    assert got["within_size"] is False and got["applies"] and got["renders"]
    assert check(real_prompt, var_sets, AppendPatch(mode="append", text="x" * 1500))["within_size"] is True


def test_within_size_counts_every_text_field():
    patch = ReplaceTextPatch(mode="replace_text", find="a" * 10, replace="b" * 10)
    assert render.patch_chars(patch) == 20
    assert render.precheck("a" * 10, patch, [{}], 20)["within_size"] is True
    assert render.precheck("a" * 10, patch, [{}], 19)["within_size"] is False
    section = ReplaceSectionPatch(mode="replace_section", start="s", end="e", text="ttt")
    assert render.patch_chars(section) == 5


def test_precheck_catches_a_patch_that_does_not_apply(real_prompt, var_sets):
    patch = ReplaceTextPatch(mode="replace_text", find="nowhere in the prompt", replace="x")
    assert check(real_prompt, var_sets, patch) == {"applies": False, "jinja_parses": False, "renders": False,
                                                   "within_size": True}


def test_precheck_catches_a_patch_that_adds_an_unknown_variable(real_prompt, var_sets):
    patch = AppendPatch(mode="append", text=f"Greet them as {marker('nickname')}.")
    got = check(real_prompt, var_sets, patch)
    assert got["applies"] and got["jinja_parses"] and got["renders"] is False


def test_precheck_catches_a_patch_that_runs_code(real_prompt, var_sets):
    patch = AppendPatch(mode="append", text="{% if ''.__class__.__mro__ %}x{% endif %}")
    assert check(real_prompt, var_sets, patch)["renders"] is False


def test_precheck_lets_literal_braces_through(real_prompt, var_sets):
    patch = AppendPatch(mode="append", text='Reply in JSON like {{"intent": "ask"}}.')
    assert all(check(real_prompt, var_sets, patch).values())
    assert 'Reply in JSON like {{"intent": "ask"}}.' in render.render(
        render.apply_patch(real_prompt, patch), var_sets[0])


def test_precheck_without_fixtures_is_loud_not_silent(real_prompt):
    got = render.precheck(real_prompt, AppendPatch(mode="append", text="x"), [], 1500)
    assert got["renders"] is False


def test_precheck_renders_against_every_fixture_set():
    t = f"{{% if flag == '1' %}}{marker('a')}{{% endif %}}"
    patch = AppendPatch(mode="append", text="ok")
    both = [{"flag": "1", "a": "x"}, {"flag": "0", "a": "y"}]
    assert render.precheck(t, patch, both, 100)["renders"] is True
    assert render.precheck(t, patch, [*both, {"flag": "1"}], 100)["renders"] is False   # one set lacks `a`


def test_patch_type_adapter_round_trip_is_what_the_proposer_uses():
    patch = TypeAdapter(Patch).validate_python({"mode": "append", "text": "t"})
    assert render.apply_patch("x\n", patch) == "x\nt\n"


# --- fixture loading ----------------------------------------------------------------------------------

def test_fixture_variable_sets_default_and_personas_fallback(monkeypatch, tmp_path):
    assert len(render.fixture_variable_sets()) == 3
    monkeypatch.setattr(render, "RENDER_VARS", tmp_path / "missing.yaml")
    seeds = render.fixture_variable_sets()
    assert len(seeds) == 10 and all("product_name" in s for s in seeds)
    monkeypatch.setattr(render, "PERSONAS_SEED", tmp_path / "missing_too.yaml")
    assert render.fixture_variable_sets() == []
    with pytest.raises(OSError):
        render.fixture_variable_sets(tmp_path / "explicit_but_absent.yaml")
