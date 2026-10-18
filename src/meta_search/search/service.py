import json
import re
from collections import defaultdict
from functools import cmp_to_key

from mcp.server.fastmcp.exceptions import ToolError

from ..policy import ANSWER_POLICY, WORKFLOW
from ..textnorm import norm_text
from . import experience, objective_filter, offers
from .models import ExperienceCondition, ExperienceTopic, ObjectiveFilter, OfferFilter, SortSpec
from .render import applied_filters, escape
from .repository import repository


def _models(items, model):
    return [item if isinstance(item, model) else model.model_validate(item) for item in items]


def _unique(items, field):
    ids = [getattr(item, field) for item in items]
    if len(ids) != len(set(ids)):
        raise ToolError(f"{field}는 호출 안에서 고유해야 합니다")


def _page(limit, offset=0, maximum=100):
    if type(limit) is not int or not 1 <= limit <= maximum or type(offset) is not int or offset < 0:
        raise ToolError(f"limit은 1~{maximum}, offset은 0 이상의 정수여야 합니다")


def _topic_stats(product, topic):
    return {
        "topic_id": topic.topic_id,
        **experience.aggregate(
            product["units"],
            topic.aspect_variant_ids,
            topic.desired_state_variant_ids,
            topic.contrary_state_variant_ids,
        ),
    }


def _top_aspects(units, limit=10):
    groups = defaultdict(list)
    for unit in units:
        groups[unit["aspect_cluster_id"] or "unmatched:" + unit["aspect_variant_id"]].append(unit)
    result = []
    for aid, selected in groups.items():
        variants = sorted({u["aspect_variant_id"] for u in selected})
        stats = experience.aggregate(selected, variants)
        result.append(
            {
                "aspect_cluster_id": selected[0]["aspect_cluster_id"],
                "label": selected[0]["aspect_label"] or selected[0]["raw_aspect"],
                "normalization_status": "matched" if selected[0]["aspect_cluster_id"] else "unmatched",
                "review_count": stats["relevant"],
                "positive": stats["positive"],
                "negative": stats["sentiment_distribution"]["negative"],
                "variant_ids": variants,
            }
        )
    return sorted(result, key=lambda a: (-a["review_count"], a["label"]))[:limit]


