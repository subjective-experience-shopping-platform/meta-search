"""Install the standalone MCP runtime and register it with Codex."""

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def executable(value):
    found = shutil.which(value)
    if found:
        return str(Path(found).resolve())
    path = Path(value).expanduser()
    if path.is_file():
        return str(path.resolve())
    raise ValueError(f"실행 파일을 찾을 수 없습니다: {value}")


def codex_executable(value=None):
    candidates = (
        [value] if value else ["codex", "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"]
    )
    for candidate in candidates:
        try:
            found = executable(candidate)
            help_text = subprocess.run(
                [found, "exec", "--help"], capture_output=True, text=True, check=True
            ).stdout
            if all(flag in help_text for flag in ("--ignore-user-config", "--output-schema", "--ephemeral")):
                return found
        except (ValueError, OSError, subprocess.CalledProcessError):
            pass
    raise ValueError(
        "현재 추출 옵션을 지원하는 Codex CLI가 필요합니다. 업데이트하거나 --codex-bin 경로를 지정하세요."
    )


def set_value(text, section, key, value):
    pattern = rf"(?ms)^\[{re.escape(section)}\]\s*\n(.*?)(?=^\[|\Z)"
    match = re.search(pattern, text)
    if not match:
        raise ValueError(f"설정 구역이 없습니다: {section}")
    body, count = re.subn(
        rf"(?m)^{re.escape(key)}\s*=.*$",
        lambda _: key + " = " + json.dumps(value, ensure_ascii=False),
        match.group(1),
        count=1,
    )
    if count != 1:
        raise ValueError(f"설정 키가 없습니다: {section}.{key}")
    return text[: match.start(1)] + body + text[match.end(1) :]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", help="Codex CLI 경로 또는 명령 이름")
    parser.add_argument("--agy-bin", default="agy", help="agy CLI 경로 또는 명령 이름")
    parser.add_argument("--token-file", type=Path, help="관리자에게 받은 토큰 원문 파일")
    parser.add_argument("--data-dir", type=Path, help="저장 폴더. 기본값은 이 폴더의 data/")
    parser.add_argument("--name", default="meta-search", help="Codex에 등록할 MCP 이름")
    parser.add_argument("--no-register", action="store_true", help="설치만 하고 Codex 등록은 생략")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.error("Python 3.11 이상이 필요합니다.")
    import tomllib

    try:
        uv = executable("uv")
        codex = codex_executable(args.codex_bin)
        agy = executable(args.agy_bin)
        config_path = ROOT / "config.toml"
        text = config_path.read_text(encoding="utf-8")
        text = set_value(text, "codex", "bin", codex)
        text = set_value(text, "agy", "bin", agy)
        if args.data_dir is not None:
            text = set_value(text, "paths", "data_dir", str(args.data_dir.expanduser().resolve()))
        config = tomllib.loads(text)
        data_dir = (ROOT / config["paths"]["data_dir"]).resolve()
        token_path = data_dir / config["remote_normalization"]["token_file"]
        if args.token_file:
            token = args.token_file.expanduser().read_text(encoding="utf-8").strip()
        elif os.environ.get("META_SEARCH_CLUSTER_TOKEN"):
            token = os.environ["META_SEARCH_CLUSTER_TOKEN"].strip()
        elif token_path.exists():
            token = token_path.read_text(encoding="utf-8").strip()
        else:
            token = getpass.getpass("관리자에게 받은 Lambda 토큰 원문: ").strip()
        if not token:
            raise ValueError("비어 있지 않은 Lambda 토큰이 필요합니다.")
        config_path.write_text(text, encoding="utf-8")
        data_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(token + "\n")
        if os.name != "nt":
            token_path.chmod(0o600)
        subprocess.run(
            [uv, "sync", "--directory", str(ROOT), "--python", "3.11", "--frozen", "--no-dev"], check=True
        )
        python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        probe = "from meta_search.config import load_settings; from meta_search.db import database,init_schema; s=load_settings(); c=database(s.db_path); conn=c.__enter__(); init_schema(conn,s.initial_table_path); c.__exit__(None,None,None)"
        subprocess.run(
            [str(python), "-c", probe],
            cwd=ROOT,
            env={**os.environ, "META_SEARCH_CONFIG": str(config_path)},
            check=True,
        )
        if not args.no_register:
            subprocess.run(
                [
                    codex,
                    "mcp",
                    "add",
                    args.name,
                    "--env",
                    f"META_SEARCH_CONFIG={config_path}",
                    "--",
                    str(python),
                    "-m",
                    "meta_search.server",
                ],
                check=True,
            )
        print(
            json.dumps(
                {
                    "installed": True,
                    "registered": not args.no_register,
                    "name": args.name,
                    "command": str(python),
                    "args": ["-m", "meta_search.server"],
                    "env": {"META_SEARCH_CONFIG": str(config_path)},
                    "token_file": str(token_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"설치 실패: {error}\n")


if __name__ == "__main__":
    main()
