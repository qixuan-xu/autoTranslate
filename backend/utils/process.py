from __future__ import annotations

import asyncio
import collections
import logging
import os
import signal
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Optional, Sequence


logger = logging.getLogger(__name__)
LineCallback = Callable[[str], Optional[Awaitable[None]]]
_OUTPUT_TAIL_CHARS = 2 * 1024 * 1024
_MAX_UNTERMINATED_CHUNK = 64 * 1024


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
        start_new_session=os.name == "posix",
    )
    output_parts: collections.deque[str] = collections.deque()
    output_chars = 0

    async def emit(raw: bytes) -> None:
        nonlocal output_chars
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        piece = f"{line}\n"
        output_parts.append(piece)
        output_chars += len(piece)
        while output_chars > _OUTPUT_TAIL_CHARS and len(output_parts) > 1:
            output_chars -= len(output_parts.popleft())
        if on_line:
            result = on_line(line)
            if result is not None:
                await result

    async def consume() -> None:
        assert process.stdout is not None
        buffered = bytearray()
        while True:
            raw = await process.stdout.read(64 * 1024)
            if not raw:
                break
            buffered.extend(raw)
            while buffered:
                newline = buffered.find(b"\n")
                carriage = buffered.find(b"\r")
                boundaries = [index for index in (newline, carriage) if index >= 0]
                if not boundaries:
                    if len(buffered) > _MAX_UNTERMINATED_CHUNK:
                        await emit(bytes(buffered[:_MAX_UNTERMINATED_CHUNK]))
                        del buffered[:_MAX_UNTERMINATED_CHUNK]
                    break
                boundary = min(boundaries)
                end = boundary + 1
                if buffered[boundary] == 13 and len(buffered) > end and buffered[end] == 10:
                    end += 1
                await emit(bytes(buffered[:end]))
                del buffered[:end]
        if buffered:
            await emit(bytes(buffered))

    async def stop_process_group() -> None:
        if process.returncode is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            pass
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    async def execute() -> int:
        await consume()
        return await process.wait()

    try:
        returncode = await execute() if timeout is None else await asyncio.wait_for(execute(), timeout)
    except asyncio.TimeoutError as exc:
        await stop_process_group()
        raise RuntimeError(f"命令运行超时：{' '.join(string_args)}") from exc
    except BaseException:
        await stop_process_group()
        raise

    output = "".join(output_parts).rstrip("\n")
    if returncode != 0:
        raise ProcessError(string_args, returncode, output)
    return ProcessResult(string_args, returncode, output)
