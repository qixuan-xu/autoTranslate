from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse

from backend.config import settings
from backend.models.domain import JobSettings
from backend.pipeline.context import JobPaths
from backend.services.job_manager import JobManager
from backend.services.job_store import JobStore, TERMINAL_STATUSES
from backend.utils.files import is_relative_to, temporary_output_path
from backend.utils.process import require_executable


router = APIRouter(prefix="/api")


def _services(request: Request) -> tuple[JobStore, JobManager]:
    return request.app.state.job_store, request.app.state.job_manager


def _bool(value: str, default: bool) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


async def _save_upload(upload: UploadFile, destination: Path, maximum_bytes: int) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = temporary_output_path(destination)
    total = 0
    try:
        with temporary.open("xb") as handle:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > maximum_bytes:
                    raise HTTPException(status_code=413, detail="上传文件超过大小限制")
                handle.write(chunk)
        if total == 0:
            raise HTTPException(status_code=400, detail="上传文件为空")
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        await upload.close()
    return destination


@router.get("/health")
async def health() -> dict:
    tools = {}
    for name, binary in (
        ("ffmpeg", settings.ffmpeg_bin),
        ("ffprobe", settings.ffprobe_bin),
        ("whisper", settings.whisper_bin),
        ("yt-dlp", settings.ytdlp_bin),
        ("conda", "conda"),
    ):
        try:
            tools[name] = {"ok": True, "path": require_executable(binary, name)}
        except RuntimeError as exc:
            tools[name] = {"ok": False, "error": str(exc)}
    cosyvoice = {"ok": False, "url": settings.cosyvoice_url}
    try:
        async with httpx.AsyncClient(timeout=2.0, trust_env=False) as client:
            response = await client.get(f"{settings.cosyvoice_url.rstrip('/')}/health")
            cosyvoice = {**response.json(), "ok": response.is_success, "url": settings.cosyvoice_url}
    except Exception:
        cosyvoice["error"] = "服务未启动；仅字幕任务仍可运行"
    return {
        "ok": all(item["ok"] for key, item in tools.items() if key != "yt-dlp"),
        "tools": tools,
        "cosyvoice": cosyvoice,
        "work_dir": str(settings.work_dir),
    }


@router.post("/jobs")
async def create_job(
    request: Request,
    youtube_url: str = Form(""),
    video_file: Optional[UploadFile] = File(None),
    reference_audio: Optional[UploadFile] = File(None),
    source_language: str = Form("auto"),
    target_language: str = Form("zh-CN"),
    whisper_model: str = Form("turbo"),
    translation_provider: str = Form("ollama"),
    translation_model: str = Form(""),
    enable_dubbing: str = Form("true"),
    voice_mode: str = Form("auto"),
    reference_segment_id: str = Form(""),
    reference_text: str = Form(""),
    preserve_background: str = Form("true"),
    separation_mode: str = Form("fast"),
    subtitle_mode: str = Form("both"),
    subtitle_font: str = Form("PingFang SC"),
) -> dict:
    store, manager = _services(request)
    url = youtube_url.strip()
    has_upload = video_file is not None and bool(video_file.filename)
    if bool(url) == has_upload:
        raise HTTPException(status_code=400, detail="请且只请选择 YouTube URL 或本地视频文件之一")

    if url:
        active = store.find_active_by_source("youtube", url)
        if active:
            active["duplicate"] = True
            return active

    try:
        segment_id = int(reference_segment_id) if reference_segment_id.strip() else None
        job_settings = JobSettings(
            source_language=source_language,
            target_language=target_language,
            whisper_model=whisper_model or settings.whisper_model,
            translation_provider=translation_provider,
            translation_model=translation_model,
            dubbing_enabled=_bool(enable_dubbing, True),
            keep_background=_bool(preserve_background, True),
            separation_mode=separation_mode,
            subtitle_mode=subtitle_mode,
            reference_mode=voice_mode,
            reference_segment_id=segment_id,
            reference_text=reference_text.strip(),
            subtitle_font=subtitle_font.strip() or "PingFang SC",
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"设置无效：{exc}") from exc

    job_id = uuid.uuid4().hex[:16]
    paths = JobPaths.create(settings.work_dir, job_id)
    max_bytes = int(settings.max_upload_gb * 1024**3)
    reference_path: Optional[Path] = None
    if has_upload:
        assert video_file is not None
        extension = Path(video_file.filename or "video.mp4").suffix.lower()
        if extension not in {".mp4", ".mov", ".mkv", ".webm", ".m4v", ".avi"}:
            raise HTTPException(status_code=400, detail=f"不支持的视频扩展名：{extension or '无'}")
        source_path = await _save_upload(video_file, paths.source / f"uploaded{extension}", max_bytes)
        source_kind, source_value = "local", str(source_path)
    else:
        source_kind, source_value = "youtube", url

    if reference_audio is not None and reference_audio.filename:
        extension = Path(reference_audio.filename).suffix.lower()
        if extension not in {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac"}:
            raise HTTPException(status_code=400, detail=f"不支持的参考音频扩展名：{extension or '无'}")
        reference_path = await _save_upload(
            reference_audio,
            paths.source / f"uploaded_reference{extension}",
            min(max_bytes, 500 * 1024**2),
        )
        job_settings.reference_mode = "upload"

    store.create_job(
        job_id=job_id,
        source_kind=source_kind,
        source_value=source_value,
        reference_path=str(reference_path) if reference_path else None,
        settings=job_settings.model_dump(),
        work_dir=str(paths.root),
    )
    store.add_log(job_id, "任务已创建")
    manager.start(job_id)
    return store.get_job(job_id)


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = 30) -> dict:
    store, _ = _services(request)
    return {"jobs": store.list_jobs(limit=max(1, min(limit, 100)))}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, request: Request) -> dict:
    store, _ = _services(request)
    try:
        return store.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request) -> dict:
    _, manager = _services(request)
    try:
        return await manager.cancel(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str, request: Request) -> dict:
    store, manager = _services(request)
    try:
        manager.retry(job_id)
        return store.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    store, manager = _services(request)
    try:
        queue = manager.subscribe(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc

    async def stream():
        try:
            initial = store.get_job(job_id)
            yield f"event: update\ndata: {json.dumps(initial, ensure_ascii=False)}\n\n"
            if initial["status"] in TERMINAL_STATUSES:
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    job = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: update\ndata: {json.dumps(job, ensure_ascii=False)}\n\n"
                    if job["status"] in TERMINAL_STATUSES:
                        return
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            manager.unsubscribe(job_id, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/jobs/{job_id}/files/{filename}")
async def get_artifact(job_id: str, filename: str, request: Request) -> FileResponse:
    store, _ = _services(request)
    try:
        job = store.get_job(job_id, include_logs=False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    output_dir = Path(job["work_dir"]) / "output"
    candidate = (output_dir / filename).resolve()
    if not is_relative_to(candidate, output_dir) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="产物不存在")
    media_type = "video/mp4" if candidate.suffix == ".mp4" else None
    return FileResponse(candidate, media_type=media_type, filename=candidate.name)
