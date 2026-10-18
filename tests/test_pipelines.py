import json
from copy import deepcopy

import pytest

from meta_search import llm, orchestrator
from meta_search.db import database, transaction
from meta_search.jobs import get_registration_status
from meta_search.pipelines import experience, extract_objective, objective, sales


def fake_llm(monkeypatch, page, extraction, fail_reviews=False, normalize_error=False):
    def agy(step, prompt, schema, model, ctx):
        if step.startswith("collect_page"):
            return deepcopy(page)
        if step.startswith("gap_research"):
            return {"values": [], "notes_markdown": ""}
        if step.startswith("sales_research"):
            return {
                "offers": [
                    {
                        "seller": "Test",
                        "retailer": "Test",
                        "url": ctx.job["url"],
                        "price": 100000,
                        "currency": "krw",
                        "shipping_fee": 0,
                        "shipping_days": 1,
                        "availability": "available",
                        "evidence": "100000원",
                    }
                ]
            }
        if step.startswith("normalize_attributes"):
            if normalize_error:
                raise json.JSONDecodeError("invalid", "bad", 0)
            payload = json.loads((ctx.job_dir / "normalization_input.json").read_text())
            groups = {}
            for product in payload["products"]:
                for field in product["fields"]:
                    key = field["name"]
                    group = groups.setdefault(
                        key,
                        {
                            "canonical_key": key,
                            "display_name": field["display_name"],
                            "definition": "비교 가능한 속성",
                            "tier": "core",
                            "criteria": {"search_value": True, "category_scope": True, "comparable": True},
                            "value_type": field["value_type"],
                            "unit": field["unit"],
                            "reason": "비교",
                            "assignments": [],
                        },
                    )
                    group["assignments"].append(
                        {"field_id": field["field_id"], "value": field["value"], "value_operation": "keep"}
                    )
            return {
                "category": payload["category"],
                "base_version": payload["base_version"],
                "groups": list(groups.values()),
                "needs_review": [],
            }
        raise AssertionError(step)

    def codex(step, prompt, schema, ctx):
        if step.startswith("extract_objective"):
            return deepcopy(extraction)
        if step.startswith("opinion"):
            if fail_reviews:
                raise llm.LLMError("review error")
            return {
                "opinion_units": [
                    {
                        "raw_aspect": "타건감",
                        "raw_status": "좋음",
                        "excerpt": "타건감이 좋고",
                        "opinion": "타건감 만족",
                        "sentiment": "positive",
                    }
                ]
            }
        raise AssertionError(step)

    monkeypatch.setattr(llm, "run_agy", agy)
    monkeypatch.setattr(llm, "run_codex", codex)


def test_orchestrator_success_first_T01(ctx, page, extraction, monkeypatch):
    notices = []
    fake_llm(monkeypatch, page, extraction)
    monkeypatch.setattr(orchestrator, "notify", lambda *args: notices.append(args))
    orchestrator.execute_job(ctx)
    status = get_registration_status(ctx.settings, ctx.job_id)
    assert status["status"] == "done"
    assert notices[0][0] == "Meta-Search 등록 완료"
    with database(ctx.settings.db_path) as c:
        assert c.execute("SELECT status FROM products").fetchone()[0] == "ready"
        assert tuple(c.execute("SELECT taxonomy_status,taxonomy_version FROM categories").fetchone()) == (
            "provisional",
            1,
        )
        assert {r[0] for r in c.execute("SELECT tier FROM category_attributes")} == {"core"}
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    assert (ctx.settings.data_dir / "docs/1.md").exists()


def test_new_failure_update_failure_preserves(ctx, page, extraction, monkeypatch):
    fake_llm(monkeypatch, page, extraction, fail_reviews=True)
    monkeypatch.setattr(orchestrator, "notify", lambda *args: None)
    orchestrator.execute_job(ctx)
    assert get_registration_status(ctx.settings, ctx.job_id)["status"] == "failed"
    with database(ctx.settings.db_path) as c:
        assert c.execute("SELECT status FROM products").fetchone()[0] == "failed"
    fake_llm(monkeypatch, page, extraction)
    orchestrator.execute_job(ctx)
    assert get_registration_status(ctx.settings, ctx.job_id)["status"] == "done"
    with database(ctx.settings.db_path) as c:
        original = [tuple(r) for r in c.execute("SELECT * FROM reviews")]
    fake_llm(monkeypatch, page, extraction, fail_reviews=True)
    orchestrator.execute_job(ctx)
    status = get_registration_status(ctx.settings, ctx.job_id)
    assert status["status"] == "failed" and status["is_update"] == 1
    assert status["objective_status"] == "skipped"
    with database(ctx.settings.db_path) as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM reviews")] == original
        assert c.execute("SELECT status FROM products").fetchone()[0] == "ready"


