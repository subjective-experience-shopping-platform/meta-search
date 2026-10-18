import json

from .. import llm
from ..db import database, now_utc, transaction
from ..validation import ValidationError, clean_value
from .normalize import apply_plan, build_normalization_input, validate_plan


def run(ctx, target, snapshot):
    with database(ctx.settings.db_path) as conn:
        category = dict(
            conn.execute("SELECT * FROM categories WHERE category_id=?", (target.category_id,)).fetchone()
        )
        category["attributes"] = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM category_attributes WHERE category_id=? ORDER BY key", (target.category_id,)
            )
        ]
        existing = []
        for row in conn.execute(
            "SELECT product_id,name FROM products WHERE category_id=? AND status='ready' ORDER BY product_id",
            (target.category_id,),
        ):
            fields = [
                dict(r)
                for r in conn.execute(
                    "SELECT a.key AS name,a.display_name,a.value_type,a.unit,a.tier AS source_tier,p.value_json,p.source FROM product_attributes p JOIN category_attributes a USING(category_id,key) WHERE p.product_id=? ORDER BY a.key",
                    (row["product_id"],),
                )
            ]
            for field in fields:
                field["value"] = json.loads(field.pop("value_json"))
            existing.append({**dict(row), "fields": fields})
    attributes = [{**a, "source": "page"} for a in target.attributes]
    details = target.details_md
    if existing:
        missing = [
            a
            for a in category["attributes"]
            if a["tier"] == "core" and a["key"] not in {f["key"] for f in attributes}
        ]
        if missing:
            by_key = {a["key"]: a for a in missing}

            def validate_gap(output):
                seen = set()
                for value in output["values"]:
                    key = value["key"]
                    if key in by_key and value["value"] is not None:
                        if key in seen:
                            raise ValidationError(f"누락 조사 키 중복: {key}")
                        seen.add(key)
                        clean_value(value["value"], by_key[key]["value_type"])
                return output

            research = llm.invoke(
                ctx,
                "gap_research",
                "agy",
                "gap_research.md",
                "gap_research.schema.json",
                {
                    "product": {**target.identity, "source_url": ctx.job["url"]},
                    "current_attributes": attributes,
                    "missing_attributes": missing,
                },
                validate_gap,
                ctx.settings.agy["research_model"],
            )
            for item in research["values"]:
                if item["key"] in by_key and item["value"] is not None:
                    attributes.append(
                        {
                            **by_key[item["key"]],
                            "value": clean_value(item["value"], by_key[item["key"]]["value_type"]),
                            "source": "research",
                        }
                    )
            if research["notes_markdown"].strip():
                details += "\n\n## 추가 조사 (agy)\n" + research["notes_markdown"]
        new_product = {
            "product_id": "new",
            "name": target.identity["name"],
            "fields": [{**a, "name": a["key"], "source_tier": "new"} for a in attributes],
        }
        payload, index = build_normalization_input(category, existing, new_product)
        ctx.write_json("normalization_input.json", payload)
        schema = json.loads((ctx.settings.schemas_dir / "normalization_plan.schema.json").read_text())
        plan = llm.invoke(
            ctx,
            "normalize_attributes",
            "agy",
            "normalize_attributes.md",
            "normalization_plan.schema.json",
            {"payload": payload},
            lambda p: validate_plan(p, payload, index, schema),
            ctx.settings.agy["normalize_model"],
        )
        definitions, values, warnings = apply_plan(plan, index)
        taxonomy_status = "normalized"
    else:
        definitions = [
            {
                "key": a["key"],
                "display_name": a["display_name"],
                "definition": "",
                "tier": "core",
                "value_type": a["value_type"],
                "unit": a["unit"],
                "reason": "첫 상품: 잠정 핵심 속성",
            }
            for a in attributes
        ]
        values = [
            {"product_ref": "new", "key": a["key"], "value": a["value"], "source": "page"} for a in attributes
        ]
        warnings, taxonomy_status = [], "provisional"
    old = {a["key"]: a for a in category["attributes"]}
    new = {a["key"]: a for a in definitions}
    diff = {
        "promoted": [k for k in new if k in old and old[k]["tier"] == "extra" and new[k]["tier"] == "core"],
        "demoted": [k for k in new if k in old and old[k]["tier"] == "core" and new[k]["tier"] == "extra"],
        "added": sorted(new.keys() - old.keys()),
        "removed": sorted(old.keys() - new.keys()),
        "renamed": [],
    }
    if existing:
        for group in plan["groups"]:
            for assignment in group["assignments"]:
                original = index[assignment["field_id"]]["name"]
                if original != group["canonical_key"]:
                    entry = {"from": original, "to": group["canonical_key"]}
                    if entry not in diff["renamed"]:
                        diff["renamed"].append(entry)
    with database(ctx.settings.db_path) as conn, transaction(conn):
        conn.execute("DELETE FROM product_attributes WHERE category_id=?", (target.category_id,))
        conn.execute("DELETE FROM category_attributes WHERE category_id=?", (target.category_id,))
        conn.executemany(
            "INSERT INTO category_attributes(category_id,key,display_name,definition,tier,value_type,unit,reason) VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    target.category_id,
                    *(
                        a[k]
                        for k in ("key", "display_name", "definition", "tier", "value_type", "unit", "reason")
                    ),
                )
                for a in definitions
            ],
        )
        conn.executemany(
            "INSERT INTO product_attributes(product_id,category_id,key,value_json,source) VALUES(?,?,?,?,?)",
            [
                (
                    target.product_id if v["product_ref"] == "new" else v["product_ref"],
                    target.category_id,
                    v["key"],
                    json.dumps(v["value"], ensure_ascii=False, allow_nan=False),
                    v["source"],
                )
                for v in values
            ],
        )
        conn.execute(
            "UPDATE categories SET taxonomy_status=?,taxonomy_version=taxonomy_version+1 WHERE category_id=?",
            (taxonomy_status, target.category_id),
        )
        conn.execute(
            "UPDATE products SET details_md=?,updated_at=? WHERE product_id=?",
            (details, now_utc(), target.product_id),
        )
    ctx.write_json("normalization_diff.json", diff)
    for warning in warnings:
        ctx.warn(warning)
