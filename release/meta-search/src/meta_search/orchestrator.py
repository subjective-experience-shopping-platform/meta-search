import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import yaml
from filelock import FileLock

from .config import load_settings
from .db import database, init_schema, now_utc, transaction
from .jobs import JobContext
from .logging import event, job_logger
from .normalization import sync_if_needed
from .notify import notify
from .pipelines import collect_page, experience, extract_objective, objective, sales
from .validation import PipelineError


def run_parallel(ctx, target, snapshot):
    pipelines = {"objective": objective.run, "experience": experience.run, "sales": sales.run}
    if target.mode == "update":
        ctx.update(objective_status="skipped")
        ctx.emit("pipeline.skipped", pipeline="objective", reason="same_model_update")
        del pipelines["objective"]

    def execute(name, function):
        ctx.update(**{name + "_status": "running"})
        started = time.perf_counter()
        ctx.emit("pipeline.started", pipeline=name)
        try:
            status = function(ctx, target, snapshot) or "done"
            ctx.update(**{name + "_status": status})
            ctx.emit(
                "pipeline.finished",
                pipeline=name,
                status=status,
                duration_sec=round(time.perf_counter() - started, 3),
            )
        except Exception as error:
            ctx.update(**{name + "_status": "failed"})
            ctx.logger.exception(
                "%s 실패",
                name,
                extra={
                    "event": "pipeline.failed",
                    "data": {"pipeline": name, "duration_sec": round(time.perf_counter() - started, 3)},
                },
            )
            return f"{name}: {error}"
        return None

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(execute, name, function) for name, function in pipelines.items()]
        errors = [error for future in futures if (error := future.result())]
    if errors:
        raise PipelineError("; ".join(errors))


def export_document(ctx, target):
    with database(ctx.settings.db_path) as conn:
        product = dict(
            conn.execute("SELECT * FROM products WHERE product_id=?", (target.product_id,)).fetchone()
        )
        attributes = conn.execute(
            "SELECT a.key,a.tier,p.value_json FROM product_attributes p JOIN category_attributes a USING(category_id,key) WHERE product_id=? ORDER BY a.key",
            (target.product_id,),
        ).fetchall()
    front = {k: product[k] for k in ("product_id", "name", "brand", "model", "source_url")}
    front.update(category=product["category_id"], core={}, extra={})
    for attribute in attributes:
        front[attribute["tier"]][attribute["key"]] = json.loads(attribute["value_json"])
    folder = ctx.settings.data_dir / "docs"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{target.product_id}.md"
    temp = path.with_suffix(".tmp")
    temp.write_text(
        "---\n"
        + yaml.safe_dump(front, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + product["details_md"]
        + "\n",
        encoding="utf-8",
    )
    temp.replace(path)
    ctx.emit("document.written", product_id=target.product_id, path=str(path), bytes=path.stat().st_size)


def execute_job(ctx):
    target = None
    job_started = time.perf_counter()
    ctx.emit("job.started", url=ctx.job["url"])
    ctx.update(status="running", started_at=now_utc())
    try:
        ctx.update(stage="collect_page")
        snapshot = collect_page.run(ctx)
        ctx.update(stage="extract_objective")
        target = extract_objective.run(ctx, snapshot)
        ctx.update(stage="parallel")
        run_parallel(ctx, target, snapshot)
        ctx.update(stage="normalization")
        sync_if_needed(ctx)
        ctx.update(stage="finalize")
        # Normalization can rename keys or change tiers for existing products too.
        with database(ctx.settings.db_path) as conn:
            document_ids = [
                row[0]
                for row in conn.execute(
                    "SELECT product_id FROM products WHERE category_id=? AND (status='ready' OR product_id=?) ORDER BY product_id",
                    (target.category_id, target.product_id),
                )
            ]
        for product_id in document_ids:
            export_document(ctx, replace(target, product_id=product_id))
        with database(ctx.settings.db_path) as conn, transaction(conn):
            conn.execute(
                "UPDATE products SET status='ready',updated_at=? WHERE product_id=?",
                (now_utc(), target.product_id),
            )
        ctx.update(status="done", stage="done", finished_at=now_utc())
        with database(ctx.settings.db_path) as conn:
            counts = [
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE product_id=?", (target.product_id,)
                ).fetchone()[0]
                for table in ("product_attributes", "reviews", "opinion_units", "offers")
            ]
            warnings = json.loads(
                conn.execute("SELECT warnings FROM jobs WHERE job_id=?", (ctx.job_id,)).fetchone()[0]
            )
        ctx.emit(
            "job.completed",
            duration_sec=round(time.perf_counter() - job_started, 3),
            product_id=target.product_id,
            mode=target.mode,
            attribute_count=counts[0],
            review_count=counts[1],
            unit_count=counts[2],
            offer_count=counts[3],
            warning_count=len(warnings),
        )
        notify(
            "Meta-Search 등록 완료",
            f"{target.identity['name']} ({target.category_id}) · 객관 {counts[0]}개 · 리뷰 {counts[1]}개/Unit {counts[2]}개 · 판매처 {counts[3]}곳"
            + (f" · 경고 {len(warnings)}건" if warnings else ""),
        )
    except Exception as error:
        ctx.emit(
            "stage.failed",
            level="ERROR",
            stage=ctx.job["stage"],
            duration_sec=round(time.perf_counter() - ctx._stage_clock, 3),
            error=str(error)[:2000],
        )
        ctx.logger.exception(
            "등록 실패",
            extra={
                "event": "job.failed",
                "data": {
                    "stage": ctx.job["stage"],
                    "duration_sec": round(time.perf_counter() - job_started, 3),
                },
            },
        )
        if target and target.mode == "new":
            with database(ctx.settings.db_path) as conn, transaction(conn):
                conn.execute("UPDATE products SET status='failed' WHERE product_id=?", (target.product_id,))
        message = f"{ctx.job['stage']}: {error}"[:2000]
        ctx.update(status="failed", error=message, finished_at=now_utc())
        notify(
            "Meta-Search 등록 실패",
            f"{target.identity['name'] if target else ctx.job['url']} · {message[:120]}",
        )


def main(job_id=None):
    if job_id is None:
        parser = argparse.ArgumentParser()
        parser.add_argument("--job-id", required=True)
        job_id = parser.parse_args().job_id
    settings = load_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with database(settings.db_path) as conn:
        init_schema(conn, settings.initial_table_path)
    # A shared file lock plus DB creation order provides FIFO, unlike flock wakeup order alone.
    logger = job_logger(settings, job_id)
    lock_started = time.perf_counter()
    event(logger, "orchestrator.lock_wait")
    with FileLock(str(settings.data_dir / "orchestrator.lock")):
        event(logger, "orchestrator.lock_acquired", wait_sec=round(time.perf_counter() - lock_started, 3))
        with database(settings.db_path) as conn:
            requested = conn.execute("SELECT rowid FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if requested is None:
                raise PipelineError("작업 없음")
            queue = [
                r[0]
                for r in conn.execute(
                    "SELECT job_id FROM jobs WHERE status='queued' AND rowid<=? ORDER BY rowid",
                    (requested[0],),
                )
            ]
        event(logger, "orchestrator.queue", job_ids=queue)
        for queued_id in queue:
            execute_job(JobContext(queued_id, settings))


if __name__ == "__main__":
    main()
