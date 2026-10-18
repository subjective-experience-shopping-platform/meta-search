import json
import os
import subprocess
import sys
import threading
import time
import uuid

from .db import database, init_schema, now_utc, transaction
from .logging import event, job_logger
from .validation import ValidationError, valid_url


class JobContext:
    def __init__(self, job_id, settings):
        self.job_id, self.settings = job_id, settings
        self.job_dir = settings.data_dir / "jobs" / job_id
        self.job_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.logger = job_logger(settings, job_id)
        self._stage_clock = time.perf_counter()
        with database(settings.db_path) as conn:
            init_schema(conn, settings.initial_table_path)
            row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise ValidationError(f"등록 작업 없음: {job_id}")
        self.job = dict(row)

    def update(self, **values):
        allowed = {
            "status",
            "stage",
            "product_id",
            "is_update",
            "objective_status",
            "experience_status",
            "sales_status",
            "error",
            "started_at",
            "finished_at",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("잘못된 job 필드")
        with database(self.settings.db_path) as conn, transaction(conn):
            conn.execute(
                "UPDATE jobs SET " + ",".join(f"{key}=?" for key in values) + " WHERE job_id=?",
                (*values.values(), self.job_id),
            )
        with self._lock:
            old_stage = self.job["stage"]
            if "stage" in values and values["stage"] != old_stage:
                self.emit(
                    "stage.finished",
                    stage=old_stage,
                    duration_sec=round(time.perf_counter() - self._stage_clock, 3),
                )
                self._stage_clock = time.perf_counter()
                if values["stage"] != "done":
                    self.emit("stage.started", stage=values["stage"])
            self.job.update(values)
        self.emit("job.updated", **values)

    def warn(self, message):
        with database(self.settings.db_path) as conn, transaction(conn):
            warnings = json.loads(
                conn.execute("SELECT warnings FROM jobs WHERE job_id=?", (self.job_id,)).fetchone()[0]
            )
            warnings.append(message)
            conn.execute(
                "UPDATE jobs SET warnings=? WHERE job_id=?",
                (json.dumps(warnings, ensure_ascii=False), self.job_id),
            )
        self.emit("job.warning", level="WARNING", warning=message)

    def emit(self, name, *, level="INFO", **data):
        event(self.logger, name, level=level, **data)

    def write_json(self, filename, value):
        path = self.job_dir / filename
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        self.emit("artifact.written", path=filename, bytes=path.stat().st_size)


def create_job(settings, url, category_hint=None):
    if not valid_url(url):
        raise ValidationError("상품 URL은 유효한 http:// 또는 https:// 주소여야 합니다.")
    job_id = uuid.uuid4().hex[:12]
    with database(settings.db_path) as conn, transaction(conn):
        conn.execute("INSERT INTO jobs(job_id,url,category_hint) VALUES(?,?,?)", (job_id, url, category_hint))
    event(job_logger(settings, job_id), "job.queued", url=url, category_hint=category_hint)
    return job_id


def spawn_orchestrator(settings, job_id):
    job_dir = settings.data_dir / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with (job_dir / "orchestrator.log").open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "meta_search.orchestrator", "--job-id", job_id],
            cwd=settings.project_root,
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "META_SEARCH_CONFIG": str(settings.config_path)},
        )
    event(job_logger(settings, job_id), "orchestrator.spawned", child_pid=getattr(process, "pid", None))


def register_product(settings, url, category_hint=None):
    job_id = create_job(settings, url, category_hint)
    try:
        spawn_orchestrator(settings, job_id)
    except OSError as error:
        JobContext(job_id, settings).update(status="failed", error=f"spawn: {error}", finished_at=now_utc())
        raise ValidationError(
            f"오케스트레이터 시작 실패: {error}. CLI 경로를 확인하고 재등록하세요."
        ) from error
    return {
        "job_id": job_id,
        "status": "queued",
        "message": "등록을 시작했습니다. 완료되면 macOS 알림이 표시됩니다.",
    }


def get_registration_status(settings, job_id=None):
    with database(settings.db_path) as conn:
        sql = "SELECT j.*,p.name AS product_name FROM jobs j LEFT JOIN products p USING(product_id)"
        rows = conn.execute(
            sql + (" WHERE job_id=?" if job_id else " ORDER BY j.rowid DESC LIMIT 10"),
            (job_id,) if job_id else (),
        ).fetchall()
    if job_id and not rows:
        raise ValidationError(f"작업 {job_id}가 없습니다. job_id 없이 최근 작업 목록을 확인하세요.")
    result = []
    for row in rows:
        item = dict(row)
        item["warnings"] = json.loads(item["warnings"])
        result.append(item)
    return result[0] if job_id else {"jobs": result}
