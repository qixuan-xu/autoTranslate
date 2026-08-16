from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlparse

from backend.config import Settings
from backend.pipeline.audio import media_cache_is_valid
from backend.utils.files import safe_filename
from backend.utils.process import ProcessError, require_executable, run_process


logger = logging.getLogger(__name__)
LogCallback = Callable[[str], Optional[Awaitable[None]]]
_FINAL_DOWNLOAD_RE = re.compile(r"^source\.download\.[^.]+$")
_TRANSIENT_DOWNLOAD_MARKERS = (
    "http error 403",
    "http error 502",
    "http error 503",
    "http error 504",
    "the page needs to be reloaded",
    "remote end closed connection",
    "connection reset",
    "operation timed out",
)
_YTDLP_ATTEMPTS = 3


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


def _build_ytdlp_args(
    binary: str,
    url: str,
    output_template: Path,
    config: Settings,
) -> list[str]:
    max_height = config.ytdlp_max_height
    if not 144 <= max_height <= 4320:
        raise RuntimeError("YTDLP_MAX_HEIGHT 必须在 144 到 4320 之间")
    args = [
        binary,
        "--ignore-config",
        "--newline",
        "--continue",
        "--no-playlist",
        "--no-overwrites",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--extractor-retries",
        "3",
        "--restrict-filenames",
        "--merge-output-format",
        "mp4",
        "--format",
        f"bestvideo*[height<={max_height}]+bestaudio/best[height<={max_height}]/best",
        "--output",
        str(output_template),
        url,
    ]
    js_runtime = config.ytdlp_js_runtime.strip()
    if js_runtime:
        if ":" not in js_runtime:
            runtime_binary = "qjs" if js_runtime == "quickjs" else js_runtime
            runtime_path = require_executable(runtime_binary, f"yt-dlp JS runtime {js_runtime}")
            js_runtime = f"{js_runtime}:{runtime_path}"
        args[1:1] = ["--js-runtimes", js_runtime]
    remote_components = config.ytdlp_remote_components.strip()
    if remote_components:
        args[1:1] = ["--remote-components", remote_components]
    if config.ytdlp_proxy:
        args[1:1] = ["--proxy", config.ytdlp_proxy]
    if config.ytdlp_cookies:
        cookie_path = Path(config.ytdlp_cookies).expanduser().resolve()
        if not cookie_path.is_file():
            raise RuntimeError(f"cookies.txt 不存在：{cookie_path}")
        args[1:1] = ["--cookies", str(cookie_path)]
    return args


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
    args = _build_ytdlp_args(
        binary,
        url,
        output_dir / "source.download.%(ext)s",
        config,
    )

    async def relay(line: str) -> None:
        if on_log and any(
            marker in line.casefold() for marker in ("[download]", "warning", "error")
        ):
            result = on_log(line)
            if result is not None:
                await result

    last_error: ProcessError | None = None
    for attempt in range(1, _YTDLP_ATTEMPTS + 1):
        logger.info("[DOWNLOAD] yt-dlp started attempt=%d/%d", attempt, _YTDLP_ATTEMPTS)
        try:
            await run_process(args, cwd=output_dir, on_line=relay)
            last_error = None
            break
        except ProcessError as exc:
            diagnostic = exc.output.casefold()
            if any(
                marker in diagnostic
                for marker in (
                    "only python versions 3.10 and above",
                    "support for python version 3.9 has been deprecated",
                    "no supported javascript runtime",
                    "javascript runtime is not available",
                    "challenge solving failed",
                )
            ):
                raise RuntimeError(
                    "YouTube 解析失败：项目需要 Python 3.10+、最新版 yt-dlp[default] "
                    "以及可用的 JavaScript runtime。请在项目目录重新运行 "
                    "./scripts/setup.sh 后重试；当前默认使用 Node（YTDLP_JS_RUNTIME=node）。"
                ) from exc
            last_error = exc
            transient = any(marker in diagnostic for marker in _TRANSIENT_DOWNLOAD_MARKERS)
            if not transient or attempt >= _YTDLP_ATTEMPTS:
                raise
            delay = float(attempt)
            message = (
                f"[DOWNLOAD] YouTube 媒体地址暂时不可用，"
                f"{delay:g} 秒后重新解析（{attempt}/{_YTDLP_ATTEMPTS - 1}）"
            )
            logger.warning(message)
            if on_log:
                result = on_log(message)
                if result is not None:
                    await result
            await asyncio.sleep(delay)
    if last_error is not None:  # Defensive: the loop either succeeds or raises.
        raise last_error
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
