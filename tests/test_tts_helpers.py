from __future__ import annotations

import io
import json
import wave
from pathlib import Path

import httpx
import pytest

from backend.pipeline.tts import CosyVoiceClient, wav_duration
from services.cosyvoice_server import PROMPT_PREFIX, format_prompt_text


def _wav_bytes(seconds: float = 0.25, sample_rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(b"\x00\x00" * int(seconds * sample_rate))
    return buffer.getvalue()


def test_prompt_prefix_is_added_exactly_once() -> None:
    assert format_prompt_text("This is the reference.") == (
        f"{PROMPT_PREFIX}This is the reference."
    )
    assert format_prompt_text(f"{PROMPT_PREFIX}This is the reference.") == (
        f"{PROMPT_PREFIX}This is the reference."
    )


def test_wav_duration_reads_pcm_frames(tmp_path: Path) -> None:
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(_wav_bytes(0.5))
    assert wav_duration(wav_path) == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_segment_synthesis_uses_cache_and_reports_ratio(tmp_path: Path) -> None:
    synthesize_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal synthesize_calls
        assert request.url.path == "/synthesize"
        synthesize_calls += 1
        return httpx.Response(200, content=_wav_bytes(0.25), headers={"content-type": "audio/wav"})

    reference = tmp_path / "reference.wav"
    reference.write_bytes(_wav_bytes(2.0))
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url="http://cosyvoice.test")
    client = CosyVoiceClient(http_client=http_client)
    segment = {
        "id": 7,
        "start": 10.0,
        "end": 10.5,
        "translated_text": "你好。",
    }
    progress: list[tuple[int, int, int]] = []

    first = await client.synthesize_segments(
        [segment],
        tts_dir=tmp_path / "tts",
        prompt_audio=reference,
        prompt_text="Hello.",
        progress=lambda done, total, segment_id: progress.append((done, total, segment_id)),
    )
    second = await client.synthesize_segments(
        [segment],
        tts_dir=tmp_path / "tts",
        prompt_audio=reference,
        prompt_text="Hello.",
    )
    segment["translated_text"] = "再见。"
    third = await client.synthesize_segments(
        [segment],
        tts_dir=tmp_path / "tts",
        prompt_audio=reference,
        prompt_text="Hello.",
    )
    await http_client.aclose()

    assert synthesize_calls == 2
    assert first[0].cached is False
    assert second[0].cached is True
    assert third[0].cached is False
    assert first[0].duration_ratio == pytest.approx(0.5)
    assert progress == [(1, 1, 7)]
    assert Path(segment["tts_file"]).name == "0007.wav"
    assert segment["tts_duration"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_synthesis_cache_invalidates_for_every_content_input(tmp_path: Path) -> None:
    """A valid WAV alone must never make stale speech reusable."""

    synthesize_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal synthesize_calls
        assert request.url.path == "/synthesize"
        synthesize_calls += 1
        return httpx.Response(200, content=_wav_bytes())

    reference = tmp_path / "reference.wav"
    reference.write_bytes(_wav_bytes(1.0))
    output = tmp_path / "tts" / "0001.wav"
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://cosyvoice.test",
    )
    client = CosyVoiceClient(http_client=http_client)

    try:
        first = await client.synthesize(
            text="第一版译文。",
            prompt_audio=reference,
            prompt_text="Original prompt.",
            output_path=output,
        )
        unchanged = await client.synthesize(
            text="第一版译文。",
            prompt_audio=reference,
            prompt_text="Original prompt.",
            output_path=output,
        )
        changed_text = await client.synthesize(
            text="第二版译文。",
            prompt_audio=reference,
            prompt_text="Original prompt.",
            output_path=output,
        )
        changed_prompt = await client.synthesize(
            text="第二版译文。",
            prompt_audio=reference,
            prompt_text="Corrected prompt.",
            output_path=output,
        )
        changed_speed = await client.synthesize(
            text="第二版译文。",
            prompt_audio=reference,
            prompt_text="Corrected prompt.",
            output_path=output,
            speed=1.05,
        )

        # Replacing the reference in place must also invalidate the cache.  The
        # different duration guarantees a different byte size even on coarse
        # timestamp filesystems.
        reference.write_bytes(_wav_bytes(1.5))
        changed_reference = await client.synthesize(
            text="第二版译文。",
            prompt_audio=reference,
            prompt_text="Corrected prompt.",
            output_path=output,
            speed=1.05,
        )
    finally:
        await http_client.aclose()

    assert synthesize_calls == 5
    assert first.cached is False
    assert unchanged.cached is True
    assert changed_text.cached is False
    assert changed_prompt.cached is False
    assert changed_speed.cached is False
    assert changed_reference.cached is False

    metadata = json.loads((output.parent / "0001.wav.meta.json").read_text(encoding="utf-8"))
    assert metadata["text"] == "第二版译文。"
    assert metadata["prompt_text"] == "Corrected prompt."
    assert metadata["prompt_audio_size"] == reference.stat().st_size
    assert metadata["speed"] == 1.05
