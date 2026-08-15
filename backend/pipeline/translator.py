from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

import httpx

from backend.config import Settings
from backend.models.domain import Segment, Transcript
from backend.utils.files import atomic_write_json, read_json


logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int], Optional[Awaitable[None]]]
TRANSLATION_CACHE_VERSION = 2


TRANSLATION_SYSTEM_PROMPT = """你是一名专业影视字幕翻译。

任务：把英文视频对白翻译成自然、口语化、适合中文配音的简体中文。

要求：
1. 准确保留原意。
2. 使用自然中文，不要逐词直译。
3. 保留人名、产品名、技术名词。
4. 不增加解释。
5. 不输出 Markdown。
6. 不输出任何翻译以外的内容。
7. 根据每条说话时间控制译文长度。
8. 优先使用简短、自然、适合说出口的中文。
9. 必须保持输入 ID 不变。
10. 只输出严格 JSON 数组，元素格式为 {"id": 整数, "zh": "译文"}。
"""


class TranslationFormatError(RuntimeError):
    pass


def _extract_json_text(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        raise TranslationFormatError("翻译模型没有返回 JSON 数组")
    return text[start : end + 1]


def parse_translation_json(payload: str | list[dict[str, Any]], expected_ids: Sequence[int]) -> dict[int, str]:
    try:
        data = json.loads(_extract_json_text(payload)) if isinstance(payload, str) else payload
    except json.JSONDecodeError as exc:
        raise TranslationFormatError(f"翻译 JSON 无法解析：{exc}") from exc
    if not isinstance(data, list):
        raise TranslationFormatError("翻译结果必须是 JSON 数组")
    translations: dict[int, str] = {}
    for item in data:
        if not isinstance(item, dict) or "id" not in item or "zh" not in item:
            raise TranslationFormatError("翻译元素必须包含 id 和 zh")
        try:
            segment_id = int(item["id"])
        except (TypeError, ValueError) as exc:
            raise TranslationFormatError("翻译 id 必须是整数") from exc
        text = str(item["zh"]).strip()
        if not text:
            raise TranslationFormatError(f"ID {segment_id} 的译文为空")
        if segment_id in translations:
            raise TranslationFormatError(f"翻译结果包含重复 ID：{segment_id}")
        translations[segment_id] = text
    expected = set(expected_ids)
    actual = set(translations)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise TranslationFormatError(f"翻译 ID 不匹配，缺少={missing}，多余={extra}")
    return translations


class Translator(abc.ABC):
    def __init__(self, model: str, *, timeout: float = 180.0, retries: int = 3):
        self.model = model
        self.timeout = timeout
        self.retries = retries

    async def translate(
        self,
        segments: Sequence[Segment],
        *,
        context_before: Sequence[Segment] = (),
        context_after: Sequence[Segment] = (),
    ) -> dict[int, str]:
        expected_ids = [segment.id for segment in segments]
        prompt = self._translation_prompt(segments, context_before, context_after)
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = await self._request(TRANSLATION_SYSTEM_PROMPT, prompt)
                return parse_translation_json(response, expected_ids)
            except (TranslationFormatError, httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                logger.warning("[TRANSLATE] attempt %d/%d failed: %s", attempt, self.retries, exc)
                if attempt < self.retries:
                    prompt += "\n上一次输出不合规。请只返回完整、有效、ID 完全匹配的 JSON 数组。"
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError(f"翻译在 {self.retries} 次尝试后仍失败：{last_error}")

    async def compress(self, source: str, translated: str, duration: float) -> str:
        system = (
            "你是中文影视配音编辑。把译文压缩成自然口语，保留原意与专有名词。"
            "只输出 JSON：{\"zh\":\"压缩后的译文\"}，不要 Markdown。"
        )
        prompt = (
            f"原文：{source}\n当前译文：{translated}\n"
            f"可用说话时长：{duration:.2f} 秒。请显著缩短，但不要变得生硬。"
        )
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                raw = await self._request(system, prompt)
                text = raw.strip()
                if text.startswith("```"):
                    text = "\n".join(text.splitlines()[1:-1])
                start, end = text.find("{"), text.rfind("}")
                data = json.loads(text[start : end + 1])
                compressed = str(data.get("zh") or "").strip()
                if not compressed:
                    raise TranslationFormatError("压缩结果为空")
                return compressed
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))
        raise RuntimeError(f"字幕压缩失败：{last_error}")

    @abc.abstractmethod
    async def _request(self, system_prompt: str, user_prompt: str) -> str:
        raise NotImplementedError

    @staticmethod
    def _translation_prompt(
        segments: Sequence[Segment],
        context_before: Sequence[Segment],
        context_after: Sequence[Segment],
    ) -> str:
        context = [
            {"id": item.id, "text": item.text}
            for item in [*context_before, *context_after]
        ]
        targets = [
            {
                "id": item.id,
                "start": round(item.start, 3),
                "end": round(item.end, 3),
                "duration": round(item.duration, 3),
                "text": item.text,
            }
            for item in segments
        ]
        return (
            "上下文仅用于理解，不要翻译上下文中的 ID：\n"
            + json.dumps(context, ensure_ascii=False)
            + f"\n必须返回恰好 {len(targets)} 个元素，ID 顺序为 "
            + json.dumps([item["id"] for item in targets], ensure_ascii=False)
            + "，最外层必须是 JSON 数组。"
            + "\n需要翻译的片段：\n"
            + json.dumps(targets, ensure_ascii=False)
        )


