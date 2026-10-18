import json
import subprocess
from types import SimpleNamespace

from meta_search import llm
from meta_search.db import database
from meta_search.pipelines import experience, extract_objective, sales


def test_codex_cli_contract(ctx, monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        output = command[command.index("--output-last-message") + 1]
        from pathlib import Path

        Path(output).write_text('{"opinion_units":[]}')
        return SimpleNamespace(returncode=0, stdout="events", stderr="")

    monkeypatch.setattr(subprocess, "run", run)
    assert llm.run_codex("opinion_001", "prompt", "opinion_units.schema.json", ctx) == {"opinion_units": []}
    assert captured["command"][captured["command"].index("--sandbox") + 1] == "read-only"
    assert 'web_search="disabled"' in captured["command"]
    assert "--ignore-user-config" in captured["command"]
    assert captured["kwargs"]["input"] == "prompt"


def test_agy_cli_contract(ctx, monkeypatch):
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, kwargs=kwargs)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"status": "SUCCESS", "structured_output": {"offers": []}}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", run)
    assert llm.run_agy("sales", "prompt", "sales_offers.schema.json", "model", ctx) == {"offers": []}
    assert "--json-schema" in captured["command"]
    assert "--dangerously-skip-permissions" in captured["command"]
    assert "--disable-slash-commands" in captured["command"]


def test_one_review_failure_success_replaces(ctx, page, extraction, monkeypatch):
    monkeypatch.setattr(llm, "run_codex", lambda *args: extraction)
    target = extract_objective.run(ctx, page)
    page["reviews"] = [{**page["reviews"][0], "body": "bad"}, page["reviews"][0]]

    def codex(step, *args):
        if step.startswith("opinion_001"):
            raise llm.LLMError("bad review")
        return {"opinion_units": []}

    monkeypatch.setattr(llm, "run_codex", codex)
    experience.run(ctx, target, page)
    with database(ctx.settings.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
    target.mode = "update"
    page["reviews"] = []
    assert experience.run(ctx, target, page) == "skipped"
    with database(ctx.settings.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def test_sales_zero_and_python_timestamp(ctx, page, extraction, monkeypatch):
    monkeypatch.setattr(llm, "run_codex", lambda *args: extraction)
    target = extract_objective.run(ctx, page)
    monkeypatch.setattr(llm, "run_agy", lambda *args: {"offers": []})
    sales.run(ctx, target, page)
    with database(ctx.settings.db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
    output = {
        "offers": [
            {
                "seller": "S",
                "retailer": "R",
                "url": ctx.job["url"],
                "price": 100,
                "currency": "KRW",
                "shipping_fee": 0,
                "shipping_days": 0,
                "availability": "unknown",
                "evidence": "100",
            }
        ]
    }
    monkeypatch.setattr(llm, "run_agy", lambda *args: output)
    sales.run(ctx, target, page)
    with database(ctx.settings.db_path) as c:
        assert c.execute("SELECT last_checked_at FROM offers").fetchone()[0].endswith("Z")
