import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from meta_search import normalization
from meta_search.db import database, init_schema, transaction
from meta_search.search.models import ExperienceCondition
from meta_search.search.service import SearchService
from meta_search.textnorm import make_id


def units(conn, count, aspect="새 속성", status="새 상태", synced=0, separate_reviews=False):
    def review():
        return conn.execute(
            "INSERT INTO reviews(product_id,body,cloud_synced) VALUES(1,'근거',?)", (synced,)
        ).lastrowid

    rid = None if separate_reviews else review()
    for i in range(count):
        if separate_reviews:
            rid = review()
        conn.execute(
            "INSERT INTO opinion_units(review_id,product_id,category_id,raw_aspect,raw_status,excerpt,opinion,"
            "sentiment,aspect_variant_id,state_variant_id,cloud_synced) VALUES(?,1,'keyboard',?,?,'근거','평가',"
            "'positive',?,?,?)",
            (
                rid,
                aspect,
                status,
                make_id("av", "keyboard", aspect),
                make_id("sv", "keyboard", status) if status else None,
                synced,
            ),
        )


def table(aspect="새 속성", status="새 상태", label="대표", state_label="대표 상태"):
    aid = make_id("ac", "keyboard", label)
    result = {
        "aspects": [
            {"category_id": "keyboard", "raw_aspect": aspect, "aspect_cluster_id": aid, "label": label}
        ],
        "states": [],
    }
    if status:
        result["states"].append(
            {
                "category_id": "keyboard",
                "raw_aspect": aspect,
                "raw_status": status,
                "aspect_cluster_id": aid,
                "state_cluster_id": make_id("sc", aid, state_label),
                "label": state_label,
            }
        )
    return result


def clear_units(settings):
    with database(settings.db_path) as c, transaction(c):
        c.execute("DELETE FROM reviews")


def test_exact_matching_normalized_keys_unknown_state_and_missing_status(catalog):
    clear_units(catalog)
    with database(catalog.db_path) as c, transaction(c):
        units(c, 1, "  키감  ", "좋음")
        units(c, 1, "키감", "새 상태")
        units(c, 1, "키감", None)
        units(c, 1, "다른 속성", None)
        normalization.apply_table(c, table("키감", "좋음"))
        rows = c.execute("SELECT * FROM opinion_units ORDER BY unit_id").fetchall()
        assert rows[0]["aspect_cluster_id"] and rows[0]["state_cluster_id"]
        assert rows[1]["aspect_cluster_id"] and rows[1]["state_cluster_id"] is None
        assert rows[2]["aspect_cluster_id"] and rows[2]["state_cluster_id"] is None
        assert rows[3]["aspect_cluster_id"] is None
        assert (
            normalization.unmatched_count(c) == 2
        )  # Units, not distinct expressions; null status is not a miss.


@pytest.mark.parametrize("count,called", [(300, False), (301, True)])
def test_threshold_and_upload_all_delta_not_just_unmatched(catalog, ctx, monkeypatch, count, called):
    ctx.settings = catalog
    clear_units(catalog)
    with database(catalog.db_path) as c, transaction(c):
        normalization.apply_table(c, table("이미 알려진 속성", None))
        units(c, 2, "이미 알려진 속성", None)
        units(c, 1, "이미 알려진 속성", None, synced=1)
        units(c, count - 1, separate_reviews=True)
        normalization.match_units(c)
    requests = []

    def request(settings, delta):
        requests.append(delta)
        known = table("이미 알려진 속성", None)
        fresh = table()
        return {"aspects": known["aspects"] + fresh["aspects"], "states": fresh["states"]}

    monkeypatch.setattr(normalization, "request_table", request)
    assert normalization.sync_if_needed(ctx) is called
    if called:
        assert len(requests[0]) == count + 1
        assert set(requests[0][0]) == {"unit_id", "review_id", "category_id", "raw_aspect", "raw_status"}
        with database(catalog.db_path) as c:
            assert normalization.unmatched_count(c) == 0
            assert c.execute("SELECT COUNT(*) FROM opinion_units WHERE cloud_synced=0").fetchone()[0] == 0
    else:
        assert not requests


