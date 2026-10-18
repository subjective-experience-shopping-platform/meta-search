import json
import runpy
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD = runpy.run_path(str(ROOT / "scripts/build_release.py"))
INSTALL = runpy.run_path(str(ROOT / "scripts/release_assets/install.py"))


def test_release_contains_only_clean_runtime_and_portable_paths(tmp_path):
    report = BUILD["build_release"](destination=tmp_path)
    with zipfile.ZipFile(report["archive"]) as zipped:
        names = zipped.namelist()
        forbidden = {"data", "cloud", "tests", "scripts", "models", ".venv", "__pycache__"}
        assert not any(forbidden.intersection(Path(n).parts) for n in names)
        assert not any(
            n.endswith(
                ("demo.py", "AGENTS.md", "VERIFICATION.md", "IMPLEMENTATION_PLAN.md", ".sqlite", ".pyc")
            )
            for n in names
        )
        assert "meta-search/install.py" in names and "meta-search/uv.lock" in names
        initial = json.loads(zipped.read("meta-search/bootstrap_exact_matches.json"))
        assert set(initial) == {"categories", "aspects", "states"}
        assert initial == json.loads((ROOT / "bootstrap_exact_matches.json").read_text())
        assert initial["aspects"] and initial["states"]
        assert "unit_id" not in json.dumps(initial) and "excerpt" not in json.dumps(initial)
        config = tomllib.loads(zipped.read("meta-search/config.toml").decode())
        assert config["paths"] == {"data_dir": "data", "prompts_dir": "prompts", "schemas_dir": "schemas"}
        assert config["codex"]["bin"] == "codex" and config["agy"]["bin"] == "agy"
        metadata = tomllib.loads(zipped.read("meta-search/pyproject.toml").decode())
        assert metadata["project"]["license"] == "GPL-3.0-only"
        assert zipped.read("meta-search/LICENSE") == (ROOT / "LICENSE").read_bytes()
        assert "dependency-groups" not in metadata
        lock = tomllib.loads(zipped.read("meta-search/uv.lock").decode())
        assert not {"pytest", "ruff", "boto3", "numpy", "torch", "sentence-transformers"}.intersection(
            p["name"] for p in lock["package"]
        )
        for name in names:
            assert b"/Users/jaewone" not in zipped.read(name)
    (tmp_path / "meta-search/data").mkdir()
    (tmp_path / "meta-search/data/my-file").write_text("keep")
    with pytest.raises(ValueError, match="사용자 데이터"):
        BUILD["build_release"](destination=tmp_path)
    assert (tmp_path / "meta-search/data/my-file").read_text() == "keep"


def test_installer_toml_escapes_windows_paths_and_preserves_endpoint():
    source = '[codex]\nbin = "codex"\nmodel = "model"\n[remote_normalization]\nurl = "https://lambda.test/"\n'
    value = r"C:\Program Files\Codex\codex.exe"
    result = INSTALL["set_value"](source, "codex", "bin", value)
    parsed = tomllib.loads(result)
    assert parsed["codex"]["bin"] == value
    assert parsed["remote_normalization"]["url"] == "https://lambda.test/"
    assert json.loads(result.splitlines()[1].split(" = ")[1]) == value
