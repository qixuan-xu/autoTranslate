"""Turn raw Whisper output into stable, translation-friendly speech segments.

The production path rebuilds sentences from Whisper word timestamps instead of
treating Whisper's internal decoding windows as semantic boundaries.  The older
``merge_short_segments`` API remains available for callers without complete word
timestamps and for backwards compatibility.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, List, Mapping, Sequence

from backend.models.domain import Segment, Transcript
from backend.utils.files import atomic_write_json, read_json


logger = logging.getLogger(__name__)
_SENTENCE_END_RE = re.compile(r"[.!?。！？…]['\"’”》〉】）)]*$")
_NO_SPACE_BEFORE = set(",.!?;:%)]}。，！？；：、》〉】）’”")
_NO_SPACE_AFTER = set("([{《〈【（‘“")
DEFAULT_MIN_DURATION = 1.2
DEFAULT_MAX_DURATION = 8.0
DEFAULT_MAX_CHARS = 160
DEFAULT_MAX_GAP = 0.8
SEMANTIC_MAX_DURATION = 12.0
SEMANTIC_MAX_CHARS = 280
SEMANTIC_MAX_GAP = 0.8
SEGMENTER_ALGORITHM_VERSION = 4
SEGMENTED_TRANSCRIPT_CACHE_VERSION = 4

# A complete sentence just beyond the target is better translation input than
# two arbitrary clauses.  These small look-ahead allowances keep the effective
# limit around 10--12 seconds / 240--280 characters without splitting a sentence
# that ends a fraction of a second later.
_SENTENCE_LOOKAHEAD_SECONDS = 0.75
_SENTENCE_LOOKAHEAD_CHARS = 20
_SOFT_BREAK_RE = re.compile(r"[,;:，；：]['\"’”》〉】）)]*$|(?:--|—|–)$")

# Whisper can occasionally hallucinate several full sentences into only a few
# hundredths of a second at the end of an audio file. Keep this deliberately
# conservative: duration and density alone are not enough to reject a genuine
# short response such as "Yes."; the segment must also be low-confidence and
# contain enough linguistic material to be physically implausible.
_IMPLAUSIBLE_MAX_DURATION = 0.75
_IMPLAUSIBLE_MAX_CONFIDENCE = 0.10
_IMPLAUSIBLE_MIN_VISIBLE_CHARS = 12
_IMPLAUSIBLE_MIN_WORDS = 3
_IMPLAUSIBLE_CHARS_PER_SECOND = 24.0
_IMPLAUSIBLE_WORDS_PER_SECOND = 7.0

# Credits removal is intentionally narrower than generic text classification:
# it only activates in the final 10% after an explicit patron/supporter/member
# thanks cue, and only when the following segment actually looks like a dense
# list of names.
_TAIL_CREDITS_START_RATIO = 0.90
_TAIL_CREDITS_MAX_GAP = 1.5
_TAIL_CREDITS_INTRO_RE = re.compile(
    r"\b(?:special\s+thanks|thanks)\s+to\s+(?:my|our)\s+"
    r"(?:patrons?|supporters?|members?)\b",
    flags=re.IGNORECASE,
)
_CREDIT_CONNECTORS = {
    "and",
    "de",
    "del",
    "der",
    "di",
    "of",
    "the",
    "van",
    "von",
}


def default_segmenter_parameters() -> dict[str, float | int]:
    """Return a new, serializable copy of production semantic parameters."""

    return {
        "min_duration": DEFAULT_MIN_DURATION,
        "max_duration": SEMANTIC_MAX_DURATION,
        "max_chars": SEMANTIC_MAX_CHARS,
        "max_gap": SEMANTIC_MAX_GAP,
    }


def _normalized_parameters(
    parameters: Mapping[str, float | int] | None,
) -> dict[str, float | int]:
    normalized = default_segmenter_parameters()
    if parameters is not None:
        unknown = set(parameters) - set(normalized)
        if unknown:
            raise ValueError(f"unknown segmenter parameters: {sorted(unknown)}")
        normalized.update(parameters)
    normalized = {
        "min_duration": float(normalized["min_duration"]),
        "max_duration": float(normalized["max_duration"]),
        "max_chars": int(normalized["max_chars"]),
        "max_gap": float(normalized["max_gap"]),
    }
    _validate_parameters(**normalized)
    return normalized


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _segmenter_cache_input(
    source: Transcript,
    parameters: Mapping[str, float | int] | None,
) -> dict[str, Any]:
    source_payload = source.model_dump(mode="json")
    return {
        "algorithm": "semantic_word_sentence_segments",
        "algorithm_version": SEGMENTER_ALGORITHM_VERSION,
        "source_transcript_sha256": _canonical_json_sha256(source_payload),
        "parameters": _normalized_parameters(parameters),
    }


def load_segmented_transcript_cache(
    path: Path,
    source: Transcript,
    *,
    parameters: Mapping[str, float | int] | None = None,
) -> Transcript | None:
    """Load a segmented transcript only when source and merge options match."""

    if not path.is_file():
        return None
    expected_input = _segmenter_cache_input(source, parameters)
    try:
        payload = read_json(path)
        if payload.get("version") != SEGMENTED_TRANSCRIPT_CACHE_VERSION:
            return None
        if payload.get("input") != expected_input:
            return None
        if payload.get("input_fingerprint") != _canonical_json_sha256(expected_input):
            return None
        transcript_payload = payload["transcript"]
        if payload.get("transcript_fingerprint") != _canonical_json_sha256(
            transcript_payload
        ):
            return None
        return Transcript.model_validate(transcript_payload)
    except (AttributeError, KeyError, OSError, TypeError, ValueError) as exc:
        logger.warning("忽略损坏的分段缓存 %s: %s", path, exc)
        return None


def save_segmented_transcript_cache(
    path: Path,
    source: Transcript,
    segmented: Transcript,
    *,
    parameters: Mapping[str, float | int] | None = None,
) -> None:
    """Atomically store an internally versioned segmented-transcript cache."""

    cache_input = _segmenter_cache_input(source, parameters)
    transcript_payload = segmented.model_dump(mode="json")
    atomic_write_json(
        path,
        {
            "version": SEGMENTED_TRANSCRIPT_CACHE_VERSION,
            "input": cache_input,
            "input_fingerprint": _canonical_json_sha256(cache_input),
            "transcript": transcript_payload,
            "transcript_fingerprint": _canonical_json_sha256(transcript_payload),
        },
    )


def ends_semantic_sentence(text: str) -> bool:
    """Return whether *text* appears to end a complete sentence."""

    return bool(_SENTENCE_END_RE.search(text.strip()))


def implausible_segment_ids(segments: Sequence[Segment]) -> list[int]:
    """Return low-confidence, physically impossible short ASR segment IDs.

    This targets a known Whisper failure mode without treating all short speech
    as bad input. In particular, a short one- or two-word response is retained
    regardless of its duration.
    """

    rejected: list[int] = []
    for segment in segments:
        confidence = segment.confidence
        if (
            segment.duration > _IMPLAUSIBLE_MAX_DURATION
            or confidence is None
            or not math.isfinite(confidence)
            or confidence > _IMPLAUSIBLE_MAX_CONFIDENCE
        ):
            continue
        visible_chars = len(re.sub(r"\s+", "", segment.text))
        word_count = len(re.findall(r"\b[\w'-]+\b", segment.text, flags=re.UNICODE))
        if visible_chars < _IMPLAUSIBLE_MIN_VISIBLE_CHARS or word_count < _IMPLAUSIBLE_MIN_WORDS:
            continue
        duration = max(segment.duration, 1e-6)
        if (
            visible_chars / duration >= _IMPLAUSIBLE_CHARS_PER_SECOND
            or word_count / duration >= _IMPLAUSIBLE_WORDS_PER_SECOND
        ):
            rejected.append(segment.id)
    return rejected


def filter_implausible_segments(segments: Sequence[Segment]) -> list[Segment]:
    """Copy *segments* while dropping only known Whisper speed hallucinations."""

    rejected = set(implausible_segment_ids(segments))
    if rejected:
        logger.warning(
            "[SEGMENT] filtered %d low-confidence impossible-speed ASR segments ids=%s",
            len(rejected),
            sorted(rejected),
        )
    return [segment.model_copy(deep=True) for segment in segments if segment.id not in rejected]


def _credit_name_word_ratio(text: str) -> float:
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][\wÀ-ÖØ-öø-ÿ'-]*", text)
    candidates = [word for word in words if word.lower() not in _CREDIT_CONNECTORS]
    if not candidates:
        return 0.0
    name_like = sum(
        1
        for word in candidates
        if word[0].isupper() or word.isupper() or (len(word) == 1 and word.isalpha())
    )
    return name_like / len(candidates)


def _looks_like_dense_credit_names(text: str) -> bool:
    """Recognize a comma-dense, title-cased list rather than normal prose."""

    comma_count = text.count(",") + text.count("，")
    word_count = len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))
    if comma_count < 2 or word_count < 3:
        return False
    # Credit lists usually contain only one to four words per comma-separated
    # entry.  The capitalization check avoids treating ordinary enumerations as
    # names even after a matching thanks sentence.
    return (
        word_count / (comma_count + 1) <= 4.5
        and _credit_name_word_ratio(text) >= 0.60
    )


def _looks_like_final_credit_names(text: str) -> bool:
    """Recognize the short final ``Name, Name and Name.`` list tail."""

    word_count = len(re.findall(r"\b[\w'-]+\b", text, flags=re.UNICODE))
    has_list_join = bool(re.search(r"[,，]|\b(?:and|&)\b", text, flags=re.IGNORECASE))
    internal_sentence_end = bool(re.search(r"[.!?].+\S", text.strip()))
    return (
        2 <= word_count <= 12
        and has_list_join
        and not internal_sentence_end
        and _credit_name_word_ratio(text) >= 0.65
    )


def tail_credit_segment_ids(transcript: Transcript) -> list[int]:
    """Return a conservative trailing patron/supporter/member credits run.

    An explicit cue alone is insufficient: it must occur in the final 10% and
    be immediately followed by a dense list of names.  Scanning stops on the
    first normal sentence, so subsequent spoken content is always retained.
    """

    rejected = set(implausible_segment_ids(transcript.segments))
    segments = [segment for segment in transcript.segments if segment.id not in rejected]
    if len(segments) < 2:
        return []
    duration = transcript.duration or max((segment.end for segment in segments), default=0.0)
    if not duration or not math.isfinite(duration):
        return []

    tail_start = duration * _TAIL_CREDITS_START_RATIO
    for intro_index, intro in enumerate(segments[:-1]):
        if intro.start < tail_start or not _TAIL_CREDITS_INTRO_RE.search(intro.text):
            continue
        first_names = segments[intro_index + 1]
        if (
            first_names.start - intro.end > _TAIL_CREDITS_MAX_GAP
            or not _looks_like_dense_credit_names(first_names.text)
        ):
            continue

        credits = [intro.id, first_names.id]
        previous = first_names
        for candidate in segments[intro_index + 2 :]:
            if candidate.start - previous.end > _TAIL_CREDITS_MAX_GAP:
                break
            if _looks_like_dense_credit_names(candidate.text):
                credits.append(candidate.id)
                previous = candidate
                continue
            # A credit roll commonly ends with only two or three names, so its
            # last line is not comma-dense. Accept that narrow form only after a
            # dense run and very near the media end.
            if (
                candidate.end >= duration * 0.98
                and _looks_like_final_credit_names(candidate.text)
            ):
                credits.append(candidate.id)
            break
        return credits
    return []


def filter_tail_credits(transcript: Transcript) -> Transcript:
    """Copy *transcript* without a confidently identified trailing credit roll."""

    rejected = set(tail_credit_segment_ids(transcript))
    if rejected:
        selected = [segment for segment in transcript.segments if segment.id in rejected]
        logger.info(
            "[SEGMENT] filtered tail credits ids=%s time=%.2f-%.2f",
            sorted(rejected),
            min(segment.start for segment in selected),
            max(segment.end for segment in selected),
        )
    return transcript.model_copy(
        update={
            "segments": [
                segment.model_copy(deep=True)
                for segment in transcript.segments
                if segment.id not in rejected
            ]
        },
        deep=True,
    )


def _contains_cjk(character: str) -> bool:
    if not character:
        return False
    codepoint = ord(character)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def _join_text(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    if right[0] in _NO_SPACE_BEFORE or left[-1] in _NO_SPACE_AFTER:
        return left + right
    if _contains_cjk(left[-1]) or _contains_cjk(right[0]):
        return left + right
    return left + " " + right


def _text_from_words(words: Sequence[Any]) -> str:
    """Reconstruct readable text from Whisper-style word tokens.

    OpenAI Whisper normally includes leading whitespace in each token, while
    several compatible exporters return bare words.  Joining through
    :func:`_join_text` supports both representations and keeps punctuation
    attached to the preceding word.
    """

    text = ""
    for item in words:
        token = str(item.word).strip()
        if token:
            # Some Whisper tokenizers split hyphenated names and apostrophe
            # suffixes (``Kai`` + ``-shek``, ``Yan`` + ``'an``).
            if text and token.startswith(("-", "'", "’")):
                text += token
            else:
                text = _join_text(text, token)
    return text


def _normalized_visible_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _has_complete_word_timestamps(segments: Sequence[Segment]) -> bool:
    """Return whether every retained segment has a faithful timed-word stream."""

    previous_start = -math.inf
    for segment in segments:
        if not segment.words:
            return False
        if _normalized_visible_text(_text_from_words(segment.words)) != (
            _normalized_visible_text(segment.text)
        ):
            return False
        for word in segment.words:
            if (
                not word.word.strip()
                or not math.isfinite(word.start)
                or not math.isfinite(word.end)
                or word.start < 0
                or word.end <= word.start
                or word.start + 1e-3 < previous_start
            ):
                return False
            previous_start = word.start
    return True


def _word_range_text(words: Sequence[Any], start: int, end: int) -> str:
    return _text_from_words(words[start:end])


def _word_range_duration(words: Sequence[Any], start: int, end: int) -> float:
    return max(0.0, words[end - 1].end - words[start].start)


def _word_range_fits(
    words: Sequence[Any],
    start: int,
    end: int,
    *,
    max_duration: float,
    max_chars: int,
) -> bool:
    return (
        _word_range_duration(words, start, end) <= max_duration + 1e-9
        and len(_word_range_text(words, start, end)) <= max_chars
    )


def _next_semantic_boundary(
    words: Sequence[Any],
    start: int,
    *,
    max_duration: float,
    max_chars: int,
    max_gap: float,
) -> int:
    """Return the exclusive word boundary for the next semantic unit."""

    last_soft_break: int | None = None
    index = start
    while index < len(words):
        if index > start and words[index].start - words[index - 1].end > max_gap:
            return index

        end = index + 1
        text = _word_range_text(words, start, end)
        duration = _word_range_duration(words, start, end)
        within_target = duration <= max_duration and len(text) <= max_chars
        if within_target:
            if ends_semantic_sentence(words[index].word):
                return end
            if _SOFT_BREAK_RE.search(words[index].word.strip()):
                last_soft_break = end
            index += 1
            continue

        # Prefer a complete sentence that finishes only slightly beyond the
        # target.  Stop looking at pauses because they are meaningful speech
        # boundaries even when punctuation is missing.
        lookahead = index
        while lookahead < len(words):
            if (
                lookahead > start
                and words[lookahead].start - words[lookahead - 1].end > max_gap
            ):
                break
            candidate_end = lookahead + 1
            candidate_duration = _word_range_duration(words, start, candidate_end)
            candidate_chars = len(_word_range_text(words, start, candidate_end))
            if (
                candidate_duration > max_duration + _SENTENCE_LOOKAHEAD_SECONDS
                or candidate_chars > max_chars + _SENTENCE_LOOKAHEAD_CHARS
            ):
                break
            if ends_semantic_sentence(words[lookahead].word):
                return candidate_end
            lookahead += 1

        if last_soft_break is not None:
            return last_soft_break
        # Always make progress, even if a single unusual token exceeds a limit.
        return max(start + 1, index)

    return len(words)


def _initial_semantic_ranges(
    words: Sequence[Any],
    *,
    max_duration: float,
    max_chars: int,
    max_gap: float,
) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < len(words):
        end = _next_semantic_boundary(
            words,
            start,
            max_duration=max_duration,
            max_chars=max_chars,
            max_gap=max_gap,
        )
        ranges.append((start, end))
        start = end
    return ranges


def _merge_short_word_ranges(
    words: Sequence[Any],
    ranges: Sequence[tuple[int, int]],
    *,
    min_duration: float,
    max_duration: float,
    max_chars: int,
    max_gap: float,
) -> list[tuple[int, int]]:
    """Attach very short utterances to the most natural adjacent unit."""

    merged: list[tuple[int, int]] = []
    index = 0
    while index < len(ranges):
        start, end = ranges[index]
        if _word_range_duration(words, start, end) < min_duration:
            if index + 1 < len(ranges):
                following_end = ranges[index + 1][1]
                gap = words[end].start - words[end - 1].end
                if gap <= max_gap and _word_range_fits(
                    words,
                    start,
                    following_end,
                    max_duration=max_duration,
                    max_chars=max_chars,
                ):
                    merged.append((start, following_end))
                    index += 2
                    continue
            if merged:
                previous_start, _ = merged[-1]
                gap = words[start].start - words[start - 1].end
                if gap <= max_gap and _word_range_fits(
                    words,
                    previous_start,
                    end,
                    max_duration=max_duration,
                    max_chars=max_chars,
                ):
                    merged[-1] = (previous_start, end)
                    index += 1
                    continue
        merged.append((start, end))
        index += 1
    return merged


def _confidence_from_words(words: Sequence[Any]) -> float | None:
    probabilities = [
        float(word.probability)
        for word in words
        if word.probability is not None and math.isfinite(float(word.probability))
    ]
    if not probabilities:
        return None
    return sum(probabilities) / len(probabilities)


def semantic_sentence_segments(
    transcript: Transcript,
    *,
    min_duration: float = DEFAULT_MIN_DURATION,
    max_duration: float = SEMANTIC_MAX_DURATION,
    max_chars: int = SEMANTIC_MAX_CHARS,
    max_gap: float = SEMANTIC_MAX_GAP,
) -> Transcript:
    """Rebuild semantic sentences from the transcript's word timestamps.

    Whisper decoding-window IDs are intentionally discarded.  Result IDs are
    deterministic and contiguous, while start/end timestamps come from the
    exact first and last words in each new sentence.  If any retained source
    fragment lacks a faithful word stream, the established fragment merger is
    used as a safe fallback.
    """

    _validate_parameters(
        min_duration=min_duration,
        max_duration=max_duration,
        max_chars=max_chars,
        max_gap=max_gap,
    )
    filtered = filter_implausible_segments(transcript.segments)
    credit_ids = set(tail_credit_segment_ids(transcript))
    if credit_ids:
        selected = [segment for segment in filtered if segment.id in credit_ids]
        logger.info(
            "[SEGMENT] filtered tail credits ids=%s time=%.2f-%.2f",
            sorted(credit_ids),
            min(segment.start for segment in selected),
            max(segment.end for segment in selected),
        )
        filtered = [segment for segment in filtered if segment.id not in credit_ids]
    if not filtered:
        return transcript.model_copy(update={"segments": []}, deep=True)
    if not _has_complete_word_timestamps(filtered):
        logger.info(
            "[SEGMENT] complete word timestamps unavailable; using legacy merge"
        )
        filtered_transcript = transcript.model_copy(
            update={"segments": filtered},
            deep=True,
        )
        return merge_short_segments(
            filtered_transcript,
            min_duration=min_duration,
            max_duration=max_duration,
            max_chars=max_chars,
            max_gap=max_gap,
        )

    words = [word.model_copy(deep=True) for segment in filtered for word in segment.words]
    ranges = _initial_semantic_ranges(
        words,
        max_duration=max_duration,
        max_chars=max_chars,
        max_gap=max_gap,
    )
    ranges = _merge_short_word_ranges(
        words,
        ranges,
        min_duration=min_duration,
        max_duration=max_duration,
        max_chars=max_chars,
        max_gap=max_gap,
    )
    segments = [
        Segment(
            id=segment_id,
            start=words[start].start,
            end=words[end - 1].end,
            text=_word_range_text(words, start, end),
            words=[word.model_copy(deep=True) for word in words[start:end]],
            confidence=_confidence_from_words(words[start:end]),
        )
        for segment_id, (start, end) in enumerate(ranges)
    ]
    return transcript.model_copy(update={"segments": segments}, deep=True)


def _weighted_confidence(left: Segment, right: Segment) -> float | None:
    weighted = []
    if left.confidence is not None:
        weighted.append((left.confidence, left.duration))
    if right.confidence is not None:
        weighted.append((right.confidence, right.duration))
    if not weighted:
        return None
    total_weight = sum(weight for _, weight in weighted)
    if total_weight <= 0:
        return sum(value for value, _ in weighted) / len(weighted)
    return sum(value * weight for value, weight in weighted) / total_weight


def _merge_pair(left: Segment, right: Segment) -> Segment:
    translated_parts = [value for value in (left.translated_text, right.translated_text) if value]
    translated_text = None
    if translated_parts:
        translated_text = translated_parts[0]
        for part in translated_parts[1:]:
            translated_text = _join_text(translated_text, part)

    return Segment(
        # The first source ID deliberately survives the merge.
        id=left.id,
        start=min(left.start, right.start),
        end=max(left.end, right.end),
        text=_join_text(left.text, right.text),
        words=[word.model_copy(deep=True) for word in (*left.words, *right.words)],
        confidence=_weighted_confidence(left, right),
        translated_text=translated_text,
        # Cached TTS belongs to the old segmentation and must not be reused.
        tts_file=None,
        tts_duration=None,
    )


def _can_merge(
    left: Segment,
    right: Segment,
    *,
    max_duration: float,
    max_chars: int,
    max_gap: float,
) -> bool:
    gap = right.start - left.end
    span = max(left.end, right.end) - min(left.start, right.start)
    combined_text = _join_text(left.text, right.text)
    return gap <= max_gap and span <= max_duration and len(combined_text) <= max_chars


def _validate_parameters(
    *,
    min_duration: float,
    max_duration: float,
    max_chars: int,
    max_gap: float,
) -> None:
    if min_duration < 0:
        raise ValueError("min_duration must be non-negative")
    if max_duration <= 0 or max_duration < min_duration:
        raise ValueError("max_duration must be at least min_duration")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if max_gap < 0:
        raise ValueError("max_gap must be non-negative")


def merge_segment_list(
    segments: Sequence[Segment],
    *,
    min_duration: float = DEFAULT_MIN_DURATION,
    max_duration: float = DEFAULT_MAX_DURATION,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_gap: float = DEFAULT_MAX_GAP,
) -> List[Segment]:
    """Merge short or semantically incomplete adjacent ASR fragments.

    Input order is preserved.  A group keeps the ID and start time of its first
    segment, its end covers the complete group, and all word timestamps survive.
    The function never mutates the caller's models.
    """

    _validate_parameters(
        min_duration=min_duration,
        max_duration=max_duration,
        max_chars=max_chars,
        max_gap=max_gap,
    )
    filtered_segments = filter_implausible_segments(segments)
    if not filtered_segments:
        return []

    result: List[Segment] = []
    current = filtered_segments[0]

    for following in filtered_segments[1:]:
        eligible = _can_merge(
            current,
            following,
            max_duration=max_duration,
            max_chars=max_chars,
            max_gap=max_gap,
        )
        needs_context = current.duration < min_duration or not ends_semantic_sentence(current.text)
        if eligible and needs_context:
            current = _merge_pair(current, following)
            continue
        result.append(current)
        current = following

    # A short final fragment has no following context.  Attach it backwards when
    # that remains within the same conservative duration/gap bounds.
    if (
        result
        and current.duration < min_duration
        and _can_merge(
            result[-1],
            current,
            max_duration=max_duration,
            max_chars=max_chars,
            max_gap=max_gap,
        )
    ):
        result[-1] = _merge_pair(result[-1], current)
    else:
        result.append(current)

    return result


def merge_short_segments(
    transcript: Transcript,
    *,
    min_duration: float = DEFAULT_MIN_DURATION,
    max_duration: float = DEFAULT_MAX_DURATION,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_gap: float = DEFAULT_MAX_GAP,
) -> Transcript:
    """Return a transcript with translation-friendly segments."""

    merged = merge_segment_list(
        transcript.segments,
        min_duration=min_duration,
        max_duration=max_duration,
        max_chars=max_chars,
        max_gap=max_gap,
    )
    return transcript.model_copy(update={"segments": merged}, deep=True)


# A concise alias for pipeline call sites.
merge_segments = merge_short_segments


__all__ = [
    "default_segmenter_parameters",
    "ends_semantic_sentence",
    "filter_implausible_segments",
    "filter_tail_credits",
    "implausible_segment_ids",
    "load_segmented_transcript_cache",
    "merge_segment_list",
    "merge_segments",
    "merge_short_segments",
    "save_segmented_transcript_cache",
    "semantic_sentence_segments",
    "tail_credit_segment_ids",
]