def test_failed_lambda_keeps_collected_units_and_pending_flags(catalog, ctx, monkeypatch):
    ctx.settings = catalog
    clear_units(catalog)
    with database(catalog.db_path) as c, transaction(c):
        units(c, 301, separate_reviews=True)

    def fail(*args):
        raise RuntimeError("network failed")

    monkeypatch.setattr(normalization, "request_table", fail)
    assert normalization.sync_if_needed(ctx) is False
    with database(catalog.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM opinion_units WHERE cloud_synced=0").fetchone()[0] == 301
        assert normalization.unmatched_count(c) == 301
    assert (
        json.loads(Path(ctx.job_dir / "events.jsonl").read_text().splitlines()[-1])["event"] == "job.warning"
    )


def test_remapping_preserves_raw_ids_and_failed_apply_rolls_back(catalog):
    clear_units(catalog)
    with database(catalog.db_path) as c, transaction(c):
        units(c, 1)
        normalization.apply_table(c, table())
        before = tuple(
            c.execute(
                "SELECT raw_aspect,raw_status,aspect_variant_id,state_variant_id FROM opinion_units"
            ).fetchone()
        )
        old_cluster = c.execute("SELECT aspect_cluster_id FROM opinion_units").fetchone()[0]
    with database(catalog.db_path) as c, transaction(c):
        normalization.apply_table(c, table(label="새 대표"))
        assert before == tuple(
            c.execute(
                "SELECT raw_aspect,raw_status,aspect_variant_id,state_variant_id FROM opinion_units"
            ).fetchone()
        )
        assert old_cluster != c.execute("SELECT aspect_cluster_id FROM opinion_units").fetchone()[0]
    with database(catalog.db_path) as c:
        saved = normalization.export_table(c)
        invalid = table()
        invalid["states"][0]["aspect_cluster_id"] = "missing"
        with pytest.raises(sqlite3.IntegrityError), transaction(c):
            normalization.apply_table(c, invalid)
        assert normalization.export_table(c) == saved


def test_nonmatched_units_are_searchable_before_lambda(catalog):
    clear_units(catalog)
    with database(catalog.db_path) as c, transaction(c):
        units(c, 1)
    service = SearchService(catalog)
    result = service.find_review_attributes(query="새 속성")["clusters"][0]
    assert result["normalization_status"] == "unmatched" and result["aspect_cluster_id"] is None
    vid = result["matched_selector"][0]
    states = service.list_review_states([vid])["state_clusters"][0]
    assert states["normalization_status"] == "unmatched" and states["state_cluster_id"] is None
    matched = service.search_products(
        experience_conditions=[
            ExperienceCondition(
                condition_id="unmatched",
                aspect_variant_ids=[vid],
                desired_state_variant_ids=[states["variants"][0]["state_variant_id"]],
            )
        ]
    )
    assert matched["results"][0]["product_id"] == 1


def test_legacy_catalog_migration_uses_existing_assignments(catalog):
    with database(catalog.db_path) as c:
        # Simulate the old schema. No model is imported during migration.
        c.execute("ALTER TABLE opinion_units DROP COLUMN cloud_synced")
        init_schema(c)
        assert c.execute("SELECT COUNT(*) FROM exact_aspect_matches").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM exact_state_matches").fetchone()[0] == 3
        normalization.match_units(c)
        assert normalization.unmatched_count(c) == 0
        c.execute("DELETE FROM reviews")
        init_schema(c)
        assert c.execute("SELECT COUNT(*) FROM exact_aspect_matches").fetchone()[0] == 1


def test_local_runtime_has_no_embedding_dependencies(settings, tmp_path):
    code = (
        "import meta_search.server; import sys; print([m for m in sys.modules if m.split('.')[0] in "
        "{'torch','numpy','sklearn','transformers','sentence_transformers','huggingface_hub','boto3'}])"
    )
    config = tmp_path / "config.toml"
    config.write_text(
        settings.config_path.read_text().replace(
            'data_dir = "data"', "data_dir = " + json.dumps(str(settings.data_dir))
        )
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        check=True,
        env={**os.environ, "META_SEARCH_CONFIG": str(config)},
    )
    assert result.stdout.strip() == "[]"


def test_lambda_authorization_and_safe_http_error(settings, monkeypatch):
    from dataclasses import replace

    settings = replace(
        settings, remote_normalization={**settings.remote_normalization, "url": "https://lambda.test"}
    )
    monkeypatch.setenv("META_SEARCH_CLUSTER_TOKEN", "private-test-token")

    def fail(request, **kwargs):
        assert request.get_header("Authorization") == "Bearer private-test-token"
        assert json.loads(request.data) == {"units": []}
        raise HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr(normalization, "urlopen", fail)
    with pytest.raises(RuntimeError, match="HTTP 403") as err:
        normalization.request_table(settings, [])
    assert "private-test-token" not in str(err.value)


def test_known_status_matches_across_raw_aspects_in_same_cluster(catalog):
    clear_units(catalog)
    mapping = table("키감", "좋음")
    mapping["aspects"].append({**mapping["aspects"][0], "raw_aspect": "타건감"})
    with database(catalog.db_path) as c, transaction(c):
        units(c, 1, "타건감", "좋음")
        normalization.apply_table(c, mapping)
        assert normalization.unmatched_count(c) == 0
        assert (
            c.execute("SELECT state_cluster_id FROM opinion_units").fetchone()[0]
            == mapping["states"][0]["state_cluster_id"]
        )


def test_301_units_in_one_review_does_not_request_lambda(catalog, ctx, monkeypatch):
    ctx.settings = catalog
    clear_units(catalog)
    with database(catalog.db_path) as c, transaction(c):
        units(c, 301)

    def unexpected(*args):
        raise AssertionError("one review is below the review threshold")

    monkeypatch.setattr(normalization, "request_table", unexpected)
    assert normalization.sync_if_needed(ctx) is False
    with database(catalog.db_path) as c:
        assert len(normalization.pending_reviews(c)) == 1
        assert normalization.unmatched_count(c) == 301


def test_all_matched_reviews_trigger_and_next_batch_resets(catalog, ctx, monkeypatch):
    ctx.settings = catalog
    clear_units(catalog)
    with database(catalog.db_path) as c, transaction(c):
        units(c, 301, separate_reviews=True)
        normalization.apply_table(c, table())
        assert normalization.unmatched_count(c) == 0
    calls = []
    monkeypatch.setattr(
        normalization, "request_table", lambda settings, delta: calls.append(len(delta)) or table()
    )
    assert normalization.sync_if_needed(ctx) is True
    with database(catalog.db_path) as c, transaction(c):
        assert normalization.pending_reviews(c) == []
        units(c, 1)
        normalization.match_units(c)
    assert normalization.sync_if_needed(ctx) is False
    assert calls == [301]


def test_initial_table_seed_first_review_and_preserve_updated_table(settings, tmp_path):
    seed = table("키감", "좋음")
    seed["categories"] = [{"category_id": "keyboard", "display_name": "키보드"}]
    path = tmp_path / "initial.json"
    path.write_text(json.dumps(seed))
    with database(settings.db_path) as c:
        init_schema(c, path)
        assert c.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
        with transaction(c):
            c.execute(
                "INSERT INTO products(category_id,name,brand,model,model_key,source_url,status) VALUES('keyboard','Test','Test','Test','test','https://example.test','ready')"
            )
            units(c, 1, "키감", "좋음")
            normalization.match_units(c)
        row = c.execute("SELECT * FROM opinion_units").fetchone()
        assert row["aspect_cluster_id"] == seed["aspects"][0]["aspect_cluster_id"]
        assert row["state_cluster_id"] == seed["states"][0]["state_cluster_id"]
        with transaction(c):
            newer = table("키감", "좋음", label="새 대표")
            normalization.apply_table(c, newer)
        init_schema(c, path)
        assert (
            c.execute("SELECT aspect_cluster_id FROM opinion_units").fetchone()[0]
            == newer["aspects"][0]["aspect_cluster_id"]
        )


def test_legacy_reviews_follow_existing_sync_flags(catalog):
    clear_units(catalog)
    with database(catalog.db_path) as c, transaction(c):
        units(c, 1, synced=1)
        units(c, 1)
    with database(catalog.db_path) as c:
        c.execute("ALTER TABLE reviews DROP COLUMN cloud_synced")
        init_schema(c)
        assert len(normalization.pending_reviews(c)) == 1
