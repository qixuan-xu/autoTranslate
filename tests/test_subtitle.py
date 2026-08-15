from pathlib import Path

import pytest

from backend.models.domain import Segment, Transcript
from backend.pipeline.subtitle import (
    SubtitleParseError,
    format_ass_timestamp,
    format_srt_timestamp,
    parse_srt,
    parse_srt_timestamp,
    read_srt,
    transcript_to_ass,
    transcript_to_srt,
    wrap_chinese_subtitle,
    write_ass,
    write_srt,
)


def test_srt_timestamp_rounding_and_parse() -> None:
    assert format_srt_timestamp(59.9996) == "00:01:00,000"
    assert format_srt_timestamp(3_661.0074) == "01:01:01,007"
    assert parse_srt_timestamp("01:01:01,007") == pytest.approx(3661.007)
    assert parse_srt_timestamp("00:00:01.25") == pytest.approx(1.25)
    assert format_ass_timestamp(59.996) == "0:01:00.00"


@pytest.mark.parametrize("value", ["00:60:00,000", "00:00:60,000", "bad"])
def test_rejects_bad_srt_timestamp(value: str) -> None:
    with pytest.raises(SubtitleParseError):
        parse_srt_timestamp(value)


def test_parse_srt_handles_bom_crlf_multiline_and_stable_ids() -> None:
    content = (
        "\ufeff7\r\n00:00:01,000 --> 00:00:02,250\r\nFirst line\r\nSecond line\r\n\r\n"
        "9\r\n00:00:03.000 --> 00:00:04.500 position:50%\r\nDone.\r\n"
    )

    segments = parse_srt(content)

    assert [segment.id for segment in segments] == [7, 9]
    assert segments[0].text == "First line\nSecond line"
    assert (segments[1].start, segments[1].end) == (3.0, 4.5)


def test_invalid_or_empty_srt_cue_is_reported_clearly() -> None:
    with pytest.raises(SubtitleParseError, match="timing"):
        parse_srt("1\nnot a time\nHello")
    with pytest.raises(SubtitleParseError, match="no text"):
        parse_srt("1\n00:00:01,000 --> 00:00:02,000")


def test_srt_generation_uses_translation_wrap_and_round_trips() -> None:
    transcript = Transcript(
        language="en",
        segments=[
            Segment(
                id=0,
                start=0,
                end=2.345,
                text="Today we discuss neural networks.",
                translated_text="今天我们来聊一聊神经网络，先看看它为什么有用。",
            )
        ],
    )

    rendered = transcript_to_srt(
        transcript,
        translated=True,
        wrap_chinese=True,
        max_line_chars=16,
    )
    parsed = parse_srt(rendered)

    assert rendered.startswith("0\n00:00:00,000 --> 00:00:02,345")
    assert len(parsed[0].text.splitlines()) == 2
    assert parsed[0].text.replace("\n", "") == transcript.segments[0].translated_text


def test_chinese_wrap_prefers_punctuation_and_never_drops_text() -> None:
    text = "今天我们来聊神经网络，首先要理解它为什么有用。"
    wrapped = wrap_chinese_subtitle(text, max_line_chars=15)

    lines = wrapped.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("，")
    assert "".join(lines) == text


def test_very_long_unpunctuated_text_is_balanced_to_two_lines() -> None:
    text = "这是一段非常非常长而且没有任何标点符号的中文字幕测试文本"
    left, right = wrap_chinese_subtitle(text, max_line_chars=10).splitlines()

    assert abs(len(left) - len(right)) <= 1
    assert left + right == text


def test_ass_generation_has_configurable_style_safe_text_and_two_lines() -> None:
    transcript = Transcript(
        segments=[
            Segment(
                id=1,
                start=1.2,
                end=3.4,
                text="original",
                translated_text="这是{测试}字幕，需要自然地分成两行显示。",
            )
        ]
    )

    rendered = transcript_to_ass(
        transcript,
        font_name="Noto Sans CJK SC",
        font_size=60,
        play_res=(1280, 720),
        max_line_chars=10,
    )

    assert "PlayResX: 1280" in rendered
    assert "PlayResY: 720" in rendered
    assert "Style: Default,Noto Sans CJK SC,60" in rendered
    assert "Dialogue: 0,0:00:01.20,0:00:03.40" in rendered
    assert r"\N" in rendered
    assert "{测试}" not in rendered
    assert "｛测试｝" in rendered


def test_write_and_read_subtitle_files(tmp_path: Path) -> None:
    transcript = Transcript(segments=[Segment(id=3, start=0.2, end=1.0, text="Hello")])
    srt_path = write_srt(transcript, tmp_path / "nested" / "original.srt")
    ass_path = write_ass(transcript, tmp_path / "nested" / "zh.ass", translated=False)

    assert read_srt(srt_path).segments[0].id == 3
    assert ass_path.read_text(encoding="utf-8").startswith("[Script Info]")