def test_five_products_T12_and_limit(ctx, page, extraction, monkeypatch):
    monkeypatch.setattr(orchestrator, "notify", lambda *args: None)
    for i in range(5):
        extraction["identity"]["model"] = str(i)
        fake_llm(monkeypatch, page, extraction)
        orchestrator.execute_job(ctx)
        assert get_registration_status(ctx.settings, ctx.job_id)["status"] == "done"
        with database(ctx.settings.db_path) as c:
            assert c.execute("PRAGMA foreign_key_check").fetchall() == []
            assert c.execute("SELECT taxonomy_version FROM categories").fetchone()[0] == i + 1
    extraction["identity"]["model"] = "sixth"
    fake_llm(monkeypatch, page, extraction)
    orchestrator.execute_job(ctx)
    assert "한도 초과" in get_registration_status(ctx.settings, ctx.job_id)["error"]


def test_normalization_invalid_twice_atomic_T10(ctx, page, extraction, monkeypatch):
    fake_llm(monkeypatch, page, extraction)
    target = extract_objective.run(ctx, page)
    objective.run(ctx, target, page)
    with database(ctx.settings.db_path) as c, transaction(c):
        c.execute("UPDATE products SET status='ready'")
        before = [tuple(r) for r in c.execute("SELECT * FROM product_attributes")]
    extraction["identity"]["model"] = "Two"
    fake_llm(monkeypatch, page, extraction, normalize_error=True)
    second = extract_objective.run(ctx, page)
    with pytest.raises(json.JSONDecodeError):
        objective.run(ctx, second, page)
    with database(ctx.settings.db_path) as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM product_attributes")] == before
    assert (ctx.job_dir / "normalize_attributes.retry.request.txt").exists()


def test_transaction_error_rollback_T11(ctx, page, extraction, monkeypatch):
    fake_llm(monkeypatch, page, extraction)
    target = extract_objective.run(ctx, page)
    objective.run(ctx, target, page)
    with database(ctx.settings.db_path) as c, transaction(c):
        before = [tuple(r) for r in c.execute("SELECT * FROM category_attributes")]
        c.execute(
            "CREATE TRIGGER fail_insert BEFORE INSERT ON category_attributes BEGIN SELECT RAISE(ABORT,'forced'); END"
        )
    with pytest.raises(Exception, match="forced"):
        objective.run(ctx, target, page)
    with database(ctx.settings.db_path) as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM category_attributes")] == before


def test_units_and_sales_validation():
    units, dropped = experience.validate_units(
        {
            "opinion_units": [
                {"raw_aspect": " ", "raw_status": None, "excerpt": "x"},
                {"raw_aspect": "소음", "raw_status": " ", "excerpt": "a b"},
                {"raw_aspect": "음질", "raw_status": None, "excerpt": "not in review"},
            ]
        },
        "a\n b",
    )
    assert dropped == 2 and units[0]["raw_status"] is None
    base = {
        "seller": "S",
        "retailer": "R",
        "price": 100,
        "currency": "krw",
        "shipping_fee": None,
        "shipping_days": None,
        "availability": "available",
        "evidence": "100",
    }
    output = {
        "offers": [
            {**base, "url": "https://x.test/p#a"},
            {**base, "url": "https://x.test/p#b"},
            {**base, "url": "bad"},
            {**base, "url": "https://y.test", "price": 0},
        ]
    }
    valid, warnings = sales.validate_offers(output, "https://x.test/p")
    assert len(valid) == 1 and valid[0]["currency"] == "KRW"
    assert len(warnings) == 2


def test_missing_assignment_retried_T09(ctx, page, extraction, monkeypatch):
    fake_llm(monkeypatch, page, extraction)
    target = extract_objective.run(ctx, page)
    objective.run(ctx, target, page)
    with database(ctx.settings.db_path) as c, transaction(c):
        c.execute("UPDATE products SET status='ready'")
    extraction["identity"]["model"] = "Two"
    fake_llm(monkeypatch, page, extraction)
    second = extract_objective.run(ctx, page)
    original_call = llm.run_agy
    calls = []

    def missing_first(step, *args):
        output = original_call(step, *args)
        if step.startswith("normalize_attributes"):
            calls.append(step)
            if len(calls) == 1:
                output["groups"][0]["assignments"].pop()
        return output

    monkeypatch.setattr(llm, "run_agy", missing_first)
    objective.run(ctx, second, page)
    assert calls == ["normalize_attributes", "normalize_attributes.retry"]
    assert "V02" in (ctx.job_dir / "normalize_attributes.retry.request.txt").read_text()


