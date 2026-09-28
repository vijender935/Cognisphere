from jobs import (
    create_job,
    get_job,
    init_jobs_db,
    list_jobs,
    mark_completed,
    mark_progress,
)


def test_persistent_job_lifecycle():
    init_jobs_db()
    job=create_job(
        "chat",
        {"message":"hello","session_id":"job-session","attachment_paths":[]},
    )
    assert job["status"]=="queued"
    assert job["payload"]["message"]=="hello"

    mark_progress(job["id"],42,"Running")
    current=get_job(job["id"])
    assert current["status"]=="queued"
    assert current["progress"]==42
    assert current["message"]=="Running"

    mark_completed(job["id"],"hello back")
    done=get_job(job["id"])
    assert done["status"]=="completed"
    assert done["progress"]==100
    assert done["result"]=="hello back"

    rows=list_jobs(statuses=["completed"],limit=10)
    assert any(row["id"]==job["id"] for row in rows)


def test_chat_task_updates_postgres_job(monkeypatch):
    import tasks

    init_jobs_db()
    job=create_job(
        "chat",
        {
            "message":"run this in worker",
            "session_id":"worker-session",
            "attachment_paths":[],
            "memory":False,
            "web_search":False,
        },
    )
    monkeypatch.setattr(tasks,"run_agent",lambda *args,**kwargs:"worker result")
    result=tasks.run_chat_job(job["id"])

    assert result=="worker result"
    stored=get_job(job["id"])
    assert stored["status"]=="completed"
    assert stored["progress"]==100
    assert stored["result"]=="worker result"
