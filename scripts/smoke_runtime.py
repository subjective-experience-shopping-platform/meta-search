"""Validate live CLI structured output; save inspectable fixtures."""

import json
from dataclasses import replace
from pathlib import Path

from meta_search import llm
from meta_search.config import load_settings
from meta_search.db import database, init_schema
from meta_search.jobs import JobContext, create_job
from meta_search.notify import notify

settings = load_settings()
settings = replace(settings, data_dir=settings.data_dir / "runtime_smoke")
with database(settings.db_path) as c:
    init_schema(c)
ctx = JobContext(create_job(settings, "https://example.com/runtime-smoke"), settings)
fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures"
fixtures.mkdir(exist_ok=True)
for provider in ("codex", "agy"):
    output = llm.invoke(
        ctx,
        "smoke_" + provider,
        provider,
        "opinion_units.md",
        "opinion_units.schema.json",
        {
            "product_name": "테스트 키보드",
            "product_category": "키보드",
            "review": "타건감이 좋고 조용해서 만족합니다.",
        },
        model=settings.agy["collect_model"],
    )
    (fixtures / f"runtime_{provider}.json").write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(provider, output, flush=True)
notify("Meta-Search 환경 검증", "CLI 구조화 출력 검증 완료")
ctx.update(status="done", stage="done")
