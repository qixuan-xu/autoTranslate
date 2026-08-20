from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.api.routes import job_events
from backend.config import Settings
from backend.pipeline import runner as runner_module
from backend.pipeline.runner import PipelineAwaitingReference
from backend.services.job_manager import JobManager
from backend.services.job_store import JobStore


def _manager(tmp_path) -> tuple[JobStore, JobManager, str]:
    job_id = "event-load-test"
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.create_job(
        job_id=job_id,
        source_kind="local",
        source_value="/tmp/input.mp4",
        reference_path=None,
        settings={},
        work_dir=str(tmp_path / job_id),
    )
    return store, JobManager(store, Settings(work_dir=tmp_path)), job_id


@pytest.mark.asyncio
async def test_thousand_progress_events_never_include_historical_logs(tmp_path) -> None:
    store, manager, job_id = _manager(tmp_path)
    for index in range(400):
        store.add_log(job_id, f"historical-{index}")

    queue = manager.subscribe(job_id)
    for index in range(1000):
        updated = await manager.update_job(
            job_id,
            progress=index / 10,
            segments_done=index,
            segments_total=1000,
        )
        event = queue.get_nowait()

        assert "logs" not in updated
        assert event["type"] == "update"
        assert "logs" not in event
        assert "logs" not in event["data"]
        assert event["data"]["segments_done"] == index

    # Full history remains available to GET/initial SSE snapshots.
    assert len(store.get_job(job_id)["logs"]) == 400


@pytest.mark.asyncio
async def test_log_events_are_single_non_repeated_increments(tmp_path) -> None:
    store, manager, job_id = _manager(tmp_path)
    queue = manager.subscribe(job_id)

    await manager.add_log(job_id, "first")
    await manager.add_log(job_id, "second", "WARNING")

    events = [queue.get_nowait(), queue.get_nowait()]
    assert [event["type"] for event in events] == ["log", "log"]
    assert [event["data"]["log"]["message"] for event in events] == ["first", "second"]
    assert [event["data"]["log"]["level"] for event in events] == ["INFO", "WARNING"]
    assert len({event["data"]["log"]["id"] for event in events}) == 2
    assert all("logs" not in event["data"] for event in events)
    assert [entry["message"] for entry in store.get_job(job_id)["logs"]] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_sse_initial_has_history_then_sends_one_log_increment(tmp_path) -> None:
    store, manager, job_id = _manager(tmp_path)
    historical = store.add_log(job_id, "already persisted")

    class RequestStub:
        app = SimpleNamespace(
            state=SimpleNamespace(job_store=store, job_manager=manager),
        )

        @staticmethod
        async def is_disconnected() -> bool:
            return False

    response = await job_events(job_id, RequestStub())
    stream = response.body_iterator
    try:
        initial_chunk = await stream.__anext__()
        initial_payload = json.loads(initial_chunk.split("data: ", 1)[1])
        assert initial_payload["logs"] == [historical]

        await manager.add_log(job_id, "new increment")
        log_chunk = await stream.__anext__()
        assert log_chunk.startswith("event: log\n")
        log_payload = json.loads(log_chunk.split("data: ", 1)[1])
        assert log_payload["log"]["message"] == "new increment"
        assert log_payload["log"]["id"] != historical["id"]
        assert "logs" not in log_payload
        assert "already persisted" not in log_chunk
    finally:
        await stream.aclose()

    assert job_id not in manager.listeners


@pytest.mark.asyncio
async def test_pipeline_pause_becomes_awaiting_reference_instead_of_failed(
    tmp_path, monkeypatch
) -> None:
    store, manager, job_id = _manager(tmp_path)

    class PausingRunner:
        def __init__(self, _config):
            pass

        async def run(self, *_args, **_kwargs):
            raise PipelineAwaitingReference(7)

    monkeypatch.setattr(runner_module, "PipelineRunner", PausingRunner)
    await manager._run(job_id)

    job = store.get_job(job_id)
    assert job["status"] == "awaiting_reference"
    assert job["error"] is None
    assert job["step_index"] == 4
    assert job["segments_total"] == 7
    assert "请选择参考声音片段" in job["logs"][-1]["message"]


@pytest.mark.asyncio
async def test_sse_ends_current_run_on_awaiting_reference(tmp_path) -> None:
    store, manager, job_id = _manager(tmp_path)
    store.update_job(job_id, status="awaiting_reference")

    class RequestStub:
        app = SimpleNamespace(
            state=SimpleNamespace(job_store=store, job_manager=manager),
        )

        @staticmethod
        async def is_disconnected() -> bool:
            return False

    response = await job_events(job_id, RequestStub())
    stream = response.body_iterator
    initial_chunk = await stream.__anext__()
    assert json.loads(initial_chunk.split("data: ", 1)[1])["status"] == "awaiting_reference"
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
    assert job_id not in manager.listeners
