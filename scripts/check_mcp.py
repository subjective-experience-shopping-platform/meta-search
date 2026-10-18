"""Exercise the actual stdio wire protocol and input validation."""

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "meta_search.server"], env=dict(os.environ)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert len(tools.tools) == 11
        assert len([t for t in tools.tools if t.annotations and t.annotations.readOnlyHint]) == 10
        guide = await session.call_tool("get_search_guide", {})
        assert not guide.isError
        assert guide.structuredContent is not None
        invalid = await session.call_tool("register_product", {"url": "file:///bad"})
        assert invalid.isError
        invalid_limit = await session.call_tool("search_product_docs", {"query": "a", "max_results": 0})
        assert invalid_limit.isError
        result = {
            "tool_names": [t.name for t in tools.tools],
            "guide": guide.structuredContent,
            "invalid_url_rejected": True,
            "invalid_limit_rejected": True,
        }
        destination = Path(__file__).resolve().parents[1] / "data/mcp_smoke.json"
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
