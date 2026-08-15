from __future__ import annotations

import io
import logging
import os
import sys
import threading
import wave
from collections.abc import Iterable, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROMPT_PREFIX = "You are a helpful assistant.<|endofprompt|>"
SUPPORTED_AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".ogg", ".opus", ".m4a", ".aac"}


def _load_project_env() -> None:
    """Load the project .env even when python-dotenv is absent in the CosyVoice env."""

    env_path = PROJECT_ROOT / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not key or not key.replace("_", "").isalnum():
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            os.environ.setdefault(key, value)
    else:
        load_dotenv(env_path, override=False)


_load_project_env()
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [COSYVOICE] %(message)s",
)
logger = logging.getLogger(__name__)


def _resolved_env_path(name: str, default: str) -> Path:
    return Path(os.path.expanduser(os.getenv(name, default))).resolve()


COSYVOICE_ROOT = _resolved_env_path("COSYVOICE_ROOT", "~/CosyVoice")
COSYVOICE_MODEL = _resolved_env_path(
    "COSYVOICE_MODEL", str(COSYVOICE_ROOT / "pretrained_models" / "Fun-CosyVoice3-0.5B")
)


def _allowed_audio_roots() -> tuple[Path, ...]:
    configured = os.getenv("COSYVOICE_ALLOWED_AUDIO_ROOTS", "").strip()
    if configured:
        raw_roots = [part for part in configured.split(os.pathsep) if part.strip()]
    else:
        raw_roots = [os.getenv("WORK_DIR", str(PROJECT_ROOT / "work"))]
    roots: list[Path] = []
    for part in raw_roots:
        root = Path(os.path.expanduser(part.strip()))
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        roots.append(root.resolve())
    return tuple(roots)


ALLOWED_AUDIO_ROOTS = _allowed_audio_roots()
MAX_REFERENCE_BYTES = int(float(os.getenv("COSYVOICE_MAX_REFERENCE_MB", "100")) * 1024 * 1024)
MIN_REFERENCE_SECONDS = float(os.getenv("COSYVOICE_MIN_REFERENCE_SECONDS", "1.0"))
MAX_REFERENCE_SECONDS = float(os.getenv("COSYVOICE_MAX_REFERENCE_SECONDS", "30.0"))
MAX_OUTPUT_SECONDS = float(os.getenv("COSYVOICE_MAX_OUTPUT_SECONDS", "600"))


def format_prompt_text(prompt_text: str) -> str:
    """Return the canonical CosyVoice3 zero-shot prompt exactly once."""

    text = prompt_text.strip()
    if not text:
        raise ValueError("参考音频对应文本不能为空")
    if text.startswith(PROMPT_PREFIX):
        transcript = text[len(PROMPT_PREFIX) :].strip()
    elif "<|endofprompt|>" in text:
        transcript = text.rsplit("<|endofprompt|>", 1)[1].strip()
    else:
        transcript = text
    if not transcript:
        raise ValueError("参考音频对应文本不能为空")
    return f"{PROMPT_PREFIX}{transcript}"


def _is_beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def validate_reference_audio(raw_path: str) -> Path:
    if "\x00" in raw_path:
        raise ValueError("参考音频路径包含非法字符")
    candidate = Path(os.path.expanduser(raw_path))
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, OSError) as exc:
        raise ValueError(f"参考音频不存在或不可读取：{candidate}") from exc
    if not resolved.is_file():
        raise ValueError(f"参考音频不是普通文件：{resolved}")
    if not any(_is_beneath(resolved, root) for root in ALLOWED_AUDIO_ROOTS):
        roots = ", ".join(str(root) for root in ALLOWED_AUDIO_ROOTS)
        raise ValueError(f"参考音频必须位于允许目录内：{roots}")
    if resolved.suffix.lower() not in SUPPORTED_AUDIO_SUFFIXES:
        raise ValueError(f"不支持的参考音频格式：{resolved.suffix or '(无扩展名)'}")
    if resolved.stat().st_size > MAX_REFERENCE_BYTES:
        raise ValueError("参考音频文件过大")
    return resolved


class SynthesizeRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4_000)
    prompt_audio: str = Field(min_length=1, max_length=4_096)
    prompt_text: str = Field(min_length=1, max_length=20_000)
    speed: float = Field(default=1.0, ge=0.75, le=1.25)

    @field_validator("text", "prompt_audio", "prompt_text")
    @classmethod
    def strip_required_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("字段不能为空")
        if "\x00" in value:
            raise ValueError("字段包含非法字符")
        return value


