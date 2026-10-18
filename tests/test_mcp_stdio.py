import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from meta_search.config import load_settings
from meta_search.db import database, init_schema
from meta_search.demo import seed
from meta_search.textnorm import make_id


def test_all_search_tools_over_stdio(tmp_path):
    original = load_settings()
    config = tmp_path / "config.toml"
    config.write_text(
        original.config_path.read_text()
        .replace('prompts_dir = "prompts"', f'prompts_dir = "{original.prompts_dir}"')
        .replace('schemas_dir = "schemas"', f'schemas_dir = "{original.schemas_dir}"')
    )
    settings = load_settings(config)
    with database(settings.db_path) as c:
        init_schema(c)
    seed(settings)

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "meta_search.server"],
            env={**os.environ, "META_SEARCH_CONFIG": str(config)},
        )
        av = make_id("av", "keyboard", "키감")
        good = make_id("sv", "keyboard", "좋음")
        calls = {
            "get_search_guide": {"category": "keyboard"},
            "search_products": {
                "filters": [{"key": "weight", "op": "lte", "value": 800}],
                "experience_conditions": [
                    {"condition_id": "feel", "aspect_variant_ids": [av], "desired_state_variant_ids": [good]}
                ],
            },
            "compare_products": {"product_ids": [1, 2]},
            "get_product_details": {"product_id": 1},
            "search_product_docs": {"query": "무선"},
            "find_review_attributes": {"query": "키감"},
            "list_review_states": {"aspect_variant_ids": [av]},
            "get_review_evidence": {"aspect_variant_ids": [av], "include_full_review": True},
            "get_offers": {"product_ids": [1]},
            "get_registration_status": {},
        }
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            for name, args in calls.items():
                response = await session.call_tool(name, args)
                assert not response.isError, (name, response)
                assert response.structuredContent is not None, name
                json.dumps(response.structuredContent, ensure_ascii=False, allow_nan=False)
            response = await session.call_tool("get_product_details", {"product_id": 5})
            assert response.isError

    asyncio.run(run())
