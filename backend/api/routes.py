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
from backend.models.domain import JobSettings, ReferenceSelection, Transcript
from backend.pipeline.context import JobPaths
from backend.services.job_manager import JobManager
from backend.services.job_store import JobStore, STREAM_END_STATUSES
from backend.utils.files import is_relative_to, read_json, temporary_output_path
from backend.utils.process import require_executable


router = APIRouter(prefix="/api")


def _services(request: Request) -> tuple[JobStore, JobManager]:
    return request.app.state.job_store, request.app.state.job_manager


def _segmented_transcript(job: dict) -> Transcript:
    path = Path(job["work_dir"]) / "asr" / "segmented_transcript.json"
    if not path.is_file():
        raise HTTPException(
            status_code=409,
            detail="参考片段尚未就绪，请先等待 ASR 和字幕整理完成",
        )
    try:
        payload = read_json(path)
        # Current segmented caches wrap the transcript with input/version
        # fingerprints; accept the earlier raw-transcript layout as well.
        transcript_payload = payload.get("transcript", payload)
        return Transcript.model_validate(transcript_payload)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="已整理的字幕缓存损坏，请重试任务",
        ) from exc


def _bool(value: str, default: bool) -> bool:
    normalized = (value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


async def _ollama_info() -> dict:
    url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
    try:
        async with httpx.AsyncClient(timeout=3.0, trust_env=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            payload = response.json()
    except Exception as exc:
        return {
            "ok": False,
            "url": settings.ollama_base_url,
            "configured_model": settings.ollama_model,
            "models": [],
            "error": f"Ollama 未响应：{exc}",
        }
    models = sorted(
        {
            str(item.get("name") or item.get("model") or "").strip()
            for item in payload.get("models", [])
            if isinstance(item, dict) and (item.get("name") or item.get("model"))
        }
    )
    return {
        "ok": True,
        "url": settings.ollama_base_url,
        "configured_model": settings.ollama_model,
        "configured_available": settings.ollama_model in models,
        "models": models,
    }


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
    ollama = await _ollama_info()
    return {
        "ok": all(item["ok"] for key, item in tools.items() if key != "yt-dlp"),
        "tools": tools,
        "cosyvoice": cosyvoice,
        "ollama": ollama,
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

    if job_settings.translation_provider == "ollama":
        requested_model = job_settings.translation_model or settings.ollama_model
        ollama = await _ollama_info()
        if not ollama["ok"]:
            raise HTTPException(
                status_code=503,
                detail="Ollama 未启动。请先运行 ollama serve，再创建任务。",
            )
        if requested_model not in ollama["models"]:
            installed = "、".join(ollama["models"]) or "无"
            raise HTTPException(
                status_code=422,
                detail=f"Ollama 模型 {requested_model} 未安装；当前可用：{installed}",
            )
        job_settings.translation_model = requested_model
    elif not (job_settings.translation_model or settings.openai_model):
        raise HTTPException(status_code=422, detail="OpenAI-compatible 模式必须填写翻译模型")

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
async def retry_job(
    job_id: str,
    request: Request,
    translation_provider: str = Form(""),
    translation_model: str = Form(""),
) -> dict:
    store, manager = _services(request)
    try:
        job = store.get_job(job_id)
        if job["status"] == "awaiting_reference":
            raise HTTPException(
                status_code=409,
                detail="任务正在等待参考片段，请使用 /reference 接口继续",
            )
        if translation_provider.strip() or translation_model.strip():
            retry_values = dict(job["settings"])
            if translation_provider.strip():
                retry_values["translation_provider"] = translation_provider.strip().lower()
            if translation_model.strip():
                retry_values["translation_model"] = translation_model.strip()
            try:
                retry_settings = JobSettings.model_validate(retry_values)
            except Exception as exc:
                raise HTTPException(status_code=422, detail=f"设置无效：{exc}") from exc
            if retry_settings.translation_provider == "ollama":
                requested_model = retry_settings.translation_model or settings.ollama_model
                ollama = await _ollama_info()
                if not ollama["ok"]:
                    raise HTTPException(status_code=503, detail="Ollama 未启动，不能重试任务")
                if requested_model not in ollama["models"]:
                    installed = "、".join(ollama["models"]) or "无"
                    raise HTTPException(
                        status_code=422,
                        detail=f"Ollama 模型 {requested_model} 未安装；当前可用：{installed}",
                    )
                retry_settings.translation_model = requested_model
            elif not (retry_settings.translation_model or settings.openai_model):
                raise HTTPException(
                    status_code=422,
                    detail="OpenAI-compatible 模式必须填写翻译模型",
                )
            store.update_settings(job_id, retry_settings.model_dump())
        manager.retry(job_id)
        return store.get_job(job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/jobs/{job_id}/segments")
async def get_reference_segments(job_id: str, request: Request) -> dict:
    store, _ = _services(request)
    try:
        job = store.get_job(job_id, include_logs=False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    transcript = _segmented_transcript(job)
    return {
        "job_id": job_id,
        "status": job["status"],
        "language": transcript.language,
        "segments": [
            {
                "id": segment.id,
                "start": segment.start,
                "end": segment.end,
                "duration": segment.duration,
                "text": segment.text,
                "confidence": segment.confidence,
            }
            for segment in transcript.segments
        ],
    }


@router.post("/jobs/{job_id}/reference")
async def select_reference_segment(
    job_id: str,
    request: Request,
) -> dict:
    store, manager = _services(request)
    try:
        content_type = request.headers.get("content-type", "").casefold()
        if "application/json" in content_type:
            raw_selection = await request.json()
        else:
            form = await request.form()
            raw_selection = {"segment_id": form.get("segment_id")}
        selection = ReferenceSelection.model_validate(raw_selection)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"参考片段设置无效：{exc}") from exc
    try:
        job = store.get_job(job_id, include_logs=False)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    if job["status"] != "awaiting_reference":
        raise HTTPException(status_code=409, detail="任务当前不在等待参考片段")

    options = JobSettings.model_validate(job["settings"])
    if not options.dubbing_enabled or options.reference_mode != "segment":
        raise HTTPException(status_code=409, detail="任务未启用手动参考片段模式")
    transcript = _segmented_transcript(job)
    segment = next(
        (item for item in transcript.segments if item.id == selection.segment_id),
        None,
    )
    if segment is None:
        raise HTTPException(
            status_code=422,
            detail=f"找不到参考 segment ID：{selection.segment_id}",
        )

    updated_options = options.model_copy(
        update={"reference_segment_id": selection.segment_id}
    )
    try:
        store.select_reference(job_id, updated_options.model_dump())
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    store.add_log(
        job_id,
        f"已选择参考 segment {segment.id}（{segment.start:.2f}s–{segment.end:.2f}s）",
    )
    try:
        await manager.resume_reference(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return store.get_job(job_id)


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
            if initial["status"] in STREAM_END_STATUSES:
                return
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    if "type" in event and "data" in event:
                        event_type = str(event["type"])
                        payload = event["data"]
                    else:
                        # Compatibility for an in-process publisher using the
                        # pre-incremental queue payload.
                        event_type = "update"
                        payload = event
                    yield (
                        f"event: {event_type}\n"
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )
                    if (
                        event_type == "update"
                        and isinstance(payload, dict)
                        and payload.get("status") in STREAM_END_STATUSES
                    ):
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
