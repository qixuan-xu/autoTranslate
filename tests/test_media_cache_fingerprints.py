from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from backend.config import Settings
from backend.models.domain import Segment
from backend.pipeline import audio, mixer, muxer, runner
from backend.pipeline.audio import extract_audio_tracks, media_cache_metadata_path
from backend.pipeline.mixer import build_dubbed_timeline, mix_audio
from backend.pipeline.muxer import mux_burned_subtitle, mux_soft_subtitle
from backend.utils.process import ProcessResult


async def _accept_media(*_args: object, **_kwargs: object) -> dict:
    return {
        "format": {"duration": "1.0"},
        "streams": [{"codec_type": "audio"}, {"codec_type": "video"}],
    }


def _write_process_output(args: list[str], payload: bytes = b"rendered") -> None:
    destination = Path(args[-1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)


@pytest.mark.asyncio
async def test_audio_extract_cache_binds_same_size_source_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"AAAA")
    calls: list[list[str]] = []

    async def fake_run(args: list[str], **_kwargs: object) -> ProcessResult:
        calls.append(args)
        _write_process_output(args)
        return ProcessResult(args=args, returncode=0, output="")

    monkeypatch.setattr(audio, "validate_media_file", _accept_media)
    monkeypatch.setattr(audio, "require_executable", lambda *_: "/mock/ffmpeg")
    monkeypatch.setattr(audio, "run_process", fake_run)
    config = Settings(ffmpeg_bin="ffmpeg")

    speech, original = await extract_audio_tracks(source, tmp_path / "audio", config)
    assert len(calls) == 2
    assert media_cache_metadata_path(speech).is_file()
    assert media_cache_metadata_path(original).is_file()

    await extract_audio_tracks(source, tmp_path / "audio", config)
    assert len(calls) == 2

    source.write_bytes(b"BBBB")
    await extract_audio_tracks(source, tmp_path / "audio", config)
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_timeline_cache_binds_tts_content_start_duration_and_chunking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tts = tmp_path / "tts.wav"
    tts.write_bytes(b"AAAA")
    output = tmp_path / "dubbed_voice.wav"
    segment = Segment(
        id=7,
        start=0.2,
        end=0.8,
        text="hello",
        tts_file=str(tts),
        tts_duration=0.5,
    )
    render_calls: list[tuple[float, float]] = []

    async def fake_mix(
        _items: object,
        destination: Path,
        duration: float,
        _config: Settings,
        *,
        timeline_origin: float = 0.0,
    ) -> None:
        render_calls.append((duration, timeline_origin))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"timeline")

    monkeypatch.setattr(audio, "validate_media_file", _accept_media)
    monkeypatch.setattr(mixer, "_mix_timeline_chunk", fake_mix)
    config = Settings()

    await build_dubbed_timeline([segment], 2.0, output, config, chunk_size=60)
    assert len(render_calls) == 2
    await build_dubbed_timeline([segment], 2.0, output, config, chunk_size=60)
    assert len(render_calls) == 2

    tts.write_bytes(b"BBBB")
    await build_dubbed_timeline([segment], 2.0, output, config, chunk_size=60)
    assert len(render_calls) == 4

    shifted = segment.model_copy(update={"start": 0.3})
    await build_dubbed_timeline([shifted], 2.0, output, config, chunk_size=60)
    assert len(render_calls) == 6

    await build_dubbed_timeline([shifted], 2.1, output, config, chunk_size=1)
    assert len(render_calls) == 8


