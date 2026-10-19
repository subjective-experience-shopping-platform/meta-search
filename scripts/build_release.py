"""Create a clean standalone source runtime and ZIP using an explicit file allowlist."""

import hashlib
import json
import runpy
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "scripts/release_assets"


def portable_config(source):
    set_value = runpy.run_path(str(ASSETS / "install.py"))["set_value"]
    text = source.read_text(encoding="utf-8")
    for section, key, value in [
        ("paths", "data_dir", "data"),
        ("paths", "prompts_dir", "prompts"),
        ("paths", "schemas_dir", "schemas"),
        ("codex", "bin", "codex"),
        ("agy", "bin", "agy"),
    ]:
        text = set_value(text, section, key, value)
    return text


def build_release(root=ROOT, destination=None):
    destination = Path(destination or root / "release")
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / "meta-search"
    if (target / "data").exists() or (target / ".venv").exists():
        raise ValueError(
            "배포 폴더에 설치 환경이나 사용자 데이터가 있습니다. 원본 배포본을 새 위치에 생성하세요."
        )
    project = tomllib.loads((root / "pyproject.toml").read_text())
    with tempfile.TemporaryDirectory(prefix="meta-search-release-") as temporary:
        staged = Path(temporary) / "meta-search"
        staged.mkdir()
        for folder in ("src/meta_search", "prompts", "schemas"):
            for file in sorted((root / folder).rglob("*")):
                if not file.is_file() or file.suffix not in (".py", ".sql", ".md", ".json"):
                    continue
                if "__pycache__" in file.parts or file.name == "demo.py":
                    continue
                relative = file.relative_to(root)
                out = staged / relative
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, out)
        (staged / "config.toml").write_text(portable_config(root / "config.toml"), encoding="utf-8")
        shutil.copyfile(root / "bootstrap_exact_matches.json", staged / "bootstrap_exact_matches.json")
        for name in ("install.py", "README.md"):
            shutil.copyfile(ASSETS / name, staged / name)
        shutil.copyfile(root / "LICENSE", staged / "LICENSE")
        metadata = (
            "\n".join(
                [
                    "[project]",
                    'name = "meta-search"',
                    'license = "GPL-3.0-only"',
                    "version = " + json.dumps(project["project"]["version"]),
                    "description = " + json.dumps(project["project"]["description"]),
                    'requires-python = ">=3.11"',
                    "dependencies = " + json.dumps(project["project"]["dependencies"]),
                    "[project.scripts]",
                    'meta-search = "meta_search.server:main"',
                    "[build-system]",
                    'requires = ["hatchling"]',
                    'build-backend = "hatchling.build"',
                    "[tool.hatch.build.targets.wheel]",
                    'packages = ["src/meta_search"]',
                ]
            )
            + "\n"
        )
        (staged / "pyproject.toml").write_text(metadata, encoding="utf-8")
        (staged / ".gitignore").write_text(".venv/\ndata/\n__pycache__/\nAGENTS.md\nAGNETS.md\n", encoding="utf-8")
        uv = shutil.which("uv")
        if not uv:
            raise ValueError("uv가 필요합니다.")
        if (target / "uv.lock").is_file():
            shutil.copyfile(target / "uv.lock", staged / "uv.lock")
        subprocess.run([uv, "lock", "--directory", str(staged)], check=True)
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(staged, target)
    archive = destination / "meta-search.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for file in sorted(target.rglob("*")):
            if file.is_file():
                zipped.write(file, file.relative_to(destination).as_posix())
    files = [p.relative_to(target).as_posix() for p in sorted(target.rglob("*")) if p.is_file()]
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    (destination / "SHA256SUMS").write_text(checksum + "  meta-search.zip\n", encoding="utf-8")
    return {
        "directory": str(target),
        "archive": str(archive),
        "files": len(files),
        "size_bytes": archive.stat().st_size,
        "sha256": checksum,
    }


if __name__ == "__main__":
    print(json.dumps(build_release(), ensure_ascii=False, indent=2))
