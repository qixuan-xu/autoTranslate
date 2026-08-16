from __future__ import annotations

import io
import json
import os
import wave
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from backend.config import settings
from backend.models.domain import JobSettings, Segment, Transcript
from backend.pipeline import runner as runner_module
from backend.pipeline.context import JobPaths
from backend.pipeline.runner import PipelineAwaitingReference, PipelineRunner
from backend.pipeline.tts import CosyVoiceClient, wav_duration


def _wav_bytes(seconds: float, sample_rate: int = 8_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buffer.getvalue()


def test_tts_change_invalidates_only_exact_downstream_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = JobPaths.create(tmp_path, "job-1")
    unlink_calls: list[tuple[Path, bool]] = []

    def record_unlink(path: Path, missing_ok: bool = False) -> None:
        unlink_calls.append((path, missing_ok))

    monkeypatch.setattr(Path, "unlink", record_unlink)

    PipelineRunner._invalidate_downstream(paths)

    expected = [
        paths.mix / "dubbed_voice.wav",
        paths.mix / "mixed.wav",
        paths.output / "dubbed_voice.wav",
        paths.output / "mixed.wav",
        paths.output / "final_zh.mp4",
        paths.output / "final_zh_subtitle.mp4",
        paths.output / "final_zh_burned.mp4",
    ]
    assert [path for path, _ in unlink_calls] == expected
    assert all(missing_ok for _, missing_ok in unlink_calls)
    assert paths.tts / "0001.wav" not in expected
    assert paths.translation / "translation.json" not in expected
    assert paths.translation / "zh.srt" not in expected
    assert paths.asr / "transcript.json" not in expected


@pytest.mark.asyncio
async def test_manual_reference_without_id_pauses_after_segmentation_before_translation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job_id = "await-reference"
    paths = JobPaths.create(tmp_path, job_id)
    source = paths.source / "input.mp4"
    source.write_bytes(b"video")
    speech = paths.audio / "speech.wav"
    original = paths.audio / "original.wav"
    transcript = Transcript(
        language="en",
        duration=8.0,
        segments=[
            Segment(
                id=12,
                start=1.0,
                end=7.0,
                text="This is a clear complete sentence for reference.",
                confidence=0.96,
            )
        ],
    )

    async def fake_media_duration(_video, _config):
        return 8.0

    async def fake_extract(_video, _audio_dir, _config):
        speech.write_bytes(b"speech")
        original.write_bytes(b"original")
        return speech, original

    class FakeWhisper:
        def __init__(self, _config):
            pass

        async def transcribe(self, *_args, **_kwargs):
            return transcript

    def translation_must_not_start(*_args, **_kwargs):
        raise AssertionError("translation must wait for manual reference selection")

    monkeypatch.setattr(runner_module, "media_duration", fake_media_duration)
    monkeypatch.setattr(runner_module, "extract_audio_tracks", fake_extract)
    monkeypatch.setattr(runner_module, "WhisperAdapter", FakeWhisper)
    monkeypatch.setattr(runner_module, "build_translator", translation_must_not_start)

    updates: list[dict] = []

    async def update(**fields):
        updates.append(fields)
        return fields

    async def log(_message: str, _level: str) -> None:
        return None

    options = JobSettings(
        translation_provider="openai",
        translation_model="unused",
        dubbing_enabled=True,
        reference_mode="segment",
        reference_segment_id=None,
    )
    job = {
        "id": job_id,
        "work_dir": str(paths.root),
        "source_kind": "local",
        "source_value": str(source),
        "settings": options.model_dump(),
    }

    with pytest.raises(PipelineAwaitingReference) as raised:
        await PipelineRunner(replace(settings, work_dir=tmp_path)).run(
            job,
            update=update,
            log=log,
            is_cancelled=lambda: False,
        )

    assert raised.value.segment_count == 1
    segmented_cache = json.loads(
        (paths.asr / "segmented_transcript.json").read_text(encoding="utf-8")
    )
    segmented = Transcript.model_validate(segmented_cache["transcript"])
    assert [segment.id for segment in segmented.segments] == [12]
    assert updates[-1]["progress"] == 44.44
    assert not (paths.translation / "translation.json").exists()


@pytest.mark.asyncio
async def test_speed_and_atempo_derivatives_are_fully_resumable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = JobPaths.create(tmp_path, "job-resume")
    reference = paths.audio / "reference.wav"
    reference.write_bytes(_wav_bytes(2.0))
    transcript = Transcript(
        language="en",
        duration=1.0,
        segments=[
            Segment(
                id=1,
                start=0.0,
                end=1.0,
                text="A sentence.",
                translated_text="一句话。",
            )
        ],
    )

    synthesis_speeds: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        speed = float(payload["speed"])
        synthesis_speeds.append(speed)
        # The canonical result is mildly long.  CosyVoice speed helps first,
        # while a small residual remains for the cached atempo derivative.
        duration = 1.10 if speed == 1.0 else 1.04
        return httpx.Response(200, content=_wav_bytes(duration))

    ffmpeg_calls: list[list[str]] = []

    async def fake_run_process(args, **_kwargs):
        command = [str(item) for item in args]
        ffmpeg_calls.append(command)
        source = Path(command[command.index("-i") + 1])
        factor = float(command[command.index("-af") + 1].split("=")[1])
        Path(command[-1]).write_bytes(_wav_bytes(wav_duration(source) / factor))

    async def fake_commit(temporary, destination, _config, **_kwargs):
        os.replace(temporary, destination)
        return destination

    monkeypatch.setattr(runner_module, "require_executable", lambda *_args: "ffmpeg")
    monkeypatch.setattr(runner_module, "run_process", fake_run_process)
    monkeypatch.setattr(runner_module, "commit_media_output", fake_commit)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://cosyvoice.test",
    )
    client = CosyVoiceClient(http_client=http_client)
    pipeline = PipelineRunner(replace(settings, work_dir=tmp_path))

    async def log(_message: str, _level: str) -> None:
        return None

    class NoCompressionTranslator:
        async def compress(self, *_args, **_kwargs):
            raise AssertionError("mild overrun must not request text compression")

    try:
        first_batch = await client.synthesize_segments(
            transcript.segments,
            tts_dir=paths.tts,
            prompt_audio=reference,
            prompt_text="A sentence.",
        )
        first_changed = await pipeline._repair_durations(
            transcript,
            NoCompressionTranslator(),
            client,
            reference,
            "A sentence.",
            paths,
            log,
            lambda: None,
        )

        base = paths.tts / "0001.wav"
        speed_adjusted = paths.tts / "0001_speed.wav"
        aligned = paths.tts / "0001_aligned.wav"
        base_metadata = json.loads(
            (paths.tts / "0001.wav.meta.json").read_text(encoding="utf-8")
        )
        aligned_metadata = json.loads(
            (paths.tts / "0001_aligned.wav.meta.json").read_text(encoding="utf-8")
        )

        assert first_batch[0].cached is False
        assert first_changed is True
        assert base.is_file() and speed_adjusted.is_file() and aligned.is_file()
        assert base_metadata["speed"] == 1.0
        assert aligned_metadata["kind"] == "ffmpeg_atempo"
        assert aligned_metadata["source"] == str(speed_adjusted.resolve())
        assert len(aligned_metadata["source_sha256"]) == 64
        assert aligned_metadata["factor"] == pytest.approx(1.04)
        assert Path(transcript.segments[0].tts_file or "") == aligned
        assert synthesis_speeds == pytest.approx([1.0, 1.1])
        assert len(ffmpeg_calls) == 1

        # A real retry begins with synthesize_segments again.  It must restore
        # the canonical base path, then reuse both derivatives without any HTTP
        # synthesis, FFmpeg work, or downstream invalidation signal.
        second_batch = await client.synthesize_segments(
            transcript.segments,
            tts_dir=paths.tts,
            prompt_audio=reference,
            prompt_text="A sentence.",
        )
        second_changed = await pipeline._repair_durations(
            transcript,
            NoCompressionTranslator(),
            client,
            reference,
            "A sentence.",
            paths,
            log,
            lambda: None,
        )

        assert second_batch[0].cached is True
        assert second_changed is False
        assert (any(not item.cached for item in second_batch) or second_changed) is False
        assert synthesis_speeds == pytest.approx([1.0, 1.1])
        assert len(ffmpeg_calls) == 1

        # Content, not just filename/mtime, fingerprints the atempo input.
        mutated = bytearray(speed_adjusted.read_bytes())
        mutated[-1] = 1
        speed_adjusted.write_bytes(mutated)
        assert await pipeline._apply_atempo(speed_adjusted, aligned, 1.04) is True
        assert len(ffmpeg_calls) == 2
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_slower_speed_derivative_is_rejected_when_fresh_and_cached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = JobPaths.create(tmp_path, "job-slower-speed")
    reference = paths.audio / "reference.wav"
    reference.write_bytes(_wav_bytes(2.0))
    target_duration = 1.48
    base_duration = 1.68
    transcript = Transcript(
        language="en",
        duration=target_duration,
        segments=[
            Segment(
                id=19,
                start=0.0,
                end=target_duration,
                text="A short sentence.",
                translated_text="一句短句。",
            )
        ],
    )

    synthesis_speeds: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        speed = float(json.loads(request.content)["speed"])
        synthesis_speeds.append(speed)
        # Real regression: asking CosyVoice for speed=1.1 made this segment
        # longer (2.02 s) than the canonical speed=1 result (1.68 s).
        duration = base_duration if speed == 1.0 else 2.02
        return httpx.Response(200, content=_wav_bytes(duration))

    ffmpeg_calls: list[list[str]] = []

    async def fake_run_process(args, **_kwargs):
        command = [str(item) for item in args]
        ffmpeg_calls.append(command)
        source = Path(command[command.index("-i") + 1])
        factor = float(command[command.index("-af") + 1].split("=")[1])
        Path(command[-1]).write_bytes(_wav_bytes(wav_duration(source) / factor))

    async def fake_commit(temporary, destination, _config, **_kwargs):
        os.replace(temporary, destination)
        return destination

    monkeypatch.setattr(runner_module, "require_executable", lambda *_args: "ffmpeg")
    monkeypatch.setattr(runner_module, "run_process", fake_run_process)
    monkeypatch.setattr(runner_module, "commit_media_output", fake_commit)

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://cosyvoice.test",
    )
    client = CosyVoiceClient(http_client=http_client)
    pipeline = PipelineRunner(replace(settings, work_dir=tmp_path))
    logs: list[tuple[str, str]] = []

    async def log(message: str, level: str) -> None:
        logs.append((message, level))

    class NoCompressionTranslator:
        async def compress(self, *_args, **_kwargs):
            raise AssertionError("mild overrun must not request text compression")

    async def restore_canonical_base() -> None:
        results = await client.synthesize_segments(
            transcript.segments,
            tts_dir=paths.tts,
            prompt_audio=reference,
            prompt_text="A short sentence.",
        )
        assert results[0].cached is (len(synthesis_speeds) > 1)

    def assert_uses_base_then_gentle_atempo() -> None:
        base = paths.tts / "0019.wav"
        speed_adjusted = paths.tts / "0019_speed.wav"
        aligned = paths.tts / "0019_aligned.wav"
        aligned_metadata = json.loads(
            (paths.tts / "0019_aligned.wav.meta.json").read_text(encoding="utf-8")
        )

        assert speed_adjusted.is_file()
        assert Path(transcript.segments[0].tts_file or "") == aligned
        assert aligned_metadata["source"] == str(base.resolve())
        assert aligned_metadata["source"] != str(speed_adjusted.resolve())
        assert aligned_metadata["factor"] == pytest.approx(1.06)
        expected_ratio = (base_duration / 1.06) / target_duration
        actual_ratio = (transcript.segments[0].tts_duration or 0) / target_duration
        assert actual_ratio == pytest.approx(expected_ratio, abs=0.001)
        assert actual_ratio < 1.08

    try:
        await restore_canonical_base()
        first_changed = await pipeline._repair_durations(
            transcript,
            NoCompressionTranslator(),
            client,
            reference,
            "A short sentence.",
            paths,
            log,
            lambda: None,
        )

        assert first_changed is True
        assert_uses_base_then_gentle_atempo()
        assert synthesis_speeds == pytest.approx([1.0, 1.1])
        assert len(ffmpeg_calls) == 1

        # Recreate the rejected speed derivative while the accepted atempo
        # derivative is already cached.  The unused file is not a downstream
        # input, so generating it must not report a pipeline content change.
        (paths.tts / "0019_speed.wav").unlink()
        (paths.tts / "0019_speed.wav.meta.json").unlink()
        await restore_canonical_base()
        fresh_rejected_changed = await pipeline._repair_durations(
            transcript,
            NoCompressionTranslator(),
            client,
            reference,
            "A short sentence.",
            paths,
            log,
            lambda: None,
        )

        assert fresh_rejected_changed is False
        assert_uses_base_then_gentle_atempo()
        assert synthesis_speeds == pytest.approx([1.0, 1.1, 1.1])
        assert len(ffmpeg_calls) == 1

        # A normal resume restores the canonical base and finds the same bad
        # speed result in cache.  It must reject it identically and remain a
        # complete cache hit through alignment.
        await restore_canonical_base()
        cached_rejected_changed = await pipeline._repair_durations(
            transcript,
            NoCompressionTranslator(),
            client,
            reference,
            "A short sentence.",
            paths,
            log,
            lambda: None,
        )

        assert cached_rejected_changed is False
        assert_uses_base_then_gentle_atempo()
        assert synthesis_speeds == pytest.approx([1.0, 1.1, 1.1])
        assert len(ffmpeg_calls) == 1
        rejection_logs = [
            (message, level) for message, level in logs if "拒绝 CosyVoice" in message
        ]
        assert len(rejection_logs) == 3
        assert all(level == "WARNING" for _, level in rejection_logs)
        assert "cached=False" in rejection_logs[1][0]
        assert "cached=True" in rejection_logs[2][0]
    finally:
        await http_client.aclose()
