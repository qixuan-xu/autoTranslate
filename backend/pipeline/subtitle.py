"""SRT/ASS parsing and generation helpers."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import List, Sequence, Tuple, Union

from backend.models.domain import Segment, Transcript
from backend.utils.files import atomic_write_text


class SubtitleParseError(ValueError):
    """Raised when an SRT cue cannot be parsed safely."""


_SRT_TIMESTAMP_RE = re.compile(
    r"^\s*(?P<hours>\d+):(?P<minutes>\d{2}):(?P<seconds>\d{2})"
    r"[,.](?P<millis>\d{1,3})\s*$"
)
_SRT_TIMING_RE = re.compile(
    r"^\s*(?P<start>\d+:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*"
    r"(?P<end>\d+:\d{2}:\d{2}[,.]\d{1,3})(?:\s+.*)?$"
)
_BREAK_AFTER = set("。！？；：，、….!?;:,)）]】}》〉”’")


def _require_finite_nonnegative(seconds: float) -> float:
    value = float(seconds)
    if not math.isfinite(value) or value < 0:
        raise ValueError("timestamp must be a finite non-negative number")
    return value


def format_srt_timestamp(seconds: float) -> str:
    """Format seconds as ``HH:MM:SS,mmm`` with rollover-safe rounding."""

    value = _require_finite_nonnegative(seconds)
    total_millis = int(value * 1000 + 0.5)
    hours, remainder = divmod(total_millis, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{millis:03d}"


def parse_srt_timestamp(timestamp: str) -> float:
    """Parse an SRT timestamp, accepting either comma or period milliseconds."""

    match = _SRT_TIMESTAMP_RE.fullmatch(timestamp)
    if not match:
        raise SubtitleParseError(f"invalid SRT timestamp: {timestamp!r}")
    hours = int(match.group("hours"))
    minutes = int(match.group("minutes"))
    seconds = int(match.group("seconds"))
    if minutes >= 60 or seconds >= 60:
        raise SubtitleParseError(f"invalid SRT timestamp: {timestamp!r}")
    millis = int(match.group("millis").ljust(3, "0"))
    return hours * 3600 + minutes * 60 + seconds + millis / 1000


# Explicit aliases make call sites read naturally in both directions.
seconds_to_srt_timestamp = format_srt_timestamp
srt_timestamp_to_seconds = parse_srt_timestamp


def parse_srt(content: str) -> List[Segment]:
    """Parse SRT text into domain segments.

    Cue IDs are preserved when numeric.  Files with non-numeric cue labels get a
    deterministic zero-based fallback ID.  Formatting line breaks in cue text are
    retained.
    """

    normalized = content.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    blocks = re.split(r"\n[ \t]*\n", normalized)
    segments: List[Segment] = []
    for block_number, block in enumerate(blocks, start=1):
        lines = block.splitlines()
        timing_index = next(
            (index for index, line in enumerate(lines[:2]) if _SRT_TIMING_RE.match(line)),
            None,
        )
        if timing_index is None:
            raise SubtitleParseError(f"cue {block_number} is missing a valid timing line")

        timing = _SRT_TIMING_RE.match(lines[timing_index])
        assert timing is not None
        start = parse_srt_timestamp(timing.group("start"))
        end = parse_srt_timestamp(timing.group("end"))
        if end <= start:
            raise SubtitleParseError(f"cue {block_number} ends before it starts")

        cue_id = len(segments)
        if timing_index == 1:
            try:
                cue_id = int(lines[0].strip())
            except ValueError:
                cue_id = len(segments)

        text = "\n".join(lines[timing_index + 1 :]).strip()
        if not text:
            raise SubtitleParseError(f"cue {block_number} has no text")
        segments.append(Segment(id=cue_id, start=start, end=end, text=text))

    return segments


def srt_to_transcript(content: str, *, language: str = "unknown") -> Transcript:
    """Parse SRT text into a :class:`Transcript`."""

    segments = parse_srt(content)
    duration = max((segment.end for segment in segments), default=0.0)
    return Transcript(language=language, segments=segments, duration=duration)


def read_srt(path: Union[str, Path], *, language: str = "unknown") -> Transcript:
    """Read an UTF-8/UTF-8-BOM SRT file."""

    return srt_to_transcript(Path(path).read_text(encoding="utf-8-sig"), language=language)


def _single_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _break_candidates(text: str) -> List[int]:
    candidates: List[int] = []
    for index, character in enumerate(text):
        if character in _BREAK_AFTER:
            candidates.append(index + 1)
        elif character.isspace():
            candidates.append(index)
    return [position for position in candidates if 0 < position < len(text)]


def wrap_chinese_subtitle(
    text: str,
    *,
    max_line_chars: int = 18,
    max_lines: int = 2,
) -> str:
    """Create at most two balanced, punctuation-aware subtitle lines.

    Content is never truncated.  When a sentence cannot fit within two requested
    line lengths, both lines are balanced and may exceed ``max_line_chars``.
    """

    if max_line_chars <= 0:
        raise ValueError("max_line_chars must be positive")
    if max_lines not in (1, 2):
        raise ValueError("max_lines must be 1 or 2")

    value = _single_line(text)
    if not value or len(value) <= max_line_chars or max_lines == 1:
        return value

    candidates = _break_candidates(value)
    midpoint = len(value) / 2
    if candidates:
        # Strongly prefer a split where both lines fit.  Within that constraint,
        # choose the most visually balanced semantic boundary.
        def score(position: int) -> Tuple[int, float]:
            left_length = len(value[:position].rstrip())
            right_length = len(value[position:].lstrip())
            overflow = max(0, left_length - max_line_chars) + max(
                0, right_length - max_line_chars
            )
            return overflow, abs(position - midpoint)

        split_at = min(candidates, key=score)
    else:
        split_at = int(midpoint + 0.5)

    left = value[:split_at].rstrip()
    right = value[split_at:].lstrip()
    return left if not right else f"{left}\n{right}"


def _extract_segments(value: Union[Transcript, Sequence[Segment]]) -> Sequence[Segment]:
    return value.segments if isinstance(value, Transcript) else value


def _subtitle_text(
    segment: Segment,
    *,
    translated: bool,
    wrap_chinese: bool,
    max_line_chars: int,
) -> str:
    text = segment.translated_text if translated and segment.translated_text is not None else segment.text
    if wrap_chinese:
        return wrap_chinese_subtitle(text, max_line_chars=max_line_chars, max_lines=2)
    return text.strip()


def transcript_to_srt(
    transcript: Union[Transcript, Sequence[Segment]],
    *,
    translated: bool = False,
    wrap_chinese: bool = False,
    max_line_chars: int = 18,
    preserve_ids: bool = True,
) -> str:
    """Serialize a transcript or segment sequence as SRT."""

    blocks = []
    for index, segment in enumerate(_extract_segments(transcript), start=1):
        cue_id = segment.id if preserve_ids else index
        text = _subtitle_text(
            segment,
            translated=translated,
            wrap_chinese=wrap_chinese,
            max_line_chars=max_line_chars,
        )
        blocks.append(
            f"{cue_id}\n{format_srt_timestamp(segment.start)} --> "
            f"{format_srt_timestamp(segment.end)}\n{text}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def write_srt(
    transcript: Union[Transcript, Sequence[Segment]],
    path: Union[str, Path],
    *,
    translated: bool = False,
    wrap_chinese: bool = False,
    max_line_chars: int = 18,
    preserve_ids: bool = True,
) -> Path:
    """Write SRT as UTF-8 and return the destination path."""

    destination = Path(path)
    atomic_write_text(
        destination,
        transcript_to_srt(
            transcript,
            translated=translated,
            wrap_chinese=wrap_chinese,
            max_line_chars=max_line_chars,
            preserve_ids=preserve_ids,
        ),
        encoding="utf-8",
    )
    return destination


def format_ass_timestamp(seconds: float) -> str:
    """Format seconds as ASS ``H:MM:SS.cc``."""

    value = _require_finite_nonnegative(seconds)
    total_centis = int(value * 100 + 0.5)
    hours, remainder = divmod(total_centis, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centis = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centis:02d}"


def _ass_text(text: str) -> str:
    # Braces introduce ASS override tags, so use visually equivalent full-width
    # characters for untrusted subtitle content.
    return text.replace("{", "｛").replace("}", "｝").replace("\n", r"\N")


def transcript_to_ass(
    transcript: Union[Transcript, Sequence[Segment]],
    *,
    translated: bool = True,
    font_name: str = "PingFang SC",
    font_size: int = 54,
    play_res: Tuple[int, int] = (1920, 1080),
    max_line_chars: int = 18,
) -> str:
    """Serialize subtitles as a bottom-centered ASS document."""

    if font_size <= 0:
        raise ValueError("font_size must be positive")
    if len(play_res) != 2 or play_res[0] <= 0 or play_res[1] <= 0:
        raise ValueError("play_res must contain two positive integers")
    play_res_x, play_res_y = int(play_res[0]), int(play_res[1])
    safe_font_name = font_name.replace(",", "，").strip() or "sans-serif"
    margin_v = max(24, round(play_res_y * 0.055))

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        f"PlayResX: {play_res_x}",
        f"PlayResY: {play_res_y}",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Default,{safe_font_name},{font_size},&H00FFFFFF,&H000000FF,"
        f"&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,3,0,2,48,48,{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for segment in _extract_segments(transcript):
        text = _subtitle_text(
            segment,
            translated=translated,
            wrap_chinese=True,
            max_line_chars=max_line_chars,
        )
        lines.append(
            f"Dialogue: 0,{format_ass_timestamp(segment.start)},"
            f"{format_ass_timestamp(segment.end)},Default,,0,0,0,,{_ass_text(text)}"
        )
    return "\n".join(lines) + "\n"


def write_ass(
    transcript: Union[Transcript, Sequence[Segment]],
    path: Union[str, Path],
    *,
    translated: bool = True,
    font_name: str = "PingFang SC",
    font_size: int = 54,
    play_res: Tuple[int, int] = (1920, 1080),
    max_line_chars: int = 18,
) -> Path:
    """Write an ASS subtitle file and return its path."""

    destination = Path(path)
    atomic_write_text(
        destination,
        transcript_to_ass(
            transcript,
            translated=translated,
            font_name=font_name,
            font_size=font_size,
            play_res=play_res,
            max_line_chars=max_line_chars,
        ),
        encoding="utf-8",
    )
    return destination


__all__ = [
    "SubtitleParseError",
    "format_ass_timestamp",
    "format_srt_timestamp",
    "parse_srt",
    "parse_srt_timestamp",
    "read_srt",
    "seconds_to_srt_timestamp",
    "srt_timestamp_to_seconds",
    "srt_to_transcript",
    "transcript_to_ass",
    "transcript_to_srt",
    "wrap_chinese_subtitle",
    "write_ass",
    "write_srt",
]
