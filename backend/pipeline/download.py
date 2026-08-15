from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

from backend.config import Settings
from backend.pipeline.audio import media_cache_is_valid
from backend.utils.files import safe_filename
from backend.utils.process import require_executable, run_process


logger = logging.getLogger(__name__)
LogCallback = Callable[[str], Optional[Awaitable[None]]]
_FINAL_DOWNLOAD_RE = re.compile(r"^source\.download\.[^.]+$")


async def _valid_final_candidate(paths: list[Path], config: Settings) -> Path | None:
    for path in paths:
        if await media_cache_is_valid(
            path,
            config,
            required_stream_types={"video", "audio"},
        ):
            return path
    return None


def validate_video_url(url: str) -> str:
    value = url.strip()
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("请输入有效的 http(s) 视频地址")
    return value


async def download_video(
    url: str,
    output_dir: Path,
    config: Settings,
    *,
    on_log: LogCallback | None = None,
) -> Path:
    """Download one video through yt-dlp with resumable partial files."""

    url = validate_video_url(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    existing = sorted(
        (
            path
            for path in output_dir.glob("source.*")
            if ".download." not in path.name
            and path.suffix not in {".part", ".ytdl", ".json"}
            and path.is_file()
        ),
        key=lambda path: (path.stem != "source", path.name),
    )
    cached = await _valid_final_candidate(existing, config)
    if cached is not None:
        logger.info("[DOWNLOAD] 使用缓存 %s", cached)
        return cached

    # yt-dlp may leave a completed-looking merge target after interruption.  Its
    # individual format files are useful for resume, but an invalid final target
    # combined with --no-overwrites would otherwise poison every retry.
    pending_finals = sorted(
        path
        for path in output_dir.glob("source.download.*")
        if path.is_file() and _FINAL_DOWNLOAD_RE.fullmatch(path.name)
    )
    pending = await _valid_final_candidate(pending_finals, config)
    if pending is None:
        for path in pending_finals:
            path.unlink(missing_ok=True)

    binary = require_executable(config.ytdlp_bin, "yt-dlp")
    args = [
        binary,
        "--newline",
        "--continue",
        "--no-playlist",
        "--no-overwrites",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "--format",
        "bestvideo*[height<=2160]+bestaudio/best[height<=2160]/best",
        "--output",
        str(output_dir / "source.download.%(ext)s"),
        url,
    ]
    if config.ytdlp_proxy:
        args[1:1] = ["--proxy", config.ytdlp_proxy]
    if config.ytdlp_cookies:
        cookie_path = Path(config.ytdlp_cookies).expanduser().resolve()
        if not cookie_path.is_file():
            raise RuntimeError(f"cookies.txt 不存在：{cookie_path}")
        args[1:1] = ["--cookies", str(cookie_path)]

    async def relay(line: str) -> None:
        if on_log and ("[download]" in line.lower() or "error" in line.lower()):
            result = on_log(line)
            if result is not None:
                await result

    logger.info("[DOWNLOAD] yt-dlp started")
    await run_process(args, cwd=output_dir, on_line=relay)
    candidates = sorted(
        path
        for path in output_dir.glob("source.download.*")
        if path.is_file() and _FINAL_DOWNLOAD_RE.fullmatch(path.name)
    )
    completed = await _valid_final_candidate(candidates, config)
    if completed is None:
        raise RuntimeError("yt-dlp 已结束，但没有找到同时含视频和音频的完整文件")
    destination = output_dir / f"source{completed.suffix.lower()}"
    os.replace(completed, destination)
    logger.info("[DOWNLOAD] completed: %s", safe_filename(destination.name))
    return destination
