from __future__ import annotations

import sys
from pathlib import Path

import pytest

from backend.config import Settings
from backend.pipeline import muxer
from backend.utils.process import ProcessResult


@pytest.mark.asyncio
async def test_soft_subtitle_mux_does_not_stop_at_subtitle_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    async def fake_run_process(args: list[str], **_: object) -> ProcessResult:
        captured.extend(args)
        return ProcessResult(args=args, returncode=0, output="")

    published: list[tuple[Path, Path]] = []

    async def fake_commit(temporary: Path, destination: Path, *_: object, **__: object) -> Path:
        published.append((temporary, destination))
        return destination

    monkeypatch.setattr(muxer, "require_executable", lambda *_: "/mock/ffmpeg")
    monkeypatch.setattr(muxer, "run_process", fake_run_process)
    monkeypatch.setattr(muxer, "commit_media_output", fake_commit)
    output = tmp_path / "output" / "final_zh.mp4"
    (tmp_path / "source.mp4").write_bytes(b"video")
    (tmp_path / "mixed.wav").write_bytes(b"audio")
    (tmp_path / "zh.srt").write_text("subtitle", encoding="utf-8")

    result = await muxer.mux_soft_subtitle(
        tmp_path / "source.mp4",
        tmp_path / "mixed.wav",
        tmp_path / "zh.srt",
        output,
        Settings(ffmpeg_bin="ffmpeg"),
    )

    assert result == output
    assert captured[0] == "/mock/ffmpeg"
    assert captured[-1] != str(output)
    assert Path(captured[-1]).parent == output.parent
    assert Path(captured[-1]).suffix == output.suffix
    assert published == [(Path(captured[-1]), output)]
    assert "-shortest" not in captured
    assert captured[captured.index("-c:v") + 1] == "copy"
    assert [captured[index + 1] for index, item in enumerate(captured) if item == "-map"] == [
        "0:v:0",
        "1:a:0",
        "2:0",
    ]


_MACOS_CJK_FALLBACK = Path("/System/Library/Fonts/STHeiti Medium.ttc")


@pytest.mark.skipif(
    sys.platform != "darwin" or not _MACOS_CJK_FALLBACK.is_file(),
    reason="requires the macOS STHeiti CJK system font",
)
@pytest.mark.asyncio
async def test_pingfang_request_uses_known_macos_cjk_fallback() -> None:
    selected = await muxer._font_path("PingFang SC")

    assert selected == _MACOS_CJK_FALLBACK.resolve()
    assert selected.is_file()
