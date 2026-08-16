from dataclasses import replace
from pathlib import Path

import pytest

from backend.config import settings
from backend.models.domain import Segment, Transcript
from backend.pipeline import whisper_asr as whisper_module
from backend.pipeline.whisper_asr import (
    ASR_CACHE_METADATA_NAME,
    WhisperAdapter,
    build_asr_cache_metadata,
    load_cached_transcript,
)
from backend.utils.files import atomic_write_json


def test_whisper_json_is_normalized_with_word_timestamps():
    transcript = WhisperAdapter._normalize(
        {
            "language": "en",
            "segments": [
                {
                    "id": 4,
                    "start": 1.0,
                    "end": 3.5,
                    "text": " Hello world. ",
                    "avg_logprob": -0.1,
                    "words": [
                        {"word": " Hello", "start": 1.0, "end": 1.7, "probability": 0.95},
                        {"word": " world.", "start": 1.7, "end": 3.4, "probability": 0.9},
                    ],
                }
            ],
        }
    )
    assert transcript.language == "en"
    assert transcript.duration == 3.5
    assert transcript.segments[0].text == "Hello world."
    assert transcript.segments[0].words[1].end == 3.4
    assert 0.9 < transcript.segments[0].confidence < 1.0


def _cached_transcript() -> Transcript:
    return Transcript(
        language="en",
        duration=2.0,
        segments=[Segment(id=0, start=0.0, end=2.0, text="Hello world.")],
    )


def _seed_cache(output_dir: Path, audio: Path) -> dict:
    output_dir.mkdir(parents=True)
    transcript = _cached_transcript()
    metadata = build_asr_cache_metadata(audio, model="turbo", language="auto")
    atomic_write_json(output_dir / "transcript.json", transcript.model_dump())
    atomic_write_json(output_dir / ASR_CACHE_METADATA_NAME, metadata)
    return metadata


@pytest.mark.asyncio
async def test_transcribe_reuses_only_a_fingerprinted_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"same audio content")
    output_dir = tmp_path / "asr"
    _seed_cache(output_dir, audio)

    def must_not_find_whisper(*_args, **_kwargs):
        raise AssertionError("a matching cache must not invoke Whisper")

    monkeypatch.setattr(whisper_module, "require_executable", must_not_find_whisper)
    adapter = WhisperAdapter(replace(settings, whisper_model="turbo"))

    transcript = await adapter.transcribe(
        audio,
        output_dir,
        model="turbo",
        language="AUTOMATIC",
    )

    assert transcript == _cached_transcript()
    assert (output_dir / "original.srt").is_file()


def test_asr_cache_invalidates_on_same_size_audio_content_model_or_language(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"alpha")
    output_dir = tmp_path / "asr"
    original_metadata = _seed_cache(output_dir, audio)
    transcript_path = output_dir / "transcript.json"
    metadata_path = output_dir / ASR_CACHE_METADATA_NAME

    assert (
        load_cached_transcript(transcript_path, metadata_path, original_metadata)
        == _cached_transcript()
    )

    audio.write_bytes(b"bravo")  # Same byte length, different speech content.
    changed_audio = build_asr_cache_metadata(audio, model="turbo", language="auto")
    assert changed_audio["input"]["audio"]["size"] == original_metadata["input"]["audio"]["size"]
    assert changed_audio["input"]["audio"]["sha256"] != original_metadata["input"]["audio"]["sha256"]
    assert load_cached_transcript(transcript_path, metadata_path, changed_audio) is None

    audio.write_bytes(b"alpha")
    changed_model = build_asr_cache_metadata(audio, model="large-v3", language="auto")
    changed_language = build_asr_cache_metadata(audio, model="turbo", language="en")
    assert load_cached_transcript(transcript_path, metadata_path, changed_model) is None
    assert load_cached_transcript(transcript_path, metadata_path, changed_language) is None


def test_legacy_transcript_without_metadata_is_a_safe_cache_miss(tmp_path: Path) -> None:
    audio = tmp_path / "speech.wav"
    audio.write_bytes(b"audio")
    transcript_path = tmp_path / "transcript.json"
    atomic_write_json(transcript_path, _cached_transcript().model_dump())
    expected = build_asr_cache_metadata(audio, model="turbo", language="auto")

    assert load_cached_transcript(
        transcript_path,
        tmp_path / ASR_CACHE_METADATA_NAME,
        expected,
    ) is None
