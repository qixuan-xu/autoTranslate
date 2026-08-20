from __future__ import annotations

import json
import hashlib
import logging
import math
import tempfile
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from backend.config import Settings
from backend.models.domain import Segment, Transcript, WordTimestamp
from backend.utils.files import atomic_write_json
from backend.utils.process import require_executable, run_process


logger = logging.getLogger(__name__)
LogCallback = Callable[[str], Optional[Awaitable[None]]]
ASR_CACHE_VERSION = 1
ASR_CACHE_METADATA_NAME = "transcript.cache.json"


def _canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _audio_identity(audio_path: Path) -> dict[str, int | str]:
    """Return a bounded-memory content identity for an ASR input file."""

    audio_path = audio_path.expanduser().resolve()
    if not audio_path.is_file():
        raise RuntimeError(f"Whisper 输入音频不存在：{audio_path}")
    before = audio_path.stat()
    digest = hashlib.sha256()
    with audio_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = audio_path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError(f"读取期间 Whisper 输入音频发生变化：{audio_path}")
    return {"size": after.st_size, "sha256": digest.hexdigest()}


def _normalized_language(language: str | None) -> str:
    value = str(language or "auto").strip().casefold()
    return "auto" if value in {"", "auto", "automatic"} else value


def build_asr_cache_metadata(
    audio_path: Path,
    *,
    model: str,
    language: str,
) -> dict[str, Any]:
    """Describe every input which can change the normalized Whisper result."""

    cache_input = {
        "audio": _audio_identity(audio_path),
        "model": str(model).strip(),
        "language": _normalized_language(language),
        "task": "transcribe",
        "word_timestamps": True,
    }
    return {
        "version": ASR_CACHE_VERSION,
        "input": cache_input,
        "input_fingerprint": _canonical_json_sha256(cache_input),
    }


def load_cached_transcript(
    transcript_path: Path,
    metadata_path: Path,
    expected_metadata: dict[str, Any],
) -> Transcript | None:
    """Load only a transcript whose sidecar exactly matches current ASR inputs."""

    if not transcript_path.is_file() or not metadata_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata != expected_metadata:
            return None
        return Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        logger.warning("[ASR] 忽略损坏的转写缓存：%s", exc)
        return None


class WhisperAdapter:
    def __init__(self, config: Settings):
        self.config = config

    async def transcribe(
        self,
        audio_path: Path,
        output_dir: Path,
        *,
        model: str | None = None,
        language: str = "auto",
        on_log: LogCallback | None = None,
    ) -> Transcript:
        output_dir.mkdir(parents=True, exist_ok=True)
        audio_path = audio_path.expanduser().resolve()
        transcript_path = output_dir / "transcript.json"
        metadata_path = output_dir / ASR_CACHE_METADATA_NAME
        original_srt = output_dir / "original.srt"
        selected_model = str(model or self.config.whisper_model).strip()
        requested_language = _normalized_language(language)
        expected_metadata = build_asr_cache_metadata(
            audio_path,
            model=selected_model,
            language=requested_language,
        )
        transcript = load_cached_transcript(
            transcript_path,
            metadata_path,
            expected_metadata,
        )
        if transcript is not None:
            if not original_srt.exists():
                from backend.pipeline.subtitle import write_srt

                write_srt(
                    transcript.segments,
                    original_srt,
                    translated=False,
                    preserve_ids=False,
                )
            logger.info("[ASR] 使用匹配输入指纹的 transcript.json 缓存")
            return transcript
        if transcript_path.exists():
            logger.info("[ASR] 输入、模型或语言已变化，将重新识别")

        binary = require_executable(self.config.whisper_bin, "Whisper CLI")

        async def relay(line: str) -> None:
            if on_log and line.strip():
                result = on_log(line)
                if result is not None:
                    await result

        logger.info("[ASR] Whisper started model=%s", selected_model)
        with tempfile.TemporaryDirectory(prefix=".whisper-", dir=str(output_dir)) as temp_name:
            raw_output_dir = Path(temp_name)
            args = [
                binary,
                str(audio_path),
                "--model",
                selected_model,
                "--output_dir",
                str(raw_output_dir),
                "--output_format",
                "json",
                "--word_timestamps",
                "True",
                "--task",
                "transcribe",
                "--verbose",
                "False",
            ]
            if requested_language != "auto":
                args.extend(["--language", requested_language])
            await run_process(args, on_line=relay)
            raw_path = raw_output_dir / f"{audio_path.stem}.json"
            if not raw_path.exists():
                json_files = list(raw_output_dir.glob("*.json"))
                if len(json_files) != 1:
                    raise RuntimeError("Whisper 已结束，但找不到唯一的 JSON 输出")
                raw_path = json_files[0]
            transcript = self._normalize(json.loads(raw_path.read_text(encoding="utf-8")))
        # Publish metadata last.  A crash between these atomic writes leaves an
        # intentionally invalid cache instead of pairing new text with old SRT.
        atomic_write_json(transcript_path, transcript.model_dump())
        from backend.pipeline.subtitle import write_srt

        write_srt(
            transcript.segments,
            original_srt,
            translated=False,
            preserve_ids=False,
        )
        atomic_write_json(metadata_path, expected_metadata)
        logger.info("[ASR] detected language=%s", transcript.language)
        logger.info("[ASR] segments=%d", len(transcript.segments))
        return transcript

    @staticmethod
    def _normalize(raw: dict) -> Transcript:
        segments: list[Segment] = []
        for index, item in enumerate(raw.get("segments") or []):
            text = str(item.get("text") or "").strip()
            start = max(0.0, float(item.get("start", 0.0)))
            end = float(item.get("end", start))
            if not text or end <= start:
                continue
            words: list[WordTimestamp] = []
            for word in item.get("words") or []:
                word_start = float(word.get("start", start))
                word_end = float(word.get("end", word_start))
                if word_end <= word_start:
                    continue
                words.append(
                    WordTimestamp(
                        word=str(word.get("word") or ""),
                        start=max(0.0, word_start),
                        end=word_end,
                        probability=word.get("probability"),
                    )
                )
            confidence = None
            if item.get("avg_logprob") is not None:
                confidence = min(1.0, max(0.0, math.exp(float(item["avg_logprob"]))))
            segments.append(
                Segment(
                    id=int(item.get("id", index)),
                    start=start,
                    end=end,
                    text=text,
                    words=words,
                    confidence=confidence,
                )
            )
        duration = max((segment.end for segment in segments), default=None)
        return Transcript(
            language=str(raw.get("language") or "unknown"),
            segments=segments,
            duration=duration,
        )
