"""One synchronous Lambda, one overwritten S3 SQLite file, no version/concurrency machinery."""

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from textnorm import norm_text

ROOT = Path(__file__).resolve().parent
LATEST_KEY = "latest.sqlite"
LOGGER = logging.getLogger(__name__)


def open_catalog(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS units (
            source TEXT NOT NULL, unit_id INTEGER NOT NULL, review_id TEXT NOT NULL,
            category_id TEXT NOT NULL, raw_aspect TEXT NOT NULL, raw_status TEXT,
            PRIMARY KEY(source, unit_id)
        );
        CREATE TABLE IF NOT EXISTS aspect_clusters (
            aspect_cluster_id TEXT PRIMARY KEY, category_id TEXT NOT NULL, label TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS state_clusters (
            state_cluster_id TEXT PRIMARY KEY, aspect_cluster_id TEXT NOT NULL, label TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS exact_aspect_matches (
            category_id TEXT NOT NULL, raw_aspect TEXT NOT NULL, aspect_cluster_id TEXT NOT NULL,
            PRIMARY KEY(category_id, raw_aspect)
        );
        CREATE TABLE IF NOT EXISTS exact_state_matches (
            category_id TEXT NOT NULL, aspect_cluster_id TEXT NOT NULL, raw_status TEXT NOT NULL,
            state_cluster_id TEXT NOT NULL, PRIMARY KEY(category_id, aspect_cluster_id, raw_status)
        );
    """)
    if "raw_aspect" in {r[1] for r in conn.execute("PRAGMA table_info(exact_state_matches)")}:
        conn.execute("ALTER TABLE exact_state_matches RENAME TO legacy_exact_state_matches")
        conn.execute(
            "CREATE TABLE exact_state_matches (category_id TEXT NOT NULL, aspect_cluster_id TEXT NOT NULL, raw_status TEXT NOT NULL, state_cluster_id TEXT NOT NULL, PRIMARY KEY(category_id,aspect_cluster_id,raw_status))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO exact_state_matches SELECT m.category_id,a.aspect_cluster_id,m.raw_status,m.state_cluster_id FROM legacy_exact_state_matches m JOIN exact_aspect_matches a ON a.category_id=m.category_id AND a.raw_aspect=m.raw_aspect"
        )
        conn.execute("DROP TABLE legacy_exact_state_matches")
        conn.commit()
    return conn


def validate_units(units):
    if not isinstance(units, list):
        raise ValueError("units must be a list")
    for u in units:
        if not isinstance(u, dict):
            raise ValueError("invalid unit")
        for field in ("unit_id", "review_id"):
            if type(u.get(field)) is not int or u[field] <= 0:
                raise ValueError(f"invalid {field}")
        for field in ("category_id", "raw_aspect"):
            if not isinstance(u.get(field), str) or not u[field].strip():
                raise ValueError(f"invalid {field}")
        if u.get("raw_status") is not None and (
            not isinstance(u["raw_status"], str) or not u["raw_status"].strip()
        ):
            raise ValueError("invalid raw_status")
    return units


def merge_units(conn, source, units):
    conn.executemany(
        "INSERT OR REPLACE INTO units VALUES(?,?,?,?,?,?)",
        [
            (
                source,
                u["unit_id"],
                f"{source}:{u['review_id']}",
                u["category_id"],
                u["raw_aspect"],
                u.get("raw_status"),
            )
            for u in units
        ],
    )


def rebuild(conn, encoder=None):
    from clustering import compute_clusters

    config = json.loads((ROOT / "config.json").read_text())
    config["embedding"]["model_path"] = str(ROOT / config["embedding"]["model_path"])
    settings = SimpleNamespace(**config)
    for table in ("exact_state_matches", "exact_aspect_matches", "state_clusters", "aspect_clusters"):
        conn.execute(f"DELETE FROM {table}")
    categories = [r[0] for r in conn.execute("SELECT DISTINCT category_id FROM units ORDER BY category_id")]
    for cid in categories:
        units = [
            dict(r)
            for r in conn.execute("SELECT * FROM units WHERE category_id=? ORDER BY source,unit_id", (cid,))
        ]
        for u in units:
            u["unit_id"] = f"{u['source']}:{u['unit_id']}"
        aspects, states, assignments = compute_clusters(cid, units, settings, encoder)
        conn.executemany("INSERT INTO aspect_clusters VALUES(?,?,?)", aspects)
        conn.executemany("INSERT INTO state_clusters VALUES(?,?,?)", states)
        assigned = {uid: (aid, sid) for aid, sid, uid in assignments}
        for u in units:
            aid, sid = assigned[u["unit_id"]]
            raw = norm_text(u["raw_aspect"])
            conn.execute("INSERT OR IGNORE INTO exact_aspect_matches VALUES(?,?,?)", (cid, raw, aid))
            if sid is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO exact_state_matches VALUES(?,?,?,?)",
                    (cid, aid, norm_text(u["raw_status"]), sid),
                )


def exact_table(conn):
    return {
        "aspects": [
            dict(r)
            for r in conn.execute(
                "SELECT m.*,c.label FROM exact_aspect_matches m JOIN aspect_clusters c USING(aspect_cluster_id) "
                "ORDER BY category_id,raw_aspect"
            )
        ],
        "states": [
            dict(r)
            for r in conn.execute(
                "SELECT m.*,c.label FROM exact_state_matches m "
                "JOIN state_clusters c USING(state_cluster_id) ORDER BY category_id,aspect_cluster_id,raw_status"
            )
        ],
    }


def authenticated_source(event):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    value = headers.get("authorization", "")
    if not value.startswith("Bearer "):
        return None
    digest = hashlib.sha256(value[7:].encode()).hexdigest()
    whitelist = json.loads((ROOT / "allowed_tokens.json").read_text())
    return next(
        (source for expected, source in whitelist.items() if hmac.compare_digest(digest, expected)), None
    )


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def lambda_handler(event, context):
    source = authenticated_source(event)
    if source is None:
        return response(403, {"error": "Forbidden"})
    if event.get("requestContext", {}).get("http", {}).get("method") != "POST":
        return response(405, {"error": "POST required"})
    try:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode()
        payload = json.loads(body)
        units = validate_units(payload["units"])
    except (ValueError, KeyError, TypeError, UnicodeError):
        return response(400, {"error": "Invalid units"})
    path = Path("/tmp/meta-search-latest.sqlite")
    conn = None
    try:
        import boto3
        from botocore.exceptions import ClientError

        s3 = boto3.client("s3")
        bucket = os.environ["CATALOG_BUCKET"]
        path.unlink(missing_ok=True)
        try:
            s3.download_file(bucket, LATEST_KEY, str(path))
        except ClientError as error:
            if error.response["Error"]["Code"] not in ("404", "NoSuchKey"):
                raise
        conn = open_catalog(path)
        with conn:
            merge_units(conn, source, units)
            rebuild(conn)
        table = exact_table(conn)
        count = conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        conn.close()
        conn = None
        s3.upload_file(str(path), bucket, LATEST_KEY)
        LOGGER.warning(
            "Recluster completed source=%s received_units=%s total_units=%s", source, len(units), count
        )
        return response(200, table)
    except Exception:
        LOGGER.exception("Recluster failed")
        return response(500, {"error": "Reclustering failed"})
    finally:
        if conn is not None:
            conn.close()
