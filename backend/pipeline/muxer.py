from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

from backend.config import Settings
from backend.pipeline.audio import (
    build_media_cache_metadata,
    commit_media_output,
    file_content_fingerprint,
    media_cache_is_valid,
)
from backend.pipeline.subtitle import read_srt
from backend.utils.files import temporary_output_path
from backend.utils.process import ProcessError, require_executable, run_process


logger = logging.getLogger(__name__)


async def mux_soft_subtitle(
    video: Path,
    audio: Path,
    subtitle: Path | None,
    output: Path,
    config: Settings,
) -> Path:
    required_streams = {"video", "audio", "subtitle"} if subtitle else {"video", "audio"}
    cache_metadata = build_media_cache_metadata(
        "soft-subtitle-mux",
        inputs={
            "video": file_content_fingerprint(video),
            "audio": file_content_fingerprint(audio),
            "subtitle": file_content_fingerprint(subtitle) if subtitle is not None else None,
        },
        parameters={
            "video_codec": "copy",
            "audio_codec": "aac",
            "audio_bitrate": "192k",
            "subtitle_codec": "mov_text" if subtitle is not None else None,
            "subtitle_language": "zho" if subtitle is not None else None,
            "subtitle_title": "简体中文" if subtitle is not None else None,
            "subtitle_default": subtitle is not None,
            "faststart": True,
            "shortest": False,
        },
    )
    if await media_cache_is_valid(
        output,
        config,
        required_stream_types=required_streams,
        expected_metadata=cache_metadata,
    ):
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    binary = require_executable(config.ffmpeg_bin, "ffmpeg")
    pending = temporary_output_path(output)
    args = [binary, "-y", "-i", str(video), "-i", str(audio)]
    if subtitle:
        args.extend(["-i", str(subtitle)])
    args.extend(["-map", "0:v:0", "-map", "1:a:0"])
    if subtitle:
        args.extend(["-map", "2:0"])
    args.extend(["-c:v", "copy", "-c:a", "aac", "-b:a", "192k"])
    if subtitle:
        args.extend(
            [
                "-c:s",
                "mov_text",
                "-metadata:s:s:0",
                "language=zho",
                "-metadata:s:s:0",
                "title=简体中文",
                "-disposition:s:0",
                "default",
            ]
        )
    # Do not use -shortest here: a selectable subtitle stream commonly ends
    # before the source video and would otherwise truncate the final MP4.
    args.extend(["-movflags", "+faststart", str(pending)])
    logger.info("[MUX] building soft-subtitle video")
    try:
        await run_process(args)
        await commit_media_output(
            pending,
            output,
            config,
            required_stream_types=required_streams,
            cache_metadata=cache_metadata,
        )
    finally:
        pending.unlink(missing_ok=True)
    return output


def _escape_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


async def mux_burned_subtitle(
    video: Path,
    audio: Path,
    ass_subtitle: Path,
    output: Path,
    config: Settings,
    *,
    fallback_srt: Path | None = None,
    font_name: str = "PingFang SC",
) -> Path:
    font_candidate = Path(font_name).expanduser()
    cache_metadata = build_media_cache_metadata(
        "burned-subtitle-mux",
        inputs={
            "video": file_content_fingerprint(video),
            "audio": file_content_fingerprint(audio),
            "ass_subtitle": file_content_fingerprint(ass_subtitle),
            "fallback_srt": (
                file_content_fingerprint(fallback_srt)
                if fallback_srt is not None
                else None
            ),
            "explicit_font_file": (
                file_content_fingerprint(font_candidate)
                if font_candidate.is_file()
                else None
            ),
        },
        parameters={
            "font_name": font_name,
            "renderer": "ffmpeg-ass-with-pillow-fallback",
            "video_codec": "libx264",
            "preset": "medium",
            "crf": 18,
            "audio_codec": "aac",
            "audio_bitrate": "192k",
            "faststart": True,
            "shortest": True,
        },
    )
    if await media_cache_is_valid(
        output,
        config,
        required_stream_types={"video", "audio"},
        expected_metadata=cache_metadata,
    ):
        return output
    output.parent.mkdir(parents=True, exist_ok=True)
    binary = require_executable(config.ffmpeg_bin, "ffmpeg")
    subtitle_filter = f"ass=filename='{_escape_filter_path(ass_subtitle)}'"
    logger.info("[MUX] building burned-subtitle video")
    pending = temporary_output_path(output)
    args = [
        binary,
        "-y",
        "-i",
        str(video),
        "-i",
        str(audio),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        subtitle_filter,
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        "-shortest",
        str(pending),
    ]
    try:
        try:
            await run_process(args)
        except ProcessError as exc:
            if "No such filter: 'ass'" not in exc.output or fallback_srt is None:
                raise
            logger.warning("[MUX] FFmpeg 没有 libass，改用 Pillow 透明字幕轨回退")
            await _mux_burned_with_pillow(
                video,
                audio,
                fallback_srt,
                pending,
                config,
                font_name=font_name,
            )
        await commit_media_output(
            pending,
            output,
            config,
            required_stream_types={"video", "audio"},
            cache_metadata=cache_metadata,
        )
    finally:
        pending.unlink(missing_ok=True)
    return output


