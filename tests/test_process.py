from __future__ import annotations

import sys

import pytest

from backend.utils.process import run_process


@pytest.mark.asyncio
async def test_run_process_handles_large_carriage_return_progress() -> None:
    result = await run_process(
        [
            sys.executable,
            "-c",
            "import sys; sys.stdout.write(('progress\\r' * 10000)); sys.stdout.flush()",
        ]
    )

    assert result.returncode == 0
    assert "progress" in result.output


@pytest.mark.asyncio
async def test_run_process_splits_crlf_and_carriage_return_callbacks() -> None:
    seen: list[str] = []

    result = await run_process(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'a\\rb\\r\\nc\\n')"],
        on_line=lambda line: seen.append(line),
    )

    assert result.output.splitlines() == ["a", "b", "c"]
    assert seen == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_run_process_timeout_terminates_child() -> None:
    with pytest.raises(RuntimeError, match="命令运行超时"):
        await run_process(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout=0.05,
        )
