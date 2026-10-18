import os
import subprocess
import sys

import pytest

from meta_search import jobs
from meta_search.config import load_settings
from meta_search.db import database, init_schema
from meta_search.validation import ValidationError


def test_register_immediate_and_detached(ctx, monkeypatch):
    captured = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: captured.append((args, kwargs)))
    result = jobs.register_product(ctx.settings, "https://shop.test/product")
    assert result["status"] == "queued"
    args, kwargs = captured[0]
    assert kwargs["start_new_session"] is True
    assert kwargs["env"]["META_SEARCH_CONFIG"] == str(ctx.settings.config_path)
    assert "--job-id" in args[0]


def test_spawn_failure(ctx, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("missing executable")

    monkeypatch.setattr(subprocess, "Popen", fail)
    with pytest.raises(ValidationError, match="시작 실패"):
        jobs.register_product(ctx.settings, "https://shop.test/product")
    assert jobs.get_registration_status(ctx.settings)["jobs"][0]["status"] == "failed"


def test_real_process_fifo_lock(tmp_path):
    original = load_settings()
    config = tmp_path / "config.toml"
    text = (
        original.config_path.read_text()
        .replace('data_dir = "data"', 'data_dir = "data"')
        .replace('prompts_dir = "prompts"', f'prompts_dir = "{original.prompts_dir}"')
        .replace('schemas_dir = "schemas"', f'schemas_dir = "{original.schemas_dir}"')
    )
    config.write_text(text)
    settings = load_settings(config)
    with database(settings.db_path) as conn:
        init_schema(conn)
    first = jobs.create_job(settings, "https://shop.test/1")
    second = jobs.create_job(settings, "https://shop.test/2")
    runner = tmp_path / "run.py"
    runner.write_text("""import sys,time
from meta_search import orchestrator

def execute(ctx):
    ctx.update(status="running")
    with (ctx.settings.data_dir/"order.txt").open("a") as f:
        f.write(ctx.job_id+"\\n")
    time.sleep(.1)
    ctx.update(status="done",stage="done")

orchestrator.execute_job=execute
orchestrator.main(sys.argv[1])
""")
    env = {**os.environ, "META_SEARCH_CONFIG": str(config)}
    processes = [
        subprocess.Popen(
            [sys.executable, str(runner), jid], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for jid in (second, first)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr.decode()
    assert (settings.data_dir / "order.txt").read_text().splitlines() == [first, second]