def _normalise_waveform(value: Any) -> Any:
    import torch

    waveform = value if torch.is_tensor(value) else torch.as_tensor(value)
    waveform = waveform.detach().to(device="cpu", dtype=torch.float32)
    while waveform.ndim > 2 and waveform.shape[0] == 1:
        waveform = waveform.squeeze(0)
    if waveform.ndim == 1:
        waveform = waveform.unsqueeze(0)
    if waveform.ndim != 2 or waveform.shape[-1] == 0:
        raise RuntimeError(f"CosyVoice 返回了无效音频形状：{tuple(waveform.shape)}")
    # CosyVoice uses channels-first. Accept time-major arrays from compatible forks too.
    if waveform.shape[0] > 8 and waveform.shape[1] <= 8:
        waveform = waveform.transpose(0, 1)
    if waveform.shape[0] > 8:
        raise RuntimeError(f"CosyVoice 返回了异常声道数：{waveform.shape[0]}")
    return waveform.contiguous()


def _collect_waveform(outputs: Any, sample_rate: int) -> Any:
    import torch

    if torch.is_tensor(outputs) or isinstance(outputs, Mapping):
        iterator: Iterable[Any] = (outputs,)
    elif isinstance(outputs, Iterable):
        iterator = outputs
    else:
        raise RuntimeError("CosyVoice inference_zero_shot 返回值不可迭代")

    chunks: list[Any] = []
    frame_count = 0
    channel_count: int | None = None
    for item in iterator:
        speech = item.get("tts_speech") if isinstance(item, Mapping) else item
        if speech is None:
            raise RuntimeError("CosyVoice 输出缺少 tts_speech")
        chunk = _normalise_waveform(speech)
        channels = int(chunk.shape[0])
        if channel_count is None:
            channel_count = channels
        elif channels != channel_count:
            raise RuntimeError("CosyVoice 分块输出的声道数不一致")
        frame_count += int(chunk.shape[-1])
        if frame_count / sample_rate > MAX_OUTPUT_SECONDS:
            raise RuntimeError("CosyVoice 输出时长超过安全限制")
        chunks.append(chunk)
    if not chunks:
        raise RuntimeError("CosyVoice 没有生成音频")
    return chunks[0] if len(chunks) == 1 else torch.cat(chunks, dim=-1)


def _waveform_to_wav_bytes(waveform: Any, sample_rate: int) -> bytes:
    import torch

    waveform = torch.nan_to_num(waveform, nan=0.0, posinf=1.0, neginf=-1.0)
    pcm = (waveform.clamp(-1.0, 1.0) * 32767.0).round().to(torch.int16)
    interleaved = pcm.transpose(0, 1).contiguous().numpy().astype("<i2", copy=False)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(int(waveform.shape[0]))
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        wav_file.writeframes(interleaved.tobytes())
    return buffer.getvalue()


