from __future__ import annotations

import abc
import logging
import math
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from backend.config import Settings
from backend.models.domain import Segment
from backend.pipeline.audio import (
    build_media_cache_metadata,
    commit_media_output,
    file_content_fingerprint,
    media_cache_is_valid,
)
from backend.utils.files import atomic_copy, temporary_output_path
from backend.utils.process import require_executable, run_process


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TimelineChunkPlan:
    """A bounded intermediate mix window for a consecutive group of clips."""

    items: tuple[Segment, ...]
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start


class AudioSeparator(abc.ABC):
    @abc.abstractmethod
    async def separate(self, original_audio: Path, output_dir: Path) -> Path:
        """Return a background/music track suitable for overlaying a dub."""


class FastAudioSeparator(AudioSeparator):
    async def separate(self, original_audio: Path, output_dir: Path) -> Path:
        logger.info("[SEPARATE] Fast 模式：保留原音轨并在混音时降低音量")
        return original_audio


class DemucsAudioSeparator(AudioSeparator):
    def __init__(self, config: Settings, model: str = "htdemucs"):
        self.config = config
        self.model = model

    async def separate(self, original_audio: Path, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        cached = output_dir / "background.wav"
        cache_metadata = build_media_cache_metadata(
            "demucs-background",
            inputs={"original_audio": file_content_fingerprint(original_audio)},
            parameters={
                "model": self.model,
                "two_stems": "vocals",
                "selected_stem": "no_vocals.wav",
            },
        )
        if await media_cache_is_valid(
            cached,
            self.config,
            required_stream_types={"audio"},
            expected_metadata=cache_metadata,
        ):
            return cached
        binary = require_executable("demucs", "Demucs（可选 Advanced 模式依赖）")
        demucs_root = output_dir / "demucs"
        await run_process(
            [
                binary,
                "--two-stems",
                "vocals",
                "-n",
                self.model,
                "-o",
                str(demucs_root),
                str(original_audio),
            ]
        )
        candidates = list(demucs_root.glob(f"{self.model}/**/no_vocals.wav"))
        if len(candidates) != 1:
            raise RuntimeError("Demucs 已结束，但没有找到 no_vocals.wav")
        temporary = temporary_output_path(cached)
        try:
            atomic_copy(candidates[0], temporary)
            await commit_media_output(
                temporary,
                cached,
                self.config,
                required_stream_types={"audio"},
                cache_metadata=cache_metadata,
            )
        finally:
            temporary.unlink(missing_ok=True)
        return cached


def build_separator(mode: str, config: Settings) -> AudioSeparator:
    if mode.lower() == "fast":
        return FastAudioSeparator()
    if mode.lower() == "demucs":
        return DemucsAudioSeparator(config)
    raise ValueError(f"未知人声分离模式：{mode}")


def _segment_rendered_duration(segment: Segment) -> float:
    duration = segment.tts_duration
    if duration is None or not math.isfinite(duration) or duration <= 0:
        duration = segment.duration
    return float(duration)


def _plan_timeline_chunks(
    items: Sequence[Segment],
    total_duration: float,
    *,
    chunk_size: int,
) -> list[TimelineChunkPlan]:
    """Group clips into bounded windows instead of one full-length WAV per group."""

    if not math.isfinite(total_duration) or total_duration <= 0:
        raise ValueError("total_duration 必须是正数")
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须是正整数")

    ordered = sorted(items, key=lambda segment: (segment.start, segment.id))
    plans: list[TimelineChunkPlan] = []
    for offset in range(0, len(ordered), chunk_size):
        chunk_items = tuple(
            segment
            for segment in ordered[offset : offset + chunk_size]
            if segment.start < total_duration
        )
        if not chunk_items:
            continue
        start = max(0.0, min(segment.start for segment in chunk_items))
        end = min(
            total_duration,
            max(
                segment.start + _segment_rendered_duration(segment)
                for segment in chunk_items
            ),
        )
        if end <= start:
            continue
        plans.append(TimelineChunkPlan(items=chunk_items, start=start, end=end))
    return plans


def _build_timeline_mix_args(
    items: Sequence[Segment],
    output: Path,
    duration: float,
    binary: str,
    *,
    timeline_origin: float = 0.0,
) -> list[str]:
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration 必须是正数")
    if not math.isfinite(timeline_origin) or timeline_origin < 0:
        raise ValueError("timeline_origin 必须是非负数")

    args = [binary, "-y"]
    filters: list[str] = []
    labels: list[str] = []
    for segment in items:
        if not segment.tts_file:
            continue
        input_index = len(labels)
        args.extend(["-i", str(segment.tts_file)])
        delay_ms = max(0, round((segment.start - timeline_origin) * 1000))
        label = f"voice{input_index}"
        filters.append(
            f"[{input_index}:a]aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"adelay={delay_ms}:all=1[{label}]"
        )
        labels.append(f"[{label}]")
    if not labels:
        raise RuntimeError("没有可用于构建配音时间轴的 TTS 文件")
    filters.append(
        "".join(labels)
        + f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
        + f"atrim=0:{duration:.3f},apad=whole_dur={duration:.3f}[out]"
    )
    args.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[out]",
            "-t",
            f"{duration:.3f}",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(output),
        ]
    )
    return args


