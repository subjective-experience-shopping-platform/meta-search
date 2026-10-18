from ..textnorm import norm_text


def evaluate_offer(offer, condition):
    verdicts = []
    if condition.max_price is not None or condition.min_price is not None:
        verdicts.append("PASS" if offer["currency"] == condition.currency else "FAIL")
    for bound, op in ((condition.min_price, lambda a, b: a >= b), (condition.max_price, lambda a, b: a <= b)):
        if bound is not None:
            verdicts.append("PASS" if op(offer["price"], bound) else "FAIL")
    if condition.availability:
        verdicts.append("PASS" if offer["availability"] in condition.availability else "FAIL")
    if condition.retailers:
        verdicts.append(
            "PASS"
            if any(norm_text(r) in norm_text(offer["retailer"]) for r in condition.retailers)
            else "FAIL"
        )
    for field, maximum in (
        ("shipping_fee", condition.max_shipping_fee),
        ("shipping_days", condition.max_shipping_days),
    ):
        if maximum is not None:
            verdicts.append(
                "UNKNOWN" if offer[field] is None else "PASS" if offer[field] <= maximum else "FAIL"
            )
    return "FAIL" if "FAIL" in verdicts else "UNKNOWN" if "UNKNOWN" in verdicts else "PASS"


def evaluate(offers, condition):
    passing = [o for o in offers if evaluate_offer(o, condition) == "PASS"]
    verdicts = [evaluate_offer(o, condition) for o in offers]
    verdict = (
        "PASS" if passing else "UNKNOWN" if not offers or all(v == "UNKNOWN" for v in verdicts) else "FAIL"
    )
    return verdict, passing


def summary(offers, currency="KRW"):
    priced = [o for o in offers if o["currency"] == currency]
    cheapest = min(priced, key=lambda o: (o["price"], o["offer_id"]), default=None)
    return {
        "offer_count": len(offers),
        "min_price": cheapest["price"] if cheapest else None,
        "currency": currency,
        "min_price_retailer": cheapest["retailer"] if cheapest else None,
        "available_count": sum(o["availability"] == "available" for o in offers),
        "last_checked_at": cheapest["last_checked_at"] if cheapest else None,
    }


def render(offers):
    result = [
        {**o, "total_price": o["price"] + o["shipping_fee"] if o["shipping_fee"] is not None else None}
        for o in offers
    ]
    return sorted(
        result,
        key=lambda o: (o["total_price"] if o["total_price"] is not None else o["price"], o["offer_id"]),
    )
