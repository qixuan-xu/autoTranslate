from dataclasses import replace
from pathlib import Path

import pytest

from backend.config import Settings
from backend.pipeline.download import _build_ytdlp_args, download_video, validate_video_url
from backend.utils.process import ProcessError, ProcessResult


def test_ytdlp_args_enable_node_ejs_and_reasonable_resolution(tmp_path: Path) -> None:
    config = replace(
        Settings(),
        ytdlp_js_runtime="node:/opt/node",
        ytdlp_max_height=1080,
        ytdlp_proxy="",
        ytdlp_cookies="",
    )
    args = _build_ytdlp_args(
        "/venv/bin/yt-dlp",
        "https://www.youtube.com/watch?v=test",
        tmp_path / "source.download.%(ext)s",
        config,
    )

    assert args[:3] == ["/venv/bin/yt-dlp", "--js-runtimes", "node:/opt/node"]
    assert "--ignore-config" in args
    assert "--extractor-retries" in args
    assert "bestvideo*[height<=1080]+bestaudio/best[height<=1080]/best" in args


def test_ytdlp_args_resolve_bare_runtime_to_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "backend.pipeline.download.require_executable",
        lambda binary, _label: f"/resolved/{binary}",
    )
    args = _build_ytdlp_args(
        "yt-dlp",
        "https://youtu.be/test",
        tmp_path / "source.download.%(ext)s",
        replace(Settings(), ytdlp_js_runtime="node", ytdlp_cookies=""),
    )

    runtime_index = args.index("--js-runtimes")
    assert args[runtime_index + 1] == "node:/resolved/node"


def test_ytdlp_args_validate_height_and_cookie_file(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="144 到 4320"):
        _build_ytdlp_args(
            "yt-dlp",
            "https://youtu.be/test",
            tmp_path / "source.download.%(ext)s",
            replace(Settings(), ytdlp_max_height=99),
        )

    with pytest.raises(RuntimeError, match="cookies.txt 不存在"):
        _build_ytdlp_args(
            "yt-dlp",
            "https://youtu.be/test",
            tmp_path / "source.download.%(ext)s",
            replace(Settings(), ytdlp_cookies=str(tmp_path / "missing.txt")),
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=T_OqU3ONq3w",
        "https://youtu.be/T_OqU3ONq3w",
    ],
)
def test_validate_video_url_accepts_http_youtube_urls(url: str) -> None:
    assert validate_video_url(url) == url


@pytest.mark.asyncio
async def test_download_reparses_transient_403_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    delays: list[float] = []
    logs: list[str] = []

    monkeypatch.setattr(
        "backend.pipeline.download.require_executable",
        lambda binary, _label: f"/resolved/{binary}",
    )

    async def valid_candidate(paths, _config):
        return paths[0] if paths else None

    async def fake_run(args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ProcessError(args, 1, "ERROR: unable to download video data: HTTP Error 403")
        completed = tmp_path / "source.download.mp4"
        completed.write_bytes(b"complete media placeholder")
        return ProcessResult(list(args), 0, "done")

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("backend.pipeline.download._valid_final_candidate", valid_candidate)
    monkeypatch.setattr("backend.pipeline.download.run_process", fake_run)
    monkeypatch.setattr("backend.pipeline.download.asyncio.sleep", fake_sleep)

    result = await download_video(
        "https://youtu.be/T_OqU3ONq3w",
        tmp_path,
        replace(Settings(), ytdlp_js_runtime="node", ytdlp_cookies=""),
        on_log=logs.append,
    )

    assert attempts == 2
    assert delays == [1.0]
    assert any("重新解析" in message for message in logs)
    assert result == tmp_path / "source.mp4"
    assert result.read_bytes() == b"complete media placeholder"


@pytest.mark.asyncio
async def test_download_does_not_retry_non_transient_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    monkeypatch.setattr(
        "backend.pipeline.download.require_executable",
        lambda binary, _label: f"/resolved/{binary}",
    )

    async def no_candidate(_paths, _config):
        return None

    async def fake_run(args, **_kwargs):
        nonlocal attempts
        attempts += 1
        raise ProcessError(args, 1, "ERROR: Video unavailable")

    monkeypatch.setattr("backend.pipeline.download._valid_final_candidate", no_candidate)
    monkeypatch.setattr("backend.pipeline.download.run_process", fake_run)

    with pytest.raises(ProcessError, match="Video unavailable"):
        await download_video(
            "https://youtu.be/missing",
            tmp_path,
            replace(Settings(), ytdlp_js_runtime="node", ytdlp_cookies=""),
        )
    assert attempts == 1
