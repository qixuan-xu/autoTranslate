from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from backend.config import Settings
from backend.models.domain import Segment, Transcript, WordTimestamp
from backend.utils.files import atomic_write_json, read_json, temporary_output_path
from backend.utils.process import require_executable, run_process


logger = logging.getLogger(__name__)
_SENTENCE_END_RE = re.compile(r"[.!?]+(?:['\"’”)}\]]+)?$")
_OPENING_PUNCTUATION = "'\"‘“([{《〈【（"
_NO_SPACE_BEFORE = set(",.!?;:%)]}。，！？；：、》〉】）’”")
_REFERENCE_MINIMUM_USABLE = 2.5
_REFERENCE_MAX_INTERNAL_GAP = 1.5
_REFERENCE_CUT_CACHE_VERSION = 1
_REFERENCE_PADDING_BEFORE = 0.12
_REFERENCE_PADDING_AFTER = 0.08
_REFERENCE_SAMPLE_RATE = 16_000
_MEDIA_ARTIFACT_CACHE_VERSION = 1


@dataclass(frozen=True)
class _TimedWord:
    timestamp: WordTimestamp
    segment_id: int
    segment_confidence: float | None


@dataclass(frozen=True)
class _ReferenceCandidate:
    segment: Segment
    probability: float
    word_count: int
    speech_coverage: float
    max_gap: float
    complete_start: bool
    complete_end: bool


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
    expected_metadata: dict[str, Any] | None = None,
) -> bool:
    if not path.exists():
        return False
    if expected_metadata is not None:
        try:
            cached_metadata = read_json(media_cache_metadata_path(path))
        except (OSError, TypeError, ValueError):
            return False
        if cached_metadata != expected_metadata:
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
    cache_metadata: dict[str, Any] | None = None,
) -> Path:
    """Validate a temporary media file, then atomically publish it."""

    await validate_media_file(
        temporary,
        config,
        required_stream_types=required_stream_types,
    )
    os.replace(temporary, destination)
    if cache_metadata is not None:
        # The sidecar itself is published atomically, and only after the media
        # has passed ffprobe and been atomically moved into place.  A crash can
        # therefore cause at worst a conservative cache miss on the next run.
        atomic_write_json(media_cache_metadata_path(destination), cache_metadata)
    return destination


def media_cache_metadata_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.meta.json")


def file_content_fingerprint(path: Path) -> dict[str, int | str]:
    """Return a content identity and reject files that change while hashing."""

    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise RuntimeError(f"缓存输入文件不存在：{resolved}")
    before = resolved.stat()
    digest = hashlib.sha256()
    with resolved.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = resolved.stat()
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity:
        raise RuntimeError(f"计算缓存指纹期间文件发生变化：{resolved}")
    return {"size": after.st_size, "sha256": digest.hexdigest()}


def build_media_cache_metadata(
    kind: str,
    *,
    inputs: Any,
    parameters: Any,
) -> dict[str, Any]:
    """Build the exact sidecar contract for a deterministic media artifact."""

    return {
        "version": _MEDIA_ARTIFACT_CACHE_VERSION,
        "kind": kind,
        "inputs": inputs,
        "parameters": parameters,
    }


def _audio_extract_cache_metadata(
    source_fingerprint: dict[str, int | str],
    *,
    kind: str,
    channels: int,
    sample_rate: int,
) -> dict[str, Any]:
    return build_media_cache_metadata(
        f"audio-extract-{kind}",
        inputs={"video": source_fingerprint},
        parameters={
            "audio_stream": "0:a:0",
            "channels": channels,
            "sample_rate": sample_rate,
            "codec": "pcm_s16le",
            "disable_video": True,
        },
    )


async def extract_audio_tracks(video: Path, audio_dir: Path, config: Settings) -> tuple[Path, Path]:
    """Create a Whisper-friendly mono track and a mixing-quality stereo track."""

    audio_dir.mkdir(parents=True, exist_ok=True)
    speech = audio_dir / "speech.wav"
    original = audio_dir / "original.wav"
    binary = require_executable(config.ffmpeg_bin, "ffmpeg")
    source_fingerprint = file_content_fingerprint(video)
    speech_metadata = _audio_extract_cache_metadata(
        source_fingerprint,
        kind="speech",
        channels=1,
        sample_rate=16_000,
    )
    original_metadata = _audio_extract_cache_metadata(
        source_fingerprint,
        kind="original",
        channels=2,
        sample_rate=48_000,
    )
    if not await media_cache_is_valid(
        speech,
        config,
        required_stream_types={"audio"},
        expected_metadata=speech_metadata,
    ):
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
                cache_metadata=speech_metadata,
            )
        finally:
            temporary.unlink(missing_ok=True)
    if not await media_cache_is_valid(
        original,
        config,
        required_stream_types={"audio"},
        expected_metadata=original_metadata,
    ):
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
                cache_metadata=original_metadata,
            )
        finally:
            temporary.unlink(missing_ok=True)
    return speech, original


