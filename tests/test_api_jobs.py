import json
from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import routes
from backend.models.domain import JobSettings, Segment, Transcript
from backend.services.job_store import JobStore


class RecordingManager:
    def __init__(self, store: JobStore):
        self.store = store
        self.start_calls: list[str] = []
        self.retry_calls: list[str] = []
        self.retry_settings_seen: list[dict] = []
        self.resume_reference_calls: list[str] = []
        self.reference_settings_seen: list[dict] = []

    def start(self, job_id: str) -> None:
        self.start_calls.append(job_id)

    def retry(self, job_id: str) -> None:
        # Capture the persisted settings at the exact point retry is invoked.
        # This proves the route updates SQLite before handing work to the manager.
        self.retry_settings_seen.append(self.store.get_job(job_id)["settings"])
        self.retry_calls.append(job_id)

    async def resume_reference(self, job_id: str) -> None:
        self.reference_settings_seen.append(self.store.get_job(job_id)["settings"])
        self.resume_reference_calls.append(job_id)
        self.store.update_job(job_id, status="queued", current_step="等待处理")


def _client(tmp_path, monkeypatch) -> tuple[TestClient, JobStore, RecordingManager]:
    store = JobStore(tmp_path / "jobs.sqlite3")
    manager = RecordingManager(store)
    app = FastAPI()
    app.state.job_store = store
    app.state.job_manager = manager
    app.include_router(routes.router)
    monkeypatch.setattr(
        routes,
        "settings",
        replace(routes.settings, work_dir=tmp_path / "work", ollama_model="default:latest"),
    )
    return TestClient(app), store, manager


def _create_awaiting_reference_job(store: JobStore, tmp_path, job_id: str = "choose-ref"):
    work_dir = tmp_path / "work" / job_id
    options = JobSettings(
        translation_provider="openai",
        translation_model="local-test-model",
        dubbing_enabled=True,
        reference_mode="segment",
        reference_segment_id=None,
    )
    store.create_job(
        job_id=job_id,
        source_kind="local",
        source_value="/tmp/input.mp4",
        reference_path=None,
        settings=options.model_dump(),
        work_dir=str(work_dir),
    )
    store.update_job(
        job_id,
        status="awaiting_reference",
        current_step="等待选择参考声音片段",
        step_index=4,
    )
    transcript = Transcript(
        language="en",
        duration=9.0,
        segments=[
            Segment(id=3, start=1.0, end=6.5, text="A clear complete sentence."),
            Segment(id=8, start=6.7, end=9.0, text="A shorter sentence."),
        ],
    )
    asr_dir = work_dir / "asr"
    asr_dir.mkdir(parents=True)
    (asr_dir / "segmented_transcript.json").write_text(
        json.dumps(transcript.model_dump()),
        encoding="utf-8",
    )
    return options


def test_create_youtube_job_rejects_uninstalled_ollama_model_without_starting(
    tmp_path, monkeypatch
):
    client, store, manager = _client(tmp_path, monkeypatch)

    async def unavailable_model():
        return {"ok": True, "models": ["gemma4:26b"]}

    monkeypatch.setattr(routes, "_ollama_info", unavailable_model)

    response = client.post(
        "/api/jobs",
        data={
            "youtube_url": "https://www.youtube.com/watch?v=T_OqU3ONq3w",
            "translation_provider": "ollama",
            "translation_model": "missing:latest",
        },
    )

    assert response.status_code == 422
    assert "missing:latest" in response.json()["detail"]
    assert "gemma4:26b" in response.json()["detail"]
    assert store.list_jobs() == []
    assert manager.start_calls == []


def test_create_youtube_job_persists_normalized_available_ollama_model(
    tmp_path, monkeypatch
):
    client, store, manager = _client(tmp_path, monkeypatch)

    async def available_model():
        return {"ok": True, "models": ["gemma4:26b"]}

    monkeypatch.setattr(routes, "_ollama_info", available_model)

    response = client.post(
        "/api/jobs",
        data={
            "youtube_url": "https://www.youtube.com/watch?v=T_OqU3ONq3w",
            "translation_provider": "ollama",
            "translation_model": "  gemma4:26b  ",
        },
    )

    assert response.status_code == 200
    job = response.json()
    assert job["settings"]["translation_model"] == "gemma4:26b"
    assert store.get_job(job["id"])["settings"]["translation_model"] == "gemma4:26b"
    assert manager.start_calls == [job["id"]]


