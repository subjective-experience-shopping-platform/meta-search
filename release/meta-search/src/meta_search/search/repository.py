import json
from contextlib import contextmanager

from mcp.server.fastmcp.exceptions import ToolError

from ..db import database


class Repository:
    def __init__(self, conn):
        self.conn = conn
        self.categories = [
            dict(r)
            for r in conn.execute(
                "SELECT c.*,COUNT(p.product_id) AS product_count FROM categories c LEFT JOIN products p ON p.category_id=c.category_id AND p.status='ready' GROUP BY c.category_id ORDER BY c.category_id"
            )
        ]
        self.products = {
            r["product_id"]: dict(r)
            for r in conn.execute("SELECT * FROM products WHERE status='ready' ORDER BY product_id")
        }
        self.definitions = [
            dict(r) for r in conn.execute("SELECT * FROM category_attributes ORDER BY tier,key")
        ]
        for product in self.products.values():
            pid = product["product_id"]
            product["attributes"] = {
                r["key"]: {**dict(r), "value": json.loads(r["value_json"])}
                for r in conn.execute(
                    "SELECT a.*,v.value_json,v.source FROM product_attributes v JOIN category_attributes a USING(category_id,key) WHERE product_id=?",
                    (pid,),
                )
            }
            product["offers"] = [
                dict(r)
                for r in conn.execute("SELECT * FROM offers WHERE product_id=? ORDER BY offer_id", (pid,))
            ]
            product["reviews"] = {
                r["review_id"]: dict(r)
                for r in conn.execute("SELECT * FROM reviews WHERE product_id=? ORDER BY review_id", (pid,))
            }
            product["units"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT u.*,a.label AS aspect_label,s.label AS state_label FROM opinion_units u LEFT JOIN aspect_clusters a USING(aspect_cluster_id) LEFT JOIN state_clusters s USING(state_cluster_id) WHERE product_id=? ORDER BY unit_id",
                    (pid,),
                )
            ]

    def category(self, category=None):
        choices = [c["category_id"] for c in self.categories]
        if category is None:
            if len(choices) != 1:
                raise ToolError(f"category를 지정하세요. 카테고리 목록: {choices}")
            category = choices[0]
        if category not in choices:
            raise ToolError(f"없는 카테고리 {category}. 사용 가능: {choices}")
        return next(c for c in self.categories if c["category_id"] == category)

    def selected(self, product_ids=None, category=None):
        if product_ids is not None:
            if not product_ids or len(set(product_ids)) != len(product_ids):
                raise ToolError("product_ids는 중복 없는 비어 있지 않은 정수 목록이어야 합니다")
            missing = [pid for pid in product_ids if pid not in self.products]
            if missing:
                raise ToolError(
                    f"검색 가능한 상품이 없습니다: {missing}. get_search_guide에서 ready 상품 ID를 확인하세요."
                )
            return [self.products[pid] for pid in product_ids]
        return [p for p in self.products.values() if category is None or p["category_id"] == category]

    def units(self, category=None, product_ids=None):
        return [u for p in self.selected(product_ids, category) for u in p["units"]]

    def validate_selectors(self, aspects, desired=None, contrary=(), category=None):
        units = self.units(category)
        valid_aspects = {u["aspect_variant_id"] for u in units}
        if not aspects or set(aspects) - valid_aspects:
            raise ToolError(
                f"없는/빈 aspect_variant_ids: {sorted(set(aspects or []) - valid_aspects)}. find_review_attributes로 ID를 확인하세요."
            )
        valid_states = {
            u["state_variant_id"]
            for u in units
            if u["aspect_variant_id"] in aspects and u["state_variant_id"] is not None
        }
        if desired == [] or (set(desired or []) | set(contrary)) - valid_states:
            raise ToolError("선택한 aspect에 없는/빈 상태 ID입니다. list_review_states로 확인하세요.")


@contextmanager
def repository(settings):
    with database(settings.db_path) as conn:
        # A single snapshot covers all reads while registration writes concurrently.
        conn.execute("BEGIN")
        try:
            yield Repository(conn)
        finally:
            conn.rollback()
