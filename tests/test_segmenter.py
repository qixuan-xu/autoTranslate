import pytest

from backend.models.domain import Segment, Transcript, WordTimestamp
from backend.pipeline.segmenter import merge_segment_list, merge_short_segments


def _word(value: str, start: float, end: float) -> WordTimestamp:
    return WordTimestamp(word=value, start=start, end=end, probability=0.9)


def test_merges_short_fragments_and_preserves_first_id_range_and_words() -> None:
    transcript = Transcript(
        language="en",
        duration=12.0,
        segments=[
            Segment(
                id=10,
                start=1.0,
                end=1.6,
                text="Today we",
                words=[_word("Today", 1.0, 1.3), _word("we", 1.35, 1.6)],
                confidence=0.8,
            ),
            Segment(
                id=11,
                start=1.7,
                end=3.0,
                text="talk about networks.",
                words=[_word("talk", 1.7, 1.95), _word("networks", 2.5, 2.95)],
                confidence=1.0,
            ),
        ],
    )

    merged = merge_short_segments(transcript)

    assert merged.language == "en"
    assert merged.duration == 12.0
    assert len(merged.segments) == 1
    segment = merged.segments[0]
    assert segment.id == 10
    assert (segment.start, segment.end) == (1.0, 3.0)
    assert segment.text == "Today we talk about networks."
    assert [word.word for word in segment.words] == ["Today", "we", "talk", "networks"]
    assert segment.confidence == pytest.approx((0.8 * 0.6 + 1.0 * 1.3) / 1.9)
    # Callers can safely retain the original raw transcript for cache/debugging.
    assert len(transcript.segments) == 2


def test_incomplete_sentence_gets_context_even_when_not_short() -> None:
    segments = [
        Segment(id=4, start=0, end=2, text="The important part is"),
        Segment(id=8, start=2.1, end=3.5, text="how the model learns."),
    ]

    merged = merge_segment_list(segments)

    assert [segment.id for segment in merged] == [4]
    assert merged[0].text == "The important part is how the model learns."


def test_complete_long_segment_stays_separate_but_short_tail_merges_back() -> None:
    segments = [
        Segment(id=2, start=0, end=2.5, text="This is already complete."),
        Segment(id=3, start=2.7, end=3.2, text="Thanks!"),
    ]

    merged = merge_segment_list(segments)

    assert len(merged) == 1
    assert merged[0].id == 2
    assert merged[0].text == "This is already complete. Thanks!"


def test_does_not_merge_across_long_pause_or_duration_limit() -> None:
    across_pause = merge_segment_list(
        [
            Segment(id=1, start=0, end=0.5, text="Wait"),
            Segment(id=2, start=2, end=3, text="for me."),
        ],
        max_gap=0.5,
    )
    over_duration = merge_segment_list(
        [
            Segment(id=1, start=0, end=1, text="Although"),
            Segment(id=2, start=1, end=9, text="this continues."),
        ],
        max_duration=8,
    )

    assert [segment.id for segment in across_pause] == [1, 2]
    assert [segment.id for segment in over_duration] == [1, 2]


def test_merging_invalidates_old_tts_cache_and_joins_existing_translation() -> None:
    merged = merge_segment_list(
        [
            Segment(
                id=1,
                start=0,
                end=0.5,
                text="Hello",
                translated_text="你好",
                tts_file="old.wav",
                tts_duration=0.4,
            ),
            Segment(id=2, start=0.5, end=1.5, text="world.", translated_text="世界。"),
        ]
    )[0]

    assert merged.translated_text == "你好世界。"
    assert merged.tts_file is None
    assert merged.tts_duration is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"min_duration": -1},
        {"min_duration": 2, "max_duration": 1},
        {"max_chars": 0},
        {"max_gap": -0.1},
    ],
)
def test_rejects_invalid_merge_configuration(kwargs) -> None:
    with pytest.raises(ValueError):
        merge_segment_list([], **kwargs)
