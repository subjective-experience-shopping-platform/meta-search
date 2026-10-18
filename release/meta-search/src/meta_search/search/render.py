import json

OPS = {
    "eq": "=",
    "ne": "≠",
    "gte": "≥",
    "lte": "≤",
    "between": "범위",
    "in": "∈",
    "not_in": "∉",
    "contains_any": "교집합",
    "contains_all": "⊇",
    "exists": "존재",
}


def escape(value):
    return str(value).replace("\n", " ").replace("|", "\\|")


def labels(ids, units, field):
    raw = "raw_aspect" if field == "aspect_variant_id" else "raw_status"
    values = sorted({u[raw] for u in units if u[field] in (ids or []) and u[raw] is not None})
    return ", ".join(values[:3]) + (f" 외 {len(values) - 3}개" if len(values) > 3 else "")


def applied_filters(
    category, filters, offer_filter, conditions, sort, units, counts, user_request, notes, definitions=()
):
    lines = [
        f"**적용 조건** — 카테고리: {escape(category['display_name'])} (상품 {category['product_count']}개)"
    ]
    if user_request:
        lines.append(f"- 요청: {escape(user_request)}")
    lines.extend(f"- 해석: {escape(note)}" for note in notes)
    by_key = {d["key"]: d for d in definitions}
    objective = []
    for condition in filters:
        definition = by_key.get(condition.key, {})
        name = definition.get("display_name", condition.key)
        if condition.op == "exists":
            objective.append(f"{name}({condition.key}) {'있음' if condition.value else '없음'}")
        else:
            unit = definition.get("unit") or ""
            objective.append(
                f"{name}({condition.key}) {OPS[condition.op]} {json.dumps(condition.value, ensure_ascii=False)} {unit}".rstrip()
            )
    lines.append("- 객관: " + (" · ".join(objective) or "제한 없음"))
    if offer_filter:
        sales = []
        for field, label in (
            ("max_price", "최저가 ≤"),
            ("min_price", "판매가 ≥"),
            ("max_shipping_fee", "배송비 ≤"),
            ("max_shipping_days", "배송일 ≤"),
        ):
            value = getattr(offer_filter, field)
            if value is not None:
                sales.append(
                    f"{label} {value}"
                    + ("일" if field == "max_shipping_days" else " " + offer_filter.currency)
                )
        if offer_filter.availability:
            sales.append("재고: " + ", ".join(offer_filter.availability))
        if offer_filter.retailers:
            sales.append("판매처: " + ", ".join(offer_filter.retailers))
        lines.append("- 판매: " + (" · ".join(sales) or "전체 판매처"))
    else:
        lines.append("- 판매: 제한 없음")
    for c in conditions:
        aspect = labels(c.aspect_variant_ids, units, "aspect_variant_id")
        state = labels(c.desired_state_variant_ids, units, "state_variant_id") or "모든 상태"
        text = f"- 체감: {c.condition_id} = {aspect}{{{state}}} 리뷰 ≥ {c.min_matching_reviews}"
        if c.contrary_state_variant_ids:
            text += " (상반: " + labels(c.contrary_state_variant_ids, units, "state_variant_id")
            if c.max_contrary_reviews is not None:
                text += f" ≤ {c.max_contrary_reviews}개"
            text += ")"
        if c.sentiments:
            text += " · 감성: " + ", ".join(c.sentiments)
        lines.append(text)
    if not conditions:
        lines.append("- 체감: 제한 없음")
    order = sort.order or ("desc" if sort.by == "experience" else "asc")
    sort_label = (
        "최저가"
        if sort.by == "price"
        else sort.key
        if sort.by == "attribute"
        else (
            "체감 점수("
            + labels(sort.aspect_variant_ids, units, "aspect_variant_id")
            + (
                " 긍정 Wilson 하한"
                if sort.metric == "positive_wilson_lower"
                else " "
                + labels(sort.desired_state_variant_ids, units, "state_variant_id")
                + " 상태 Wilson 하한"
            )
            + ")"
        )
    )
    lines.append(f"- 정렬: {sort_label} {'내림차순' if order == 'desc' else '오름차순'}")
    lines.append(f"- 결과: 일치 {counts[0]} · 판정 불가 {counts[1]} · 제외 {counts[2]}")
    return "\n".join(lines)