def test_retry_updates_translation_settings_before_calling_manager(tmp_path, monkeypatch):
    client, store, manager = _client(tmp_path, monkeypatch)
    initial_settings = JobSettings(
        translation_provider="openai",
        translation_model="old-model",
    )
    store.create_job(
        job_id="retry-model",
        source_kind="youtube",
        source_value="https://www.youtube.com/watch?v=T_OqU3ONq3w",
        reference_path=None,
        settings=initial_settings.model_dump(),
        work_dir=str(tmp_path / "work" / "retry-model"),
    )
    store.update_job("retry-model", status="failed", error="model missing")

    async def available_model():
        return {"ok": True, "models": ["gemma4:26b"]}

    monkeypatch.setattr(routes, "_ollama_info", available_model)

    response = client.post(
        "/api/jobs/retry-model/retry",
        data={
            "translation_provider": " OLLAMA ",
            "translation_model": "  gemma4:26b  ",
        },
    )

    assert response.status_code == 200
    assert manager.retry_calls == ["retry-model"]
    assert manager.retry_settings_seen == [
        {
            **initial_settings.model_dump(),
            "translation_provider": "ollama",
            "translation_model": "gemma4:26b",
        }
    ]
    persisted = store.get_job("retry-model")["settings"]
    assert persisted["translation_provider"] == "ollama"
    assert persisted["translation_model"] == "gemma4:26b"
    assert response.json()["settings"] == persisted


def test_retry_rejects_invalid_provider_without_mutating_job(tmp_path, monkeypatch):
    client, store, manager = _client(tmp_path, monkeypatch)
    initial_settings = JobSettings(translation_model="gemma4:26b")
    store.create_job(
        job_id="invalid-provider",
        source_kind="youtube",
        source_value="https://www.youtube.com/watch?v=T_OqU3ONq3w",
        reference_path=None,
        settings=initial_settings.model_dump(),
        work_dir=str(tmp_path / "work" / "invalid-provider"),
    )
    store.update_job("invalid-provider", status="failed", error="old failure")

    response = client.post(
        "/api/jobs/invalid-provider/retry",
        data={"translation_provider": "not-a-provider"},
    )

    assert response.status_code == 422
    assert manager.retry_calls == []
    assert store.get_job("invalid-provider")["settings"] == initial_settings.model_dump()


def test_retry_rejects_openai_without_any_model(tmp_path, monkeypatch):
    client, store, manager = _client(tmp_path, monkeypatch)
    monkeypatch.setattr(routes, "settings", replace(routes.settings, openai_model=""))
    initial_settings = JobSettings(translation_model="")
    store.create_job(
        job_id="openai-no-model",
        source_kind="youtube",
        source_value="https://www.youtube.com/watch?v=T_OqU3ONq3w",
        reference_path=None,
        settings=initial_settings.model_dump(),
        work_dir=str(tmp_path / "work" / "openai-no-model"),
    )
    store.update_job("openai-no-model", status="failed", error="old failure")

    response = client.post(
        "/api/jobs/openai-no-model/retry",
        data={"translation_provider": "openai"},
    )

    assert response.status_code == 422
    assert "翻译模型" in response.json()["detail"]
    assert manager.retry_calls == []


def test_reference_segments_endpoint_returns_normalized_choices(tmp_path, monkeypatch):
    client, store, _ = _client(tmp_path, monkeypatch)
    _create_awaiting_reference_job(store, tmp_path)

    response = client.get("/api/jobs/choose-ref/segments")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "awaiting_reference"
    assert payload["language"] == "en"
    assert payload["segments"] == [
        {
            "id": 3,
            "start": 1.0,
            "end": 6.5,
            "duration": 5.5,
            "text": "A clear complete sentence.",
            "confidence": None,
        },
        {
            "id": 8,
            "start": 6.7,
            "end": 9.0,
            "duration": 2.3,
            "text": "A shorter sentence.",
            "confidence": None,
        },
    ]


def test_reference_selection_validates_id_then_persists_and_resumes(tmp_path, monkeypatch):
    client, store, manager = _client(tmp_path, monkeypatch)
    _create_awaiting_reference_job(store, tmp_path)

    missing = client.post("/api/jobs/choose-ref/reference", json={"segment_id": 99})
    assert missing.status_code == 422
    assert manager.resume_reference_calls == []
    assert store.get_job("choose-ref")["settings"]["reference_segment_id"] is None

    selected = client.post("/api/jobs/choose-ref/reference", data={"segment_id": "3"})
    assert selected.status_code == 200
    assert selected.json()["status"] == "queued"
    assert manager.resume_reference_calls == ["choose-ref"]
    assert manager.reference_settings_seen[0]["reference_segment_id"] == 3
    persisted = store.get_job("choose-ref")
    assert persisted["settings"]["reference_segment_id"] == 3
    assert "segment 3" in persisted["logs"][-1]["message"]

    repeated = client.post("/api/jobs/choose-ref/reference", json={"segment_id": 8})
    assert repeated.status_code == 409
    assert store.get_job("choose-ref")["settings"]["reference_segment_id"] == 3


def test_retry_cannot_replace_reference_selection_flow_or_mutate_settings(
    tmp_path, monkeypatch
):
    client, store, manager = _client(tmp_path, monkeypatch)
    initial = _create_awaiting_reference_job(store, tmp_path)

    response = client.post(
        "/api/jobs/choose-ref/retry",
        data={"translation_provider": "ollama", "translation_model": "changed:model"},
    )

    assert response.status_code == 409
    assert "/reference" in response.json()["detail"]
    assert manager.retry_calls == []
    assert store.get_job("choose-ref")["settings"] == initial.model_dump()
