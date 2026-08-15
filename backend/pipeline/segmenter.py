"""Utilities for turning raw ASR fragments into translation-friendly segments.

Whisper occasionally emits very small fragments.  Translating those fragments in
isolation loses context, so this module joins them without renumbering the first
segment in each group.  Keeping that ID stable is important because translation
and TTS cache files are keyed by it.
"""

from __future__ import annotations

import re
from typing import List, Sequence

from backend.models.domain import Segment, Transcript


_SENTENCE_END_RE = re.compile(r"[.!?。！？…]['\"’”》〉】）)]*$")
_NO_SPACE_BEFORE = set(",.!?;:%)]}。，！？；：、》〉】）’”")
_NO_SPACE_AFTER = set("([{《〈【（‘“")


def ends_semantic_sentence(text: str) -> bool:
    """Return whether *text* appears to end a complete sentence."""

    return bool(_SENTENCE_END_RE.search(text.strip()))


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


def merge_segment_list(
    segments: Sequence[Segment],
    *,
    min_duration: float = 1.2,
    max_duration: float = 8.0,
    max_chars: int = 160,
    max_gap: float = 0.8,
) -> List[Segment]:
    """Merge short or semantically incomplete adjacent ASR fragments.

    Input order is preserved.  A group keeps the ID and start time of its first
    segment, its end covers the complete group, and all word timestamps survive.
    The function never mutates the caller's models.
    """

    if min_duration < 0:
        raise ValueError("min_duration must be non-negative")
    if max_duration <= 0 or max_duration < min_duration:
        raise ValueError("max_duration must be at least min_duration")
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if max_gap < 0:
        raise ValueError("max_gap must be non-negative")
    if not segments:
        return []

    result: List[Segment] = []
    current = segments[0].model_copy(deep=True)

    for source in segments[1:]:
        following = source.model_copy(deep=True)
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
    min_duration: float = 1.2,
    max_duration: float = 8.0,
    max_chars: int = 160,
    max_gap: float = 0.8,
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
    "ends_semantic_sentence",
    "merge_segment_list",
    "merge_segments",
    "merge_short_segments",
]