@pytest.mark.asyncio
async def test_mix_cache_binds_inputs_duration_volume_and_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    background = tmp_path / "background.wav"
    dubbed = tmp_path / "dubbed.wav"
    output = tmp_path / "mixed.wav"
    background.write_bytes(b"AAAA")
    dubbed.write_bytes(b"DDDD")
    calls: list[list[str]] = []

    async def fake_run(args: list[str], **_kwargs: object) -> ProcessResult:
        calls.append(args)
        _write_process_output(args)
        return ProcessResult(args=args, returncode=0, output="")

    monkeypatch.setattr(audio, "validate_media_file", _accept_media)
    monkeypatch.setattr(mixer, "require_executable", lambda *_: "/mock/ffmpeg")
    monkeypatch.setattr(mixer, "run_process", fake_run)
    config = Settings(original_audio_volume=0.14)

    await mix_audio(background, dubbed, output, 2.0, config, fast_mode=True)
    assert len(calls) == 1
    await mix_audio(background, dubbed, output, 2.0, config, fast_mode=True)
    assert len(calls) == 1

    await mix_audio(
        background,
        dubbed,
        output,
        2.0,
        replace(config, original_audio_volume=0.2),
        fast_mode=True,
    )
    assert len(calls) == 2

    background.write_bytes(b"BBBB")
    await mix_audio(background, dubbed, output, 2.0, config, fast_mode=True)
    assert len(calls) == 3
    await mix_audio(background, dubbed, output, 2.1, config, fast_mode=False)
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_soft_mux_cache_binds_all_input_content_and_subtitle_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "source.mp4"
    mixed = tmp_path / "mixed.wav"
    subtitle = tmp_path / "zh.srt"
    output = tmp_path / "final_zh.mp4"
    video.write_bytes(b"VVVV")
    mixed.write_bytes(b"AAAA")
    subtitle.write_bytes(b"1111")
    calls: list[list[str]] = []

    async def fake_run(args: list[str], **_kwargs: object) -> ProcessResult:
        calls.append(args)
        _write_process_output(args)
        return ProcessResult(args=args, returncode=0, output="")

    monkeypatch.setattr(audio, "validate_media_file", _accept_media)
    monkeypatch.setattr(muxer, "require_executable", lambda *_: "/mock/ffmpeg")
    monkeypatch.setattr(muxer, "run_process", fake_run)
    config = Settings()

    await mux_soft_subtitle(video, mixed, subtitle, output, config)
    assert len(calls) == 1
    await mux_soft_subtitle(video, mixed, subtitle, output, config)
    assert len(calls) == 1

    subtitle.write_bytes(b"2222")
    await mux_soft_subtitle(video, mixed, subtitle, output, config)
    assert len(calls) == 2

    mixed.write_bytes(b"BBBB")
    await mux_soft_subtitle(video, mixed, subtitle, output, config)
    assert len(calls) == 3

    await mux_soft_subtitle(video, mixed, None, output, config)
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_burn_mux_cache_binds_subtitles_font_and_media_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    video = tmp_path / "source.mp4"
    mixed = tmp_path / "mixed.wav"
    ass = tmp_path / "zh.ass"
    srt = tmp_path / "zh.srt"
    output = tmp_path / "final_zh_burned.mp4"
    video.write_bytes(b"VVVV")
    mixed.write_bytes(b"AAAA")
    ass.write_bytes(b"1111")
    srt.write_bytes(b"2222")
    calls: list[list[str]] = []

    async def fake_run(args: list[str], **_kwargs: object) -> ProcessResult:
        calls.append(args)
        _write_process_output(args)
        return ProcessResult(args=args, returncode=0, output="")

    monkeypatch.setattr(audio, "validate_media_file", _accept_media)
    monkeypatch.setattr(muxer, "require_executable", lambda *_: "/mock/ffmpeg")
    monkeypatch.setattr(muxer, "run_process", fake_run)
    config = Settings()

    await mux_burned_subtitle(
        video,
        mixed,
        ass,
        output,
        config,
        fallback_srt=srt,
        font_name="Font A",
    )
    assert len(calls) == 1
    await mux_burned_subtitle(
        video,
        mixed,
        ass,
        output,
        config,
        fallback_srt=srt,
        font_name="Font A",
    )
    assert len(calls) == 1

    await mux_burned_subtitle(
        video,
        mixed,
        ass,
        output,
        config,
        fallback_srt=srt,
        font_name="Font B",
    )
    assert len(calls) == 2

    ass.write_bytes(b"3333")
    await mux_burned_subtitle(
        video,
        mixed,
        ass,
        output,
        config,
        fallback_srt=srt,
        font_name="Font B",
    )
    assert len(calls) == 3

    video.write_bytes(b"WWWW")
    await mux_burned_subtitle(
        video,
        mixed,
        ass,
        output,
        config,
        fallback_srt=srt,
        font_name="Font B",
    )
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_soft_subtitle_alias_cache_binds_final_mux_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "final_zh.mp4"
    alias = tmp_path / "final_zh_subtitle.mp4"
    source.write_bytes(b"AAAA")
    validations = 0

    async def count_validation(*args: object, **kwargs: object) -> dict:
        nonlocal validations
        validations += 1
        return await _accept_media(*args, **kwargs)

    monkeypatch.setattr(audio, "validate_media_file", count_validation)

    await runner._copy_soft_subtitle_alias(source, alias, Settings())
    assert alias.read_bytes() == b"AAAA"
    first_validations = validations

    await runner._copy_soft_subtitle_alias(source, alias, Settings())
    assert validations == first_validations + 1

    source.write_bytes(b"BBBB")
    await runner._copy_soft_subtitle_alias(source, alias, Settings())
    assert alias.read_bytes() == b"BBBB"
    assert validations == first_validations + 2


def test_media_sidecar_publication_is_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacements: list[tuple[str | bytes | os.PathLike[str] | os.PathLike[bytes], object]] = []
    real_replace = os.replace

    def record_replace(source: object, destination: object) -> None:
        replacements.append((source, destination))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", record_replace)
    sidecar = media_cache_metadata_path(tmp_path / "artifact.wav")
    from backend.utils.files import atomic_write_json

    atomic_write_json(sidecar, {"version": 1})

    assert sidecar.is_file()
    assert replacements[-1][1] == sidecar
