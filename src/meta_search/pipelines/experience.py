import re
from concurrent.futures import ThreadPoolExecutor

from .. import llm
from ..db import database, transaction
from ..normalization import match_units
from ..textnorm import make_id
from ..validation import PipelineError


def validate_units(output, review):
    body = re.sub(r"\s+", " ", review)
    valid, dropped = [], 0
    for unit in output["opinion_units"]:
        excerpt = re.sub(r"\s+", " ", unit["excerpt"].strip())
        aspect = unit["raw_aspect"].strip()
        if not aspect or not excerpt or excerpt not in body:
            dropped += 1
            continue
        status = (unit["raw_status"] or "").strip() or None
        valid.append({**unit, "raw_aspect": aspect, "raw_status": status})
    return valid, dropped


def run(ctx, target, snapshot):
    if not snapshot["reviews"]:
        ctx.warn("리뷰 없음")
        # A successful refresh with zero reviews replaces old evidence as well.
        if target.mode == "update":
            persist(ctx, target, [])
        return "skipped"
    with database(ctx.settings.db_path) as conn:
        display_name = conn.execute(
            "SELECT display_name FROM categories WHERE category_id=?", (target.category_id,)
        ).fetchone()[0]

    def extract(index_review):
        i, review = index_review
        try:
            output = llm.invoke(
                ctx,
                f"opinion_{i:03d}",
                "codex",
                "opinion_units.md",
                "opinion_units.schema.json",
                {
                    "product_name": target.identity["name"],
                    "product_category": display_name,
                    "review": review["body"],
                },
            )
            units, dropped = validate_units(output, review["body"])
            if dropped:
                ctx.warn(f"리뷰 {i}: opinion unit {dropped}개 제외(excerpt 불일치 등)")
            return review, units
        except Exception as error:
            ctx.warn(f"리뷰 {i} 추출 실패: {str(error)[:300]}")
            return None

    with ThreadPoolExecutor(max_workers=ctx.settings.codex["review_workers"]) as executor:
        rows = [row for row in executor.map(extract, enumerate(snapshot["reviews"], 1)) if row is not None]
    if not rows:
        raise PipelineError("모든 리뷰의 Opinion Unit 추출 실패")
    persist(ctx, target, rows)


def persist(ctx, target, rows):
    with database(ctx.settings.db_path) as conn, transaction(conn):
        conn.execute("DELETE FROM reviews WHERE product_id=?", (target.product_id,))
        for review, units in rows:
            rid = conn.execute(
                "INSERT INTO reviews(product_id,body,rating,written_at) VALUES(?,?,?,?)",
                (target.product_id, review["body"], review["rating"], review["written_at"]),
            ).lastrowid
            for unit in units:
                conn.execute(
                    "INSERT INTO opinion_units(review_id,product_id,category_id,raw_aspect,raw_status,excerpt,opinion,sentiment,aspect_variant_id,state_variant_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        rid,
                        target.product_id,
                        target.category_id,
                        *(unit[k] for k in ("raw_aspect", "raw_status", "excerpt", "opinion", "sentiment")),
                        make_id("av", target.category_id, unit["raw_aspect"]),
                        make_id("sv", target.category_id, unit["raw_status"]) if unit["raw_status"] else None,
                    ),
                )
        match_units(conn, target.category_id)
