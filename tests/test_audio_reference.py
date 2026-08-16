from __future__ import annotations

import json
import math
import shutil
import wave
from dataclasses import replace
from pathlib import Path

import pytest

from backend.config import settings
from backend.models.domain import Segment, Transcript, WordTimestamp
from backend.pipeline import audio as audio_module
from backend.pipeline.audio import cut_reference_audio, select_reference_segment


def _timed_words(
    tokens: list[str],
    start: float,
    end: float,
    probability: float,
) -> list[WordTimestamp]:
    step = (end - start) / len(tokens)
    return [
        WordTimestamp(
            word=("" if index == 0 else " ") + token,
            start=start + index * step,
            end=start + (index + 0.82) * step,
            probability=probability,
        )
        for index, token in enumerate(tokens)
    ]


def test_word_boundaries_build_a_complete_span_and_reject_tail_credit_list() -> None:
    first_words = _timed_words(
        ["Today", "we", "explain", "how", "the", "system", "really", "works."],
        10.0,
        13.7,
        0.86,
    )
    second_words = _timed_words(
        ["Then", "we", "will", "try", "it", "on", "a", "real", "example."],
        13.8,
        18.0,
        0.86,
    )
    credit_words = _timed_words(
        [
            "Alice,",
            "Bob,",
            "Carol,",
            "David,",
            "Erin,",
            "Frank,",
            "Grace,",
            "Henry.",
        ],
        91.0,
        98.0,
        0.99,
    )
    transcript = Transcript(
        language="en",
        duration=100.0,
        segments=[
            Segment(
                id=4,
                start=10.0,
                end=13.7,
                text="Today we explain how the system really works.",
                words=first_words,
                confidence=0.86,
            ),
            Segment(
                id=5,
                start=13.8,
                end=18.0,
                text="Then we will try it on a real example.",
                words=second_words,
                confidence=0.86,
            ),
            Segment(
                id=99,
                start=91.0,
                end=98.0,
                text="Alice, Bob, Carol, David, Erin, Frank, Grace, Henry.",
                words=credit_words,
                confidence=0.99,
            ),
        ],
    )

    selected = select_reference_segment(transcript)

    assert selected.id == 4
    assert selected.start == pytest.approx(10.0)
    assert selected.end == pytest.approx(second_words[-1].end)
    assert 5.0 <= selected.duration <= 12.0
    assert selected.text.endswith(".")
    assert "real example" in selected.text
    assert "Alice" not in selected.text
    assert len(selected.words) == len(first_words) + len(second_words)


def test_word_probability_contributes_to_reference_ranking() -> None:
    low_words = _timed_words(
        ["This", "is", "a", "complete", "but", "uncertain", "spoken", "sentence."],
        2.0,
        8.0,
        0.42,
    )
    high_words = _timed_words(
        ["This", "is", "a", "complete", "and", "clearly", "spoken", "sentence."],
        12.0,
        18.0,
        0.97,
    )
    transcript = Transcript(
        language="en",
        duration=30.0,
        segments=[
            Segment(id=1, start=2.0, end=8.0, text="Low.", words=low_words),
            Segment(id=2, start=12.0, end=18.0, text="High.", words=high_words),
        ],
    )

    selected = select_reference_segment(transcript)

    assert selected.id == 2
    assert selected.confidence == pytest.approx(0.97)


def test_no_word_timestamp_fallback_merges_sentences_and_avoids_credits() -> None:
    transcript = Transcript(
        language="en",
        duration=100.0,
        segments=[
            Segment(
                id=7,
                start=5.0,
                end=8.0,
                text="This opening thought is",
                confidence=0.82,
            ),
            Segment(
                id=8,
                start=8.2,
                end=12.0,
                text="finished as one clear sentence.",
                confidence=0.84,
            ),
            Segment(
                id=20,
                start=25.0,
                end=31.0,
                text="and this fragment has no proper ending",
                confidence=0.98,
            ),
            Segment(
                id=90,
                start=91.0,
                end=98.0,
                text="Alice Smith, Bob Jones, Carol Wu, David Lee, Erin Chen.",
                confidence=0.99,
            ),
        ],
    )

    selected = select_reference_segment(transcript)

    assert selected.id == 7
    assert selected.start == pytest.approx(5.0)
    assert selected.end == pytest.approx(12.0)
    assert selected.text == "This opening thought is finished as one clear sentence."


def test_reference_selection_rejects_only_too_short_fallback() -> None:
    transcript = Transcript(
        language="en",
        duration=1.0,
        segments=[Segment(id=1, start=0.0, end=1.0, text="Hello.")],
    )

    with pytest.raises(RuntimeError, match="2.5"):
        select_reference_segment(transcript)


def _write_tone(path: Path, *, frequency: float, seconds: float = 4.0) -> None:
    sample_rate = 16_000
    frames = bytearray()
    for index in range(int(sample_rate * seconds)):
        sample = int(8_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames.extend(sample.to_bytes(2, byteorder="little", signed=True))
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(frames)


@pytest.mark.asyncio
async def test_reference_cut_ffmpeg_cache_tracks_selection_and_source_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe are required for the integration test")

    source = tmp_path / "speech.wav"
    output = tmp_path / "reference.wav"
    _write_tone(source, frequency=330.0)
    original_source_size = source.stat().st_size
    segment = Segment(
        id=3,
        start=0.8,
        end=2.2,
        text="This is the selected sentence.",
    )
    config = replace(settings, ffmpeg_bin=ffmpeg, ffprobe_bin=ffprobe)

    real_run_process = audio_module.run_process
    ffmpeg_calls: list[list[str]] = []

    async def tracking_run_process(args, **kwargs):
        command = [str(item) for item in args]
        if Path(command[0]).name == "ffmpeg":
            ffmpeg_calls.append(command)
        return await real_run_process(args, **kwargs)

    monkeypatch.setattr(audio_module, "run_process", tracking_run_process)

    await cut_reference_audio(source, segment, output, config)
    metadata_path = tmp_path / "reference.wav.meta.json"
    first_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert len(ffmpeg_calls) == 1
    assert first_metadata["selection"]["text"] == segment.text
    assert first_metadata["source"]["size"] == original_source_size
    assert len(first_metadata["source"]["sha256"]) == 64
    with wave.open(str(output), "rb") as wav_file:
        assert wav_file.getframerate() == 16_000
        assert wav_file.getnchannels() == 1
        output_duration = wav_file.getnframes() / wav_file.getframerate()
    assert output_duration == pytest.approx(1.6, abs=0.04)

    # Exact source, selection, text, and cut parameters reuse the validated WAV.
    await cut_reference_audio(source, segment, output, config)
    assert len(ffmpeg_calls) == 1

    changed_timing = segment.model_copy(update={"end": 2.35})
    await cut_reference_audio(source, changed_timing, output, config)
    assert len(ffmpeg_calls) == 2

    changed_text = changed_timing.model_copy(update={"text": "A different prompt transcript."})
    await cut_reference_audio(source, changed_text, output, config)
    assert len(ffmpeg_calls) == 3

    # Replacing the source in place with a same-size WAV must still recut.
    _write_tone(source, frequency=660.0)
    assert source.stat().st_size == original_source_size
    await cut_reference_audio(source, changed_text, output, config)
    assert len(ffmpeg_calls) == 4
    final_metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert final_metadata["source"]["sha256"] != first_metadata["source"]["sha256"]
