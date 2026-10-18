import json
import runpy
from pathlib import Path

import pytest

SYNC = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/sync_antigravity.py"))


def source_project(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text(
        '[paths]\ndata_dir = "data"\nprompts_dir = "prompts"\nschemas_dir = "schemas"\n[remote_normalization]\nurl = "https://example.test"\n'
    )
    for relative, content in [
        ("src/meta_search/server.py", "new server"),
        ("models/model/model.safetensors", "model bytes"),
        ("data/catalog.sqlite", "user DB"),
        (".venv/bin/python", "original environment"),
        ("src/meta_search/__pycache__/x.pyc", "cache"),
    ]:
        file = source / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    return source


def test_copy_is_independent_data_shared_and_no_runtime_overwrite(tmp_path):
    source = source_project(tmp_path)
    destination = tmp_path / "runtime"
    own = destination / "data/existing.sqlite"
    own.parent.mkdir(parents=True)
    own.write_text("keep target data")
    report = SYNC["sync_files"](source, destination)
    assert report["verified_files"] == 2
    assert (destination / "src/meta_search/server.py").read_text() == "new server"
    model = destination / "models/model/model.safetensors"
    assert not model.exists()
    assert own.read_text() == "keep target data"
    assert not (destination / "data/catalog.sqlite").exists()
    assert not (destination / ".venv").exists()
    import tomllib

    config = tomllib.loads((destination / "config.toml").read_text())
    assert config["paths"]["data_dir"] == str(source / "data")
    assert config["remote_normalization"]["url"] == "https://example.test"


def test_unchanged_files_stay_unchanged_and_managed_deletion_only(tmp_path):
    source = source_project(tmp_path)
    destination = tmp_path / "runtime"
    SYNC["sync_files"](source, destination)
    target = destination / "src/meta_search/server.py"
    timestamp = target.stat().st_mtime_ns
    user_file = destination / "my-note.txt"
    user_file.write_text("keep")
    report = SYNC["sync_files"](source, destination)
    assert report["copied"] == [] and report["removed"] == []
    assert target.stat().st_mtime_ns == timestamp
    (source / "src/meta_search/server.py").unlink()
    report = SYNC["sync_files"](source, destination)
    assert report["removed"] == ["src/meta_search/server.py"]
    assert user_file.read_text() == "keep"


def test_reject_overlapping_paths(tmp_path):
    source = source_project(tmp_path)
    with pytest.raises(ValueError, match="겹치지"):
        SYNC["sync_files"](source, source / "runtime")


def test_deployment_manifest_cannot_delete_external_files(tmp_path):
    source = source_project(tmp_path)
    destination = tmp_path / "runtime"
    destination.mkdir()
    outside = tmp_path / "keep.txt"
    outside.write_text("keep")
    (destination / SYNC["MANIFEST"]).write_text(json.dumps({"source": str(source), "files": ["../keep.txt"]}))
    with pytest.raises(ValueError, match="관리 범위"):
        SYNC["sync_files"](source, destination)
    assert outside.read_text() == "keep"


def test_old_managed_models_removed_but_cloud_and_unmanaged_files_preserved(tmp_path):
    source = source_project(tmp_path)
    (source / "cloud").mkdir()
    (source / "cloud/requirements.txt").write_text("torch")
    destination = tmp_path / "runtime"
    managed = destination / "models/model/model.safetensors"
    managed.parent.mkdir(parents=True)
    managed.write_text("old managed model")
    unmanaged = destination / "models/my-note.txt"
    unmanaged.write_text("keep")
    (destination / SYNC["MANIFEST"]).write_text(
        json.dumps(
            {
                "source": str(source),
                "files": ["models/model/model.safetensors"],
            }
        )
    )
    report = SYNC["sync_files"](source, destination)
    assert report["removed"] == ["models/model/model.safetensors"]
    assert not managed.exists() and unmanaged.read_text() == "keep"
    assert not (destination / "cloud").exists()
