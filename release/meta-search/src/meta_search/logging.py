"""Job-scoped human and JSONL logs, without writing to MCP stdout."""

import hashlib
import json
import logging
import os
import time
import traceback
from pathlib import Path

from .db import now_utc


class AppendHandler(logging.Handler):
    """Open per record to avoid accumulating descriptors in a long-lived MCP server."""

    def __init__(self, path, formatter):
        super().__init__()
        self.path = Path(path)
        self.setFormatter(formatter)

    def emit(self, record):
        try:
            payload = (self.format(record) + "\n").encode("utf-8")
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                # A single append keeps events from parallel processes separate.
                written = os.write(descriptor, payload)
                if written != len(payload):
                    raise OSError("Incomplete log write")
            finally:
                os.close(descriptor)
        except Exception:
            self.handleError(record)


class EventFormatter(logging.Formatter):
    def __init__(self, job_id):
        super().__init__()
        self.job_id = job_id

    def format(self, record):
        item = {
            "timestamp": now_utc(),
            "level": record.levelname,
            "job_id": self.job_id,
            "pid": record.process,
            "thread": record.threadName,
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
            "data": getattr(record, "data", {}),
        }
        if record.exc_info:
            item["traceback"] = "".join(traceback.format_exception(*record.exc_info))
        return json.dumps(item, ensure_ascii=False, default=str)


class HumanFormatter(logging.Formatter):
    converter = time.gmtime

    def __init__(self, job_id):
        super().__init__(
            f"%(asctime)s %(levelname)s job={job_id} pid=%(process)d thread=%(threadName)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )


def job_logger(settings, job_id):
    folder = settings.data_dir / "jobs" / job_id
    folder.mkdir(parents=True, exist_ok=True)
    identity = hashlib.sha1(str(folder.resolve()).encode()).hexdigest()[:16]
    logger = logging.getLogger(f"meta_search.job.{identity}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        logger.addHandler(AppendHandler(folder / "orchestrator.log", HumanFormatter(job_id)))
        logger.addHandler(AppendHandler(folder / "events.jsonl", EventFormatter(job_id)))
    return logger


def event(logger, name, *, level="INFO", **data):
    message = name + (" " + json.dumps(data, ensure_ascii=False, default=str) if data else "")
    logger.log(getattr(logging, level), message, extra={"event": name, "data": data})
