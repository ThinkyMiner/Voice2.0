import json

import pytest

from abtest.ext import rubrics
from abtest.ext.models import CUSTOM_RUBRICS

QA_COLUMNS = [
    "Outcome Accuracy (Intent)", "Quantity Accuracy (Numeric)", "Quantity Unit Accuracy",
    "Quantity Probing Error", "Quantity Missed Opportunity (Capture)", "Spec1 Accuracy", "Spec2 Accuracy",
    "Spec Missed Opportunity", "Additional Details Accuracy", "Additional Details Missed Opportunity",
    "Looping Behavior", "WER/STT", "Dead air", "Pronunciation", "Product Change/ No Product Accuracy",
    "MCAT/ Search errors", "Category ISQ's Error", "OnCall AstBuy Error (Transferred)",
]
NEW = {"id": "polite_close", "name": "Polite close", "category": "Custom",
       "question": "Did the bot close the call politely?", "guidance": "A thank-you is enough."}


def test_builtin_rubrics_are_the_qa_matrix_plus_prompt_rules():
    rs = rubrics.builtin()
    qa = [r for r in rs if r.qa_column]
    assert [r.qa_column for r in qa] == QA_COLUMNS            # one per graded column, sheet order
    assert all(r.id == rubrics.snake(r.qa_column) and r.builtin and r.active for r in qa)
    root = {r.id: r.root_type for r in rs}
    assert root["wer_stt"] == "asr"
    assert {i for i, t in root.items() if t == "platform"} == {"dead_air", "pronunciation"}
    assert {r.id for r in rs if r.needs_audio} == {"dead_air", "pronunciation"}
    assert {i for i, t in root.items() if t == "knowledge"} == {"mcat_search_errors", "category_isqs_error"}
    assert {i for i, t in root.items() if t == "flow"} == {"product_change_no_product_accuracy",
                                                            "oncall_astbuy_error_transferred"}
    prompt_rules = [r for r in rs if not r.qa_column]
    assert 2 <= len(prompt_rules) <= 3
    assert all(r.builtin and r.author == "Vani prompt rules" and r.category == "Compliance" for r in prompt_rules)
    assert len({r.id for r in rs}) == len(rs)


def test_save_creates_version_1_then_appends_versions():
    r1 = rubrics.save(NEW, "asha")
    assert (r1.version, r1.builtin, r1.author) == (1, False, "asha")
    r2 = rubrics.save({"id": "polite_close", "guidance": "Thank the buyer by name.", "version": 9}, "ravi")
    assert (r2.version, r2.author, r2.question) == (2, "ravi", NEW["question"])   # merged; version is server-set
    stored = json.loads(CUSTOM_RUBRICS.read_text())
    assert [(d["id"], d["version"]) for d in stored] == [("polite_close", 1), ("polite_close", 2)]
    latest = [r for r in rubrics.all_rubrics() if r.id == "polite_close"]
    assert latest == [r2] and rubrics.get("polite_close") == r2
    assert rubrics.all_rubrics()[-1] == r2                    # custom rubrics come after the built-ins


def test_id_is_derived_from_the_name_when_missing():
    assert rubrics.save({k: v for k, v in NEW.items() if k != "id"}, "asha").id == "polite_close"


def test_custom_version_overrides_a_builtin_in_place():
    before = [r.id for r in rubrics.all_rubrics()]
    r = rubrics.save({"id": "looping_behavior", "guidance": "Stricter."}, "qa lead")
    assert (r.version, r.builtin, r.qa_column, r.guidance) == (2, True, "Looping Behavior", "Stricter.")
    assert [x.id for x in rubrics.all_rubrics()] == before
    assert rubrics.get("looping_behavior").guidance == "Stricter."


def test_archive_is_a_new_version_and_save_restores():
    a = rubrics.archive("dead_air", "qa lead")
    assert (a.version, a.active) == (2, False)
    assert "dead_air" not in {r.id for r in rubrics.all_rubrics()}
    assert rubrics.get("dead_air") == a and a in rubrics.all_rubrics(include_archived=True)
    assert rubrics.archive("dead_air", "qa lead") == a        # archiving twice changes nothing
    assert rubrics.save({"id": "dead_air"}, "qa lead").active
    with pytest.raises(KeyError):
        rubrics.archive("no_such_rubric", "qa lead")


@pytest.mark.parametrize("bad", [
    {**NEW, "id": "Polite Close"},
    {**NEW, "id": "1st_check"},
    {**NEW, "question": "  "},
    {k: v for k, v in NEW.items() if k != "question"},
    {**NEW, "root_type": "vibes"},
    {**NEW, "unknown_field": 1},
])
def test_save_validates(bad):
    with pytest.raises(ValueError):
        rubrics.save(bad, "asha")
    assert not CUSTOM_RUBRICS.exists()


def test_save_needs_an_actor():
    with pytest.raises(ValueError, match="actor"):
        rubrics.save(NEW, " ")


def test_set_hash_tracks_versions_and_texts_not_order():
    rs = rubrics.builtin()
    h = rubrics.set_hash(rs)
    assert rubrics.set_hash(list(reversed(rs))) == h and len(h) == 64
    edited = rs[0].model_copy(update={"guidance": rs[0].guidance + " More."})
    assert rubrics.set_hash([edited, *rs[1:]]) != h
    assert rubrics.set_hash([rs[0].model_copy(update={"version": 2}), *rs[1:]]) != h
    assert rubrics.set_hash([rs[0].model_copy(update={"weight": 1}), *rs[1:]]) == h   # weight is not graded
