from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import uuid
import wave
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import httpx

from backend.config import settings
from backend.utils.files import atomic_write_json


logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int, int], Optional[Awaitable[None]]]


class TTSServiceError(RuntimeError):
    """The local CosyVoice service could not complete a request."""


class TTSSegmentError(TTSServiceError):
    def __init__(self, segment_id: int, completed: int, total: int, cause: Exception):
        super().__init__(
            f"第 {segment_id} 段中文配音失败（已完成 {completed}/{total}）：{cause}。"
            "已生成的 WAV 会保留，修复问题后可直接续跑。"
        )
        self.segment_id = segment_id
        self.completed = completed
        self.total = total
        self.cause = cause


@dataclass(frozen=True)
class SynthesisResult:
    path: Path
    duration: float
    speed: float
    cached: bool


@dataclass(frozen=True)
class SegmentTTSResult:
    segment_id: int
    path: Path
    target_duration: float
    tts_duration: float
    duration_ratio: float
    speed: float
    cached: bool


def wav_duration(path: str | Path) -> float:
    """Read the exact duration of the PCM WAV emitted by cosyvoice_server."""

    wav_path = Path(path)
    try:
        with wave.open(str(wav_path), "rb") as wav_file:
            frame_rate = wav_file.getframerate()
            frame_count = wav_file.getnframes()
    except (OSError, EOFError, wave.Error) as exc:
        raise TTSServiceError(f"无法读取 WAV 文件 {wav_path}：{exc}") from exc
    if frame_rate <= 0 or frame_count <= 0:
        raise TTSServiceError(f"WAV 文件没有有效音频：{wav_path}")
    return frame_count / frame_rate


def _cached_wav_duration(path: Path) -> float | None:
    if not path.is_file() or path.stat().st_size < 44:
        return None
    try:
        duration = wav_duration(path)
    except TTSServiceError:
        logger.warning("忽略损坏的 TTS 缓存：%s", path)
        return None
    return duration if duration > 0 else None


def _cache_metadata_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.meta.json")


def _cache_metadata(
    *,
    text: str,
    prompt_text: str,
    reference: Path,
    speed: float,
) -> dict[str, Any]:
    stat = reference.stat()
    return {
        "version": 1,
        "text": text,
        "prompt_text": prompt_text,
        "prompt_audio": str(reference),
        "prompt_audio_size": stat.st_size,
        "prompt_audio_mtime_ns": stat.st_mtime_ns,
        "speed": round(float(speed), 6),
    }


def _metadata_matches(path: Path, expected: dict[str, Any]) -> bool:
    try:
        actual = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return actual == expected


def _response_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        detail = response.text.strip()
    else:
        if isinstance(payload, Mapping):
            detail = str(payload.get("detail") or payload.get("error") or "").strip()
        else:
            detail = str(payload).strip()
    return detail[:2_000] or f"HTTP {response.status_code}"


def _segment_get(segment: Any, field: str) -> Any:
    if isinstance(segment, Mapping):
        return segment.get(field)
    return getattr(segment, field, None)


def _segment_set(segment: Any, field: str, value: Any) -> None:
    if isinstance(segment, MutableMapping):
        segment[field] = value
    else:
        setattr(segment, field, value)


def _segment_speed(segment_id: int, default: float, overrides: Mapping[int, float] | None) -> float:
    value = overrides.get(segment_id, default) if overrides else default
    value = float(value)
    if not 0.75 <= value <= 1.25:
        raise ValueError(f"segment {segment_id} 的 speed 必须在 0.75 到 1.25 之间")
    return value


