from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
from typing import Iterable

from backend.config import Settings
from backend.models.domain import Segment, Transcript
from backend.utils.files import temporary_output_path
from backend.utils.process import require_executable, run_process


logger = logging.getLogger(__name__)


async def probe_media(path: Path, config: Settings) -> dict:
    binary = require_executable(config.ffprobe_bin, "ffprobe")
    result = await run_process(
        [
            binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,duration",
            "-of",
            "json",
            str(path),
        ],
        timeout=60,
    )
    try:
        return json.loads(result.output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe 返回了无效 JSON：{path}") from exc


async def media_duration(path: Path, config: Settings) -> float:
    data = await probe_media(path, config)
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"无法读取媒体时长：{path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"媒体时长无效：{duration}")
    return duration


async def validate_media_file(
    path: Path,
    config: Settings,
    *,
    required_stream_types: Iterable[str] = (),
) -> dict:
    """Raise when a completed media artifact is empty, unparseable, or incomplete."""

    if not path.is_file() or path.stat().st_size <= 0:
        raise RuntimeError(f"媒体文件不存在或为空：{path}")
    data = await probe_media(path, config)
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"无法读取媒体时长：{path}") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"媒体时长无效：{path}")
    stream_types = {
        str(stream.get("codec_type"))
        for stream in data.get("streams", [])
        if isinstance(stream, dict)
    }
    missing = set(required_stream_types) - stream_types
    if missing:
        raise RuntimeError(f"媒体文件缺少轨道 {sorted(missing)}：{path}")
    return data


async def media_cache_is_valid(
    path: Path,
    config: Settings,
    *,
    required_stream_types: Iterable[str] = (),
) -> bool:
    if not path.exists():
        return False
    try:
        await validate_media_file(
            path,
            config,
            required_stream_types=required_stream_types,
        )
    except Exception as exc:
        logger.warning("忽略无效媒体缓存 %s：%s", path, exc)
        return False
    return True


async def commit_media_output(
    temporary: Path,
    destination: Path,
    config: Settings,
    *,
    required_stream_types: Iterable[str] = (),
) -> Path:
    """Validate a temporary media file, then atomically publish it."""

    await validate_media_file(
        temporary,
        config,
        required_stream_types=required_stream_types,
    )
    os.replace(temporary, destination)
    return destination


async def extract_audio_tracks(video: Path, audio_dir: Path, config: Settings) -> tuple[Path, Path]:
    """Create a Whisper-friendly mono track and a mixing-quality stereo track."""

    audio_dir.mkdir(parents=True, exist_ok=True)
    speech = audio_dir / "speech.wav"
    original = audio_dir / "original.wav"
    binary = require_executable(config.ffmpeg_bin, "ffmpeg")
    if not await media_cache_is_valid(speech, config, required_stream_types={"audio"}):
        logger.info("[AUDIO] extracting 16 kHz speech track")
        temporary = temporary_output_path(speech)
        try:
            await run_process(
                [
                    binary,
                    "-y",
                    "-i",
                    str(video),
                    "-vn",
                    "-map",
                    "0:a:0",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    str(temporary),
                ]
            )
            await commit_media_output(
                temporary,
                speech,
                config,
                required_stream_types={"audio"},
            )
        finally:
            temporary.unlink(missing_ok=True)
    if not await media_cache_is_valid(original, config, required_stream_types={"audio"}):
        logger.info("[AUDIO] extracting 48 kHz mix track")
        temporary = temporary_output_path(original)
        try:
            await run_process(
                [
                    binary,
                    "-y",
                    "-i",
                    str(video),
                    "-vn",
                    "-map",
                    "0:a:0",
                    "-ac",
                    "2",
                    "-ar",
                    "48000",
                    "-c:a",
                    "pcm_s16le",
                    str(temporary),
                ]
            )
            await commit_media_output(
                temporary,
                original,
                config,
                required_stream_types={"audio"},
            )
        finally:
            temporary.unlink(missing_ok=True)
    return speech, original


def select_reference_segment(
    transcript: Transcript,
    *,
    minimum: float = 5.0,
    maximum: float = 12.0,
) -> Segment:
    """Pick a clear, sentence-like reference using the metadata Whisper provides."""

    if not transcript.segments:
        raise RuntimeError("转写结果为空，无法自动选择声音参考")

    def score(segment: Segment) -> tuple[float, float, float]:
        duration = segment.duration
        in_window = 1.0 if minimum <= duration <= maximum else 0.0
        duration_score = max(0.0, 1.0 - abs(duration - 8.0) / 8.0)
        confidence = segment.confidence if segment.confidence is not None else 0.5
        punctuation = 0.15 if segment.text.rstrip().endswith((".", "?", "!")) else 0.0
        word_density = min(len(segment.text.split()) / max(duration, 0.1) / 3.0, 1.0)
        return (in_window, confidence + punctuation + duration_score * 0.5 + word_density * 0.15, duration)

    candidate = max(transcript.segments, key=score)
    if candidate.duration < 2.5:
        raise RuntimeError("自动找到的参考语音太短（少于 2.5 秒），请手动上传 reference.wav")
    return candidate


async def cut_reference_audio(
    source_audio: Path,
    segment: Segment,
    output: Path,
    config: Settings,
) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if await media_cache_is_valid(output, config, required_stream_types={"audio"}):
        return output
    binary = require_executable(config.ffmpeg_bin, "ffmpeg")
    padding = min(0.12, segment.start)
    start = max(0.0, segment.start - padding)
    duration = segment.end - start + 0.08
    temporary = temporary_output_path(output)
    try:
        await run_process(
            [
                binary,
                "-y",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{duration:.3f}",
                "-i",
                str(source_audio),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(temporary),
            ]
        )
        await commit_media_output(
            temporary,
            output,
            config,
            required_stream_types={"audio"},
        )
    finally:
        temporary.unlink(missing_ok=True)
    return output
