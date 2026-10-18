import json
from dataclasses import dataclass

from .. import llm
from ..db import database, transaction
from ..textnorm import model_key
from ..validation import PipelineError, ValidationError, clean_value, valid_key

SALES_KEYS = {
    "price",
    "price_krw",
    "list_price",
    "discount",
    "url",
    "seller",
    "retailer",
    "shipping_fee",
    "shipping",
    "delivery",
    "availability",
    "stock",
    "rating",
    "review_count",
}


@dataclass
class Target:
    product_id: int
    category_id: str
    mode: str
    identity: dict
    attributes: list
    details_md: str


def validate_extraction(output, categories):
    category = output["category"]
    key = category["category_id"]
    if not valid_key(key) or (not category["is_new"] and key not in categories):
        raise ValidationError(f"유효하지 않은 카테고리: {key}; 기존 카테고리: {list(categories)}")
    identity = {key: value.strip() for key, value in output["identity"].items()}
    if not all(identity.values()):
        raise ValidationError("brand, model, name은 빈 문자열일 수 없습니다")
    seen, attributes = set(), []
    for field in output["attributes"]:
        key = field["key"]
        if key in SALES_KEYS or key.startswith("price_"):
            continue
        if not valid_key(key) or key in seen:
            raise ValidationError(f"속성 키 오류/중복: {key}")
        seen.add(key)
        if field["unit"] is not None and field["value_type"] != "number":
            raise ValidationError(f"숫자 이외 단위 금지: {key}")
        attributes.append({**field, "value": clean_value(field["value"], field["value_type"])})
    return {**output, "identity": identity, "attributes": attributes}


def run(ctx, snapshot):
    with database(ctx.settings.db_path) as conn:
        categories = [
            dict(r)
            for r in conn.execute("SELECT category_id,display_name FROM categories ORDER BY category_id")
        ]
        for category in categories:
            category["core_keys"] = [
                r[0]
                for r in conn.execute(
                    "SELECT key FROM category_attributes WHERE category_id=? AND tier='core' ORDER BY key",
                    (category["category_id"],),
                )
            ]
    output = llm.invoke(
        ctx,
        "extract_objective",
        "codex",
        "extract_objective.md",
        "objective_extraction.schema.json",
        {
            "url": ctx.job["url"],
            "category_hint": ctx.job["category_hint"],
            "existing_categories": categories,
            "page": {k: v for k, v in snapshot.items() if k != "reviews"},
        },
        lambda o: validate_extraction(o, {c["category_id"] for c in categories}),
    )
    category, identity = output["category"], output["identity"]
    cid, mk = category["category_id"], model_key(identity["brand"], identity["model"])
    with database(ctx.settings.db_path) as conn, transaction(conn):
        conn.execute(
            "INSERT OR IGNORE INTO categories(category_id,display_name) VALUES(?,?)",
            (cid, category["display_name"]),
        )
        existing = conn.execute(
            "SELECT * FROM products WHERE category_id=? AND model_key=?", (cid, mk)
        ).fetchone()
        if existing and existing["status"] == "ready":
            pid, mode = existing["product_id"], "update"
        else:
            if existing:
                conn.execute("DELETE FROM products WHERE product_id=?", (existing["product_id"],))
            count = conn.execute(
                "SELECT COUNT(*) FROM products WHERE category_id=? AND status IN ('ready','collecting')",
                (cid,),
            ).fetchone()[0]
            if count >= ctx.settings.limits["max_products_per_category"]:
                raise PipelineError("카테고리 상품 수 한도 초과")
            pid = conn.execute(
                "INSERT INTO products(category_id,name,brand,model,model_key,source_url,details_md,raw_attributes) VALUES(?,?,?,?,?,?,?,?)",
                (
                    cid,
                    identity["name"],
                    identity["brand"],
                    identity["model"],
                    mk,
                    ctx.job["url"],
                    output["details_markdown"],
                    json.dumps(output["attributes"], ensure_ascii=False),
                ),
            ).lastrowid
            mode = "new"
        conn.execute(
            "UPDATE jobs SET product_id=?,is_update=? WHERE job_id=?",
            (pid, int(mode == "update"), ctx.job_id),
        )
    ctx.job.update(product_id=pid, is_update=int(mode == "update"))
    return Target(pid, cid, mode, identity, output["attributes"], output["details_markdown"])
