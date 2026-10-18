import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError as PydanticError

from meta_search.db import database, transaction
from meta_search.search.experience import aggregate, review_sentiment, wilson
from meta_search.search.models import ExperienceCondition, ObjectiveFilter, OfferFilter, SortSpec
from meta_search.search.objective_filter import evaluate, validate_filter
from meta_search.search.offers import evaluate as evaluate_offers
from meta_search.search.service import SearchService
from meta_search.textnorm import make_id


@pytest.mark.parametrize(
    "op,value,stored,expected",
    [
        ("eq", " TKL ", "tkl", "PASS"),
        ("ne", 1, 1, "FAIL"),
        ("in", [1, 2], 2, "PASS"),
        ("not_in", [1, 2], 2, "FAIL"),
        ("gte", 2, 3, "PASS"),
        ("lte", 3, 4, "FAIL"),
        ("between", [2, 4], 4, "PASS"),
        ("contains_any", ["BLUETOOTH"], ["bluetooth"], "PASS"),
        ("contains_all", ["a", "b"], ["a"], "FAIL"),
        ("exists", False, None, "PASS"),
        ("exists", True, None, "FAIL"),
        ("ne", 0, None, "UNKNOWN"),
    ],
)
def test_filter_ops(op, value, stored, expected):
    assert evaluate(ObjectiveFilter(key="k", op=op, value=value), stored)[0] == expected


@pytest.mark.parametrize(
    "op,value",
    [("gte", "5"), ("eq", True), ("between", [4, 2]), ("in", []), ("contains_all", ["a"]), ("exists", 1)],
)
def test_filter_format(op, value):
    with pytest.raises(ToolError):
        validate_filter(
            ObjectiveFilter(key="weight", op=op, value=value), [{"key": "weight", "value_type": "number"}]
        )


def test_search_unknown_excluded_limit(catalog):
    service = SearchService(catalog)
    result = service.search_products(filters=[{"key": "weight", "op": "lte", "value": 800}], limit=1)
    assert [p["product_id"] for p in result["results"]] == [2]
    assert [p["product_id"] for p in result["undetermined"]] == [3]
    assert [p["product_id"] for p in result["excluded"]] == [1]
    assert "일치 2" in result["applied_filters_markdown"]
    assert result["candidate_count"] == 4
    assert [
        p["product_id"]
        for p in service.search_products(sort={"by": "attribute", "key": "weight", "order": "desc"})[
            "results"
        ]
    ] == [1, 2, 4, 3]


def test_zero_relaxation_no_implicit_relax(catalog):
    result = SearchService(catalog).search_products(filters=[{"key": "weight", "op": "lte", "value": 1}])
    assert result["results"] == []
    assert result["relaxation_suggestions"][0]["would_match"] == 4


def test_guide_compare_details_docs(catalog):
    service = SearchService(catalog)
    guide = service.get_search_guide("keyboard")
    assert len(guide["products"]) == 4
    weight = next(a for a in guide["attributes"] if a["key"] == "weight")
    assert weight["coverage"] == "3/4"
    comparison = service.compare_products([1, 3])
    assert next(a for a in comparison["attributes"] if a["key"] == "weight")["differs"]
    assert service.get_product_details(1)["review_summary"]["review_count"] == 2
    assert service.search_product_docs("무선, 연결", [1])["results"][0]["heading"] == "특징"
    assert service.get_offers([1])["products"][0]["offers"][0]["total_price"] == 50000
    with pytest.raises(ToolError):
        service.get_product_details(5)
    with pytest.raises(ToolError):
        service.search_products(filters=[{"key": "missing", "op": "eq", "value": 1}])


def test_category_ambiguity_and_mismatch(catalog):
    with database(catalog.db_path) as c, transaction(c):
        c.execute("INSERT INTO categories(category_id,display_name) VALUES('monitor','모니터')")
        c.execute(
            "INSERT INTO products(category_id,name,brand,model,model_key,source_url,status) VALUES('monitor','Monitor','T','M','tm','https://x.test','ready')"
        )
    service = SearchService(catalog)
    with pytest.raises(ToolError, match="category"):
        service.search_products()
    with pytest.raises(ToolError, match="같은 카테고리"):
        service.compare_products([1, 6])


def test_experience_evidence_duplicate_votes(catalog):
    service = SearchService(catalog)
    av, good, bad = (
        make_id("av", "keyboard", "키감"),
        make_id("sv", "keyboard", "좋음"),
        make_id("sv", "keyboard", "나쁨"),
    )
    found = service.find_review_attributes(query="키감")
    assert found["clusters"][0]["matched_selector"] == [av]
    states = service.list_review_states([av], [1])
    assert (
        next(v for c in states["state_clusters"] for v in c["variants"] if v["state_variant_id"] == good)[
            "review_count"
        ]
        == 1
    )
    evidence = service.get_review_evidence([av], [good], [bad], [1], include_full_review=True)
    assert evidence["counts"] == {"matching": 1, "contrary": 1, "other": 1, "unknown_state": 0}
    assert evidence["reviews"][0]["labels"] == ["matching", "contrary"]
    assert "full_review" in evidence["reviews"][0]
    result = service.search_products(
        experience_conditions=[
            {
                "condition_id": "feel",
                "aspect_variant_ids": [av],
                "desired_state_variant_ids": [good],
                "min_matching_reviews": 1,
            }
        ],
        sort={"by": "experience", "aspect_variant_ids": [av]},
    )
    assert len(result["results"]) == 4
    assert all(r["conditions"][0]["matching"] == 1 for r in result["results"])
    assert "키감" in result["applied_filters_markdown"]
    assert av not in result["applied_filters_markdown"]
    assert all(r["score"]["n"] == 2 for r in result["results"])
    with pytest.raises(ToolError):
        service.list_review_states(["missing"])


