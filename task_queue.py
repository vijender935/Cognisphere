"""Free-tier in-process job runner.

Jobs are durable in PostgreSQL, while execution happens inside the existing
Render Free web service. No paid Background Worker or Redis/Key Value broker
is required.

Important limitation: Render Free web services can restart or spin down after
15 minutes without inbound traffic, so this runner cannot guarantee execution
through a platform restart. Pending jobs remain in PostgreSQL and are recovered
when the service receives another request.
"""
from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor

from jobs import list_jobs, is_cancelled

logger = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="assistant-job")
_futures: dict[str, Future] = {}
_lock = threading.Lock()


def _run(job_id: str) -> None:
    from tasks import run_chat_job

    try:
        if is_cancelled(job_id):
            return
        run_chat_job(job_id)
    except Exception:
        logger.exception("Background job failed: %s", job_id)
    finally:
        with _lock:
            _futures.pop(job_id, None)


def enqueue_chat_job(job_id: str) -> Future:
    """Queue a chat job on the single in-process worker."""
    with _lock:
        existing = _futures.get(job_id)
        if existing and not existing.done():
            return existing
        future = _executor.submit(_run, job_id)
        _futures[job_id] = future
        return future


def recover_pending_jobs() -> int:
    """Re-submit durable jobs that are not terminal after a process restart."""
    jobs = list_jobs(statuses=["queued", "retrying"], limit=100)
    for job in jobs:
        enqueue_chat_job(job["id"])
    return len(jobs)


def active_job_count() -> int:
    with _lock:
        return sum(not f.done() for f in _futures.values())
