"""Celery queue configuration for durable background jobs."""
from __future__ import annotations

import os

from celery import Celery


BROKER_URL = os.getenv("CELERY_BROKER_URL", "").strip()

# A memory broker keeps imports/tests usable when the queue is not configured.
# Production enqueueing is explicitly blocked unless CELERY_BROKER_URL exists.
celery_app = Celery("personal_ai_assistant", broker=BROKER_URL or "memory://")

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_reject_on_worker_lost=True,
    broker_transport_options={"visibility_timeout": 21600},
    result_backend_transport_options={"visibility_timeout": 21600},
    visibility_timeout=21600,
    task_default_queue="personal-ai-assistant",
)


def enqueue_chat_job(job_id: str):
    if not BROKER_URL:
        raise RuntimeError("CELERY_BROKER_URL is not configured.")
    # Import here so API startup does not depend on task registration order.
    from tasks import run_chat_job

    return run_chat_job.apply_async(args=[job_id], task_id=job_id)
