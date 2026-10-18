from .. import llm
from ..textnorm import norm_text
from ..validation import PipelineError, ValidationError


def validate_snapshot(output, max_reviews):
    if not output["page_ok"]:
        raise PipelineError(output["failure_reason"] or "상품 페이지 접근 실패")
    if not output["spec_text"].strip() and not output["description_text"].strip():
        raise ValidationError("사양과 설명이 모두 비어 있습니다")
    seen, reviews = set(), []
    for review in output["reviews"]:
        body = review["body"].strip()
        key = norm_text(body)
        if not key or key in seen:
            continue
        seen.add(key)
        rating = review["rating"]
        reviews.append(
            {**review, "body": body, "rating": rating if rating is None or 0 <= rating <= 5 else None}
        )
        if len(reviews) >= max_reviews:
            break
    return {**output, "reviews": reviews}


def run(ctx):
    output = llm.invoke(
        ctx,
        "collect_page",
        "agy",
        "collect_page.md",
        "page_snapshot.schema.json",
        {"url": ctx.job["url"], "max_reviews": ctx.settings.limits["max_reviews_per_product"]},
        lambda o: validate_snapshot(o, ctx.settings.limits["max_reviews_per_product"]),
        ctx.settings.agy["collect_model"],
    )
    ctx.write_json("page_snapshot.json", output)
    return output
