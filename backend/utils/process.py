from __future__ import annotations

import asyncio
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional, Sequence


logger = logging.getLogger(__name__)
LineCallback = Callable[[str], Optional[Awaitable[None]]]


class ProcessError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int, output: str):
        command = " ".join(str(part) for part in args)
        tail = output[-4000:].strip()
        super().__init__(f"命令执行失败（退出码 {returncode}）：{command}\n{tail}")
        self.command_args = list(args)
        self.returncode = returncode
        self.output = output


@dataclass
class ProcessResult:
    args: list[str]
    returncode: int
    output: str


def require_executable(binary: str, label: str | None = None) -> str:
    if os.path.sep in binary:
        candidate = Path(binary).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    else:
        found = shutil.which(binary)
        if found:
            return found
    raise RuntimeError(f"找不到 {label or binary}。请先安装并检查环境变量配置。")


async def run_process(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    on_line: LineCallback | None = None,
) -> ProcessResult:
    """Run a subprocess without a shell and merge stderr into its streamed output."""

    if not args:
        raise ValueError("process args must not be empty")
    string_args = [str(part) for part in args]
    logger.info("运行命令: %s", " ".join(string_args))
    process = await asyncio.create_subprocess_exec(
        *string_args,
        cwd=str(cwd) if cwd else None,
        env=dict(env) if env else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    lines: list[str] = []

    async def consume() -> None:
        assert process.stdout is not None
        while True:
            raw = await process.stdout.readline()
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip()
            lines.append(line)
            if on_line:
                result = on_line(line)
                if result is not None:
                    await result

    try:
        if timeout is None:
            await consume()
            returncode = await process.wait()
        else:
            await asyncio.wait_for(consume(), timeout=timeout)
            returncode = await asyncio.wait_for(process.wait(), timeout=10)
    except (asyncio.CancelledError, KeyboardInterrupt):
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        raise
    except asyncio.TimeoutError as exc:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
        raise RuntimeError(f"命令运行超时：{' '.join(string_args)}") from exc

    output = "\n".join(lines)
    if returncode != 0:
        raise ProcessError(string_args, returncode, output)
    return ProcessResult(string_args, returncode, output)
