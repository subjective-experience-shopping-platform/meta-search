from dataclasses import replace

import pytest

from meta_search.config import load_settings
from meta_search.db import database, init_schema
from meta_search.demo import seed
from meta_search.jobs import JobContext, create_job


@pytest.fixture
def settings(tmp_path):
    original = load_settings()
    config = tmp_path / "config.toml"
    config.write_text(original.config_path.read_text())
    s = replace(original, config_path=config, data_dir=tmp_path / "data")
    with database(s.db_path) as conn:
        init_schema(conn)
    return s


@pytest.fixture
def ctx(settings):
    return JobContext(create_job(settings, "https://example.com/product"), settings)


@pytest.fixture
def page():
    return {
        "page_ok": True,
        "failure_reason": None,
        "page_title": "상품",
        "product_name": "Keyboard One",
        "brand_text": "Test",
        "price_text": "100000원",
        "spec_text": "무게: 1000g",
        "description_text": "키보드",
        "reviews": [{"body": "타건감이 좋고 조용해요", "rating": 5, "written_at": "2026-10-01"}],
        "review_count_on_page": 1,
    }


@pytest.fixture
def extraction():
    return {
        "category": {"category_id": "keyboard", "display_name": "키보드", "is_new": True},
        "identity": {"name": "Keyboard One", "brand": "Test", "model": "One"},
        "attributes": [
            {"key": "weight", "display_name": "무게", "value_type": "number", "unit": "g", "value": 1000},
            {"key": "layout", "display_name": "배열", "value_type": "string", "unit": None, "value": "TKL"},
        ],
        "details_markdown": "## 특징\n무선 연결\n가볍고 작음",
    }


@pytest.fixture
def catalog(settings):
    seed(settings)
    return settings
