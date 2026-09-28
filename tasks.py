"""Chat job execution for the free-tier in-process runner."""
from __future__ import annotations

import logging
import os
import time

from agent import run_agent
from document_parser import is_supported_document
from multimodal import image_data_url, r2_enabled, ensure_local_file
from config import FILE_ROOT
from jobs import get_job, mark_completed, mark_failed, mark_progress, mark_retrying, mark_running

logger = logging.getLogger(__name__)


def _execute_once(job_id: str) -> str:
    job = get_job(job_id)
    if not job:
        raise ValueError(f"Job {job_id} not found.")

    if job["status"] == "completed":
        return job["result"] or ""

    mark_running(job_id)
    payload = job["payload"]
    mark_progress(job_id, 10, "Preparing assistant task")

    image_urls = []
    rag_sources = list(payload.get("rag_sources") or [])
    for path in payload.get("attachment_paths") or []:
        try:
            image_urls.append(image_data_url(path))
            continue
        except ValueError:
            pass

        if not r2_enabled():
            raise RuntimeError(
                "Background jobs with file attachments require Cloudflare R2."
            )
        candidate = ensure_local_file(path)
        if not is_supported_document(candidate):
            raise ValueError(f"Unsupported attachment: {path}")
        rag_sources.append(str(candidate.relative_to(FILE_ROOT.resolve())))

    mark_progress(job_id, 35, "Running assistant")
    answer = run_agent(
        payload["message"],
        session_id=payload["session_id"],
        image_urls=image_urls or None,
        rag_sources=rag_sources or None,
        memory_enabled=bool(payload.get("memory", True)),
        web_search_enabled=bool(payload.get("web_search", True)),
        verbose=False,
    )

    if isinstance(answer, str) and answer.startswith("❌"):
        raise RuntimeError(answer)

    mark_progress(job_id, 95, "Saving result")
    mark_completed(job_id, answer)
    return answer


def run_chat_job(job_id: str) -> str:
    """Execute a durable job with bounded retries inside this process."""
    max_retries = max(0, int(os.getenv("JOB_MAX_RETRIES", "3")))
    delay = 2.0

    for attempt in range(max_retries + 1):
        try:
            return _execute_once(job_id)
        except Exception as exc:
            if attempt >= max_retries:
                mark_failed(job_id, str(exc))
                raise
            mark_retrying(
                job_id,
                f"Retry {attempt + 1}/{max_retries}: {str(exc)[:300]}",
            )
            time.sleep(delay)
            delay = min(delay * 2, 30.0)

    raise RuntimeError("Unreachable")
