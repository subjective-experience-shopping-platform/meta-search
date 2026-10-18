import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn, initial_table=None):
    conn.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
    # Convert any early exact-state cache to the existing aspect-cluster boundary.
    if "raw_aspect" in {r[1] for r in conn.execute("PRAGMA table_info(exact_state_matches)")}:
        with transaction(conn):
            conn.execute("ALTER TABLE exact_state_matches RENAME TO legacy_exact_state_matches")
            conn.execute(
                "CREATE TABLE exact_state_matches (category_id TEXT NOT NULL REFERENCES categories(category_id) ON DELETE CASCADE, aspect_cluster_id TEXT NOT NULL REFERENCES aspect_clusters(aspect_cluster_id) ON DELETE CASCADE, raw_status TEXT NOT NULL, state_cluster_id TEXT NOT NULL REFERENCES state_clusters(state_cluster_id) ON DELETE CASCADE, PRIMARY KEY(category_id,aspect_cluster_id,raw_status))"
            )
            conn.execute(
                "INSERT OR IGNORE INTO exact_state_matches SELECT m.category_id,a.aspect_cluster_id,m.raw_status,m.state_cluster_id FROM legacy_exact_state_matches m JOIN exact_aspect_matches a ON a.category_id=m.category_id AND a.raw_aspect=m.raw_aspect"
            )
            conn.execute("DROP TABLE legacy_exact_state_matches")
    # One-time migration of the old locally clustered catalog, without running a model.
    columns = {r[1] for r in conn.execute("PRAGMA table_info(opinion_units)")}
    if "cloud_synced" not in columns:
        from .textnorm import norm_text

        with transaction(conn):
            conn.execute("ALTER TABLE opinion_units ADD COLUMN cloud_synced INTEGER NOT NULL DEFAULT 0")
            for row in conn.execute("SELECT * FROM opinion_units ORDER BY unit_id").fetchall():
                aspect = norm_text(row["raw_aspect"])
                if row["aspect_cluster_id"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO exact_aspect_matches VALUES(?,?,?)",
                        (row["category_id"], aspect, row["aspect_cluster_id"]),
                    )
                if row["state_cluster_id"] and row["raw_status"] is not None:
                    conn.execute(
                        "INSERT OR IGNORE INTO exact_state_matches VALUES(?,?,?,?)",
                        (
                            row["category_id"],
                            row["aspect_cluster_id"],
                            norm_text(row["raw_status"]),
                            row["state_cluster_id"],
                        ),
                    )
    if "cloud_synced" not in {r[1] for r in conn.execute("PRAGMA table_info(reviews)")}:
        with transaction(conn):
            conn.execute("ALTER TABLE reviews ADD COLUMN cloud_synced INTEGER NOT NULL DEFAULT 0")
            conn.execute(
                "UPDATE reviews SET cloud_synced=1 WHERE NOT EXISTS "
                "(SELECT 1 FROM opinion_units u WHERE u.review_id=reviews.review_id AND u.cloud_synced=0)"
            )
    if initial_table is not None and Path(initial_table).is_file():
        if not conn.execute("SELECT 1 FROM exact_aspect_matches LIMIT 1").fetchone():
            from .normalization import apply_table

            table = json.loads(Path(initial_table).read_text(encoding="utf-8"))
            with transaction(conn):
                for category in table["categories"]:
                    conn.execute(
                        "INSERT OR IGNORE INTO categories(category_id,display_name) VALUES(?,?)",
                        (category["category_id"], category["display_name"]),
                    )
                apply_table(conn, table)


@contextmanager
def database(path):
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
