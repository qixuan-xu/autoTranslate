from __future__ import annotations

import math
import shutil
import struct
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from backend.config import Settings
from backend.models.domain import Segment
from backend.pipeline.mixer import (
    _build_timeline_mix_args,
    _plan_timeline_chunks,
    build_dubbed_timeline,
)


def _long_video_segments(count: int = 1_000, duration: float = 3_600.0) -> list[Segment]:
    interval = duration / count
    return [
        Segment(
            id=index,
            start=index * interval,
            end=min(duration, index * interval + 2.0),
            text=f"segment {index}",
            tts_file=f"/tmp/tts-{index:04d}.wav",
            tts_duration=1.0,
        )
        for index in range(count)
    ]


def test_one_hour_timeline_chunks_only_cover_their_own_windows() -> None:
    total_duration = 3_600.0
    chunk_size = 60
    segments = _long_video_segments(duration=total_duration)

    plans = _plan_timeline_chunks(
        segments,
        total_duration,
        chunk_size=chunk_size,
    )

    expected_chunks = math.ceil(len(segments) / chunk_size)
    old_intermediate_duration = expected_chunks * total_duration
    new_intermediate_duration = sum(plan.duration for plan in plans)

    assert len(plans) == expected_chunks
    assert all(0 < plan.duration < total_duration for plan in plans)
    assert new_intermediate_duration < total_duration
    assert new_intermediate_duration < old_intermediate_duration / 10
    assert [segment.id for plan in plans for segment in plan.items] == list(range(1_000))


def test_chunk_commands_use_local_delays_then_restore_absolute_positions() -> None:
    total_duration = 3_600.0
    plans = _plan_timeline_chunks(
        _long_video_segments(duration=total_duration),
        total_duration,
        chunk_size=60,
    )
    first = plans[1]
    local_args = _build_timeline_mix_args(
        first.items,
        Path("/tmp/chunk.wav"),
        first.duration,
        "/usr/bin/ffmpeg",
        timeline_origin=first.start,
    )
    local_filter = local_args[local_args.index("-filter_complex") + 1]

    assert "adelay=0:all=1" in local_filter
    assert f"adelay={round((first.items[-1].start - first.start) * 1000)}:all=1" in local_filter
    assert local_args[local_args.index("-t") + 1] == f"{first.duration:.3f}"

    placeholders = [
        Segment(
            id=index,
            start=plan.start,
            end=plan.end,
            text="chunk",
            tts_file=f"/tmp/chunk-{index:04d}.wav",
            tts_duration=plan.duration,
        )
        for index, plan in enumerate(plans)
    ]
    final_args = _build_timeline_mix_args(
        placeholders,
        Path("/tmp/final.wav"),
        total_duration,
        "/usr/bin/ffmpeg",
    )
    final_filter = final_args[final_args.index("-filter_complex") + 1]

    assert f"adelay={round(plans[1].start * 1000)}:all=1" in final_filter
    assert final_args[final_args.index("-t") + 1] == "3600.000"


@pytest.mark.parametrize("chunk_size", [0, -1])
def test_timeline_chunk_plan_rejects_invalid_chunk_size(chunk_size: int) -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        _plan_timeline_chunks(
            _long_video_segments(count=2, duration=10.0),
            10.0,
            chunk_size=chunk_size,
        )


def _write_tone(path: Path, *, frequency: float, duration: float = 0.2) -> None:
    sample_rate = 48_000
    amplitude = 12_000
    samples = [
        round(amplitude * math.sin(2 * math.pi * frequency * index / sample_rate))
        for index in range(round(duration * sample_rate))
    ]
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _window_rms(
    samples: tuple[int, ...],
    *,
    channels: int,
    sample_rate: int,
    start: float,
    end: float,
) -> float:
    first = round(start * sample_rate) * channels
    last = round(end * sample_rate) * channels
    window = samples[first:last]
    assert window
    return math.sqrt(sum(value * value for value in window) / len(window))


@pytest.mark.asyncio
async def test_ffmpeg_places_multiple_chunks_at_absolute_timestamps(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg/ffprobe 不可用")

    first_tone = tmp_path / "first.wav"
    second_tone = tmp_path / "second.wav"
    _write_tone(first_tone, frequency=440)
    _write_tone(second_tone, frequency=880)
    segments = [
        Segment(
            id=1,
            start=0.4,
            end=0.7,
            text="first",
            tts_file=str(first_tone),
            tts_duration=0.2,
        ),
        Segment(
            id=2,
            start=1.4,
            end=1.7,
            text="second",
            tts_file=str(second_tone),
            tts_duration=0.2,
        ),
    ]
    output = tmp_path / "dubbed_voice.wav"
    config = replace(
        Settings(),
        ffmpeg_bin=ffmpeg,
        ffprobe_bin=ffprobe,
    )

    result = await build_dubbed_timeline(
        segments,
        2.0,
        output,
        config,
        chunk_size=1,
    )

    assert result == output
    with wave.open(str(output), "rb") as rendered:
        channels = rendered.getnchannels()
        sample_rate = rendered.getframerate()
        frame_count = rendered.getnframes()
        assert rendered.getsampwidth() == 2
        raw = rendered.readframes(frame_count)
    samples = struct.unpack(f"<{len(raw) // 2}h", raw)

    assert channels == 2
    assert sample_rate == 48_000
    assert frame_count / sample_rate == pytest.approx(2.0, abs=0.02)
    assert _window_rms(
        samples,
        channels=channels,
        sample_rate=sample_rate,
        start=0.05,
        end=0.30,
    ) < 50
    assert _window_rms(
        samples,
        channels=channels,
        sample_rate=sample_rate,
        start=0.42,
        end=0.57,
    ) > 1_000
    assert _window_rms(
        samples,
        channels=channels,
        sample_rate=sample_rate,
        start=0.80,
        end=1.20,
    ) < 50
    assert _window_rms(
        samples,
        channels=channels,
        sample_rate=sample_rate,
        start=1.42,
        end=1.57,
    ) > 1_000
    assert _window_rms(
        samples,
        channels=channels,
        sample_rate=sample_rate,
        start=1.75,
        end=1.95,
    ) < 50