def _ends_sentence(text: str) -> bool:
    return bool(_SENTENCE_END_RE.search(text.strip()))


def _looks_like_sentence_start(text: str) -> bool:
    value = text.strip().lstrip(_OPENING_PUNCTUATION)
    if not value:
        return False
    first = value[0]
    return first.isupper() or first.isdigit() or ord(first) >= 0x3400


def _join_word_tokens(words: Iterable[WordTimestamp]) -> str:
    result = ""
    for word in words:
        raw = word.word
        value = raw.strip()
        if not value:
            continue
        if not result:
            result = value
        elif value[0] in _NO_SPACE_BEFORE:
            result += value
        elif raw[:1].isspace():
            result += " " + value
        else:
            result += " " + value
    return result.strip()


def _join_segment_text(segments: Iterable[Segment]) -> str:
    result = ""
    for segment in segments:
        value = segment.text.strip()
        if not value:
            continue
        if not result:
            result = value
        elif value[0] in _NO_SPACE_BEFORE:
            result += value
        else:
            result += " " + value
    return result


def _average_probability(words: list[_TimedWord]) -> float:
    values: list[float] = []
    for item in words:
        probability = item.timestamp.probability
        if probability is None or not math.isfinite(probability):
            probability = item.segment_confidence
        if probability is not None and math.isfinite(probability):
            values.append(min(1.0, max(0.0, probability)))
    return sum(values) / len(values) if values else 0.5


def _maximum_word_gap(words: list[_TimedWord]) -> float:
    return max(
        (
            max(0.0, following.timestamp.start - current.timestamp.end)
            for current, following in zip(words, words[1:])
        ),
        default=0.0,
    )


def _word_candidates(
    transcript: Transcript,
    *,
    minimum: float,
    maximum: float,
) -> list[_ReferenceCandidate]:
    words = sorted(
        (
            _TimedWord(word.model_copy(deep=True), segment.id, segment.confidence)
            for segment in transcript.segments
            for word in segment.words
            if math.isfinite(word.start)
            and math.isfinite(word.end)
            and word.end > word.start
        ),
        key=lambda item: (item.timestamp.start, item.timestamp.end),
    )
    if not words:
        return []

    # Sentence ranges end only at explicit .?! word boundaries.  Adjacent full
    # sentences can then be combined into a natural 5–12 second voice prompt.
    sentences: list[tuple[int, int, bool]] = []
    sentence_start = 0
    for index, item in enumerate(words):
        if not _ends_sentence(item.timestamp.word):
            continue
        complete_start = sentence_start > 0 or _looks_like_sentence_start(
            words[sentence_start].timestamp.word
        )
        sentences.append((sentence_start, index, complete_start))
        sentence_start = index + 1

    candidates: list[_ReferenceCandidate] = []
    for first_sentence in range(len(sentences)):
        first_word, _, complete_start = sentences[first_sentence]
        for last_sentence in range(first_sentence, len(sentences)):
            _, last_word, _ = sentences[last_sentence]
            selected_words = words[first_word : last_word + 1]
            start = selected_words[0].timestamp.start
            end = selected_words[-1].timestamp.end
            duration = end - start
            if duration > maximum:
                break
            if duration < minimum:
                continue
            max_gap = _maximum_word_gap(selected_words)
            if max_gap > _REFERENCE_MAX_INTERNAL_GAP:
                continue
            timestamps = [item.timestamp.model_copy(deep=True) for item in selected_words]
            text = _join_word_tokens(timestamps)
            if not _ends_sentence(text):
                continue
            probability = _average_probability(selected_words)
            voiced = sum(item.end - item.start for item in timestamps)
            candidates.append(
                _ReferenceCandidate(
                    segment=Segment(
                        id=selected_words[0].segment_id,
                        start=start,
                        end=end,
                        text=text,
                        words=timestamps,
                        confidence=probability,
                    ),
                    probability=probability,
                    word_count=len(timestamps),
                    speech_coverage=min(1.0, voiced / max(duration, 0.001)),
                    max_gap=max_gap,
                    complete_start=complete_start,
                    complete_end=True,
                )
            )
    return candidates