async def _mix_timeline_chunk(
    items: Sequence[Segment],
    output: Path,
    duration: float,
    config: Settings,
    *,
    timeline_origin: float = 0.0,
) -> None:
    binary = require_executable(config.ffmpeg_bin, "ffmpeg")
    args = _build_timeline_mix_args(
        items,
        output,
        duration,
        binary,
        timeline_origin=timeline_origin,
    )
    await run_process(args)


async def build_dubbed_timeline(
    segments: Sequence[Segment],
    total_duration: float,
    output: Path,
    config: Settings,
    *,
    chunk_size: int = 60,
) -> Path:
    """Place each clip at its original absolute start; pauses are preserved."""

    usable = [segment for segment in segments if segment.tts_file and Path(segment.tts_file).exists()]
    if not usable:
        raise RuntimeError("没有成功生成的 TTS 片段，无法构建配音音轨")
    chunk_plans = _plan_timeline_chunks(
        usable,
        total_duration,
        chunk_size=chunk_size,
    )
    if not chunk_plans:
        raise RuntimeError("TTS 片段都位于视频时间范围之外，无法构建配音音轨")
    ordered_usable = sorted(usable, key=lambda segment: (segment.start, segment.id))
    cache_metadata = build_media_cache_metadata(
        "dubbed-voice-timeline",
        inputs={
            "segments": [
                {
                    "id": segment.id,
                    "start": segment.start,
                    "rendered_duration": _segment_rendered_duration(segment),
                    "tts_audio": file_content_fingerprint(Path(segment.tts_file or "")),
                }
                for segment in ordered_usable
            ]
        },
        parameters={
            "total_duration": total_duration,
            "chunk_size": chunk_size,
            "sample_rate": 48_000,
            "channels": 2,
            "codec": "pcm_s16le",
            "placement": "absolute-start-ms",
            "mix_normalize": False,
        },
    )
    if await media_cache_is_valid(
        output,
        config,
        required_stream_types={"audio"},
        expected_metadata=cache_metadata,
    ):
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    logger.info("[ALIGN] building timeline clips=%d duration=%.2f", len(usable), total_duration)
    logger.info(
        "[ALIGN] timeline chunks=%d intermediate_duration=%.2fs",
        len(chunk_plans),
        sum(plan.duration for plan in chunk_plans),
    )
    pending = temporary_output_path(output)
    try:
        with tempfile.TemporaryDirectory(prefix="timeline-", dir=str(output.parent)) as temp_name:
            temporary = Path(temp_name)
            rendered_chunks: list[tuple[TimelineChunkPlan, Path]] = []
            for index, plan in enumerate(chunk_plans):
                chunk = temporary / f"chunk_{index:04d}.wav"
                await _mix_timeline_chunk(
                    plan.items,
                    chunk,
                    plan.duration,
                    config,
                    timeline_origin=plan.start,
                )
                rendered_chunks.append((plan, chunk))

            # The bounded chunk WAVs start at zero. Place each one back at its
            # absolute source time while producing the single full-length output.
            placeholders = [
                Segment(
                    id=index,
                    start=plan.start,
                    end=plan.end,
                    text="chunk",
                    tts_file=str(path),
                    tts_duration=plan.duration,
                )
                for index, (plan, path) in enumerate(rendered_chunks)
            ]
            await _mix_timeline_chunk(placeholders, pending, total_duration, config)
        await commit_media_output(
            pending,
            output,
            config,
            required_stream_types={"audio"},
            cache_metadata=cache_metadata,
        )
    finally:
        pending.unlink(missing_ok=True)
    return output


