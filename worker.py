"""Render Background Worker entrypoint.

Run with:
    celery -A worker:celery_app worker --loglevel=info --concurrency=1
"""
from task_queue import celery_app
from tasks import run_chat_job

__all__ = ["celery_app", "run_chat_job"]