class CosyVoiceRuntime:
    """Own one model instance and serialize inference for thread safety."""

    def __init__(self, root: Path, model_path: Path):
        self.root = root
        self.model_path = model_path
        self.model: Any | None = None
        self.sample_rate: int | None = None
        self.load_error: str | None = None
        self._load_attempted = False
        self._load_lock = threading.Lock()
        self._inference_lock = threading.Lock()

    def load(self) -> None:
        with self._load_lock:
            if self._load_attempted:
                return
            self._load_attempted = True
            try:
                if not self.root.is_dir():
                    raise RuntimeError(f"COSYVOICE_ROOT 不存在：{self.root}")
                if not (self.root / "third_party" / "Matcha-TTS").is_dir():
                    raise RuntimeError("CosyVoice third_party/Matcha-TTS 不存在")
                if not self.model_path.is_dir():
                    raise RuntimeError(f"COSYVOICE_MODEL 不存在：{self.model_path}")
                if not (self.model_path / "cosyvoice3.yaml").is_file():
                    raise RuntimeError(f"目标目录不是 CosyVoice3 模型：{self.model_path}")

                for import_path in (self.root, self.root / "third_party" / "Matcha-TTS"):
                    path_text = str(import_path)
                    if path_text not in sys.path:
                        sys.path.insert(0, path_text)
                from cosyvoice.cli.cosyvoice import AutoModel

                logger.info("正在加载 CosyVoice3 模型：%s", self.model_path)
                model = AutoModel(model_dir=str(self.model_path))
                sample_rate = int(getattr(model, "sample_rate", 0))
                if sample_rate <= 0:
                    raise RuntimeError("CosyVoice 模型没有提供有效 sample_rate")
                self.model = model
                self.sample_rate = sample_rate
                logging.getLogger().setLevel(
                    getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
                )
                logger.info("CosyVoice3 模型加载完成，sample_rate=%s", sample_rate)
            except Exception as exc:
                self.load_error = str(exc) or exc.__class__.__name__
                logger.exception("CosyVoice3 模型加载失败")

    def _reference_duration(self, path: Path) -> float:
        try:
            import soundfile as sf

            info = sf.info(str(path))
            duration = float(info.duration)
            if info.samplerate < 16_000:
                raise ValueError(f"参考音频采样率过低：{info.samplerate} Hz（至少 16000 Hz）")
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError(f"无法读取参考音频：{exc}") from exc
        if duration < MIN_REFERENCE_SECONDS:
            raise ValueError(
                f"参考音频太短：{duration:.2f} 秒（至少 {MIN_REFERENCE_SECONDS:.1f} 秒）"
            )
        if duration > MAX_REFERENCE_SECONDS:
            raise ValueError(
                f"参考音频太长：{duration:.2f} 秒（最多 {MAX_REFERENCE_SECONDS:.1f} 秒）"
            )
        return duration

    def synthesize(self, request: SynthesizeRequest, prompt_audio: Path) -> tuple[bytes, int]:
        if self.model is None or self.sample_rate is None:
            detail = self.load_error or "模型尚未加载"
            raise RuntimeError(f"CosyVoice 服务不可用：{detail}")
        prompt_text = format_prompt_text(request.prompt_text)
        self._reference_duration(prompt_audio)
        with self._inference_lock:
            outputs = self.model.inference_zero_shot(
                request.text,
                prompt_text,
                str(prompt_audio),
                stream=False,
                speed=request.speed,
            )
            waveform = _collect_waveform(outputs, self.sample_rate)
            return _waveform_to_wav_bytes(waveform, self.sample_rate), self.sample_rate

    def health(self) -> dict[str, Any]:
        loaded = self.model is not None and self.sample_rate is not None
        return {
            "status": "ok" if loaded else "error",
            "loaded": loaded,
            "model": str(self.model_path),
            "sample_rate": self.sample_rate,
            "error": self.load_error,
            "allowed_audio_roots": [str(root) for root in ALLOWED_AUDIO_ROOTS],
        }


runtime = CosyVoiceRuntime(COSYVOICE_ROOT, COSYVOICE_MODEL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await run_in_threadpool(runtime.load)
    yield


app = FastAPI(
    title="Local CosyVoice3 Service",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)


@app.get("/health")
async def health() -> dict[str, Any]:
    return runtime.health()


@app.post("/synthesize")
async def synthesize(request: SynthesizeRequest) -> Response:
    if runtime.model is None:
        raise HTTPException(status_code=503, detail=runtime.load_error or "CosyVoice 模型尚未加载")
    try:
        prompt_audio = validate_reference_audio(request.prompt_audio)
        wav_data, sample_rate = await run_in_threadpool(runtime.synthesize, request, prompt_audio)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        logger.exception("语音合成失败")
        raise HTTPException(status_code=500, detail=f"语音合成失败：{str(exc)[:1000]}") from exc
    except Exception as exc:
        logger.exception("语音合成出现未预期错误")
        raise HTTPException(
            status_code=500, detail=f"语音合成出现未预期错误：{str(exc)[:1000]}"
        ) from exc
    return Response(
        content=wav_data,
        media_type="audio/wav",
        headers={
            "Content-Disposition": 'inline; filename="synthesized.wav"',
            "X-Audio-Sample-Rate": str(sample_rate),
        },
    )


def main() -> None:
    import uvicorn

    host = os.getenv("COSYVOICE_HOST", "127.0.0.1")
    if (
        host not in {"127.0.0.1", "localhost", "::1"}
        and os.getenv("COSYVOICE_ALLOW_REMOTE", "0") != "1"
    ):
        raise RuntimeError(
            "为保护本地音频路径，CosyVoice 服务默认只允许监听本机地址；"
            "确需远程监听请设置 COSYVOICE_ALLOW_REMOTE=1"
        )
    port = int(os.getenv("COSYVOICE_PORT", "50001"))
    uvicorn.run(app, host=host, port=port, log_level=os.getenv("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