def test_wilson_and_sentiment():
    assert wilson(18, 22) == pytest.approx(0.6148, abs=0.001)
    assert wilson(0, 0) is None
    assert review_sentiment([{"sentiment": "positive"}, {"sentiment": "unknown"}]) == "positive"
    assert review_sentiment([{"sentiment": "positive"}, {"sentiment": "negative"}]) == "mixed"
    units = [
        {
            "review_id": 1,
            "aspect_variant_id": "a",
            "state_variant_id": "s",
            "sentiment": "positive",
            "excerpt": "x",
        }
    ] * 3
    assert aggregate(units, ["a"], ["s"])["matching"] == 1


def test_sale_all_conditions_same_offer():
    base = {"currency": "KRW", "availability": "available", "retailer": "Shop", "shipping_days": 1}
    offers = [{**base, "price": 10, "shipping_fee": 100}, {**base, "price": 100, "shipping_fee": 0}]
    assert evaluate_offers(offers, OfferFilter(max_price=20, max_shipping_fee=0))[0] == "FAIL"
    assert (
        evaluate_offers([{**base, "price": 10, "shipping_fee": None}], OfferFilter(max_shipping_fee=0))[0]
        == "UNKNOWN"
    )
    assert evaluate_offers([], OfferFilter())[0] == "UNKNOWN"


@pytest.mark.parametrize(
    "model,value",
    [
        (ExperienceCondition, {"condition_id": "a", "aspect_variant_ids": []}),
        (ExperienceCondition, {"condition_id": "a", "aspect_variant_ids": ["a"], "max_contrary_reviews": 0}),
        (SortSpec, {"by": "attribute"}),
        (SortSpec, {"by": "experience", "aspect_variant_ids": ["a"], "metric": "state_wilson_lower"}),
    ],
)
def test_input_models(model, value):
    with pytest.raises(PydanticError):
        model.model_validate(value)


def test_raw_selector_does_not_expand_to_cluster(catalog):
    with database(catalog.db_path) as c, transaction(c):
        unit = dict(c.execute("SELECT * FROM opinion_units LIMIT 1").fetchone())
        c.execute(
            "INSERT INTO opinion_units(review_id,product_id,category_id,raw_aspect,raw_status,excerpt,opinion,sentiment,aspect_variant_id,aspect_cluster_id) VALUES(?,?,?,'타건감',NULL,'키감','타건감 좋음','positive',?,?)",
            (
                unit["review_id"],
                unit["product_id"],
                unit["category_id"],
                make_id("av", "keyboard", "타건감"),
                unit["aspect_cluster_id"],
            ),
        )
    cluster = SearchService(catalog).find_review_attributes(query="타건감")["clusters"][0]
    assert cluster["matched_selector"] == [make_id("av", "keyboard", "타건감")]
    assert len(cluster["cluster_selector"]) == 2
    assert next(v for v in cluster["variants"] if v["raw_aspect"] == "키감")["matched"] is False


def test_experience_unknown_and_null_rank_last(catalog):
    with database(catalog.db_path) as c, transaction(c):
        c.execute("DELETE FROM opinion_units WHERE product_id=4")
        c.execute(
            "UPDATE opinion_units SET raw_status=NULL,state_variant_id=NULL,state_cluster_id=NULL WHERE product_id=3"
        )
    av, good = make_id("av", "keyboard", "키감"), make_id("sv", "keyboard", "좋음")
    service = SearchService(catalog)
    result = service.search_products(
        experience_conditions=[
            {"condition_id": "feel", "aspect_variant_ids": [av], "desired_state_variant_ids": [good]}
        ]
    )
    assert {p["product_id"] for p in result["undetermined"]} == {3, 4}
    ranked = service.search_products(sort={"by": "experience", "aspect_variant_ids": [av]})["results"]
    assert ranked[-1]["product_id"] == 4 and ranked[-1]["score"]["value"] is None
    assert ranked[-1]["score"]["n"] == 0


def test_tie_price_then_id_and_limit_after_sort(catalog):
    with database(catalog.db_path) as c, transaction(c):
        c.execute("UPDATE offers SET price=1 WHERE product_id=2")
    result = SearchService(catalog).search_products(sort={"by": "attribute", "key": "layout"}, limit=1)
    assert result["results"][0]["product_id"] == 3
