import pytest
from pathlib import Path

from backend.models.domain import Segment, Transcript, WordTimestamp
from backend.pipeline.segmenter import (
    SEGMENTED_TRANSCRIPT_CACHE_VERSION,
    SEGMENTER_ALGORITHM_VERSION,
    default_segmenter_parameters,
    implausible_segment_ids,
    load_segmented_transcript_cache,
    merge_segment_list,
    merge_short_segments,
    save_segmented_transcript_cache,
    semantic_sentence_segments,
    tail_credit_segment_ids,
)
from backend.utils.files import read_json


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


def test_filters_actual_whisper_tail_hallucination_shapes_but_keeps_short_reply(
    caplog: pytest.LogCaptureFixture,
) -> None:
    segments = [
        Segment(
            id=126,
            start=501.06,
            end=501.42,
            text=(
                "These guys have found us in the city. I might find out what I am. "
                "You've gotta flow. I can't realize your heart, I am."
            ),
            confidence=0.017,
        ),
        Segment(
            id=134,
            start=501.42,
            end=501.98,
            text=(
                "Many times I might that. I wandered on the land of the churches. "
                "You'll know that the hospital has finally been locked through."
            ),
            confidence=0.017,
        ),
        Segment(
            id=138,
            start=501.98,
            end=502.04,
            text="I am not response to anything.",
            confidence=0.017,
        ),
        Segment(
            id=132,
            start=501.36,
            end=501.40,
            text="You've gotta flow.",
            confidence=0.017,
        ),
        Segment(
            id=139,
            start=502.10,
            end=502.22,
            text="Yes.",
            confidence=0.01,
        ),
    ]

    assert implausible_segment_ids(segments) == [126, 134, 138, 132]
    merged = merge_segment_list(segments, max_gap=0.01)

    assert [segment.id for segment in merged] == [139]
    assert merged[0].text == "Yes."
    assert "ids=[126, 132, 134, 138]" in caplog.text


def test_impossible_speed_filter_requires_low_confidence_and_substantive_text() -> None:
    segments = [
        Segment(
            id=1,
            start=0.0,
            end=0.1,
            text="This is genuine rapid speech.",
            confidence=0.8,
        ),
        Segment(id=2, start=0.2, end=0.25, text="No.", confidence=0.0),
        Segment(
            id=3,
            start=0.3,
            end=0.35,
            text="Confidence was not reported here.",
            confidence=None,
        ),
    ]

    assert implausible_segment_ids(segments) == []


def _tail_credits_transcript(*, intro_start: float = 91.0) -> Transcript:
    return Transcript(
        language="en",
        duration=100.0,
        segments=[
            Segment(
                id=10,
                start=86.0,
                end=89.0,
                text="This is the final historical conclusion.",
            ),
            Segment(
                id=11,
                start=intro_start,
                end=intro_start + 2.0,
                text="Special thanks to our patrons,",
            ),
            Segment(
                id=12,
                start=intro_start + 2.1,
                end=intro_start + 5.0,
                text="Alice Example, Bob Sample, Carol Person, Dana Name,",
            ),
            Segment(
                id=13,
                start=intro_start + 5.1,
                end=99.0,
                text="Evan Person, Frank Sample and Grace Example.",
            ),
        ],
    )


def test_tail_credit_filter_requires_tail_cue_and_dense_name_run() -> None:
    transcript = _tail_credits_transcript()

    assert tail_credit_segment_ids(transcript) == [11, 12, 13]


def test_tail_credit_filter_does_not_remove_early_or_unconfirmed_thanks() -> None:
    early = _tail_credits_transcript(intro_start=85.0)
    ordinary_tail = Transcript(
        language="en",
        duration=100.0,
        segments=[
            Segment(
                id=1,
                start=92.0,
                end=95.0,
                text="Special thanks to our members for making this research possible.",
            ),
            Segment(
                id=2,
                start=95.1,
                end=99.0,
                text="The final result follows from all of the evidence.",
            ),
        ],
    )
    ordinary_list = Transcript(
        language="en",
        duration=100.0,
        segments=[
            Segment(
                id=3,
                start=92.0,
                end=99.0,
                text="China, Japan, Korea, Vietnam, Thailand, and India.",
            )
        ],
    )

    assert tail_credit_segment_ids(early) == []
    assert tail_credit_segment_ids(ordinary_tail) == []
    assert tail_credit_segment_ids(ordinary_list) == []


