import json
from types import SimpleNamespace

import numpy as np
import pytest
from clustering import compute_clusters, opposite_status
from handler import ROOT, authenticated_source, exact_table, merge_units, open_catalog, rebuild


class FakeEncoder:
    def encode(self, texts, **kwargs):
        vectors = {
            "타건감": [1, 0, 0, 0],
            "키감": [1, 0, 0, 0],
            "조용함": [0, 1, 0, 0],
            "시끄러움": [0, 1, 0, 0],
            "있음": [0, 0, 1, 0],
            "없음": [0, 0, 1, 0],
        }
        return np.array([vectors.get(t, [0, 0, 0, 1]) for t in texts], dtype=float)


def test_cluster_opposites_variants():
    config = json.loads((ROOT / "config.json").read_text())
    settings = SimpleNamespace(**config)
    units = [
        {"unit_id": i, "review_id": i, "raw_aspect": a, "raw_status": s}
        for i, (a, s) in enumerate(
            [
                ("타건감", "조용함"),
                ("키감", "시끄러움"),
                ("타건감", "있음"),
                ("타건감", "없음"),
                ("전반적 상품 경험", None),
            ]
        )
    ]
    aspects, states, assigned = compute_clusters("keyboard", units, settings, FakeEncoder())
    assert len(aspects) == 2 and len(states) == 4
    assert assigned == compute_clusters("keyboard", list(reversed(units)), settings, FakeEncoder())[2]
    assert {s[2] for s in states} == {"조용함", "시끄러움", "있음", "없음"}


@pytest.mark.parametrize(
    "a,b", [("높음", "낮음"), ("소음 있음", "소음 없음"), ("가능", "사용 불가"), ("시끄러움", "조용함")]
)
def test_opposites(a, b):
    assert opposite_status(a, b) and opposite_status(b, a)


def test_merge_retry_friend_ids_and_full_rebuild(tmp_path):
    conn = open_catalog(tmp_path / "cloud.sqlite")
    unit = {
        "unit_id": 1,
        "review_id": 1,
        "category_id": "keyboard",
        "raw_aspect": "키감",
        "raw_status": "조용함",
    }
    with conn:
        merge_units(conn, "owner", [unit])
        merge_units(conn, "owner", [unit])
        merge_units(conn, "friend", [{**unit, "raw_aspect": "타건감", "raw_status": "시끄러움"}])
        rebuild(conn, FakeEncoder())
    assert conn.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(DISTINCT review_id) FROM units").fetchone()[0] == 2
    table = exact_table(conn)
    assert len(table["aspects"]) == 2 and len(table["states"]) == 2
    assert len({a["aspect_cluster_id"] for a in table["aspects"]}) == 1
    assert len({s["state_cluster_id"] for s in table["states"]}) == 2
    with conn:
        merge_units(conn, "owner", [{**unit, "unit_id": 2, "raw_aspect": "새 표현", "raw_status": None}])
        rebuild(conn, FakeEncoder())
    assert len(exact_table(conn)["aspects"]) == 3
    conn.close()


def test_auth_whitelist_and_missing_token():
    assert authenticated_source({"headers": {}}) is None
    assert authenticated_source({"headers": {"Authorization": "Bearer not-allowed"}}) is None


def test_old_state_cache_migrates_to_aspect_cluster_key(tmp_path):
    path = tmp_path / "old.sqlite"
    conn = open_catalog(path)
    conn.execute("INSERT INTO aspect_clusters VALUES('ac_test','keyboard','타건감')")
    conn.execute("INSERT INTO state_clusters VALUES('sc_test','ac_test','좋음')")
    conn.execute("INSERT INTO exact_aspect_matches VALUES('keyboard','키감','ac_test')")
    conn.execute("DROP TABLE exact_state_matches")
    conn.execute(
        "CREATE TABLE exact_state_matches (category_id TEXT, raw_aspect TEXT, raw_status TEXT, state_cluster_id TEXT)"
    )
    conn.execute("INSERT INTO exact_state_matches VALUES('keyboard','키감','좋음','sc_test')")
    conn.commit()
    conn.close()
    conn = open_catalog(path)
    assert exact_table(conn)["states"][0]["aspect_cluster_id"] == "ac_test"
    assert "raw_aspect" not in exact_table(conn)["states"][0]
    conn.close()
