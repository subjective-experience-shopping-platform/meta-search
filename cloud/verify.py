"""Verify real bearer authentication, full clustering/S3 persistence and local remapping.

Uses only existing collected inputs, without LLM/browser calls or synthetic cloud rows.
Run after deployment: uv run --with 'boto3[crt]' python cloud/verify.py
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def raw_hash(conn):
    rows = [
        tuple(r)
        for r in conn.execute(
            "SELECT unit_id,review_id,product_id,category_id,raw_aspect,raw_status,excerpt,opinion,sentiment,"
            "aspect_variant_id,state_variant_id FROM opinion_units ORDER BY unit_id"
        )
    ]
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest()


def main():
    from meta_search.config import load_settings
    from meta_search.db import database, init_schema, transaction
    from meta_search.normalization import (
        apply_table,
        export_table,
        pending_units,
        request_table,
        unmatched_count,
    )

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replay-existing",
        action="store_true",
        help="Replay one existing Unit to verify delta merge without adding test data",
    )
    args = parser.parse_args()
    settings = load_settings()
    deployed = json.loads((ROOT / "cloud/.build/deployment.json").read_text())
    checks = {}
    for label, headers in [("no_token", {}), ("invalid_token", {"Authorization": "Bearer invalid-token"})]:
        try:
            with urlopen(
                Request(
                    settings.remote_normalization["url"], data=b'{"units":[]}', headers=headers, method="POST"
                ),
                timeout=60,
            ):
                raise AssertionError("unauthorized request was accepted")
        except HTTPError as error:
            assert error.code == 403
            checks[label + "_status"] = error.code
    with database(settings.db_path) as conn:
        init_schema(conn)
        before = raw_hash(conn)
        units = pending_units(conn)
        if args.replay_existing and not units:
            units = [
                dict(r)
                for r in conn.execute(
                    "SELECT unit_id,review_id,category_id,raw_aspect,raw_status FROM opinion_units ORDER BY unit_id LIMIT 1"
                )
            ]
    started = time.perf_counter()
    table = request_table(settings, units)
    duration = round(time.perf_counter() - started, 3)
    with database(settings.db_path) as conn, transaction(conn):
        apply_table(conn, table)
        conn.executemany(
            "UPDATE opinion_units SET cloud_synced=1 WHERE unit_id=?", [(u["unit_id"],) for u in units]
        )
        assert before == raw_hash(conn)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        local_table = export_table(conn)
        assert local_table == table
        checks["local_units"] = conn.execute("SELECT COUNT(*) FROM opinion_units").fetchone()[0]
        checks["unmatched_units"] = unmatched_count(conn)
        checks["aspect_clusters"] = conn.execute("SELECT COUNT(*) FROM aspect_clusters").fetchone()[0]
        checks["state_clusters"] = conn.execute("SELECT COUNT(*) FROM state_clusters").fetchone()[0]
    path = ROOT / "cloud/.build/verified-latest.sqlite"
    boto3.client("s3", region_name=deployed["region"]).download_file(
        deployed["bucket"], "latest.sqlite", str(path)
    )
    cloud = sqlite3.connect(path)
    checks["s3_units"] = cloud.execute("SELECT COUNT(*) FROM units").fetchone()[0]
    checks["s3_aspect_matches"] = cloud.execute("SELECT COUNT(*) FROM exact_aspect_matches").fetchone()[0]
    checks["s3_state_matches"] = cloud.execute("SELECT COUNT(*) FROM exact_state_matches").fetchone()[0]
    cloud.close()
    checks.update(
        raw_fields_unchanged=True,
        raw_sha256=before,
        duration_sec=duration,
        delta_units_sent=len(units),
        aspect_matches=len(table["aspects"]),
        state_matches=len(table["states"]),
    )
    (ROOT / "data/deployment/lambda_verification.json").write_text(json.dumps(checks, indent=2) + "\n")
    print(json.dumps(checks, indent=2), flush=True)


if __name__ == "__main__":
    main()