def test_tail_credit_filter_stops_when_normal_complete_speech_resumes() -> None:
    transcript = Transcript(
        language="en",
        duration=100.0,
        segments=[
            Segment(
                id=1,
                start=90.0,
                end=92.0,
                text="Thanks to my supporters,",
            ),
            Segment(
                id=2,
                start=92.1,
                end=94.0,
                text="Alice Example, Bob Sample, Carol Person,",
            ),
            Segment(
                id=3,
                start=94.1,
                end=97.0,
                text="After that, we return to the main argument.",
            ),
            Segment(
                id=4,
                start=97.1,
                end=99.9,
                text="Dana Example, Evan Sample, Frank Person,",
            ),
        ],
    )

    assert tail_credit_segment_ids(transcript) == [1, 2]


def test_semantic_segmenter_filters_credits_before_word_coverage_check() -> None:
    transcript = Transcript(
        language="en",
        duration=100.0,
        segments=[
            Segment(
                id=10,
                start=86.0,
                end=89.0,
                text="Final historical conclusion.",
                words=[
                    _word("Final", 86.0, 87.0),
                    _word("historical", 87.0, 88.0),
                    _word("conclusion.", 88.0, 89.0),
                ],
            ),
            Segment(
                id=11,
                start=91.0,
                end=93.0,
                text="Special thanks to our patrons,",
            ),
            Segment(
                id=12,
                start=93.1,
                end=96.0,
                text="Alice Example, Bob Sample, Carol Person, Dana Name,",
            ),
            Segment(
                id=13,
                start=96.1,
                end=99.0,
                text="Evan Person, Frank Sample and Grace Example.",
            ),
        ],
    )

    segmented = semantic_sentence_segments(transcript)

    assert [segment.id for segment in segmented.segments] == [0]
    assert [segment.text for segment in segmented.segments] == [
        "Final historical conclusion."
    ]
    assert segmented.segments[0].end == 89.0


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


def test_semantic_segmenter_rebuilds_sentence_across_whisper_windows() -> None:
    transcript = Transcript(
        language="en",
        duration=5.0,
        segments=[
            Segment(
                id=17,
                start=0.2,
                end=1.8,
                text="The important part is",
                words=[
                    _word("The", 0.2, 0.5),
                    _word("important", 0.5, 0.9),
                    _word("part", 1.0, 1.3),
                    _word("is", 1.5, 1.8),
                ],
            ),
            Segment(
                id=42,
                start=1.9,
                end=3.5,
                text="how the model learns.",
                words=[
                    _word("how", 1.9, 2.2),
                    _word("the", 2.2, 2.4),
                    _word("model", 2.5, 2.9),
                    _word("learns.", 3.0, 3.5),
                ],
            ),
        ],
    )

    segmented = semantic_sentence_segments(transcript)

    assert segmented.language == "en"
    assert segmented.duration == 5.0
    assert len(segmented.segments) == 1
    sentence = segmented.segments[0]
    assert sentence.id == 0
    assert (sentence.start, sentence.end) == (0.2, 3.5)
    assert sentence.text == "The important part is how the model learns."
    assert [word.word for word in sentence.words] == [
        "The",
        "important",
        "part",
        "is",
        "how",
        "the",
        "model",
        "learns.",
    ]
    assert sentence.confidence == pytest.approx(0.9)


def test_semantic_segmenter_uses_punctuation_then_contiguous_ids() -> None:
    words = [
        _word("First", 0.0, 0.3),
        _word("sentence.", 0.3, 1.0),
        _word("Second", 1.2, 1.6),
        _word("sentence?", 1.6, 2.4),
        _word("Third", 2.6, 3.0),
        _word("sentence!", 3.0, 3.8),
    ]
    transcript = Transcript(
        language="en",
        segments=[
            Segment(
                id=100,
                start=0.0,
                end=3.8,
                text="First sentence. Second sentence? Third sentence!",
                words=words,
            )
        ],
    )

    segmented = semantic_sentence_segments(transcript, min_duration=0)

    assert [segment.id for segment in segmented.segments] == [0, 1, 2]
    assert [segment.text for segment in segmented.segments] == [
        "First sentence.",
        "Second sentence?",
        "Third sentence!",
    ]


def test_semantic_segmenter_splits_overlong_sentence_at_last_comma() -> None:
    words = [
        _word("This", 0.0, 0.8),
        _word("long", 0.8, 1.6),
        _word("clause,", 1.6, 2.4),
        _word("keeps", 2.4, 3.2),
        _word("going", 3.2, 4.0),
        _word("for", 4.0, 4.8),
        _word("several", 4.8, 5.6),
        _word("more", 5.6, 6.4),
        _word("words.", 6.4, 7.1),
    ]
    transcript = Transcript(
        language="en",
        segments=[
            Segment(
                id=9,
                start=0.0,
                end=7.1,
                text="This long clause, keeps going for several more words.",
                words=words,
            )
        ],
    )

    segmented = semantic_sentence_segments(
        transcript,
        min_duration=0,
        max_duration=4.0,
        max_chars=200,
    )

    assert [segment.text for segment in segmented.segments] == [
        "This long clause,",
        "keeps going for several more words.",
    ]
    assert segmented.segments[0].end == 2.4
    assert segmented.segments[1].start == 2.4


