"""Deploy completed meta-search changes into Antigravity2's independent MCP runtime."""

import argparse
import filecmp
import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

EXCLUDED = {
    ".git",
    ".venv",
    "data",
    "dist",
    "cloud",
    "release",
    "models",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".cache",
}
MANIFEST = ".deployment-manifest.json"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not path.is_symlink() and path.read_bytes() == content:
        return False
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        file.write(content)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def deployment_config(source):
    text = (source / "config.toml").read_text(encoding="utf-8")
    config = tomllib.loads(text)
    data_dir = (source / config["paths"]["data_dir"]).resolve()
    match = re.search(r"(?ms)^\[paths\]\s*\n(.*?)(?=^\[|\Z)", text)
    if match is None:
        raise ValueError("config.toml에 [paths]가 없습니다")
    body, count = re.subn(
        r"(?m)^data_dir\s*=.*$",
        lambda _: "data_dir = " + json.dumps(str(data_dir), ensure_ascii=False),
        match.group(1),
        count=1,
    )
    if count != 1:
        raise ValueError("config.toml의 paths.data_dir를 찾을 수 없습니다")
    return (text[: match.start(1)] + body + text[match.end(1) :]).encode("utf-8")


def source_files(source):
    return {
        path.relative_to(source).as_posix(): path
        for path in source.rglob("*")
        if path.is_file()
        and not any(part in EXCLUDED or part.endswith(".egg-info") for part in path.relative_to(source).parts)
        and path.name != MANIFEST
    }


def sync_files(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("원본과 복사 대상은 서로 겹치지 않는 폴더여야 합니다")
    destination.mkdir(parents=True, exist_ok=True)
    marker = destination / MANIFEST
    previous = json.loads(marker.read_text()) if marker.exists() else {}
    if previous and previous.get("source") != str(source):
        raise ValueError("다른 원본에서 배포한 폴더입니다. source를 확인하세요")
    files = source_files(source)
    copied, removed = [], []
    for relative, file in sorted(files.items()):
        target = destination / relative
        if not target.parent.resolve().is_relative_to(destination):
            raise ValueError(f"관리 범위 밖의 복사 경로: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        if relative == "config.toml":
            if atomic_write(target, deployment_config(source)):
                copied.append(relative)
        elif not target.is_file() or target.is_symlink() or not filecmp.cmp(file, target, shallow=False):
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as temp:
                temporary = Path(temp.name)
            try:
                shutil.copy2(file, temporary)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            copied.append(relative)
    for relative in sorted(set(previous.get("files", [])) - files.keys()):
        target = destination / relative
        # The old deployment included model weights. Remove only previously managed model files.
        legacy_model = Path(relative).parts[0] == "models" and ".." not in Path(relative).parts
        if not target.resolve().is_relative_to(destination) or any(
            part in EXCLUDED and not (part == "models" and legacy_model) for part in Path(relative).parts
        ):
            raise ValueError(f"관리 범위 밖의 삭제 경로: {relative}")
        if target.is_file():
            target.unlink()
            removed.append(relative)
    filecmp.clear_cache()
    # Verify the lightweight local runtime, excluding the independent cloud build.
    for relative, file in files.items():
        target = destination / relative
        same = (
            target.read_bytes() == deployment_config(source)
            if relative == "config.toml"
            else filecmp.cmp(file, target, shallow=False)
        )
        if not same or target.is_symlink():
            raise RuntimeError(f"복사 검증 실패: {relative}")
    atomic_write(
        marker,
        json.dumps(
            {"source": str(source), "files": sorted(files), "synced_at": now()}, ensure_ascii=False, indent=2
        ).encode(),
    )
    return {
        "source": str(source),
        "destination": str(destination),
        "verified_files": len(files),
        "copied": copied,
        "removed": removed,
        "synced_at": now(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path.home() / ".gemini/config/mcp/meta-search")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1]
    destination = args.destination.resolve()
    report = sync_files(source, destination)
    uv = shutil.which("uv") or str(Path.home() / ".local/bin/uv")
    subprocess.run(
        [uv, "sync", "--directory", str(destination), "--python", "3.11", "--frozen", "--no-dev"], check=True
    )
    registry = Path.home() / ".gemini/config/mcp_config.json"
    before = json.loads(registry.read_text())
    config = tomllib.loads((source / "config.toml").read_text())
    subprocess.run(
        [
            config["agy"]["bin"],
            "mcp",
            "add",
            "--env",
            f"META_SEARCH_CONFIG={destination / 'config.toml'}",
            "meta-search",
            str(destination / ".venv/bin/python"),
            "-m",
            "meta_search.server",
        ],
        check=True,
    )
    after = json.loads(registry.read_text())
    for key, value in before.get("mcpServers", {}).items():
        if key != "meta-search" and after.get("mcpServers", {}).get(key) != value:
            raise RuntimeError(f"관계없는 MCP 설정 변경: {key}")
    for key, value in before.items():
        if key != "mcpServers" and after.get(key) != value:
            raise RuntimeError(f"관계없는 설정 변경: {key}")
    report["dependencies_installed"] = True
    report["registered"] = True
    atomic_write(
        source / "data/deployment/antigravity_sync.json",
        json.dumps(report, ensure_ascii=False, indent=2).encode(),
    )
    print(
        json.dumps(
            {
                "destination": report["destination"],
                "verified_files": report["verified_files"],
                "copied_files": len(report["copied"]),
                "removed_files": len(report["removed"]),
                "registered": True,
                "report": str(source / "data/deployment/antigravity_sync.json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