class OllamaTranslator(Translator):
    def __init__(self, base_url: str, model: str, **kwargs: Any):
        super().__init__(model, **kwargs)
        self.base_url = base_url.rstrip("/")

    async def _request(self, system_prompt: str, user_prompt: str) -> str:
        if not self.model:
            raise RuntimeError("未配置 Ollama 模型")
        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json={
                        "model": self.model,
                        "stream": False,
                        "format": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer"},
                                    "zh": {"type": "string"},
                                },
                                "required": ["id", "zh"],
                                "additionalProperties": False,
                            },
                        },
                        "options": {"temperature": 0.2},
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.ConnectError as exc:
            raise RuntimeError(f"无法连接 Ollama：{self.base_url}。请先运行 ollama serve。") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError("Ollama 翻译请求超时") from exc
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise RuntimeError("Ollama 返回结构异常") from exc


class OpenAICompatibleTranslator(Translator):
    def __init__(self, base_url: str, api_key: str, model: str, **kwargs: Any):
        super().__init__(model, **kwargs)
        base = base_url.rstrip("/")
        self.endpoint = base if base.endswith("/chat/completions") else f"{base}/chat/completions"
        self.api_key = api_key

    async def _request(self, system_prompt: str, user_prompt: str) -> str:
        if not self.endpoint.startswith(("http://", "https://")):
            raise RuntimeError("OPENAI_BASE_URL 未配置或无效")
        if not self.model:
            raise RuntimeError("OPENAI_MODEL 未配置")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.endpoint,
                    headers=headers,
                    json={
                        "model": self.model,
                        "temperature": 0.2,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                response.raise_for_status()
                data = response.json()
        except httpx.ConnectError as exc:
            raise RuntimeError(f"无法连接 OpenAI-compatible 服务：{self.endpoint}") from exc
        except httpx.TimeoutException as exc:
            raise RuntimeError("OpenAI-compatible 翻译请求超时") from exc
        try:
            content = data["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(item.get("text", "")) for item in content if isinstance(item, dict))
            return str(content)
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("OpenAI-compatible 服务返回结构异常") from exc


def build_translator(config: Settings, provider: str, model: str = "") -> Translator:
    provider = provider.strip().lower()
    if provider == "ollama":
        return OllamaTranslator(config.ollama_base_url, model or config.ollama_model)
    if provider in {"openai", "openai-compatible", "openai_compatible"}:
        return OpenAICompatibleTranslator(
            config.openai_base_url,
            config.openai_api_key,
            model or config.openai_model,
        )
    raise ValueError(f"不支持的翻译提供方：{provider}")


def _batches(segments: Sequence[Segment], maximum: int = 8, maximum_chars: int = 1600) -> list[list[Segment]]:
    result: list[list[Segment]] = []
    current: list[Segment] = []
    chars = 0
    for segment in segments:
        size = len(segment.text)
        if current and (len(current) >= maximum or chars + size > maximum_chars):
            result.append(current)
            current = []
            chars = 0
        current.append(segment)
        chars += size
    if current:
        result.append(current)
    return result


def _translation_cache_input(
    transcript: Transcript,
    *,
    provider: str,
    model: str,
    target_language: str,
) -> dict[str, Any]:
    return {
        "provider": provider.strip().lower(),
        "model": model.strip(),
        "source_language": transcript.language,
        "target_language": target_language,
        "segments": [
            {
                "id": segment.id,
                "start": segment.start,
                "end": segment.end,
                "source": segment.text,
            }
            for segment in transcript.segments
        ],
    }


def _translation_cache_fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _load_cached_translations(
    path: Path,
    transcript: Transcript,
    *,
    provider: str,
    model: str,
    target_language: str,
) -> dict[int, str]:
    if not path.exists():
        return {}
    try:
        data = read_json(path)
        expected_input = _translation_cache_input(
            transcript,
            provider=provider,
            model=model,
            target_language=target_language,
        )
        expected_fingerprint = _translation_cache_fingerprint(expected_input)
        if not isinstance(data, dict) or data.get("cache_version") != TRANSLATION_CACHE_VERSION:
            logger.info("[TRANSLATE] 旧版缓存将重新翻译：%s", path)
            return {}
        if (
            data.get("input_fingerprint") != expected_fingerprint
            or data.get("input") != expected_input
        ):
            logger.info("[TRANSLATE] 输入或模型已变化，忽略缓存：%s", path)
            return {}
        valid_ids = {segment.id for segment in transcript.segments}
        items = data.get("segments", [])
        if not isinstance(items, list):
            return {}
        result = {}
        for item in items:
            segment_id = int(item["id"])
            text = str(item.get("zh") or "").strip()
            if segment_id in valid_ids and text:
                result[segment_id] = text
        return result
    except Exception as exc:
        logger.warning("[TRANSLATE] 忽略损坏的缓存 %s: %s", path, exc)
        return {}


def save_translation_cache(
    path: Path,
    transcript: Transcript,
    translations: dict[int, str],
    provider: str,
    model: str,
    target_language: str,
) -> None:
    cache_input = _translation_cache_input(
        transcript,
        provider=provider,
        model=model,
        target_language=target_language,
    )
    atomic_write_json(
        path,
        {
            "cache_version": TRANSLATION_CACHE_VERSION,
            "input_fingerprint": _translation_cache_fingerprint(cache_input),
            "input": cache_input,
            "provider": cache_input["provider"],
            "model": cache_input["model"],
            "source_language": transcript.language,
            "target_language": target_language,
            "segments": [
                {
                    "id": segment.id,
                    "start": segment.start,
                    "end": segment.end,
                    "source": segment.text,
                    "zh": translations[segment.id],
                }
                for segment in transcript.segments
                if segment.id in translations
            ],
        },
    )


async def translate_transcript(
    transcript: Transcript,
    output_path: Path,
    translator: Translator,
    *,
    provider: str,
    target_language: str = "zh-CN",
    on_progress: ProgressCallback | None = None,
) -> Transcript:
    translations = _load_cached_translations(
        output_path,
        transcript,
        provider=provider,
        model=translator.model,
        target_language=target_language,
    )
    missing = [segment for segment in transcript.segments if segment.id not in translations]
    batches = _batches(missing)
    total = len(batches)
    by_id_position = {segment.id: index for index, segment in enumerate(transcript.segments)}
    for batch_index, batch in enumerate(batches, start=1):
        first = by_id_position[batch[0].id]
        last = by_id_position[batch[-1].id]
        context_before = transcript.segments[max(0, first - 2) : first]
        context_after = transcript.segments[last + 1 : last + 3]
        logger.info("[TRANSLATE] batch %d/%d", batch_index, total)
        translations.update(
            await translator.translate(
                batch,
                context_before=context_before,
                context_after=context_after,
            )
        )
        save_translation_cache(
            output_path,
            transcript,
            translations,
            provider,
            translator.model,
            target_language,
        )
        if on_progress:
            result = on_progress(batch_index, total)
            if result is not None:
                await result

    updated = transcript.model_copy(deep=True)
    for segment in updated.segments:
        segment.translated_text = translations.get(segment.id)
    if len(translations) != len(transcript.segments):
        raise RuntimeError("翻译未覆盖全部字幕片段")
    if not output_path.exists():
        save_translation_cache(
            output_path,
            transcript,
            translations,
            provider,
            translator.model,
            target_language,
        )
    return updated
