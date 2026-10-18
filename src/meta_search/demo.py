"""Synthetic catalog for explicit demo/testing use only."""

import json

from .db import database, transaction
from .textnorm import make_id, model_key


def seed(settings, include_failed=True):
    with database(settings.db_path) as c, transaction(c):
        c.execute(
            "INSERT INTO categories(category_id,display_name,taxonomy_status,taxonomy_version) VALUES('keyboard','키보드','normalized',2)"
        )
        for key, kind, unit in (
            ("weight", "number", "g"),
            ("layout", "string", None),
            ("wireless", "boolean", None),
            ("connections", "string_list", None),
        ):
            c.execute(
                "INSERT INTO category_attributes(category_id,key,display_name,tier,value_type,unit) VALUES('keyboard',?,?,'core',?,?)",
                (key, key, kind, unit),
            )
        for i in range(1, 6 if include_failed else 5):
            pid = c.execute(
                "INSERT INTO products(category_id,name,brand,model,model_key,source_url,details_md,status) VALUES('keyboard',?,'Test',?,?,?,? ,?)",
                (
                    f"Keyboard {i}",
                    str(i),
                    model_key("Test", str(i)),
                    f"https://shop.test/{i}",
                    "## 특징\n무선 연결\n## 사양\n무게 스펙",
                    "ready" if i < 5 else "failed",
                ),
            ).lastrowid
            values = {
                "layout": "TKL" if i < 3 else "Full",
                "wireless": i % 2 == 0,
                "connections": ["usb_c", "bluetooth"],
            }
            if i != 3:
                values["weight"] = 1000 - 100 * i
            for key, value in values.items():
                c.execute(
                    "INSERT INTO product_attributes VALUES(?,'keyboard',?,?,'page')",
                    (pid, key, json.dumps(value)),
                )
            if i != 4:
                c.execute(
                    "INSERT INTO offers(product_id,seller,retailer,url,price,currency,shipping_fee,shipping_days,availability,last_checked_at) VALUES(?,'판매자','Store',?,?,'KRW',?,1,'available','2026-10-11T00:00:00Z')",
                    (pid, f"https://shop.test/{i}", 50000 * i, 0 if i == 1 else None),
                )
            aid = make_id("ac", "keyboard", "타건감")
            c.execute("INSERT OR IGNORE INTO aspect_clusters VALUES(?,'keyboard','타건감')", (aid,))
            for status in ("좋음", "나쁨", "보통"):
                c.execute(
                    "INSERT OR IGNORE INTO state_clusters VALUES(?,?,?)",
                    (make_id("sc", aid, status), aid, status),
                )
            for j in range(2):
                rid = c.execute(
                    "INSERT INTO reviews(product_id,body,rating) VALUES(?,'키감 좋음 나쁨 보통',5)", (pid,)
                ).lastrowid
                states = ["좋음", "좋음", "나쁨"] if j == 0 else ([None] if i == 2 else ["보통"])
                for status in states:
                    c.execute(
                        "INSERT INTO opinion_units(review_id,product_id,category_id,raw_aspect,raw_status,excerpt,opinion,sentiment,aspect_variant_id,state_variant_id,aspect_cluster_id,state_cluster_id) VALUES(?,?,'keyboard','키감',?,'키감','키감 평가',?,?,?,?,?)",
                        (
                            rid,
                            pid,
                            status,
                            "negative" if status == "나쁨" else "neutral" if status == "보통" else "positive",
                            make_id("av", "keyboard", "키감"),
                            make_id("sv", "keyboard", status) if status else None,
                            aid,
                            make_id("sc", aid, status) if status else None,
                        ),
                    )