async def mix_audio(
    background: Path | None,
    dubbed_voice: Path | None,
    output: Path,
    duration: float,
    config: Settings,
    *,
    fast_mode: bool,
) -> Path:
    if background is None and dubbed_voice is None:
        raise RuntimeError("没有可混合的音轨")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration 必须是正数")
    effective_background_volume = (
        config.original_audio_volume if fast_mode else 1.0
    )
    cache_metadata = build_media_cache_metadata(
        "mixed-audio",
        inputs={
            "background": (
                file_content_fingerprint(background) if background is not None else None
            ),
            "dubbed_voice": (
                file_content_fingerprint(dubbed_voice) if dubbed_voice is not None else None
            ),
        },
        parameters={
            "duration": duration,
            "fast_mode": fast_mode,
            "configured_original_audio_volume": config.original_audio_volume,
            "effective_background_volume": effective_background_volume,
            "sample_rate": 48_000,
            "channels": 2,
            "codec": "pcm_s16le",
            "mix_normalize": False,
            "limiter": 0.95,
        },
    )
    if await media_cache_is_valid(
        output,
        config,
        required_stream_types={"audio"},
        expected_metadata=cache_metadata,
    ):
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    binary = require_executable(config.ffmpeg_bin, "ffmpeg")
    pending = temporary_output_path(output)
    try:
        if dubbed_voice is None:
            await run_process(
                [binary, "-y", "-i", str(background), "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(pending)]
            )
            await commit_media_output(
                pending,
                output,
                config,
                required_stream_types={"audio"},
                cache_metadata=cache_metadata,
            )
            return output
        if background is None:
            await run_process(
                [binary, "-y", "-i", str(dubbed_voice), "-t", f"{duration:.3f}", "-c:a", "pcm_s16le", str(pending)]
            )
            await commit_media_output(
                pending,
                output,
                config,
                required_stream_types={"audio"},
                cache_metadata=cache_metadata,
            )
            return output

        background_volume = effective_background_volume
        filter_graph = (
            f"[0:a]volume={background_volume:.4f},aresample=48000[bg];"
            "[1:a]aresample=48000[dub];"
            f"[bg][dub]amix=inputs=2:duration=longest:normalize=0,"
            f"alimiter=limit=0.95,atrim=0:{duration:.3f}[mix]"
        )
        logger.info("[MIX] background_volume=%.4f", background_volume)
        await run_process(
            [
                binary,
                "-y",
                "-i",
                str(background),
                "-i",
                str(dubbed_voice),
                "-filter_complex",
                filter_graph,
                "-map",
                "[mix]",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(pending),
            ]
        )
        await commit_media_output(
            pending,
            output,
            config,
            required_stream_types={"audio"},
            cache_metadata=cache_metadata,
        )
    finally:
        pending.unlink(missing_ok=True)
    return output
