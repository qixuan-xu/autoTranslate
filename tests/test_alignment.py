import math

import pytest

from backend.models.domain import Segment
from backend.pipeline.alignment import (
    atempo_filter_chain,
    build_atempo_filter,
    calculate_atempo_factor,
    calculate_timeline_placements,
    calculate_timeline_total_samples,
    plan_duration_match,
)


def test_short_tts_keeps_natural_speed_and_pads_silence() -> None:
    plan = plan_duration_match(2.5, 4.0)

    assert plan.action == "pad"
    assert plan.tts_speed == 1.0
    assert plan.atempo_factor == 1.0
    assert plan.padding_duration == pytest.approx(1.5)
    assert not plan.needs_text_compression


def test_mild_overrun_prefers_tts_speed_then_gentle_atempo() -> None:
    plan = plan_duration_match(4.56, 4.0)  # 14% long

    assert plan.action == "speed_up"
    assert 1 < plan.tts_speed <= 1.10
    assert 1 <= plan.atempo_factor <= 1.06
    assert plan.expected_duration == pytest.approx(4.0)
    assert not plan.needs_text_compression


def test_severe_overrun_requests_text_compression_without_extreme_speed() -> None:
    plan = plan_duration_match(6.0, 4.0)

    assert plan.action == "compress_text"
    assert plan.needs_text_compression
    assert plan.tts_speed == 1.0
    assert plan.atempo_factor == 1.0
    assert plan.overflow_duration == pytest.approx(2.0)


def test_final_atempo_factor_is_conservatively_capped() -> None:
    assert calculate_atempo_factor(3.0, 4.0) == 1.0
    assert calculate_atempo_factor(4.1, 4.0) == pytest.approx(1.025)
    assert calculate_atempo_factor(8.0, 4.0) == 1.06


def test_atempo_chain_respects_ffmpeg_range_and_product() -> None:
    chain = atempo_filter_chain(4.5)

    assert all(0.5 <= item <= 2.0 for item in chain)
    assert math.prod(chain) == pytest.approx(4.5)
    assert build_atempo_filter(1.025) == "atempo=1.025000"


def test_timeline_uses_absolute_start_times_and_preserves_pauses() -> None:
    segments = [
        Segment(id=20, start=5, end=7, text="Second"),
        Segment(id=10, start=2, end=4, text="First"),
    ]

    placements = calculate_timeline_placements(
        segments,
        tts_durations={10: 1.0, 20: 1.5},
        sample_rate=1000,
    )

    assert [placement.segment_id for placement in placements] == [10, 20]
    first, second = placements
    assert (first.start_sample, first.end_sample) == (2000, 3000)
    assert first.leading_silence_samples == 2000
    assert first.padding_after_samples == 1000
    # The second clip starts at source time 5.0, not immediately after the first.
    assert second.start_sample == 5000
    assert second.leading_silence_samples == 2000
    assert calculate_timeline_total_samples(
        placements, sample_rate=1000, media_duration=10
    ) == 10_000


def test_unresolved_long_clip_flags_overlap_but_does_not_shift_next_start() -> None:
    segments = [
        Segment(id=1, start=0, end=1, text="Too verbose"),
        Segment(id=2, start=2, end=3, text="Still fixed"),
    ]

    first, second = calculate_timeline_placements(
        segments,
        tts_durations={1: 3.0, 2: 1.0},
        sample_rate=1000,
    )

    assert first.match.needs_text_compression
    assert first.end_sample == 3000
    assert second.start_sample == 2000
    assert second.overlap_samples == 1000
    # The finished dub track still follows the source video's exact boundary.
    assert calculate_timeline_total_samples(
        [first, second], sample_rate=1000, media_duration=2.5
    ) == 2500


@pytest.mark.parametrize(
    "generated,target",
    [(-1, 2), (1, 0), (float("inf"), 2)],
)
def test_duration_match_rejects_invalid_values(generated: float, target: float) -> None:
    with pytest.raises(ValueError):
        plan_duration_match(generated, target)