def _segment_candidates(
    transcript: Transcript,
    *,
    minimum: float,
    maximum: float,
) -> list[_ReferenceCandidate]:
    segments = sorted(transcript.segments, key=lambda item: (item.start, item.end))
    candidates: list[_ReferenceCandidate] = []
    for first_index, first in enumerate(segments):
        selected: list[Segment] = []
        max_gap = 0.0
        for last_index in range(first_index, len(segments)):
            following = segments[last_index]
            if selected:
                gap = max(0.0, following.start - selected[-1].end)
                max_gap = max(max_gap, gap)
                if gap > _REFERENCE_MAX_INTERNAL_GAP:
                    break
            selected.append(following)
            duration = max(item.end for item in selected) - min(item.start for item in selected)
            if duration > maximum:
                break
            if duration < _REFERENCE_MINIMUM_USABLE:
                continue
            probabilities = [
                (item.confidence, item.duration)
                for item in selected
                if item.confidence is not None and math.isfinite(item.confidence)
            ]
            probability = 0.5
            if probabilities:
                weight = sum(item_duration for _, item_duration in probabilities)
                probability = (
                    sum(value * item_duration for value, item_duration in probabilities) / weight
                    if weight > 0
                    else sum(value for value, _ in probabilities) / len(probabilities)
                )
            text = _join_segment_text(selected)
            words = [
                word.model_copy(deep=True)
                for item in selected
                for word in item.words
            ]
            complete_start = (
                first_index > 0 and _ends_sentence(segments[first_index - 1].text)
            ) or _looks_like_sentence_start(first.text)
            complete_end = _ends_sentence(selected[-1].text)
            candidates.append(
                _ReferenceCandidate(
                    segment=Segment(
                        id=first.id,
                        start=first.start,
                        end=selected[-1].end,
                        text=text,
                        words=words,
                        confidence=probability,
                    ),
                    probability=min(1.0, max(0.0, probability)),
                    word_count=max(len(words), len(re.findall(r"\b[\w'-]+\b", text))),
                    speech_coverage=0.55,
                    max_gap=max_gap,
                    complete_start=complete_start,
                    complete_end=complete_end,
                )
            )

        # A long original segment cannot be shortened safely without word
        # boundaries, but retaining it is still better than a sub-2.5s prompt.
        if first.duration > maximum:
            candidates.append(
                _ReferenceCandidate(
                    segment=first.model_copy(deep=True),
                    probability=first.confidence if first.confidence is not None else 0.5,
                    word_count=len(re.findall(r"\b[\w'-]+\b", first.text)),
                    speech_coverage=0.5,
                    max_gap=0.0,
                    complete_start=_looks_like_sentence_start(first.text),
                    complete_end=_ends_sentence(first.text),
                )
            )
    return candidates


def _candidate_score(
    candidate: _ReferenceCandidate,
    *,
    total_duration: float,
    minimum: float,
    maximum: float,
) -> tuple[float, float, float]:
    segment = candidate.segment
    duration = segment.duration
    target = (minimum + maximum) / 2.0
    half_window = max((maximum - minimum) / 2.0, 0.1)
    duration_fit = max(0.0, 1.0 - abs(duration - target) / half_window)
    density = candidate.word_count / max(duration, 0.1)
    if 1.2 <= density <= 3.8:
        density_fit = 1.0
    elif density < 1.2:
        density_fit = max(0.0, density / 1.2)
    else:
        density_fit = max(0.0, 1.0 - (density - 3.8) / 3.8)

    separator_count = len(re.findall(r"[,，;；/|]", segment.text))
    separator_ratio = separator_count / max(candidate.word_count, 1)
    list_penalty = max(0, separator_count - 1) * 0.28
    list_penalty += max(0.0, separator_ratio - 0.08) * 4.0

    midpoint_ratio = ((segment.start + segment.end) / 2.0) / max(total_duration, 0.1)
    tail_penalty = max(0.0, (midpoint_ratio - 0.72) / 0.28) * 2.4
    if segment.start / max(total_duration, 0.1) >= 0.9:
        tail_penalty += 0.8

    score = (
        candidate.probability * 2.0
        + duration_fit * 1.2
        + density_fit * 0.55
        + candidate.speech_coverage * 0.45
        - max(0.0, candidate.max_gap - 0.45) * 0.6
        - list_penalty
        - tail_penalty
    )
    score += 0.45 if candidate.complete_start else -0.8
    score += 0.55 if candidate.complete_end else -1.2
    in_window = 1.0 if minimum <= duration <= maximum else 0.0
    # Prefer an earlier span only as a final deterministic tie-breaker.
    return (in_window, score, -segment.start)