class CosyVoiceClient:
    """Async client with atomic per-segment cache writes and resumable batches."""

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float | None = None,
        max_response_mb: float | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or settings.cosyvoice_url).rstrip("/")
        self.timeout_seconds = float(
            timeout_seconds or os.getenv("COSYVOICE_REQUEST_TIMEOUT", "900")
        )
        self.max_response_bytes = int(
            float(max_response_mb or os.getenv("COSYVOICE_MAX_RESPONSE_MB", "256")) * 1024 * 1024
        )
        self._client = http_client
        self._owns_client = http_client is None
        self._path_locks: dict[Path, asyncio.Lock] = {}

    async def __aenter__(self) -> "CosyVoiceClient":
        self._ensure_client()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            timeout = httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds))
            # CosyVoice is intentionally a loopback service.  Ignoring HTTP(S)_PROXY
            # prevents localhost requests from being sent to a corporate/system proxy.
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=timeout,
                trust_env=False,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._ensure_client().request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise TTSServiceError(
                f"无法连接 CosyVoice 服务 {self.base_url}。请先运行："
                "conda run -n cosyvoice python services/cosyvoice_server.py"
            ) from exc
        except httpx.TimeoutException as exc:
            raise TTSServiceError(
                f"CosyVoice 请求超过 {self.timeout_seconds:g} 秒；可通过 "
                "COSYVOICE_REQUEST_TIMEOUT 调整超时"
            ) from exc
        except httpx.HTTPError as exc:
            raise TTSServiceError(f"CosyVoice HTTP 请求失败：{exc}") from exc

    async def health(self) -> dict[str, Any]:
        response = await self._request("GET", "/health")
        if response.status_code >= 400:
            raise TTSServiceError(f"CosyVoice 健康检查失败：{_response_error(response)}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TTSServiceError("CosyVoice /health 返回了无效 JSON") from exc
        if not isinstance(payload, dict):
            raise TTSServiceError("CosyVoice /health 返回格式不正确")
        if not payload.get("loaded"):
            raise TTSServiceError(f"CosyVoice 模型未就绪：{payload.get('error') or '未知错误'}")
        return payload

    async def synthesize(
        self,
        *,
        text: str,
        prompt_audio: str | Path,
        prompt_text: str,
        output_path: str | Path,
        speed: float = 1.0,
        overwrite: bool = False,
    ) -> SynthesisResult:
        text = text.strip()
        prompt_text = prompt_text.strip()
        if not text:
            raise ValueError("TTS 文本不能为空")
        if not prompt_text:
            raise ValueError("参考音频对应文本不能为空")
        if not 0.75 <= float(speed) <= 1.25:
            raise ValueError("speed 必须在 0.75 到 1.25 之间")

        reference = Path(prompt_audio).expanduser().resolve()
        if not reference.is_file():
            raise TTSServiceError(f"参考音频不存在：{reference}")
        destination = Path(output_path).expanduser().resolve()
        if destination.suffix.lower() != ".wav":
            raise ValueError(f"TTS 输出必须使用 .wav 扩展名：{destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        metadata_path = _cache_metadata_path(destination)
        expected_metadata = _cache_metadata(
            text=text,
            prompt_text=prompt_text,
            reference=reference,
            speed=float(speed),
        )

        lock = self._path_locks.setdefault(destination, asyncio.Lock())
        async with lock:
            if not overwrite:
                cached_duration = _cached_wav_duration(destination)
                if cached_duration is not None and _metadata_matches(
                    metadata_path, expected_metadata
                ):
                    return SynthesisResult(destination, cached_duration, float(speed), True)

            response = await self._request(
                "POST",
                "/synthesize",
                json={
                    "text": text,
                    "prompt_audio": str(reference),
                    "prompt_text": prompt_text,
                    "speed": float(speed),
                },
            )
            if response.status_code >= 400:
                raise TTSServiceError(
                    f"CosyVoice 合成失败（HTTP {response.status_code}）：{_response_error(response)}"
                )
            wav_data = response.content
            if len(wav_data) > self.max_response_bytes:
                raise TTSServiceError(
                    f"CosyVoice 返回音频超过 {self.max_response_bytes / 1024 / 1024:g} MB 限制"
                )
            if (
                len(wav_data) < 44
                or wav_data[:4] not in {b"RIFF", b"RF64"}
                or wav_data[8:12] != b"WAVE"
            ):
                raise TTSServiceError("CosyVoice 返回的内容不是有效 WAV")

            temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
            try:
                temp_path.write_bytes(wav_data)
                duration = wav_duration(temp_path)
                os.replace(temp_path, destination)
                atomic_write_json(metadata_path, expected_metadata)
            finally:
                temp_path.unlink(missing_ok=True)
            logger.info(
                "[TTS] generated=%s duration=%.2fs speed=%.3f", destination.name, duration, speed
            )
            return SynthesisResult(destination, duration, float(speed), False)

    async def synthesize_segments(
        self,
        segments: Sequence[Any],
        *,
        tts_dir: str | Path,
        prompt_audio: str | Path,
        prompt_text: str,
        speed: float = 1.0,
        speed_overrides: Mapping[int, float] | None = None,
        overwrite: bool = False,
        progress: ProgressCallback | None = None,
    ) -> list[SegmentTTSResult]:
        """Generate missing segment WAVs; prior successful files make the batch resumable.

        The returned duration_ratio is generated_duration / source_slot_duration.  Timeline
        policy (small speed adjustment vs. translation compression) intentionally remains in
        the orchestrator instead of silently distorting speech here.
        """

        output_dir = Path(tts_dir).expanduser().resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        total = len(segments)
        seen_ids: set[int] = set()
        results: list[SegmentTTSResult] = []

        for index, segment in enumerate(segments):
            raw_id = _segment_get(segment, "id")
            try:
                segment_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"TTS segment id 无效：{raw_id!r}") from exc
            if segment_id < 0:
                raise ValueError(f"TTS segment id 不能为负数：{segment_id}")
            if segment_id in seen_ids:
                raise ValueError(f"TTS segments 包含重复 id：{segment_id}")
            seen_ids.add(segment_id)

            translated_text = str(_segment_get(segment, "translated_text") or "").strip()
            if not translated_text:
                raise ValueError(f"segment {segment_id} 缺少 translated_text")
            try:
                start = float(_segment_get(segment, "start"))
                end = float(_segment_get(segment, "end"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"segment {segment_id} 时间戳无效") from exc
            target_duration = end - start
            if start < 0 or target_duration <= 0:
                raise ValueError(f"segment {segment_id} 时间范围无效：{start} -> {end}")

            segment_speed = _segment_speed(segment_id, speed, speed_overrides)
            output_path = output_dir / f"{segment_id:04d}.wav"
            try:
                synthesis = await self.synthesize(
                    text=translated_text,
                    prompt_audio=prompt_audio,
                    prompt_text=prompt_text,
                    output_path=output_path,
                    speed=segment_speed,
                    overwrite=overwrite,
                )
            except (TTSServiceError, OSError, ValueError) as exc:
                raise TTSSegmentError(segment_id, index, total, exc) from exc

            _segment_set(segment, "tts_file", str(synthesis.path))
            _segment_set(segment, "tts_duration", synthesis.duration)
            result = SegmentTTSResult(
                segment_id=segment_id,
                path=synthesis.path,
                target_duration=target_duration,
                tts_duration=synthesis.duration,
                duration_ratio=synthesis.duration / target_duration,
                speed=synthesis.speed,
                cached=synthesis.cached,
            )
            results.append(result)
            completed = index + 1
            logger.info(
                "[TTS] %d/%d segment=%d target=%.2fs generated=%.2fs ratio=%.3f cached=%s",
                completed,
                total,
                segment_id,
                target_duration,
                synthesis.duration,
                result.duration_ratio,
                synthesis.cached,
            )
            if progress is not None:
                callback_result = progress(completed, total, segment_id)
                if inspect.isawaitable(callback_result):
                    await callback_result
        return results


TTSAdapter = CosyVoiceClient


async def synthesize_segments(*args: Any, **kwargs: Any) -> list[SegmentTTSResult]:
    """Convenience entry point that always closes its HTTP connection."""

    async with CosyVoiceClient() as client:
        return await client.synthesize_segments(*args, **kwargs)