class SearchService:
    def __init__(self, settings):
        self.settings = settings

    def get_search_guide(self, category=None):
        with repository(self.settings) as repo:
            output = {"categories": repo.categories, "workflow": WORKFLOW, "answer_policy": ANSWER_POLICY}
            if category is None:
                return output
            cat = repo.category(category)
            products = repo.selected(category=cat["category_id"])
            definitions = [d for d in repo.definitions if d["category_id"] == category]
            attributes = []
            for definition in definitions:
                values = [
                    p["attributes"][definition["key"]]["value"]
                    for p in products
                    if definition["key"] in p["attributes"]
                ]
                kind = definition["value_type"]
                if kind == "number":
                    observed = {
                        "min": min(values) if values else None,
                        "max": max(values) if values else None,
                    }
                elif kind == "boolean":
                    observed = {
                        "true": sum(v is True for v in values),
                        "false": sum(v is False for v in values),
                    }
                else:
                    flattened = (
                        [item for value in values for item in value] if kind == "string_list" else values
                    )
                    observed = sorted(set(flattened))[:20]
                attributes.append(
                    {
                        **definition,
                        "allowed_ops": objective_filter.OPS[kind],
                        "coverage": f"{len(values)}/{len(products)}",
                        "observed": observed,
                    }
                )
            units = repo.units(category)
            output.update(
                attributes=attributes,
                products=[{k: p[k] for k in ("product_id", "name", "brand", "model")} for p in products],
                experience_summary={
                    "review_count": sum(len(p["reviews"]) for p in products),
                    "unit_count": len(units),
                    "aspect_cluster_count": len(
                        {u["aspect_cluster_id"] for u in units if u["aspect_cluster_id"]}
                    ),
                    "top_aspects": [a["label"] for a in _top_aspects(units, 15)],
                },
                offer_filter_fields=list(OfferFilter.model_fields),
                sort_options=SortSpec.model_json_schema(),
            )
            return output

    def search_products(
        self,
        category=None,
        filters=(),
        offer_filter=None,
        experience_conditions=(),
        sort=None,
        evidence_topics=(),
        limit=None,
        user_request=None,
        interpretation_notes=(),
    ):
        filters = _models(filters, ObjectiveFilter)
        conditions = _models(experience_conditions, ExperienceCondition)
        topics = _models(evidence_topics, ExperienceTopic)
        sort = sort if isinstance(sort, SortSpec) else SortSpec.model_validate(sort or {})
        offer_filter = (
            offer_filter
            if isinstance(offer_filter, OfferFilter) or offer_filter is None
            else OfferFilter.model_validate(offer_filter)
        )
        _unique(conditions, "condition_id")
        _unique(topics, "topic_id")
        if limit is not None and (type(limit) is not int or limit < 1):
            raise ToolError("limit은 1 이상의 정수 또는 null이어야 합니다")
        with repository(self.settings) as repo:
            cat = repo.category(category)
            cid = cat["category_id"]
            products, units = repo.selected(category=cid), repo.units(cid)
            definitions = [d for d in repo.definitions if d["category_id"] == cid]
            for condition in filters:
                objective_filter.validate_filter(condition, definitions)
            for topic in [*conditions, *topics]:
                repo.validate_selectors(
                    topic.aspect_variant_ids,
                    topic.desired_state_variant_ids,
                    topic.contrary_state_variant_ids,
                    cid,
                )
            if sort.by == "attribute":
                if not any(
                    d["key"] == sort.key and d["value_type"] in ("number", "string") for d in definitions
                ):
                    raise ToolError(
                        f"정렬 key는 number/string이어야 합니다. 가능 키: {[d['key'] for d in definitions if d['value_type'] in ('number', 'string')]}"
                    )
            if sort.by == "experience":
                repo.validate_selectors(sort.aspect_variant_ids, sort.desired_state_variant_ids, category=cid)
            currency = offer_filter.currency if offer_filter else "KRW"
            rows, undetermined, excluded = [], [], []
            evaluations = []
            for product in products:
                checks = []
                for condition in filters:
                    value = product["attributes"].get(condition.key, {}).get("value")
                    verdict, reason = objective_filter.evaluate(condition, value)
                    checks.append(
                        {
                            "id": f"{condition.key} {condition.op} {json.dumps(condition.value, ensure_ascii=False)}",
                            "type": "objective",
                            "verdict": verdict,
                            "reason": reason,
                        }
                    )
                selected_offers = product["offers"]
                if offer_filter:
                    verdict, selected_offers = offers.evaluate(product["offers"], offer_filter)
                    checks.append(
                        {
                            "id": "offer_filter",
                            "type": "sales",
                            "verdict": verdict,
                            "reason": "판매 조건 충족"
                            if verdict == "PASS"
                            else "판매 조건 미충족"
                            if verdict == "FAIL"
                            else "판매 정보 미확인",
                        }
                    )
                checks.extend(experience.evaluate(product["units"], c) for c in conditions)
                evaluations.append(checks)
                reasons = [c["reason"] for c in checks if c["verdict"] != "PASS"]
                if any(c["verdict"] == "FAIL" for c in checks):
                    excluded.append(
                        {
                            "product_id": product["product_id"],
                            "name": product["name"],
                            "reasons": reasons,
                            "conditions": checks,
                        }
                    )
                    continue
                if any(c["verdict"] == "UNKNOWN" for c in checks):
                    undetermined.append(
                        {
                            "product_id": product["product_id"],
                            "name": product["name"],
                            "reasons": reasons,
                            "conditions": checks,
                        }
                    )
                    continue
                summary = offers.summary(selected_offers, currency)
                scoring = experience.score(product["units"], sort) if sort.by == "experience" else None
                sort_value = (
                    summary["min_price"]
                    if sort.by == "price"
                    else product["attributes"].get(sort.key, {}).get("value")
                    if sort.by == "attribute"
                    else scoring["value"]
                )
                rows.append(
                    {
                        **{k: product[k] for k in ("product_id", "name", "brand", "model")},
                        "core_attributes": {
                            k: a["value"] for k, a in product["attributes"].items() if a["tier"] == "core"
                        },
                        "offer_summary": summary,
                        "conditions": checks,
                        "score": scoring,
                        "evidence_topics": [_topic_stats(product, topic) for topic in topics],
                        "_sort": sort_value,
                        "_price": offers.summary(product["offers"], currency)["min_price"],
                    }
                )
            descending = (sort.order or ("desc" if sort.by == "experience" else "asc")) == "desc"

            def compare(a, b):
                for field, reverse in (("_sort", descending), ("_price", False), ("product_id", False)):
                    x, y = a[field], b[field]
                    if x is None or y is None:
                        if x is not y:
                            return 1 if x is None else -1
                    elif x != y:
                        return ((x > y) - (x < y)) * (-1 if reverse else 1)
                return 0

            rows.sort(key=cmp_to_key(compare))
            total_matches = len(rows)
            rows = rows[:limit]
            for rank, row in enumerate(rows, 1):
                row["rank"] = rank
                row.pop("_sort")
                row.pop("_price")
            suggestions = []
            if not total_matches and evaluations:
                for i, check in enumerate(evaluations[0]):
                    count = sum(
                        all(c["verdict"] == "PASS" for j, c in enumerate(checks) if j != i)
                        for checks in evaluations
                    )
                    if count:
                        prefix = "filter " if check["type"] == "objective" else ""
                        suggestions.append({"remove": prefix + check["id"], "would_match": count})
                suggestions.sort(key=lambda item: (-item["would_match"], item["remove"]))
            return {
                "category": cid,
                "candidate_count": len(products),
                "results": rows,
                "undetermined": undetermined,
                "excluded": excluded,
                "relaxation_suggestions": suggestions,
                "applied_filters_markdown": applied_filters(
                    cat,
                    filters,
                    offer_filter,
                    conditions,
                    sort,
                    units,
                    (total_matches, len(undetermined), len(excluded)),
                    user_request,
                    interpretation_notes,
                    definitions,
                ),
            }

    def compare_products(self, product_ids, attribute_keys=None, experience_topics=()):
        if not 2 <= len(product_ids) <= 5:
            raise ToolError("비교 상품은 2~5개여야 합니다")
        topics = _models(experience_topics, ExperienceTopic)
        _unique(topics, "topic_id")
        with repository(self.settings) as repo:
            products = repo.selected(product_ids)
            categories = {p["category_id"] for p in products}
            if len(categories) != 1:
                raise ToolError("비교 상품은 같은 카테고리여야 합니다")
            cid = next(iter(categories))
            definitions = [d for d in repo.definitions if d["category_id"] == cid]
            keys = (
                attribute_keys
                if attribute_keys is not None
                else [
                    d["key"]
                    for d in definitions
                    if d["tier"] == "core" or any(d["key"] in p["attributes"] for p in products)
                ]
            )
            if set(keys) - {d["key"] for d in definitions}:
                raise ToolError(f"없는 속성 키. 가능 키: {[d['key'] for d in definitions]}")
            for topic in topics:
                repo.validate_selectors(
                    topic.aspect_variant_ids,
                    topic.desired_state_variant_ids,
                    topic.contrary_state_variant_ids,
                    cid,
                )
            attributes = []
            table = [
                "| 속성 | " + " | ".join(escape(p["name"]) for p in products) + " |",
                "|---|" + "---|" * len(products),
            ]
            for definition in definitions:
                key = definition["key"]
                if key not in keys:
                    continue
                values = {str(p["product_id"]): p["attributes"].get(key, {}).get("value") for p in products}
                distinct = {json.dumps(v, sort_keys=True, ensure_ascii=False) for v in values.values()}
                differs = len(distinct) > 1
                attributes.append(
                    {
                        **{k: definition[k] for k in ("key", "display_name", "tier", "unit")},
                        "values": values,
                        "differs": differs,
                    }
                )
                if differs:
                    table.append(
                        "| "
                        + escape(definition["display_name"])
                        + " | "
                        + " | ".join(escape(v) if v is not None else "미확인" for v in values.values())
                        + " |"
                    )
            return {
                "category": cid,
                "products": [{"product_id": p["product_id"], "name": p["name"]} for p in products],
                "attributes": attributes,
                "offers": {str(p["product_id"]): offers.summary(p["offers"]) for p in products},
                "experience": [
                    {
                        "topic_id": t.topic_id,
                        "by_product": {str(p["product_id"]): _topic_stats(p, t) for p in products},
                    }
                    for t in topics
                ],
                "summary_markdown": "\n".join(table),
            }

    def get_product_details(self, product_id, experience_topics=()):
        topics = _models(experience_topics, ExperienceTopic)
        _unique(topics, "topic_id")
        with repository(self.settings) as repo:
            product = repo.selected([product_id])[0]
            for topic in topics:
                repo.validate_selectors(
                    topic.aspect_variant_ids,
                    topic.desired_state_variant_ids,
                    topic.contrary_state_variant_ids,
                    product["category_id"],
                )
            attributes = [
                {k: a[k] for k in ("key", "display_name", "value", "unit", "source")}
                for a in product["attributes"].values()
            ]
            return {
                **{
                    k: product[k]
                    for k in (
                        "product_id",
                        "name",
                        "brand",
                        "model",
                        "source_url",
                        "updated_at",
                        "details_md",
                    )
                },
                "category": product["category_id"],
                "core_attributes": [
                    a for a in attributes if product["attributes"][a["key"]]["tier"] == "core"
                ],
                "extra_attributes": [
                    a for a in attributes if product["attributes"][a["key"]]["tier"] == "extra"
                ],
                "offers": offers.render(product["offers"]),
                "review_summary": {
                    "review_count": len(product["reviews"]),
                    "unit_count": len(product["units"]),
                    "top_aspects": _top_aspects(product["units"]),
                },
                "experience": [_topic_stats(product, t) for t in topics],
            }

    def search_product_docs(self, query, product_ids=None, category=None, max_results=15):
        _page(max_results, maximum=50)
        terms = list(dict.fromkeys(norm_text(s) for s in re.split(r"[\s,]+", query) if norm_text(s)))
        if not terms:
            raise ToolError("query에 검색어를 넣으세요")
        with repository(self.settings) as repo:
            if product_ids is None and category is not None:
                repo.category(category)
            products = repo.selected(product_ids, category)
            results = []
            for product in products:
                heading = None
                for i, line in enumerate(product["details_md"].splitlines(), 1):
                    prior_heading = heading
                    if re.match(r"^\s{0,3}#{1,6}\s+", line):
                        heading = re.sub(r"^\s*#+\s+", "", line).strip()
                    matched = [t for t in terms if t in norm_text(line)]
                    if matched:
                        results.append(
                            {
                                "product_id": product["product_id"],
                                "name": product["name"],
                                "line_no": i,
                                "heading": prior_heading,
                                "line": line,
                                "matched_terms": matched,
                            }
                        )
            results.sort(key=lambda r: (-len(r["matched_terms"]), r["product_id"], r["line_no"]))
            return {"total": len(results), "results": results[:max_results]}

    def find_review_attributes(self, category=None, query="", limit=20, offset=0):
        _page(limit, offset)
        with repository(self.settings) as repo:
            cid = repo.category(category)["category_id"]
            units = repo.units(cid)
            grouped = defaultdict(list)
            for unit in units:
                grouped[unit["aspect_cluster_id"] or "unmatched:" + unit["aspect_variant_id"]].append(unit)
            results = []
            q = norm_text(query)
            for aid, selected in grouped.items():
                label = selected[0]["aspect_label"] or selected[0]["raw_aspect"]
                variants = []
                for vid in sorted({u["aspect_variant_id"] for u in selected}):
                    members = [u for u in selected if u["aspect_variant_id"] == vid]
                    raw = members[0]["raw_aspect"]
                    variants.append(
                        {
                            "aspect_variant_id": vid,
                            "raw_aspect": raw,
                            "review_count": len({u["review_id"] for u in members}),
                            "matched": not q or q in norm_text(raw),
                        }
                    )
                matched = [v["aspect_variant_id"] for v in variants if v["matched"]]
                if not matched and q not in norm_text(label):
                    continue
                results.append(
                    {
                        "aspect_cluster_id": selected[0]["aspect_cluster_id"],
                        "normalization_status": "matched"
                        if selected[0]["aspect_cluster_id"]
                        else "unmatched",
                        "label": label,
                        "review_count": len({u["review_id"] for u in selected}),
                        "unit_count": len(selected),
                        "variants": variants,
                        "matched_selector": matched,
                        "cluster_selector": [v["aspect_variant_id"] for v in variants],
                        "examples": list(dict.fromkeys(u["opinion"] for u in selected))[:3],
                        "_exact": any(norm_text(v["raw_aspect"]) == q for v in variants),
                    }
                )
            results.sort(key=lambda c: (not c["_exact"], -c["review_count"], c["label"]))
            for result in results:
                result.pop("_exact")
            return {"total": len(results), "clusters": results[offset : offset + limit]}

    def list_review_states(self, aspect_variant_ids, product_ids=None, query="", limit=30, offset=0):
        _page(limit, offset)
        with repository(self.settings) as repo:
            repo.validate_selectors(aspect_variant_ids)
            units = [
                u for u in repo.units(product_ids=product_ids) if u["aspect_variant_id"] in aspect_variant_ids
            ]
            groups = defaultdict(list)
            for unit in units:
                if unit["raw_status"] is not None:
                    groups[unit["state_cluster_id"] or "unmatched:" + unit["state_variant_id"]].append(unit)
            clusters = []
            q = norm_text(query)
            for sid, selected in groups.items():
                label, variants = selected[0]["state_label"] or selected[0]["raw_status"], []
                for vid in sorted({u["state_variant_id"] for u in selected}):
                    members = [u for u in selected if u["state_variant_id"] == vid]
                    if q and q not in norm_text(members[0]["raw_status"]) and q not in norm_text(label):
                        continue
                    stats = experience.aggregate(members, aspect_variant_ids)
                    by_product = {
                        str(pid): len({u["review_id"] for u in members if u["product_id"] == pid})
                        for pid in sorted({u["product_id"] for u in members})
                    }
                    variants.append(
                        {
                            "state_variant_id": vid,
                            "raw_status": members[0]["raw_status"],
                            "review_count": stats["relevant"],
                            "sentiment_distribution": stats["sentiment_distribution"],
                            "by_product": by_product,
                            "samples": list(dict.fromkeys(u["excerpt"] for u in members))[:3],
                        }
                    )
                if variants:
                    clusters.append(
                        {
                            "state_cluster_id": selected[0]["state_cluster_id"],
                            "normalization_status": "matched"
                            if selected[0]["state_cluster_id"]
                            else "unmatched",
                            "label": label,
                            "variants": variants,
                            "review_count": len({u["review_id"] for u in selected}),
                        }
                    )
            clusters.sort(key=lambda c: (-c["review_count"], c["label"], c["state_cluster_id"] or ""))
            return {
                "aspect_labels": sorted({u["raw_aspect"] for u in units}),
                "null_status_reviews": len({u["review_id"] for u in units if u["raw_status"] is None}),
                "total": len(clusters),
                "state_clusters": clusters[offset : offset + limit],
            }

    def get_review_evidence(
        self,
        aspect_variant_ids,
        desired_state_variant_ids=None,
        contrary_state_variant_ids=(),
        product_ids=None,
        sentiments=None,
        group="all",
        include_full_review=False,
        limit=10,
        offset=0,
    ):
        _page(limit, offset)
        if group not in ("all", "matching", "contrary", "other", "unknown_state"):
            raise ToolError("group은 all/matching/contrary/other/unknown_state 중 하나여야 합니다")
        if sentiments is not None and (not sentiments or set(sentiments) - set(experience.SENTIMENTS)):
            raise ToolError("유효한 sentiments를 지정하세요")
        with repository(self.settings) as repo:
            repo.validate_selectors(aspect_variant_ids, desired_state_variant_ids, contrary_state_variant_ids)
            selected_products = repo.selected(product_ids)
            counts = dict.fromkeys(("matching", "contrary", "other", "unknown_state"), 0)
            result = []
            for product in sorted(selected_products, key=lambda p: p["product_id"]):
                grouped = defaultdict(list)
                for unit in product["units"]:
                    if unit["aspect_variant_id"] in aspect_variant_ids and (
                        sentiments is None or unit["sentiment"] in sentiments
                    ):
                        grouped[unit["review_id"]].append(unit)
                for rid in sorted(grouped):
                    units = grouped[rid]
                    states = {u["state_variant_id"] for u in units}
                    tags = []
                    if desired_state_variant_ids is None or states & set(desired_state_variant_ids):
                        tags.append("matching")
                    if states & set(contrary_state_variant_ids):
                        tags.append("contrary")
                    if desired_state_variant_ids is not None and (
                        states - {None} - set(desired_state_variant_ids) - set(contrary_state_variant_ids)
                    ):
                        tags.append("other")
                    if states == {None}:
                        tags.append("unknown_state")
                    for tag in tags:
                        counts[tag] += 1
                    if group != "all" and group not in tags:
                        continue
                    review = product["reviews"][rid]
                    item = {
                        "review_id": rid,
                        "product_id": product["product_id"],
                        "product_name": product["name"],
                        "rating": review["rating"],
                        "labels": tags,
                        "units": [
                            {
                                k: u[k]
                                for k in (
                                    "unit_id",
                                    "raw_aspect",
                                    "raw_status",
                                    "aspect_label",
                                    "state_label",
                                    "opinion",
                                    "sentiment",
                                    "excerpt",
                                )
                            }
                            for u in units
                        ],
                    }
                    if include_full_review:
                        item["full_review"] = review["body"]
                    result.append(item)
            return {"counts": counts, "total": len(result), "reviews": result[offset : offset + limit]}

    def get_offers(self, product_ids, availability=None):
        if availability is not None and (
            not availability or set(availability) - {"available", "sold_out", "discontinued", "unknown"}
        ):
            raise ToolError("availability 목록을 확인하세요")
        with repository(self.settings) as repo:
            return {
                "products": [
                    {
                        "product_id": p["product_id"],
                        "name": p["name"],
                        "offers": offers.render(
                            [
                                o
                                for o in p["offers"]
                                if availability is None or o["availability"] in availability
                            ]
                        ),
                    }
                    for p in repo.selected(product_ids)
                ]
            }
