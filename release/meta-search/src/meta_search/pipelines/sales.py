import re
from datetime import datetime
from urllib.parse import urldefrag
from zoneinfo import ZoneInfo

from .. import llm
from ..db import database, now_utc, transaction
from ..validation import valid_url


def validate_offers(output, source_url):
    valid, warnings, seen = [], [], set()
    for offer in output["offers"]:
        url, _ = urldefrag(offer["url"])
        currency = offer["currency"].upper()
        if (
            not valid_url(url)
            or offer["price"] <= 0
            or not re.fullmatch(r"[A-Z]{3}", currency)
            or not offer["seller"].strip()
            or not offer["retailer"].strip()
            or any(offer[k] is not None and offer[k] < 0 for k in ("shipping_fee", "shipping_days"))
        ):
            warnings.append(f"잘못된 판매처 제외: {url}")
            continue
        if url in seen:
            continue
        seen.add(url)
        if len(valid) < 10:
            valid.append(
                {k: v for k, v in {**offer, "url": url, "currency": currency}.items() if k != "evidence"}
            )
    if urldefrag(source_url)[0] not in {o["url"] for o in valid}:
        warnings.append("원본 판매처 미포함")
    if not valid:
        warnings.append("판매처 0곳")
    return valid, warnings


def run(ctx, target, snapshot):
    output = llm.invoke(
        ctx,
        "sales_research",
        "agy",
        "sales_research.md",
        "sales_offers.schema.json",
        {
            "product": target.identity,
            "source_url": ctx.job["url"],
            "price_text": snapshot["price_text"],
            "max_offers": 10,
            "today": datetime.now(ZoneInfo("Asia/Seoul")).date().isoformat(),
        },
        model=ctx.settings.agy["research_model"],
    )
    offers, warnings = validate_offers(output, ctx.job["url"])
    timestamp = now_utc()
    with database(ctx.settings.db_path) as conn, transaction(conn):
        conn.execute("DELETE FROM offers WHERE product_id=?", (target.product_id,))
        conn.executemany(
            "INSERT INTO offers(product_id,seller,retailer,url,price,currency,shipping_fee,shipping_days,availability,last_checked_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    target.product_id,
                    *(
                        o[k]
                        for k in (
                            "seller",
                            "retailer",
                            "url",
                            "price",
                            "currency",
                            "shipping_fee",
                            "shipping_days",
                            "availability",
                        )
                    ),
                    timestamp,
                )
                for o in offers
            ],
        )
    for warning in warnings:
        ctx.warn(warning)
