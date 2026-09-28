"""Celery task definitions for Personal AI Assistant background work."""
from __future__ import annotations

import logging
import os

from celery import Task

from agent import run_agent
from jobs import get_job, mark_completed, mark_failed, mark_progress, mark_retrying, mark_running
from task_queue import celery_app

logger = logging.getLogger(__name__)


class DurableJobTask(Task):
    autoretry_for = (Exception,)
    retry_backoff = True
    retry_backoff_max = 300
    retry_jitter = True
    max_retries = max(0, int(os.getenv("JOB_MAX_RETRIES", "3")))
    acks_late = True
    reject_on_worker_lost = True

    def on_retry(self, exc, task_id, args, kwargs, einfo):
        mark_retrying(task_id, f"Retrying after error: {str(exc)[:300]}")

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        mark_failed(task_id, str(exc))
        logger.exception("Background job failed: %s", task_id, exc_info=(type(exc), exc, exc.__traceback__))

    def on_success(self, retval, task_id, args, kwargs):
        # The task body writes the durable result before Celery acknowledges it.
        return super().on_success(retval, task_id, args, kwargs)


@celery_app.task(
    bind=True,
    base=DurableJobTask,
    name="personal_ai_assistant.run_chat_job",
)
def run_chat_job(self, job_id: str):
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found.")

    if job["status"] == "completed":
        return job["result"]

    mark_running(job_id)
    payload = job["payload"]
    mark_progress(job_id, 10, "Preparing assistant task")

    answer = run_agent(
        payload["message"],
        session_id=payload["session_id"],
        image_urls=None,
        rag_sources=payload.get("rag_sources") or None,
        memory_enabled=bool(payload.get("memory", True)),
        web_search_enabled=bool(payload.get("web_search", True)),
        verbose=False,
    )

    if isinstance(answer, str) and answer.startswith("❌"):
        raise RuntimeError(answer)

    mark_progress(job_id, 95, "Saving result")
    mark_completed(job_id, answer)
    return answer
