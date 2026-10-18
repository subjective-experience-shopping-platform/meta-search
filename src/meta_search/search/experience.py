from collections import defaultdict
from math import sqrt

SENTIMENTS = ("positive", "negative", "mixed", "neutral", "unknown")


def wilson(k, n):
    if not n:
        return None
    z, p = 1.96, k / n
    return (p + z * z / (2 * n) - z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n)


def review_sentiment(units):
    values = {u["sentiment"] for u in units} - {"unknown"}
    return next(iter(values)) if len(values) == 1 else "mixed" if values else "unknown"


def aggregate(units, aspects, desired=None, contrary=(), sentiments=None):
    by_review = defaultdict(list)
    for unit in units:
        if unit["aspect_variant_id"] in aspects:
            by_review[unit["review_id"]].append(unit)
    distribution = dict.fromkeys(SENTIMENTS, 0)
    matching, against, excerpts = set(), set(), []
    for rid, selected in by_review.items():
        distribution[review_sentiment(selected)] += 1
        for unit in selected:
            if (desired is None or unit["state_variant_id"] in desired) and (
                sentiments is None or unit["sentiment"] in sentiments
            ):
                matching.add(rid)
                if unit["excerpt"] not in excerpts:
                    excerpts.append(unit["excerpt"])
            if unit["state_variant_id"] in contrary:
                against.add(rid)
    n, k = len(by_review), distribution["positive"]
    return {
        "relevant": n,
        "matching": len(matching),
        "contrary": len(against) if contrary else None,
        "positive": k,
        "sentiment_distribution": distribution,
        "positive_wilson_lower": wilson(k, n),
        "sample_excerpts": excerpts[:3],
    }


def evaluate(units, condition):
    stats = aggregate(
        units,
        condition.aspect_variant_ids,
        condition.desired_state_variant_ids,
        condition.contrary_state_variant_ids,
        condition.sentiments,
    )
    selected = [u for u in units if u["aspect_variant_id"] in condition.aspect_variant_ids]
    if not stats["relevant"]:
        verdict, reason = "UNKNOWN", "관련 리뷰 없음"
    elif condition.desired_state_variant_ids and all(u["state_variant_id"] is None for u in selected):
        verdict, reason = "UNKNOWN", "상태 미기재"
    elif stats["matching"] >= condition.min_matching_reviews and (
        condition.max_contrary_reviews is None or stats["contrary"] <= condition.max_contrary_reviews
    ):
        verdict, reason = "PASS", "리뷰 조건 충족"
    else:
        verdict, reason = "FAIL", "리뷰 조건 미충족"
    return {"id": condition.condition_id, "type": "experience", "verdict": verdict, "reason": reason, **stats}


def score(units, sort):
    stats = aggregate(units, sort.aspect_variant_ids, sort.desired_state_variant_ids)
    k = stats["positive"] if sort.metric == "positive_wilson_lower" else stats["matching"]
    n = stats["relevant"]
    return {"metric": sort.metric, "value": wilson(k, n), "k": k, "n": n}
