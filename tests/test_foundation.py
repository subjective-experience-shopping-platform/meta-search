import sqlite3

import pytest

from meta_search import llm
from meta_search.db import database, transaction
from meta_search.jobs import get_registration_status
from meta_search.pipelines.collect_page import validate_snapshot
from meta_search.pipelines.extract_objective import validate_extraction
from meta_search.textnorm import make_id, model_key, norm_text
from meta_search.validation import PipelineError, ValidationError, clean_value


def test_textnorm():
    assert model_key("Keychron", "K8 Pro") == model_key("keychron", "k8-pro")
    assert norm_text(" Ａ\n  B ") == "a b"
    assert make_id("av", "Keyboard", " 타건감 ") == make_id("av", "keyboard", "타건감")


def test_db_fk_and_rollback(settings):
    with database(settings.db_path) as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.IntegrityError), transaction(conn):
            conn.execute("INSERT INTO categories(category_id,display_name) VALUES('x','X')")
            conn.execute("INSERT INTO product_attributes VALUES(1,'x','missing','1','page')")
        assert not conn.execute("SELECT * FROM categories").fetchall()
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize(
    "kind,value",
    [("number", True), ("number", float("nan")), ("boolean", 1), ("string_list", []), ("string", " ")],
)
def test_invalid_values(kind, value):
    with pytest.raises(ValidationError):
        clean_value(value, kind)


def test_snapshot(page):
    page["reviews"] += [
        {"body": " ", "rating": 2, "written_at": None},
        {**page["reviews"][0], "body": "  타건감이 좋고 조용해요  "},
        {"body": "다른 리뷰", "rating": 8, "written_at": None},
    ]
    result = validate_snapshot(page, 100)
    assert len(result["reviews"]) == 2
    assert result["reviews"][1]["rating"] is None
    assert len(validate_snapshot(page, 1)["reviews"]) == 1


def test_page_failed_no_retry(page):
    page["page_ok"] = False
    called = []
    with pytest.raises(PipelineError):
        llm.call_with_retry(
            lambda prompt: called.append(prompt) or page,
            "test",
            lambda output: validate_snapshot(output, 100),
        )
    assert len(called) == 1


def test_retry_and_error_feedback():
    calls = []

    def call(prompt):
        calls.append(prompt)
        if len(calls) == 1:
            raise ValidationError("V02 missing f001")
        return {"valid": True}

    assert llm.call_with_retry(call, "test") == {"valid": True}
    assert "V02 missing f001" in calls[1]


def test_render():
    assert llm.render("{{a}} {{a}} {{b}}", a="{{b}}", b={"x": 1}) == '{{b}} {{b}} {\n  "x": 1\n}'
    with pytest.raises(ValidationError):
        llm.render("{{missing}}")


def test_extraction(extraction):
    extraction["attributes"] += [
        {"key": "price_krw", "value_type": "number", "value": 100, "display_name": "가격", "unit": None}
    ]
    result = validate_extraction(extraction, set())
    assert len(result["attributes"]) == 2
    extraction["attributes"][0]["key"] = "Invalid"
    with pytest.raises(ValidationError):
        validate_extraction(extraction, set())


def test_job_status(ctx):
    ctx.warn("warning 1")
    ctx.warn("warning 2")
    status = get_registration_status(ctx.settings, ctx.job_id)
    assert status["warnings"] == ["warning 1", "warning 2"]
    assert get_registration_status(ctx.settings)["jobs"][0]["job_id"] == ctx.job_id


def test_review_cap_100(page):
    page["reviews"] = [{"body": f"리뷰 {i}", "rating": None, "written_at": None} for i in range(120)]
    assert len(validate_snapshot(page, 100)["reviews"]) == 100
