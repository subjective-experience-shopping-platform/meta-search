"""Complete the plan's real search/evidence/update/failure acceptance scenario."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from meta_search.config import load_settings
from meta_search.db import database

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = load_settings(ROOT / "data/live_e2e_config.toml")
REPORT = {"checks": {}, "responses": {}, "jobs": []}


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "meta_search.server"],
        env={**os.environ, "META_SEARCH_CONFIG": str(SETTINGS.config_path)},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()

        async def call(name, args):
            result = await session.call_tool(name, args)
            assert not result.isError, (name, result)
            return result.structuredContent

        async def register(url):
            response = await call("register_product", {"url": url})
            assert response["status"] == "queued"
            print("START", response["job_id"], url, flush=True)
            while True:
                status = await call("get_registration_status", {"job_id": response["job_id"]})
                if status["status"] in ("done", "failed"):
                    REPORT["jobs"].append(status)
                    print("FINISHED", json.dumps(status, ensure_ascii=False), flush=True)
                    return status
                await asyncio.sleep(10)

        guide = await call("get_search_guide", {})
        assert len(guide["categories"]) == 1
        category = guide["categories"][0]["category_id"]
        full = await call("get_search_guide", {"category": category})
        assert len(full["products"]) == 3
        assert guide["categories"][0]["taxonomy_status"] == "normalized"
        REPORT["responses"]["guide"] = full
        REPORT["checks"]["three_ready_products_normalized"] = True
        pid = full["products"][0]["product_id"]
        details = await call("get_product_details", {"product_id": pid})
        topics = await call("find_review_attributes", {"category": category})
        choice = next(
            c
            for c in topics["clusters"]
            if any(
                u["aspect_cluster_id"] == c["aspect_cluster_id"]
                for u in details["review_summary"]["top_aspects"]
            )
        )
        aspects = choice["cluster_selector"]
        states = await call("list_review_states", {"aspect_variant_ids": aspects, "product_ids": [pid]})
        desired = (
            [states["state_clusters"][0]["variants"][0]["state_variant_id"]]
            if states["state_clusters"]
            else None
        )
        core = details["core_attributes"][0]["key"]
        search = await call(
            "search_products",
            {
                "category": category,
                "filters": [{"key": core, "op": "exists", "value": True}],
                "experience_conditions": [
                    {
                        "condition_id": "actual_review",
                        "aspect_variant_ids": aspects,
                        "desired_state_variant_ids": desired,
                    }
                ],
                "sort": {"by": "experience", "aspect_variant_ids": aspects},
                "user_request": "실제 리뷰 근거가 있는 상품 검색",
            },
        )
        assert search["results"] and search["applied_filters_markdown"]
        assert all(r["score"]["n"] > 0 for r in search["results"])
        REPORT["responses"]["search"] = search
        REPORT["responses"]["evidence"] = await call(
            "get_review_evidence",
            {
                "aspect_variant_ids": aspects,
                "desired_state_variant_ids": desired,
                "include_full_review": True,
            },
        )
        REPORT["responses"]["offers"] = await call(
            "get_offers", {"product_ids": [p["product_id"] for p in full["products"]]}
        )
        REPORT["responses"]["comparison"] = await call(
            "compare_products", {"product_ids": [p["product_id"] for p in full["products"]]}
        )
        REPORT["responses"]["docs"] = await call("search_product_docs", {"query": "제품, 특징, 연결"})
        REPORT["checks"]["objective_experience_rank_and_evidence"] = True
        with database(SETTINGS.db_path) as conn:
            before = dict(
                conn.execute(
                    "SELECT raw_attributes,details_md FROM products WHERE product_id=?", (pid,)
                ).fetchone()
            )
            review_ids = {
                r[0] for r in conn.execute("SELECT review_id FROM reviews WHERE product_id=?", (pid,))
            }
            offer_ids = {r[0] for r in conn.execute("SELECT offer_id FROM offers WHERE product_id=?", (pid,))}
            version = conn.execute(
                "SELECT taxonomy_version FROM categories WHERE category_id=?", (category,)
            ).fetchone()[0]
        updated = await register(details["source_url"])
        assert (
            updated["status"] == "done"
            and updated["is_update"] == 1
            and updated["objective_status"] == "skipped"
        )
        with database(SETTINGS.db_path) as conn:
            assert (
                dict(
                    conn.execute(
                        "SELECT raw_attributes,details_md FROM products WHERE product_id=?", (pid,)
                    ).fetchone()
                )
                == before
            )
            assert (
                version
                == conn.execute(
                    "SELECT taxonomy_version FROM categories WHERE category_id=?", (category,)
                ).fetchone()[0]
            )
            after_reviews = {
                r[0] for r in conn.execute("SELECT review_id FROM reviews WHERE product_id=?", (pid,))
            }
            after_offers = {
                r[0] for r in conn.execute("SELECT offer_id FROM offers WHERE product_id=?", (pid,))
            }
            assert not review_ids & after_reviews
            assert not offer_ids & after_offers
        REPORT["checks"]["update_objective_preserved_reviews_offers_replaced"] = True
        invalid = await register("https://www.keychron.com/products/meta-search-e2e-does-not-exist-20261012")
        assert invalid["status"] == "failed"
        assert len((await call("search_products", {"category": category}))["results"]) == 3
        REPORT["checks"]["invalid_product_page_failed_not_searchable"] = True
        with database(SETTINGS.db_path) as conn:
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not conn.execute("PRAGMA foreign_key_check").fetchall()
            assert conn.execute("SELECT taxonomy_version FROM categories").fetchone()[0] >= 2
        assert len(list((SETTINGS.data_dir / "docs").glob("*.md"))) == 3
        REPORT["checks"]["documents_and_db_integrity"] = True
        REPORT["notification_verification"] = (
            "osascript called by orchestrator; visual receipt requires user verification"
        )
        REPORT["review_limit"] = SETTINGS.limits["max_reviews_per_product"]
        (SETTINGS.data_dir / "acceptance_report.json").write_text(
            json.dumps(REPORT, ensure_ascii=False, indent=2)
        )
        print("ALL LIVE ACCEPTANCE CHECKS PASSED", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
