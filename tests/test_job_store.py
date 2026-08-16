from backend.services.job_store import JobStore


def test_job_store_persists_progress_and_logs(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create_job(
        job_id="abc",
        source_kind="local",
        source_value="/tmp/input.mp4",
        reference_path=None,
        settings={"whisper_model": "turbo"},
        work_dir=str(tmp_path / "abc"),
    )
    updated = store.update_job(
        "abc", status="running", progress=33.3, segments_done=2, segments_total=6
    )
    log_entry = store.add_log("abc", "Whisper started")

    job = store.get_job("abc")
    assert "logs" not in updated
    assert job["status"] == "running"
    assert job["progress"] == 33.3
    assert job["settings"]["whisper_model"] == "turbo"
    assert job["logs"][0]["message"] == "Whisper started"
    assert log_entry == job["logs"][0]
    assert store.find_active_by_source("local", "/tmp/input.mp4")["id"] == "abc"


def test_job_store_cancel_flag_is_durable(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create_job(
        job_id="cancel-me",
        source_kind="youtube",
        source_value="https://youtube.com/watch?v=test",
        reference_path=None,
        settings={},
        work_dir=str(tmp_path / "cancel-me"),
    )
    store.update_job("cancel-me", cancel_requested=1)
    assert store.is_cancel_requested("cancel-me") is True


def test_job_store_makes_interrupted_jobs_retryable_without_losing_history(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    for job_id, status in (("queued-job", "queued"), ("running-job", "running")):
        store.create_job(
            job_id=job_id,
            source_kind="local",
            source_value=f"/tmp/{job_id}.mp4",
            reference_path=None,
            settings={},
            work_dir=str(tmp_path / job_id),
        )
        store.update_job(job_id, status=status, cancel_requested=1)
    store.create_job(
        job_id="done-job",
        source_kind="local",
        source_value="/tmp/done.mp4",
        reference_path=None,
        settings={},
        work_dir=str(tmp_path / "done-job"),
    )
    store.update_job("done-job", status="completed")

    assert store.mark_interrupted_jobs() == ["queued-job", "running-job"]
    for job_id in ("queued-job", "running-job"):
        job = store.get_job(job_id)
        assert job["status"] == "failed"
        assert job["cancel_requested"] is False
        assert "缓存已保留" in job["error"]
        assert job["logs"][-1]["level"] == "WARNING"
    assert store.get_job("done-job")["status"] == "completed"
    assert store.mark_interrupted_jobs() == []


def test_job_store_can_update_retry_settings(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create_job(
        job_id="retry-model",
        source_kind="youtube",
        source_value="https://youtu.be/test",
        reference_path=None,
        settings={"translation_provider": "ollama", "translation_model": "missing"},
        work_dir=str(tmp_path / "retry-model"),
    )

    updated = store.update_settings(
        "retry-model",
        {"translation_provider": "ollama", "translation_model": "installed:latest"},
    )

    assert updated["settings"]["translation_model"] == "installed:latest"


def test_awaiting_reference_job_still_blocks_duplicate_source(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create_job(
        job_id="waiting",
        source_kind="youtube",
        source_value="https://youtu.be/reference",
        reference_path=None,
        settings={},
        work_dir=str(tmp_path / "waiting"),
    )
    store.update_job("waiting", status="awaiting_reference")

    duplicate = store.find_active_by_source("youtube", "https://youtu.be/reference")
    assert duplicate is not None
    assert duplicate["id"] == "waiting"
