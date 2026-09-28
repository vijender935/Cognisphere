"""Render Background Worker entrypoint.

Run with:
    celery -A worker:celery_app worker --loglevel=info --concurrency=1
"""
from task_queue import celery_app
from tasks import run_chat_job
from tools import init_db
from jobs import init_jobs_db
from memory import init_semantic_store
from connectors import init_connectors_db
from preferences import init_preferences_db

# The worker is a separate Render service, so it must initialize the shared
# PostgreSQL schema itself instead of relying on the web process.
init_db()
init_jobs_db()
init_semantic_store()
init_connectors_db()
init_preferences_db()

__all__ = ["celery_app", "run_chat_job"]
