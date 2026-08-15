from __future__ import annotations

import json
import logging
import math
import tempfile
from pathlib import Path
from typing import Awaitable, Callable, Optional

from backend.config import Settings
from backend.models.domain import Segment, Transcript, WordTimestamp
from backend.utils.files import atomic_write_json
from backend.utils.process import require_executable, run_process


logger = logging.getLogger(__name__)
LogCallback = Callable[[str], Optional[Awaitable[None]]]


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
        transcript_path = output_dir / "transcript.json"
        original_srt = output_dir / "original.srt"
        if transcript_path.exists():
            try:
                transcript = Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
                if not original_srt.exists():
                    from backend.pipeline.subtitle import write_srt

                    write_srt(
                        transcript.segments,
                        original_srt,
                        translated=False,
                        preserve_ids=False,
                    )
                logger.info("[ASR] 使用缓存 transcript.json")
                return transcript
            except Exception as exc:
                logger.warning("[ASR] 缓存不可用，将重新识别：%s", exc)

        binary = require_executable(self.config.whisper_bin, "Whisper CLI")
        selected_model = model or self.config.whisper_model
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
            if language and language.lower() not in {"auto", "automatic"}:
                args.extend(["--language", language])
            await run_process(args, on_line=relay)
            raw_path = raw_output_dir / f"{audio_path.stem}.json"
            if not raw_path.exists():
                json_files = list(raw_output_dir.glob("*.json"))
                if len(json_files) != 1:
                    raise RuntimeError("Whisper 已结束，但找不到唯一的 JSON 输出")
                raw_path = json_files[0]
            transcript = self._normalize(json.loads(raw_path.read_text(encoding="utf-8")))
        atomic_write_json(transcript_path, transcript.model_dump())
        from backend.pipeline.subtitle import write_srt

        write_srt(
            transcript.segments,
            original_srt,
            translated=False,
            preserve_ids=False,
        )
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
