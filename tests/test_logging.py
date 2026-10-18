import json
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from meta_search import llm
from meta_search.jobs import JobContext
from meta_search.logging import AppendHandler


def records(ctx):
    return [json.loads(line) for line in (ctx.job_dir / "events.jsonl").read_text().splitlines()]


def test_structured_logging_stage_warning_no_stdout(ctx, capsys):
    ctx.update(stage="collect_page", status="running")
    ctx.warn("줄바꿈\n경고")
    items = records(ctx)
    assert items[0]["event"] == "job.queued"
    assert any(r["event"] == "stage.started" for r in items)
    assert items[-1]["event"] == "job.warning"
    assert items[-1]["data"]["warning"] == "줄바꿈\n경고"
    assert all(r["timestamp"].endswith("Z") and r["job_id"] == ctx.job_id for r in items)
    assert all(r["pid"] and r["thread"] for r in items)
    assert not ctx.logger.propagate
    assert capsys.readouterr().out == ""


def test_parallel_events_complete_without_duplicate_handlers(ctx):
    same = JobContext(ctx.job_id, ctx.settings)
    assert sum(isinstance(handler, AppendHandler) for handler in same.logger.handlers) == 2

    def emit(i):
        same.emit("parallel.test", index=i)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(emit, range(100)))
    items = [r for r in records(ctx) if r["event"] == "parallel.test"]
    assert len(items) == 100
    assert {r["data"]["index"] for r in items} == set(range(100))


def test_timeout_preserves_partial_output_and_logs(ctx, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            ["test", "private prompt"], 3, output=b"partial stdout", stderr=b"partial stderr"
        )

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(llm.LLMError, match="timeout after 3s"):
        llm._execute(["test", "private prompt"], ctx, "step", timeout=3)
    assert (ctx.job_dir / "step.stdout.txt").read_text() == "partial stdout"
    assert (ctx.job_dir / "step.stderr.txt").read_text() == "partial stderr"
    failed = records(ctx)[-1]
    assert failed["event"] == "process.failed"
    assert failed["data"]["error_type"] == "TimeoutExpired"
    assert "private prompt" not in failed["message"]
    assert failed["data"]["duration_sec"] >= 0


def test_retry_and_validation_events(ctx, monkeypatch):
    calls = []

    def call(*args):
        calls.append(args)
        return {"invalid": True} if len(calls) == 1 else {"opinion_units": []}

    monkeypatch.setattr(llm, "run_codex", call)
    llm.invoke(
        ctx,
        "opinions",
        "codex",
        "opinion_units.md",
        "opinion_units.schema.json",
        {"product_name": "Test", "product_category": "키보드", "review": "리뷰"},
    )
    events = [r["event"] for r in records(ctx)]
    assert events.count("llm.request") == 2
    assert events.count("llm.retry") == 1
    assert "llm.validation_failed" in events and "llm.validated" in events
