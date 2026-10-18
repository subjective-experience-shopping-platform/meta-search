from copy import deepcopy

import pytest

from meta_search.pipelines.normalize import apply_plan, build_normalization_input, validate_plan
from meta_search.validation import ValidationError


def setup_plan(kind="string", original="텐키리스", unit=None):
    category = {
        "category_id": "keyboard",
        "taxonomy_version": 1,
        "taxonomy_status": "provisional",
        "attributes": [],
    }

    def product(pid, key, value, source="page"):
        return {
            "product_id": pid,
            "name": str(pid),
            "fields": [
                {
                    "name": key,
                    "display_name": key,
                    "value_type": kind,
                    "unit": unit,
                    "value": value,
                    "source": source,
                }
            ],
        }

    payload, index = build_normalization_input(
        category, [product(1, "layout", original)], product("new", "key_layout", original)
    )
    group = {
        "canonical_key": "layout",
        "display_name": "배열",
        "definition": "물리적 배열",
        "value_type": kind,
        "unit": unit,
        "criteria": {"search_value": True, "category_scope": True, "comparable": True},
        "tier": "core",
        "reason": "비교",
        "assignments": [{"field_id": fid, "value": original, "value_operation": "keep"} for fid in index],
    }
    plan = {"category": "keyboard", "base_version": 1, "groups": [group], "needs_review": []}
    return plan, payload, index


def test_alias_merge_T02_T03():
    plan, payload, index = setup_plan()
    for assignment in plan["groups"][0]["assignments"]:
        assignment.update(value="TKL", value_operation="alias")
    validate_plan(plan, payload, index)
    definitions, values, warnings = apply_plan(plan, index)
    assert len(definitions) == 1
    assert [v["value"] for v in values] == ["TKL", "TKL"]
    assert warnings == []


def test_separate_concepts_T04():
    plan, payload, index = setup_plan()
    first = plan["groups"][0]
    second = deepcopy(first)
    second["canonical_key"] = "bottom_force"
    first["assignments"], second["assignments"] = [first["assignments"][0]], [first["assignments"][1]]
    plan["groups"].append(second)
    validate_plan(plan, payload, index)
    assert len(apply_plan(plan, index)[0]) == 2


def test_tier_and_sparse_T05_T06_T07():
    plan, payload, index = setup_plan()
    group = plan["groups"][0]
    group["criteria"]["search_value"] = False
    group["tier"] = "extra"
    validate_plan(plan, payload, index)
    assert apply_plan(plan, index)[0][0]["tier"] == "extra"
    group["criteria"]["search_value"] = True
    group["tier"] = "core"
    index.pop("f002")
    group["assignments"].pop()
    validate_plan(plan, payload, index)
    assert apply_plan(plan, index)[0][0]["tier"] == "core"


@pytest.mark.parametrize(
    "case,code",
    [
        ("category", "V01"),
        ("missing", "V02"),
        ("duplicate", "V02"),
        ("unknown", "V02"),
        ("key", "V03"),
        ("empty", "V03"),
        ("criteria", "V04"),
        ("type", "V05"),
        ("keep", "V06"),
        ("alias", "V07"),
        ("unit", "V08"),
        ("conflict", "V09"),
    ],
)
def test_validation_rules(case, code):
    plan, payload, index = setup_plan("number", 100, "g") if case in ("alias", "unit") else setup_plan()
    group = plan["groups"][0]
    if case == "category":
        plan["category"] = "other"
    if case == "missing":
        group["assignments"].pop()
    if case == "duplicate":
        group["assignments"].append(deepcopy(group["assignments"][0]))
    if case == "unknown":
        group["assignments"][0]["field_id"] = "f999"
    if case == "key":
        group["canonical_key"] = "bad-key"
    if case == "empty":
        group["assignments"] = []
        plan["needs_review"] = [{"field_id": fid, "reason": "unknown"} for fid in index]
    if case == "criteria":
        group["criteria"]["comparable"] = False
    if case == "type":
        group["assignments"][0]["value"] = 1
    if case == "keep":
        group["assignments"][0]["value"] = "TKL"
    if case == "alias":
        group["assignments"][0]["value_operation"] = "alias"
    if case == "unit":
        index["f002"]["unit"] = "kg"
    if case == "conflict":
        index["f002"]["product_ref"] = 1
        index["f002"]["value"] = "FULL"
        group["assignments"][1]["value"] = "FULL"
    with pytest.raises(ValidationError, match=code):
        validate_plan(plan, payload, index)


def test_page_priority_conflict():
    plan, payload, index = setup_plan()
    index["f002"].update(product_ref=1, source="research", value="FULL")
    plan["groups"][0]["assignments"][1]["value"] = "FULL"
    validate_plan(plan, payload, index)
    _, values, warnings = apply_plan(plan, index)
    assert values[0]["value"] == "텐키리스"
    assert len(values) == 1 and len(warnings) == 1


def test_needs_review_T13():
    plan, payload, index = setup_plan()
    plan["needs_review"] = [{"field_id": "f002", "reason": "기준 불명확"}]
    plan["groups"][0]["assignments"].pop()
    index["f002"]["name"] = "layout"
    validate_plan(plan, payload, index)
    definitions, values, warnings = apply_plan(plan, index)
    assert definitions[1]["key"] == "layout_2"
    assert definitions[1]["tier"] == "extra"
    assert len(warnings) == 1 and len(values) == 2