def test_same_product_conflict_twice_no_write_T08(ctx, page, extraction, monkeypatch):
    extraction["attributes"] = [
        {"key": "layout", "display_name": "배열", "value_type": "string", "unit": None, "value": "TKL"},
        {
            "key": "key_layout",
            "display_name": "키 배열",
            "value_type": "string",
            "unit": None,
            "value": "Full",
        },
    ]
    fake_llm(monkeypatch, page, extraction)
    target = extract_objective.run(ctx, page)
    objective.run(ctx, target, page)
    with database(ctx.settings.db_path) as c, transaction(c):
        c.execute("UPDATE products SET status='ready'")
        before = [tuple(r) for r in c.execute("SELECT * FROM product_attributes")]
    extraction["identity"]["model"] = "Two"
    second = extract_objective.run(ctx, page)
    original_call = llm.run_agy

    def conflict(step, *args):
        output = original_call(step, *args)
        if step.startswith("normalize_attributes"):
            output["groups"][0]["assignments"].extend(output["groups"][1]["assignments"])
            output["groups"].pop()
        return output

    monkeypatch.setattr(llm, "run_agy", conflict)
    with pytest.raises(Exception, match="V09"):
        objective.run(ctx, second, page)
    with database(ctx.settings.db_path) as c:
        assert before == [tuple(r) for r in c.execute("SELECT * FROM product_attributes")]


def test_existing_documents_reflect_category_normalization(ctx, page, extraction, monkeypatch):
    import yaml

    fake_llm(monkeypatch, page, extraction)
    monkeypatch.setattr(orchestrator, "notify", lambda *args: None)
    orchestrator.execute_job(ctx)
    extraction["identity"]["model"] = "Two"
    fake_llm(monkeypatch, page, extraction)
    original_call = llm.run_agy

    def demote(step, *args):
        output = original_call(step, *args)
        if step.startswith("normalize_attributes"):
            for group in output["groups"]:
                group["tier"] = "extra"
                group["criteria"]["search_value"] = False
        return output

    monkeypatch.setattr(llm, "run_agy", demote)
    orchestrator.execute_job(ctx)
    assert get_registration_status(ctx.settings, ctx.job_id)["status"] == "done"
    first = (ctx.settings.data_dir / "docs/1.md").read_text()
    front = yaml.safe_load(first.split("---\n")[1])
    assert not front["core"]
    assert front["extra"]["weight"] == 1000


def test_lambda_sync_runs_after_collection_including_current_product(ctx, page, extraction, monkeypatch):
    from meta_search import normalization
    from meta_search.textnorm import make_id

    fake_llm(monkeypatch, page, extraction)
    monkeypatch.setattr(orchestrator, "notify", lambda *args: None)
    orchestrator.execute_job(ctx)
    with database(ctx.settings.db_path) as c, transaction(c):
        template = dict(c.execute("SELECT * FROM opinion_units LIMIT 1").fetchone())
        for i in range(299):
            rid = c.execute(
                "INSERT INTO reviews(product_id,body) VALUES(1,'타건감이 좋고 조용해요')"
            ).lastrowid
            c.execute(
                "INSERT INTO opinion_units(review_id,product_id,category_id,raw_aspect,raw_status,excerpt,opinion,sentiment,aspect_variant_id,state_variant_id) VALUES(?,1,'keyboard','타건감','좋음','타건감이 좋고','평가','positive',?,?)",
                (rid, template["aspect_variant_id"], template["state_variant_id"]),
            )
    extraction["identity"]["model"] = "Two"
    fake_llm(monkeypatch, page, extraction)
    received = []
    aid = make_id("ac", "keyboard", "타건감")

    def request(settings, delta):
        received.extend(delta)
        return {
            "aspects": [
                {
                    "category_id": "keyboard",
                    "raw_aspect": "타건감",
                    "aspect_cluster_id": aid,
                    "label": "타건감",
                }
            ],
            "states": [
                {
                    "category_id": "keyboard",
                    "raw_aspect": "타건감",
                    "raw_status": "좋음",
                    "aspect_cluster_id": aid,
                    "state_cluster_id": make_id("sc", aid, "좋음"),
                    "label": "좋음",
                }
            ],
        }

    monkeypatch.setattr(normalization, "request_table", request)
    orchestrator.execute_job(ctx)
    assert get_registration_status(ctx.settings, ctx.job_id)["status"] == "done"
    assert len(received) == 301
    with database(ctx.settings.db_path) as c:
        assert normalization.unmatched_count(c) == 0
        assert c.execute("SELECT SUM(cloud_synced) FROM opinion_units").fetchone()[0] == 301
