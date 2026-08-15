from __future__ import annotations

import abc
import logging
import tempfile
from pathlib import Path
from typing import Sequence

from backend.config import Settings
from backend.models.domain import Segment
from backend.pipeline.audio import commit_media_output, media_cache_is_valid
from backend.utils.files import atomic_copy, temporary_output_path
from backend.utils.process import require_executable, run_process


logger = logging.getLogger(__name__)


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
        if await media_cache_is_valid(cached, self.config, required_stream_types={"audio"}):
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


async def _mix_timeline_chunk(
    items: Sequence[Segment],
    output: Path,
    duration: float,
    config: Settings,
) -> None:
    binary = require_executable(config.ffmpeg_bin, "ffmpeg")
    args = [binary, "-y"]
    filters: list[str] = []
    labels: list[str] = []
    for index, segment in enumerate(items):
        if not segment.tts_file:
            continue
        args.extend(["-i", str(segment.tts_file)])
        delay_ms = max(0, round(segment.start * 1000))
        label = f"voice{index}"
        filters.append(
            f"[{index}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
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

    if await media_cache_is_valid(output, config, required_stream_types={"audio"}):
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    usable = [segment for segment in segments if segment.tts_file and Path(segment.tts_file).exists()]
    if not usable:
        raise RuntimeError("没有成功生成的 TTS 片段，无法构建配音音轨")
    logger.info("[ALIGN] building timeline clips=%d duration=%.2f", len(usable), total_duration)
    pending = temporary_output_path(output)
    try:
        with tempfile.TemporaryDirectory(prefix="timeline-", dir=str(output.parent)) as temp_name:
            temporary = Path(temp_name)
            chunks: list[Path] = []
            for offset in range(0, len(usable), chunk_size):
                chunk = temporary / f"chunk_{offset // chunk_size:04d}.wav"
                await _mix_timeline_chunk(usable[offset : offset + chunk_size], chunk, total_duration, config)
                chunks.append(chunk)
            if len(chunks) == 1:
                atomic_copy(chunks[0], pending)
            else:
                placeholders = [
                    Segment(id=index, start=0.0, end=total_duration, text="chunk", tts_file=str(path))
                    for index, path in enumerate(chunks)
                ]
                await _mix_timeline_chunk(placeholders, pending, total_duration, config)
        await commit_media_output(
            pending,
            output,
            config,
            required_stream_types={"audio"},
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
    if await media_cache_is_valid(output, config, required_stream_types={"audio"}):
        return output
    if background is None and dubbed_voice is None:
        raise RuntimeError("没有可混合的音轨")
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
            )
            return output

        background_volume = config.original_audio_volume if fast_mode else 1.0
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
        )
    finally:
        pending.unlink(missing_ok=True)
    return output