async def _video_geometry(video: Path, config: Settings) -> tuple[int, int, float]:
    ffprobe = require_executable(config.ffprobe_bin, "ffprobe")
    result = await run_process(
        [
            ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height:format=duration",
            "-of",
            "json",
            str(video),
        ]
    )
    try:
        payload = json.loads(result.output)
        stream = payload["streams"][0]
        return int(stream["width"]), int(stream["height"]), float(payload["format"]["duration"])
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("无法读取视频尺寸，不能生成字幕压制回退轨") from exc


async def _font_path(font_name: str) -> Path:
    candidate = Path(font_name).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    # fontconfig on macOS may silently map PingFang to Verdana, which renders
    # Chinese as tofu boxes. Prefer a known system CJK collection first.
    if "pingfang" in font_name.casefold():
        heiti = Path("/System/Library/Fonts/STHeiti Medium.ttc")
        if heiti.is_file():
            return heiti
    try:
        matcher = require_executable("fc-match", "fontconfig fc-match")
        result = await run_process([matcher, "-f", "%{file}\n", font_name], timeout=15)
        matched = Path(result.output.splitlines()[0].strip()).resolve()
        if matched.is_file():
            return matched
    except Exception:
        pass
    for fallback in (
        Path("/System/Library/Fonts/STHeiti Medium.ttc"),
        Path("/System/Library/Fonts/Hiragino Sans GB.ttc"),
    ):
        if fallback.is_file():
            return fallback
    raise RuntimeError(f"找不到可用于压制中文字幕的字体：{font_name}")


def _render_subtitle_png(
    path: Path,
    text: str,
    width: int,
    height: int,
    font_path: Path,
) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:
        raise RuntimeError("FFmpeg 缺少 libass，回退压制需要 Pillow；请重新安装 requirements.txt") from exc
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if text:
        draw = ImageDraw.Draw(image)
        font_size = max(18, round(height * 0.052))
        font = ImageFont.truetype(str(font_path), font_size)
        spacing = max(4, round(font_size * 0.22))
        stroke = max(2, round(height * 0.0035))
        bbox = draw.multiline_textbbox(
            (0, 0), text, font=font, align="center", spacing=spacing, stroke_width=stroke
        )
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        x = (width - text_width) / 2 - bbox[0]
        margin_bottom = max(22, round(height * 0.07))
        y = height - margin_bottom - text_height - bbox[1]
        draw.multiline_text(
            (x, y),
            text,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=stroke,
            stroke_fill=(0, 0, 0, 255),
            align="center",
            spacing=spacing,
        )
    image.save(path, format="PNG", optimize=True)


def _concat_quote(path: Path) -> str:
    return str(path.resolve()).replace("'", "'\\''")


async def _mux_burned_with_pillow(
    video: Path,
    audio: Path,
    subtitle: Path,
    output: Path,
    config: Settings,
    *,
    font_name: str,
) -> None:
    width, height, duration = await _video_geometry(video, config)
    transcript = read_srt(subtitle, language="zh-CN")
    font_path = await _font_path(font_name)
    points = {0.0, duration}
    for segment in transcript.segments:
        points.add(max(0.0, min(duration, segment.start)))
        points.add(max(0.0, min(duration, segment.end)))
    ordered = sorted(points)
    ffmpeg = require_executable(config.ffmpeg_bin, "ffmpeg")

    with tempfile.TemporaryDirectory(prefix="subtitle-overlay-", dir=str(output.parent)) as name:
        temporary = Path(name)
        manifest_lines: list[str] = []
        image_by_text: dict[str, Path] = {}
        final_image: Path | None = None
        for index, (start, end) in enumerate(zip(ordered, ordered[1:])):
            interval_duration = end - start
            if interval_duration <= 0:
                continue
            active = [
                item.text
                for item in transcript.segments
                if item.start < end - 1e-6 and item.end > start + 1e-6
            ]
            text = "\n".join(active)
            image_path = image_by_text.get(text)
            if image_path is None:
                image_path = temporary / f"subtitle_{len(image_by_text):04d}.png"
                _render_subtitle_png(image_path, text, width, height, font_path)
                image_by_text[text] = image_path
            manifest_lines.extend(
                [f"file '{_concat_quote(image_path)}'", f"duration {interval_duration:.6f}"]
            )
            final_image = image_path
        if final_image is None:
            final_image = temporary / "blank.png"
            _render_subtitle_png(final_image, "", width, height, font_path)
            manifest_lines.extend([f"file '{_concat_quote(final_image)}'", f"duration {duration:.6f}"])
        # concat demuxer needs a final repeated frame for the previous duration.
        manifest_lines.append(f"file '{_concat_quote(final_image)}'")
        manifest = temporary / "overlay.ffconcat"
        manifest.write_text("ffconcat version 1.0\n" + "\n".join(manifest_lines) + "\n", encoding="utf-8")
        overlay_video = temporary / "overlay.mov"
        await run_process(
            [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(manifest),
                "-fps_mode",
                "vfr",
                "-pix_fmt",
                "argb",
                "-c:v",
                "qtrle",
                str(overlay_video),
            ]
        )
        await run_process(
            [
                ffmpeg,
                "-y",
                "-i",
                str(video),
                "-i",
                str(audio),
                "-i",
                str(overlay_video),
                "-filter_complex",
                "[0:v][2:v]overlay=0:0:format=auto:eof_action=pass:shortest=0[v]",
                "-map",
                "[v]",
                "-map",
                "1:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-t",
                f"{duration:.3f}",
                str(output),
            ]
        )
