"""Persistent job state stored in PostgreSQL.

The queue itself is Celery/Render Key Value; PostgreSQL is the durable source
of truth for job status and results so the API can recover after restarts.
"""
from __future__ import annotations

import json
import uuid
from typing import Any

from db import connect


JOB_STATUSES = {"queued", "running", "retrying", "completed", "failed", "cancelled"}


def init_jobs_db() -> None:
    with connect() as con:
        con.execute(
            """CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                message TEXT,
                result TEXT,
                error TEXT,
                celery_task_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at TIMESTAMP,
                completed_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at)")


def _decode(row):
    if not row:
        return None
    (
        job_id,
        job_type,
        status,
        payload,
        progress,
        message,
        result,
        error,
        celery_task_id,
        attempts,
        created_at,
        started_at,
        completed_at,
        updated_at,
    ) = row
    return {
        "id": job_id,
        "type": job_type,
        "status": status,
        "payload": json.loads(payload or "{}"),
        "progress": int(progress or 0),
        "message": message,
        "result": result,
        "error": error,
        "celery_task_id": celery_task_id,
        "attempts": int(attempts or 0),
        "created_at": created_at,
        "started_at": started_at,
        "completed_at": completed_at,
        "updated_at": updated_at,
    }


def create_job(job_type: str, payload: dict[str, Any]) -> dict:
    job_id = str(uuid.uuid4())
    with connect() as con:
        con.execute(
            """INSERT INTO jobs(id,type,status,payload,progress,message)
               VALUES(?,?,?,?,?,?)""",
            (job_id, job_type, "queued", json.dumps(payload, ensure_ascii=False), 0, "Queued"),
        )
    return get_job(job_id)


def get_job(job_id: str) -> dict | None:
    with connect() as con:
        row = con.execute(
            """SELECT id,type,status,payload,progress,message,result,error,
                      celery_task_id,attempts,created_at,started_at,completed_at,updated_at
               FROM jobs WHERE id=?""",
            (job_id,),
        ).fetchone()
    return _decode(row)


def set_celery_task_id(job_id: str, task_id: str) -> None:
    with connect() as con:
        con.execute(
            "UPDATE jobs SET celery_task_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (task_id, job_id),
        )


def mark_running(job_id: str) -> None:
    with connect() as con:
        con.execute(
            """UPDATE jobs
               SET status='running',progress=5,message='Worker started',attempts=attempts+1,
                   started_at=COALESCE(started_at,CURRENT_TIMESTAMP),updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (job_id,),
        )


def mark_progress(job_id: str, progress: int, message: str) -> None:
    progress = max(0, min(99, int(progress)))
    with connect() as con:
        con.execute(
            """UPDATE jobs SET progress=?,message=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status NOT IN ('completed','failed','cancelled')""",
            (progress, message[:500], job_id),
        )


def mark_retrying(job_id: str, message: str) -> None:
    with connect() as con:
        con.execute(
            """UPDATE jobs SET status='retrying',message=?,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND status NOT IN ('completed','cancelled')""",
            (message[:500], job_id),
        )


def mark_completed(job_id: str, result: str) -> None:
    with connect() as con:
        con.execute(
            """UPDATE jobs
               SET status='completed',progress=100,message='Completed',result=?,error=NULL,
                   completed_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (result, job_id),
        )


def mark_failed(job_id: str, error: str) -> None:
    with connect() as con:
        con.execute(
            """UPDATE jobs
               SET status='failed',message='Failed',error=?,completed_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (error[:4000], job_id),
        )


def list_jobs(statuses: list[str] | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(int(limit), 100))
    clauses = []
    params = []
    if statuses:
        valid = [x for x in statuses if x in JOB_STATUSES]
        if valid:
            placeholders = ",".join("?" for _ in valid)
            clauses.append(f"status IN ({placeholders})")
            params.extend(valid)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as con:
        rows = con.execute(
            f"""SELECT id,type,status,payload,progress,message,result,error,
                       celery_task_id,attempts,created_at,started_at,completed_at,updated_at
                FROM jobs{where} ORDER BY created_at DESC LIMIT ?""",
            (*params, limit),
        ).fetchall()
    return [_decode(row) for row in rows]