def select_reference_segment(
    transcript: Transcript,
    *,
    minimum: float = 5.0,
    maximum: float = 12.0,
) -> Segment:
    """Pick a complete, clear reference span while avoiding end-credit lists."""

    if not transcript.segments:
        raise RuntimeError("转写结果为空，无法自动选择声音参考")
    if minimum < _REFERENCE_MINIMUM_USABLE or maximum < minimum:
        raise ValueError("reference duration bounds are invalid")

    total_duration = transcript.duration or max(
        (segment.end for segment in transcript.segments),
        default=0.0,
    )
    # Word timestamps provide real sentence boundaries and can join several
    # short Whisper fragments without guessing from segment edges.
    candidates = _word_candidates(transcript, minimum=minimum, maximum=maximum)
    if not candidates:
        candidates = _segment_candidates(transcript, minimum=minimum, maximum=maximum)
    if not candidates:
        raise RuntimeError("自动找到的参考语音太短（少于 2.5 秒），请手动上传 reference.wav")

    selected = max(
        candidates,
        key=lambda item: _candidate_score(
            item,
            total_duration=total_duration,
            minimum=minimum,
            maximum=maximum,
        ),
    ).segment
    if selected.duration < _REFERENCE_MINIMUM_USABLE:
        raise RuntimeError("自动找到的参考语音太短（少于 2.5 秒），请手动上传 reference.wav")
    return selected


async def cut_reference_audio(
    source_audio: Path,
    segment: Segment,
    output: Path,
    config: Settings,
) -> Path:
    source_audio = source_audio.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source_audio.is_file():
        raise RuntimeError(f"声音参考源不存在：{source_audio}")
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata_path = output.with_name(f"{output.name}.meta.json")
    source_before = source_audio.stat()
    source_digest = hashlib.sha256()
    with source_audio.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            source_digest.update(chunk)
    source_after = source_audio.stat()
    if (source_before.st_size, source_before.st_mtime_ns) != (
        source_after.st_size,
        source_after.st_mtime_ns,
    ):
        raise RuntimeError(f"读取期间声音参考源发生变化：{source_audio}")

    padding = min(_REFERENCE_PADDING_BEFORE, segment.start)
    start = max(0.0, segment.start - padding)
    duration = segment.end - start + _REFERENCE_PADDING_AFTER
    expected_metadata = {
        "version": _REFERENCE_CUT_CACHE_VERSION,
        "source": {
            "size": source_after.st_size,
            "sha256": source_digest.hexdigest(),
        },
        "selection": {
            "id": segment.id,
            "start": segment.start,
            "end": segment.end,
            "text": segment.text,
        },
        "parameters": {
            "padding_before": _REFERENCE_PADDING_BEFORE,
            "padding_after": _REFERENCE_PADDING_AFTER,
            "effective_start": start,
            "effective_duration": duration,
            "channels": 1,
            "sample_rate": _REFERENCE_SAMPLE_RATE,
            "codec": "pcm_s16le",
        },
    }
    try:
        cached_metadata = read_json(metadata_path)
    except (OSError, TypeError, ValueError):
        cached_metadata = None
    if cached_metadata == expected_metadata and await media_cache_is_valid(
        output,
        config,
        required_stream_types={"audio"},
    ):
        return output

    binary = require_executable(config.ffmpeg_bin, "ffmpeg")
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
                str(_REFERENCE_SAMPLE_RATE),
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
        # Publish the sidecar only after the new WAV has been validated and
        # atomically moved into place.  A failed recut can never bless stale data.
        atomic_write_json(metadata_path, expected_metadata)
    finally:
        temporary.unlink(missing_ok=True)
    return output
