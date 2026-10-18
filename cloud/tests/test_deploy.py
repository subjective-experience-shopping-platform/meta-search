import hashlib
import json
import shutil
import sqlite3

import deploy
import pytest


def test_alias_persisted_on_following_deploy(tmp_path):
    token = tmp_path / "token"
    token.write_text("test-owner-token\n")
    path = tmp_path / "allowed_tokens.json"
    digest = hashlib.sha256(b"test-owner-token").hexdigest()
    path.write_text(json.dumps({digest: "old-arn", "friend-hash": "friend"}))
    source, previous, count = deploy.owner_source(path, token, "aws-arn", "jaewonE:7701")
    assert (source, previous, count) == ("jaewonE:7701", "old-arn", 2)
    assert deploy.owner_source(path, token, "aws-arn") == ("jaewonE:7701", "jaewonE:7701", 2)
    assert json.loads(path.read_text())["friend-hash"] == "friend"


class FakeS3:
    def __init__(self, remote):
        self.remote = remote
        self.uploads = 0

    def download_file(self, bucket, key, path):
        shutil.copyfile(self.remote, path)

    def upload_file(self, path, bucket, key):
        self.uploads += 1
        shutil.copyfile(path, self.remote)


def catalog(tmp_path, second_source="friend"):
    path = tmp_path / "latest.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE units(source TEXT,unit_id INTEGER,review_id TEXT,raw_aspect TEXT,PRIMARY KEY(source,unit_id))"
    )
    conn.executemany(
        "INSERT INTO units VALUES(?,?,?,?)",
        [
            ("old-arn", 1, "old-arn:1", "키감"),
            ("old-arn", 2, "old-arn:2", "소음"),
            (second_source, 1, second_source + ":1", "무게"),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_alias_migration_preserves_inputs_and_replay_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "CLOUD", tmp_path)
    path = catalog(tmp_path)
    s3 = FakeS3(path)
    assert deploy.rename_source(s3, "bucket", "old-arn", "jaewonE:7701") == 2
    assert s3.uploads == 1
    conn = sqlite3.connect(path)
    assert conn.execute(
        "SELECT source,unit_id,review_id,raw_aspect FROM units ORDER BY raw_aspect"
    ).fetchall() == [
        ("friend", 1, "friend:1", "무게"),
        ("jaewonE:7701", 2, "jaewonE:7701:2", "소음"),
        ("jaewonE:7701", 1, "jaewonE:7701:1", "키감"),
    ]
    conn.close()
    assert deploy.rename_source(s3, "bucket", "old-arn", "jaewonE:7701") == 0
    assert s3.uploads == 1


def test_alias_collision_keeps_remote_catalog_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "CLOUD", tmp_path)
    path = catalog(tmp_path, "jaewonE:7701")
    before = path.read_bytes()
    s3 = FakeS3(path)
    with pytest.raises(ValueError, match="이미 데이터"):
        deploy.rename_source(s3, "bucket", "old-arn", "jaewonE:7701")
    assert path.read_bytes() == before and s3.uploads == 0