def test_semantic_segmenter_merges_very_short_sentence_forward() -> None:
    transcript = Transcript(
        language="en",
        segments=[
            Segment(
                id=4,
                start=0.0,
                end=2.6,
                text="Yes. We can begin.",
                words=[
                    _word("Yes.", 0.0, 0.4),
                    _word("We", 0.5, 0.8),
                    _word("can", 0.8, 1.2),
                    _word("begin.", 1.2, 2.6),
                ],
            )
        ],
    )

    segmented = semantic_sentence_segments(transcript)

    assert [segment.text for segment in segmented.segments] == ["Yes. We can begin."]
    assert segmented.segments[0].id == 0


def test_semantic_segmenter_filters_hallucination_before_word_coverage_check() -> None:
    transcript = Transcript(
        language="en",
        segments=[
            Segment(
                id=126,
                start=0.0,
                end=0.1,
                text="These impossible hallucinated words cannot fit here.",
                confidence=0.01,
                words=[],
            ),
            Segment(
                id=200,
                start=1.0,
                end=2.0,
                text="Real speech.",
                words=[_word("Real", 1.0, 1.4), _word("speech.", 1.4, 2.0)],
            ),
        ],
    )

    segmented = semantic_sentence_segments(transcript, min_duration=0)

    assert [segment.id for segment in segmented.segments] == [0]
    assert [segment.text for segment in segmented.segments] == ["Real speech."]


def test_semantic_segmenter_safely_falls_back_when_words_are_incomplete() -> None:
    transcript = Transcript(
        language="en",
        segments=[
            Segment(
                id=7,
                start=0.0,
                end=0.5,
                text="This is",
                words=[_word("This", 0.0, 0.25)],
            ),
            Segment(id=8, start=0.5, end=1.8, text="a sentence."),
        ],
    )

    segmented = semantic_sentence_segments(transcript)

    assert [segment.id for segment in segmented.segments] == [7]
    assert segmented.segments[0].text == "This is a sentence."


def _cache_source() -> Transcript:
    return Transcript(
        language="en",
        duration=4.0,
        segments=[
            Segment(id=1, start=0.0, end=0.6, text="This is"),
            Segment(id=2, start=0.7, end=2.0, text="a complete sentence."),
        ],
    )


def test_segmented_cache_round_trip_binds_source_and_parameters(tmp_path: Path) -> None:
    source = _cache_source()
    parameters = default_segmenter_parameters()
    segmented = merge_short_segments(source, **parameters)
    cache_path = tmp_path / "segmented_transcript.json"

    save_segmented_transcript_cache(
        cache_path,
        source,
        segmented,
        parameters=parameters,
    )

    assert (
        load_segmented_transcript_cache(
            cache_path,
            source,
            parameters=parameters,
        )
        == segmented
    )
    payload = read_json(cache_path)
    assert payload["version"] == SEGMENTED_TRANSCRIPT_CACHE_VERSION == 4
    assert payload["input"]["algorithm"] == "semantic_word_sentence_segments"
    assert payload["input"]["algorithm_version"] == SEGMENTER_ALGORITHM_VERSION == 4
    assert payload["input"]["parameters"] == parameters
    assert len(payload["input"]["source_transcript_sha256"]) == 64
    assert len(payload["input_fingerprint"]) == 64


def test_segmented_cache_invalidates_when_transcript_or_parameters_change(
    tmp_path: Path,
) -> None:
    source = _cache_source()
    parameters = default_segmenter_parameters()
    cache_path = tmp_path / "segmented_transcript.json"
    save_segmented_transcript_cache(
        cache_path,
        source,
        merge_short_segments(source, **parameters),
        parameters=parameters,
    )

    changed_source = source.model_copy(deep=True)
    changed_source.segments[0].text = "This was changed"
    changed_parameters = {**parameters, "max_gap": 1.5}

    assert (
        load_segmented_transcript_cache(
            cache_path,
            changed_source,
            parameters=parameters,
        )
        is None
    )
    assert (
        load_segmented_transcript_cache(
            cache_path,
            source,
            parameters=changed_parameters,
        )
        is None
    )


def test_legacy_plain_segmented_transcript_is_a_safe_cache_miss(
    tmp_path: Path,
) -> None:
    source = _cache_source()
    cache_path = tmp_path / "segmented_transcript.json"
    cache_path.write_text(
        merge_short_segments(source).model_dump_json(),
        encoding="utf-8",
    )

    assert load_segmented_transcript_cache(cache_path, source) is None
