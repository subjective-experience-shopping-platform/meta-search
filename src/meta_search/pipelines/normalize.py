import json
from collections import Counter, defaultdict
from pathlib import Path

import jsonschema

from ..validation import ValidationError, clean_value, valid_key


def build_normalization_input(category, existing_products, new_product):
    index, products = {}, []
    for product in [*existing_products, new_product]:
        fields = []
        for field in product["fields"]:
            fid = f"f{len(index) + 1:03d}"
            index[fid] = {
                **field,
                "product_ref": product["product_id"],
                "source": field.get("source", "page"),
            }
            fields.append(
                {
                    "field_id": fid,
                    "source_tier": field.get("source_tier", "new"),
                    "name": field["name"],
                    "display_name": field["display_name"],
                    "value_type": field["value_type"],
                    "unit": field["unit"],
                    "value": field["value"],
                }
            )
        products.append({"product_id": product["product_id"], "name": product["name"], "fields": fields})
    payload = {
        "category": category["category_id"],
        "base_version": category["taxonomy_version"],
        "taxonomy_status": category["taxonomy_status"],
        "existing_taxonomy": category["attributes"],
        "products": products,
    }
    return payload, index


def _fail(code, target, reason):
    raise ValidationError(f"{code} {target}: {reason}")


def _select(assignments, index, key, warnings):
    grouped = defaultdict(list)
    for assignment in assignments:
        field = index[assignment["field_id"]]
        grouped[field["product_ref"]].append((field, assignment["value"]))
    chosen = {}
    for product, pairs in grouped.items():
        different = any(value != pairs[0][1] for _, value in pairs[1:])
        if different:
            page = [(field, value) for field, value in pairs if field["source"] == "page"]
            if len(page) != 1:
                _fail("V09", key, f"{product}: 같은 상품의 다른 값을 통합하지 말 것")
            chosen[product] = page[0]
            warnings.append(f"V09: {product} {key} — page 값 채택")
        else:
            chosen[product] = next((pair for pair in pairs if pair[0]["source"] == "page"), pairs[0])
    return chosen


def validate_plan(plan, payload, index, schema=None):
    if schema is None:
        schema = json.loads(
            (Path(__file__).resolve().parents[3] / "schemas/normalization_plan.schema.json").read_text()
        )
    try:
        jsonschema.validate(plan, schema)
    except jsonschema.ValidationError as error:
        _fail("V01", "schema", error.message)
    if plan["category"] != payload["category"] or plan["base_version"] != payload["base_version"]:
        _fail("V01", "category/base_version", "입력 불일치")
    ids = [a["field_id"] for g in plan["groups"] for a in g["assignments"]] + [
        a["field_id"] for a in plan["needs_review"]
    ]
    if Counter(ids) != Counter(index.keys()):
        _fail("V02", ",".join(ids), "모든 field_id는 정확히 한 번 배정되어야 합니다")
    keys = set()
    for group in plan["groups"]:
        key, kind = group["canonical_key"], group["value_type"]
        if not valid_key(key) or key in keys or not group["assignments"]:
            _fail("V03", key, "유효한 고유 키와 비어 있지 않은 assignments 필요")
        keys.add(key)
        if (group["tier"] == "core") != all(group["criteria"].values()):
            _fail("V04", key, "tier와 criteria 불일치")
        if kind != "number" and group["unit"] is not None:
            _fail("V05", key, "숫자 이외 단위 금지")
        for assignment in group["assignments"]:
            fid = assignment["field_id"]
            field = index[fid]
            try:
                value = clean_value(assignment["value"], kind)
                original = clean_value(field["value"], field["value_type"])
            except ValidationError as error:
                _fail("V05", fid, str(error))
            if kind != field["value_type"]:
                _fail("V05", fid, "원본 타입 변경 금지")
            if assignment["value_operation"] == "keep" and value != original:
                _fail("V06", fid, "keep 원본값 변경")
            if assignment["value_operation"] == "alias" and kind not in ("string", "string_list"):
                _fail("V07", fid, "숫자/boolean alias 금지")
            if field["unit"] != group["unit"]:
                _fail("V08", fid, "서로 다른 단위 통합/변경 금지")
        _select([{**a, "value": clean_value(a["value"], kind)} for a in group["assignments"]], index, key, [])
    return plan


def apply_plan(plan, index):
    definitions, values, warnings = [], [], []
    for group in plan["groups"]:
        key = group["canonical_key"]
        definitions.append(
            {
                "key": key,
                **{
                    k: group[k]
                    for k in ("display_name", "definition", "tier", "value_type", "unit", "reason")
                },
            }
        )
        assignments = [
            {**a, "value": clean_value(a["value"], group["value_type"])} for a in group["assignments"]
        ]
        for product, (field, value) in _select(assignments, index, key, warnings).items():
            values.append({"product_ref": product, "key": key, "value": value, "source": field["source"]})
    keys = {d["key"] for d in definitions}
    for item in plan["needs_review"]:
        field = index[item["field_id"]]
        key, suffix = field["name"], 2
        while key in keys:
            key = f"{field['name']}_{suffix}"
            suffix += 1
        keys.add(key)
        definitions.append(
            {
                "key": key,
                "display_name": field["display_name"],
                "definition": "",
                "tier": "extra",
                "value_type": field["value_type"],
                "unit": field["unit"],
                "reason": "needs_review: " + item["reason"],
            }
        )
        values.append(
            {
                "product_ref": field["product_ref"],
                "key": key,
                "value": clean_value(field["value"], field["value_type"]),
                "source": field["source"],
            }
        )
        warnings.append(f"needs_review: {field['product_ref']} {field['name']} — {item['reason']}")
    return definitions, values, warnings
