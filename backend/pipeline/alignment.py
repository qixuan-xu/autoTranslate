"""Pure planning helpers for time-aligned dubbed audio.

This module deliberately performs no file I/O.  It tells the audio pipeline where
each clip belongs and when a clip should be padded, gently sped up, or sent back
for a shorter translation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Literal, Mapping, Optional, Sequence

from backend.models.domain import Segment


DurationAction = Literal["pad", "speed_up", "compress_text"]


@dataclass(frozen=True)
class DurationMatchPlan:
    """A non-destructive strategy for fitting one TTS clip into its slot."""

    generated_duration: float
    target_duration: float
    ratio: float
    action: DurationAction
    tts_speed: float
    atempo_factor: float
    expected_duration: float
    padding_duration: float
    overflow_duration: float
    needs_text_compression: bool


@dataclass(frozen=True)
class TimelinePlacement:
    """Sample-accurate placement information for one segment."""

    segment_id: int
    start: float
    slot_end: float
    source_duration: float
    rendered_duration: float
    start_sample: int
    end_sample: int
    slot_end_sample: int
    leading_silence_samples: int
    padding_after_samples: int
    overlap_samples: int
    match: DurationMatchPlan

    @property
    def target_duration(self) -> float:
        return self.slot_end - self.start


def _finite_nonnegative(value: float, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return result


def _seconds_to_samples(seconds: float, sample_rate: int) -> int:
    return int(seconds * sample_rate + 0.5)


def plan_duration_match(
    generated_duration: float,
    target_duration: float,
    *,
    mild_overrun_ratio: float = 1.15,
    max_tts_speed: float = 1.10,
    max_atempo: float = 1.06,
) -> DurationMatchPlan:
    """Plan a natural duration correction.

    Short clips retain normal speed and leave silence.  Clips up to 15 percent
    long first use CosyVoice's speed control, with only a small residual FFmpeg
    ``atempo`` correction.  Larger overruns are explicitly returned for text
    compression instead of applying an unnatural speed-up.
    """

    generated = _finite_nonnegative(generated_duration, "generated_duration")
    target = _finite_nonnegative(target_duration, "target_duration")
    if target <= 0:
        raise ValueError("target_duration must be positive")
    if mild_overrun_ratio <= 1:
        raise ValueError("mild_overrun_ratio must be greater than 1")
    if not 1 <= max_tts_speed <= mild_overrun_ratio:
        raise ValueError("max_tts_speed must be between 1 and mild_overrun_ratio")
    if not 1 <= max_atempo <= mild_overrun_ratio:
        raise ValueError("max_atempo must be between 1 and mild_overrun_ratio")

    ratio = generated / target
    if ratio <= 1:
        return DurationMatchPlan(
            generated_duration=generated,
            target_duration=target,
            ratio=ratio,
            action="pad",
            tts_speed=1.0,
            atempo_factor=1.0,
            expected_duration=generated,
            padding_duration=target - generated,
            overflow_duration=0.0,
            needs_text_compression=False,
        )

    if ratio <= mild_overrun_ratio + 1e-9:
        tts_speed = min(ratio, max_tts_speed)
        residual_factor = ratio / tts_speed
        # The defaults guarantee this is gentle.  Custom limits may be tighter;
        # in that case do not hide the remaining excess.
        atempo_factor = min(residual_factor, max_atempo)
        expected = generated / (tts_speed * atempo_factor)
        overflow = max(0.0, expected - target)
        return DurationMatchPlan(
            generated_duration=generated,
            target_duration=target,
            ratio=ratio,
            action="speed_up",
            tts_speed=tts_speed,
            atempo_factor=atempo_factor,
            expected_duration=expected,
            padding_duration=max(0.0, target - expected),
            overflow_duration=overflow,
            needs_text_compression=overflow > 1e-6,
        )

    return DurationMatchPlan(
        generated_duration=generated,
        target_duration=target,
        ratio=ratio,
        action="compress_text",
        tts_speed=1.0,
        atempo_factor=1.0,
        expected_duration=generated,
        padding_duration=0.0,
        overflow_duration=generated - target,
        needs_text_compression=True,
    )


def calculate_atempo_factor(
    generated_duration: float,
    target_duration: float,
    *,
    max_speedup: float = 1.06,
) -> float:
    """Return a conservative final-pass FFmpeg atempo factor.

    This helper never slows a short clip (silence is preferable) and never exceeds
    ``max_speedup``.  Use :func:`plan_duration_match` to detect whether translation
    compression or a CosyVoice speed retry is required.
    """

    generated = _finite_nonnegative(generated_duration, "generated_duration")
    target = _finite_nonnegative(target_duration, "target_duration")
    if target <= 0:
        raise ValueError("target_duration must be positive")
    if max_speedup < 1:
        raise ValueError("max_speedup must be at least 1")
    return min(max(generated / target, 1.0), max_speedup)


def atempo_filter_chain(factor: float) -> List[float]:
    """Split a factor into values accepted by FFmpeg's ``atempo`` filter.

    Current alignment policy normally produces one value close to 1.  Splitting
    here makes the low-level helper safe if it is reused for a wider valid factor.
    """

    value = float(factor)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("factor must be a finite positive number")

    factors: List[float] = []
    while value > 2.0:
        factors.append(2.0)
        value /= 2.0
    while value < 0.5:
        factors.append(0.5)
        value /= 0.5
    if not math.isclose(value, 1.0, rel_tol=0, abs_tol=1e-12) or not factors:
        factors.append(value)
    return factors


def build_atempo_filter(factor: float) -> str:
    """Return an FFmpeg audio-filter expression without invoking a shell."""

    return ",".join(f"atempo={item:.6f}" for item in atempo_filter_chain(factor))


def calculate_timeline_placements(
    segments: Sequence[Segment],
    *,
    tts_durations: Optional[Mapping[int, float]] = None,
    sample_rate: int = 24_000,
    mild_overrun_ratio: float = 1.15,
) -> List[TimelinePlacement]:
    """Calculate absolute placements; clips are never concatenated back-to-back.

    ``start_sample`` always derives from the source segment's absolute timestamp,
    so silence in the original remains silence and one long clip cannot shift all
    following speech.  ``overlap_samples`` flags source overlaps or an unresolved
    severe overrun for the mixer.
    """

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    duration_overrides: Mapping[int, float] = tts_durations or {}
    ordered = sorted(segments, key=lambda segment: (segment.start, segment.id))
    placements: List[TimelinePlacement] = []
    previous_rendered_end_sample = 0

    for segment in ordered:
        if segment.id in duration_overrides:
            source_duration = duration_overrides[segment.id]
        elif segment.tts_duration is not None:
            source_duration = segment.tts_duration
        else:
            source_duration = segment.duration
        source_duration = _finite_nonnegative(source_duration, f"segment {segment.id} duration")
        match = plan_duration_match(
            source_duration,
            segment.duration,
            mild_overrun_ratio=mild_overrun_ratio,
        )

        start_sample = _seconds_to_samples(segment.start, sample_rate)
        slot_end_sample = _seconds_to_samples(segment.end, sample_rate)
        rendered_samples = _seconds_to_samples(match.expected_duration, sample_rate)
        end_sample = start_sample + rendered_samples
        leading_silence = max(0, start_sample - previous_rendered_end_sample)
        overlap = max(0, previous_rendered_end_sample - start_sample)
        padding_after = max(0, slot_end_sample - end_sample)

        placements.append(
            TimelinePlacement(
                segment_id=segment.id,
                start=segment.start,
                slot_end=segment.end,
                source_duration=source_duration,
                rendered_duration=match.expected_duration,
                start_sample=start_sample,
                end_sample=end_sample,
                slot_end_sample=slot_end_sample,
                leading_silence_samples=leading_silence,
                padding_after_samples=padding_after,
                overlap_samples=overlap,
                match=match,
            )
        )
        previous_rendered_end_sample = max(previous_rendered_end_sample, end_sample)

    return placements


def calculate_timeline_total_samples(
    placements: Sequence[TimelinePlacement],
    *,
    sample_rate: int = 24_000,
    media_duration: Optional[float] = None,
) -> int:
    """Calculate output length, exactly matching original media when given.

    A placement that extends beyond that boundary must first be shortened according
    to its match plan; the final PCM builder should trim at this returned length.
    """

    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    planned_end = max(
        (max(placement.end_sample, placement.slot_end_sample) for placement in placements),
        default=0,
    )
    if media_duration is None:
        return planned_end
    duration = _finite_nonnegative(media_duration, "media_duration")
    return _seconds_to_samples(duration, sample_rate)


def duration_plans_by_id(
    segments: Sequence[Segment],
    *,
    tts_durations: Optional[Mapping[int, float]] = None,
) -> Dict[int, DurationMatchPlan]:
    """Convenience mapping used by resumable per-segment TTS stages."""

    return {
        placement.segment_id: placement.match
        for placement in calculate_timeline_placements(
            segments,
            tts_durations=tts_durations,
        )
    }


__all__ = [
    "DurationAction",
    "DurationMatchPlan",
    "TimelinePlacement",
    "atempo_filter_chain",
    "build_atempo_filter",
    "calculate_atempo_factor",
    "calculate_timeline_placements",
    "calculate_timeline_total_samples",
    "duration_plans_by_id",
    "plan_duration_match",
]
