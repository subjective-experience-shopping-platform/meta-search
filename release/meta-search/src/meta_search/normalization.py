"""Exact-match locally; ask Lambda to rebuild after collecting over 300 new reviews."""

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .db import database, transaction
from .textnorm import norm_text


def match_units(conn, category_id=None):
    aspects = {
        (r["category_id"], r["raw_aspect"]): r["aspect_cluster_id"]
        for r in conn.execute("SELECT * FROM exact_aspect_matches")
    }
    states = {
        (r["category_id"], r["aspect_cluster_id"], r["raw_status"]): r["state_cluster_id"]
        for r in conn.execute("SELECT * FROM exact_state_matches")
    }
    query = "SELECT unit_id,category_id,raw_aspect,raw_status FROM opinion_units"
    params = ()
    if category_id is not None:
        query += " WHERE category_id=?"
        params = (category_id,)
    assignments = []
    for u in conn.execute(query, params):
        key = (u["category_id"], norm_text(u["raw_aspect"]))
        aid = aspects.get(key)
        sid = (
            states.get((u["category_id"], aid, norm_text(u["raw_status"])))
            if aid and u["raw_status"] is not None
            else None
        )
        assignments.append((aid, sid, u["unit_id"]))
    conn.executemany(
        "UPDATE opinion_units SET aspect_cluster_id=?,state_cluster_id=? WHERE unit_id=?", assignments
    )


def export_table(conn):
    return {
        "aspects": [
            dict(r)
            for r in conn.execute(
                "SELECT m.*,c.label FROM exact_aspect_matches m JOIN aspect_clusters c USING(aspect_cluster_id)"
            )
        ],
        "states": [
            dict(r)
            for r in conn.execute(
                "SELECT m.*,c.label FROM exact_state_matches m JOIN state_clusters c USING(state_cluster_id)"
            )
        ],
    }


def apply_table(conn, table):
    """Replace the latest table and remap every local raw expression in the same transaction."""
    categories = {r[0] for r in conn.execute("SELECT category_id FROM categories")}
    for cid in categories:
        conn.execute("DELETE FROM aspect_clusters WHERE category_id=?", (cid,))
    for row in table["aspects"]:
        if row["category_id"] not in categories:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO aspect_clusters VALUES(?,?,?)",
            (row["aspect_cluster_id"], row["category_id"], row["label"]),
        )
        conn.execute(
            "INSERT INTO exact_aspect_matches VALUES(?,?,?)",
            (row["category_id"], norm_text(row["raw_aspect"]), row["aspect_cluster_id"]),
        )
    for row in table["states"]:
        if row["category_id"] not in categories:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO state_clusters VALUES(?,?,?)",
            (row["state_cluster_id"], row["aspect_cluster_id"], row["label"]),
        )
        conn.execute(
            "INSERT INTO exact_state_matches VALUES(?,?,?,?)",
            (
                row["category_id"],
                row["aspect_cluster_id"],
                norm_text(row["raw_status"]),
                row["state_cluster_id"],
            ),
        )
    match_units(conn)


def pending_units(conn, product_id=None):
    # Include the current product before it becomes ready, but exclude other failed registrations.
    return [
        dict(r)
        for r in conn.execute(
            "SELECT u.unit_id,u.review_id,u.category_id,u.raw_aspect,u.raw_status FROM opinion_units u "
            "JOIN products p USING(product_id) WHERE u.cloud_synced=0 AND (p.status='ready' OR p.product_id=?) "
            "ORDER BY u.unit_id",
            (product_id,),
        )
    ]


def unmatched_count(conn, product_id=None):
    return conn.execute(
        "SELECT COUNT(*) FROM opinion_units u JOIN products p USING(product_id) "
        "WHERE (p.status='ready' OR p.product_id=?) AND "
        "(u.aspect_cluster_id IS NULL OR (u.raw_status IS NOT NULL AND u.state_cluster_id IS NULL))",
        (product_id,),
    ).fetchone()[0]


def pending_reviews(conn, product_id=None):
    return [
        r[0]
        for r in conn.execute(
            "SELECT r.review_id FROM reviews r JOIN products p USING(product_id) "
            "WHERE r.cloud_synced=0 AND (p.status='ready' OR p.product_id=?) ORDER BY r.review_id",
            (product_id,),
        )
    ]


def request_table(settings, units):
    config = settings.remote_normalization
    url = config["url"]
    if not url.startswith("https://"):
        raise ValueError("remote_normalization.url에 HTTPS Lambda URL이 필요합니다")
    token = os.environ.get("META_SEARCH_CLUSTER_TOKEN")
    if not token:
        token = (settings.data_dir / config["token_file"]).read_text().strip()
    if not token:
        raise ValueError("Lambda 인증 토큰이 없습니다")
    request = Request(
        url,
        data=json.dumps({"units": units}, ensure_ascii=False).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=config["timeout_sec"]) as response:
            return json.load(response)
    except HTTPError as error:
        # Do not include headers or the token in errors/logs.
        raise RuntimeError(f"Lambda 정규화 요청 실패: HTTP {error.code}") from None
    except URLError:
        raise RuntimeError("Lambda 정규화 서버에 연결할 수 없습니다") from None


def sync_if_needed(ctx):
    with database(ctx.settings.db_path) as conn:
        count = unmatched_count(conn, ctx.job.get("product_id"))
        units = pending_units(conn, ctx.job.get("product_id"))
        reviews = pending_reviews(conn, ctx.job.get("product_id"))
    config = ctx.settings.remote_normalization
    threshold = config.get("review_threshold", config.get("unmatched_threshold", 300))
    ctx.emit(
        "normalization.exact_match", unmatched_units=count, pending_reviews=len(reviews), threshold=threshold
    )
    if len(reviews) <= threshold:
        return False
    ctx.emit(
        "normalization.request", unmatched_units=count, sent_units=len(units), pending_reviews=len(reviews)
    )
    try:
        table = request_table(ctx.settings, units)
        with database(ctx.settings.db_path) as conn, transaction(conn):
            apply_table(conn, table)
            conn.executemany(
                "UPDATE opinion_units SET cloud_synced=1 WHERE unit_id=?", [(u["unit_id"],) for u in units]
            )
            conn.executemany(
                "UPDATE reviews SET cloud_synced=1 WHERE review_id=?", [(rid,) for rid in reviews]
            )
    except Exception as error:
        # Collection is already saved. A later registration can send the same pending rows again.
        ctx.emit("normalization.failed", level="ERROR", error=str(error)[:300])
        ctx.warn(f"Lambda 정규화 실패; 수집 데이터와 비매칭 상태 유지: {str(error)[:300]}")
        return False
    ctx.emit(
        "normalization.synced",
        sent_units=len(units),
        synced_reviews=len(reviews),
        aspect_matches=len(table["aspects"]),
        state_matches=len(table["states"]),
    )
    return True
