from __future__ import annotations

import abc
import asyncio
import hashlib
import json
import logging
import math
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional, Sequence

import httpx

from backend.config import Settings
from backend.models.domain import Segment, Transcript
from backend.utils.files import atomic_write_json, read_json


logger = logging.getLogger(__name__)
ProgressCallback = Callable[[int, int], Optional[Awaitable[None]]]
TRANSLATION_CACHE_VERSION = 3
TRANSLATION_PROMPT_VERSION = "subtitle-dubbing-zh-v3"
TRANSLATION_LENGTH_STRATEGY_VERSION = "zh-visible-chars-cps-v1"
ZH_CHARS_PER_SECOND = 4.4
MIN_ZH_CHARS = 4


# These marks can be omitted without dropping a spoken word. Delimiters such as
# quotes, brackets, slashes, hyphens, plus signs, and percent signs are
# intentionally excluded because removing one can change meaning or leave an
# unmatched pair. ASCII full stops inside numbers/abbreviations are protected
# separately below.
_OPTIONAL_SPOKEN_PUNCTUATION = frozenset("，。！？；：、,.!?;:…")
_CJK_CHARACTER = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


TRANSLATION_SYSTEM_PROMPT = """你是一名专业影视字幕翻译。

任务：把英文视频对白翻译成自然、口语化、适合中文配音的简体中文。

要求：
1. 准确保留原意。
2. 使用自然中文，不要逐词直译。
3. 保留人名、产品名、技术名词。
4. 不增加解释。
5. 不输出 Markdown。
6. 不输出任何翻译以外的内容。
7. 每个 target 的 max_zh_chars 是硬上限；译文所有可见字符（含汉字、字母、数字、标点和空格）都计入，绝不能超出。
8. 优先使用极简、自然、适合说出口的中文；少用逗号、语气词和会产生停顿的冗余连接词，保留核心事实、数字和专有名词。
9. targets 按时间顺序排列。相邻 target 之间可以调整从句、修饰语和措辞的边界，但整批总语义不得遗漏、重复或新增。
10. 每个输入 ID 必须恰好输出一次，ID 不得改变，译文不得为空。
11. 只输出严格 JSON 数组，元素格式为 {"id": 整数, "zh": "译文"}。
"""


class TranslationFormatError(RuntimeError):
    pass


def max_zh_chars_for_duration(
    duration: float,
    *,
    chars_per_second: float | None = None,
) -> int:
    """Return a conservative visible-character budget for Chinese dubbing."""
    cps = ZH_CHARS_PER_SECOND if chars_per_second is None else chars_per_second
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("duration 必须是非负有限数")
    if not math.isfinite(cps) or cps <= 0:
        raise ValueError("chars_per_second 必须是正有限数")
    return max(MIN_ZH_CHARS, math.ceil(duration * cps))


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


def _minimize_translation_formatting(text: str, max_chars: int) -> str | None:
    """Fit *text* by removing only redundant spacing and spoken punctuation.

    This is deliberately not a generic truncation helper: every Han character,
    letter, number, and symbol is retained in its original order. Returning
    ``None`` means formatting alone cannot satisfy the hard dubbing budget.
    """
    candidate = re.sub(r"\s+", " ", text.strip())
    candidate = re.sub(
        rf"(?<=[{_CJK_CHARACTER}]) (?=[{_CJK_CHARACTER}])",
        "",
        candidate,
    )
    punctuation_class = re.escape("".join(_OPTIONAL_SPOKEN_PUNCTUATION))
    candidate = re.sub(rf"\s+([{punctuation_class}])", r"\1", candidate)
    candidate = re.sub(rf"([{punctuation_class}])\s+", r"\1", candidate)
    if len(candidate) <= max_chars:
        return candidate

    characters = list(candidate)

    def removable(index: int) -> bool:
        char = characters[index]
        if char not in _OPTIONAL_SPOKEN_PUNCTUATION:
            return False
        # Keep decimal points and dots inside ASCII abbreviations/identifiers.
        if char == "." and 0 < index < len(characters) - 1:
            previous = characters[index - 1]
            following = characters[index + 1]
            if previous.isascii() and following.isascii():
                if previous.isalnum() and following.isalnum():
                    return False
        # A colon between digits is part of a time or ratio, not decoration.
        if char == ":" and 0 < index < len(characters) - 1:
            if characters[index - 1].isdigit() and characters[index + 1].isdigit():
                return False
        return True

    # Sentence-final punctuation is the least consequential for TTS. Then
    # remove internal pauses from right to left, stopping as soon as the budget
    # is met so the output remains as readable as possible.
    prioritized_indices = [
        index
        for index in range(len(characters) - 1, -1, -1)
        if index == len(characters) - 1 and removable(index)
    ]
    prioritized_indices.extend(
        index
        for index in range(len(characters) - 1, -1, -1)
        if index not in prioritized_indices and removable(index)
    )
    removed: set[int] = set()
    for index in prioritized_indices:
        removed.add(index)
        if len(characters) - len(removed) <= max_chars:
            minimized = "".join(
                char for position, char in enumerate(characters) if position not in removed
            )
            return minimized
    return None


def parse_translation_json(
    payload: str | list[dict[str, Any]],
    expected_ids: Sequence[int],
    *,
    max_chars_by_id: dict[int, int] | None = None,
) -> dict[int, str]:
    try:
        data = json.loads(_extract_json_text(payload)) if isinstance(payload, str) else payload
    except json.JSONDecodeError as exc:
        raise TranslationFormatError(f"翻译 JSON 无法解析：{exc}") from exc
    if not isinstance(data, list):
        raise TranslationFormatError("翻译结果必须是 JSON 数组")
    translations: dict[int, str] = {}
    over_budget: list[str] = []
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
        max_chars = (max_chars_by_id or {}).get(segment_id)
        if max_chars is not None and len(text) > max_chars:
            over_budget.append(f"ID {segment_id}: {len(text)}>{max_chars}")
        if segment_id in translations:
            raise TranslationFormatError(f"翻译结果包含重复 ID：{segment_id}")
        translations[segment_id] = text
    expected = set(expected_ids)
    actual = set(translations)
    errors: list[str] = []
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        errors.append(f"翻译 ID 不匹配，缺少={missing}，多余={extra}")
    if over_budget:
        errors.append("译文超过 max_zh_chars：" + "，".join(over_budget))
    if errors:
        raise TranslationFormatError("；".join(errors))
    return translations


class Translator(abc.ABC):
    def __init__(self, model: str, *, timeout: float = 180.0, retries: int = 4):
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
        max_chars_by_id = {
            segment.id: max_zh_chars_for_duration(segment.duration)
            for segment in segments
        }
        base_prompt = self._translation_prompt(segments, context_before, context_after)
        prompt = base_prompt
        last_error: Exception | None = None
        last_valid_candidate: dict[int, str] | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = await self._request(TRANSLATION_SYSTEM_PROMPT, prompt)
                candidate = parse_translation_json(response, expected_ids)
                last_valid_candidate = candidate
                return parse_translation_json(
                    [
                        {"id": segment_id, "zh": candidate[segment_id]}
                        for segment_id in expected_ids
                    ],
                    expected_ids,
                    max_chars_by_id=max_chars_by_id,
                )
            except (TranslationFormatError, httpx.HTTPError, RuntimeError) as exc:
                last_error = exc
                logger.warning("[TRANSLATE] attempt %d/%d failed: %s", attempt, self.retries, exc)
                if attempt < self.retries:
                    prompt = (
                        base_prompt
                        + f"\n上一次具体错误：{exc}"
                        + "\n请一次检查并修正上面列出的所有 ID。只返回完整、有效、"
                        + "ID 完全匹配的 JSON 数组，并逐条严格遵守 max_zh_chars 硬上限。"
                    )
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))

        if last_valid_candidate is None:
            raise RuntimeError(f"翻译在 {self.retries} 次尝试后仍失败：{last_error}")

        fallback_ids = [
            segment_id
            for segment_id in expected_ids
            if len(last_valid_candidate[segment_id]) > max_chars_by_id[segment_id]
        ]
        logger.warning(
            "[TRANSLATE] batch budget fallback to per-segment compression ids=%s",
            fallback_ids,
        )
        repaired = dict(last_valid_candidate)
        segments_by_id = {segment.id: segment for segment in segments}
        for segment_id in fallback_ids:
            segment = segments_by_id[segment_id]
            try:
                repaired[segment_id] = await self.compress(
                    segment.text,
                    last_valid_candidate[segment_id],
                    segment.duration,
                )
            except Exception as exc:
                raise RuntimeError(f"逐条压缩失败（ID {segment_id}）：{exc}") from exc

        try:
            return parse_translation_json(
                [
                    {"id": segment_id, "zh": repaired[segment_id]}
                    for segment_id in expected_ids
                ],
                expected_ids,
                max_chars_by_id=max_chars_by_id,
            )
        except TranslationFormatError as exc:
            raise RuntimeError(f"逐条压缩后的翻译仍不合规：{exc}") from exc

    async def compress(self, source: str, translated: str, duration: float) -> str:
        max_chars = max_zh_chars_for_duration(duration)
        system = (
            "你是中文影视配音编辑。把译文压缩成自然口语，保留原意与专有名词。"
            "max_zh_chars 是所有可见字符（含标点和空格）的硬上限。"
            "只输出严格 JSON 数组：[{\"id\":0,\"zh\":\"压缩后的译文\"}]，"
            "ID 必须为 0，不要 Markdown。"
        )
        base_prompt = (
            f"原文：{source}\n当前译文：{translated}\n"
            f"可用说话时长：{duration:.2f} 秒；max_zh_chars={max_chars}。"
            "请保留核心语义并显著缩短，最终译文绝不能超过该字符数。"
        )
        prompt = base_prompt
        last_error: Exception | None = None
        last_valid_candidate: str | None = None
        for attempt in range(1, self.retries + 1):
            try:
                raw = await self._request(system, prompt)
                candidate = parse_translation_json(raw, [0])
                last_valid_candidate = candidate[0]
                return parse_translation_json(
                    [{"id": 0, "zh": last_valid_candidate}],
                    [0],
                    max_chars_by_id={0: max_chars},
                )[0]
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    prompt = (
                        base_prompt
                        + f"\n上一次具体错误：{exc}。请进一步压缩并严格修正。"
                    )
                    await asyncio.sleep(min(2 ** (attempt - 1), 4))

        if last_valid_candidate is not None:
            minimized = _minimize_translation_formatting(
                last_valid_candidate,
                max_chars,
            )
            if minimized is not None:
                logger.warning(
                    "[TRANSLATE] compression formatting fallback %d->%d chars",
                    len(last_valid_candidate),
                    len(minimized),
                )
                return parse_translation_json(
                    [{"id": 0, "zh": minimized}],
                    [0],
                    max_chars_by_id={0: max_chars},
                )[0]
            raise RuntimeError(
                "字幕压缩失败：存在结构和 ID 有效的候选，"
                f"但仅移除标点/冗余空白仍无法满足 {max_chars} 字上限；"
                f"最后错误：{last_error}"
            )
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
                "max_zh_chars": max_zh_chars_for_duration(item.duration),
                "text": item.text,
            }
            for item in segments
        ]
        total_budget = sum(item["max_zh_chars"] for item in targets)
        return (
            "上下文仅用于理解，不要翻译上下文中的 ID：\n"
            + json.dumps(context, ensure_ascii=False)
            + f"\n必须返回恰好 {len(targets)} 个元素，ID 顺序为 "
            + json.dumps([item["id"] for item in targets], ensure_ascii=False)
            + "，最外层必须是 JSON 数组。"
            + f"\n本批总字符预算为 {total_budget}；每条 max_zh_chars 都是独立硬上限，"
            + "所有可见字符（含标点和空格）均计数。相邻片段可协调措辞边界，"
            + "但不得遗漏或重复整批语义。先压缩措辞，再输出 JSON。"
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
                        # Subtitle translation needs concise structured output,
                        # not a long hidden reasoning trace. Thinking-capable
                        # Ollama models enable that trace by default.
                        "think": False,
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
        return OllamaTranslator(
            config.ollama_base_url,
            model or config.ollama_model,
            timeout=config.translation_timeout_seconds,
        )
    if provider in {"openai", "openai-compatible", "openai_compatible"}:
        return OpenAICompatibleTranslator(
            config.openai_base_url,
            config.openai_api_key,
            model or config.openai_model,
            timeout=config.translation_timeout_seconds,
        )
    raise ValueError(f"不支持的翻译提供方：{provider}")


def _batches(segments: Sequence[Segment], maximum: int = 6, maximum_chars: int = 1600) -> list[list[Segment]]:
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


_TRANSLATION_CACHE_GLOBAL_KEYS = (
    "provider",
    "model",
    "source_language",
    "target_language",
    "prompt_version",
    "length_strategy",
)
_TRANSLATION_CACHE_INPUT_KEYS = frozenset(
    (*_TRANSLATION_CACHE_GLOBAL_KEYS, "segments")
)
_TRANSLATION_CACHE_SEGMENT_KEYS = frozenset(
    {"id", "start", "end", "source"}
)
_TRANSLATION_CACHE_ITEM_KEYS = frozenset(
    {*_TRANSLATION_CACHE_SEGMENT_KEYS, "zh"}
)


def _translation_cache_global_input(
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
        "prompt_version": TRANSLATION_PROMPT_VERSION,
        "length_strategy": {
            "version": TRANSLATION_LENGTH_STRATEGY_VERSION,
            "zh_chars_per_second": ZH_CHARS_PER_SECOND,
            "minimum_zh_chars": MIN_ZH_CHARS,
        },
    }


def _translation_cache_segment_input(segment: Segment) -> dict[str, Any]:
    return {
        "id": segment.id,
        "start": segment.start,
        "end": segment.end,
        "source": segment.text,
    }


def _translation_cache_identity_matches(
    value: dict[str, Any],
    expected: dict[str, Any],
) -> bool:
    return all(
        key in value
        and type(value[key]) is type(expected_value)
        and value[key] == expected_value
        for key, expected_value in expected.items()
    )


def _translation_cache_input(
    transcript: Transcript,
    *,
    provider: str,
    model: str,
    target_language: str,
) -> dict[str, Any]:
    return {
        **_translation_cache_global_input(
            transcript,
            provider=provider,
            model=model,
            target_language=target_language,
        ),
        "segments": [
            _translation_cache_segment_input(segment)
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
) -> tuple[dict[int, str], bool]:
    """Load reusable translations and report whether the whole input matched.

    A complete fingerprint hit is the fast path.  When only the segment list
    changed, v3 cache entries may still be reused one-by-one, but only after
    the stored cache has proved internally consistent and every global input
    remains exactly the same.
    """
    if not path.exists():
        return {}, False
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
            return {}, False

        cached_input = data.get("input")
        if (
            not isinstance(cached_input, dict)
            or frozenset(cached_input) != _TRANSLATION_CACHE_INPUT_KEYS
            or not isinstance(cached_input.get("segments"), list)
            or data.get("input_fingerprint")
            != _translation_cache_fingerprint(cached_input)
        ):
            logger.info("[TRANSLATE] 缓存格式或指纹无效，将重新翻译：%s", path)
            return {}, False

        exact_input_match = (
            data.get("input_fingerprint") == expected_fingerprint
            and cached_input == expected_input
        )
        if not exact_input_match:
            expected_globals = {
                key: expected_input[key] for key in _TRANSLATION_CACHE_GLOBAL_KEYS
            }
            cached_globals = {
                key: cached_input[key] for key in _TRANSLATION_CACHE_GLOBAL_KEYS
            }
            top_level_globals = {
                "provider": data.get("provider"),
                "model": data.get("model"),
                "source_language": data.get("source_language"),
                "target_language": data.get("target_language"),
            }
            expected_top_level_globals = {
                key: expected_globals[key] for key in top_level_globals
            }
            if (
                cached_globals != expected_globals
                or top_level_globals != expected_top_level_globals
            ):
                logger.info("[TRANSLATE] 翻译全局输入已变化，忽略缓存：%s", path)
                return {}, False
            logger.info("[TRANSLATE] 字幕片段已变化，尝试逐段复用缓存：%s", path)

        items = data.get("segments", [])
        if not isinstance(items, list):
            return {}, False

        # The fingerprint covers ``input.segments``.  Bind every translation
        # item back to that protected identity before comparing it with the
        # current transcript; item metadata alone is not fingerprinted.
        protected_by_id: dict[int, dict[str, Any]] = {}
        for protected in cached_input["segments"]:
            if (
                not isinstance(protected, dict)
                or frozenset(protected) != _TRANSLATION_CACHE_SEGMENT_KEYS
                or type(protected.get("id")) is not int
                or type(protected.get("start")) is not float
                or type(protected.get("end")) is not float
                or type(protected.get("source")) is not str
                or protected["id"] in protected_by_id
            ):
                logger.info("[TRANSLATE] 缓存片段身份无效，将重新翻译：%s", path)
                return {}, False
            protected_by_id[protected["id"]] = protected

        # A duplicated valid entry is ambiguous even when one copy happens to
        # match, so poison that ID instead of silently choosing one.
        cached_by_id: dict[int, dict[str, Any]] = {}
        ambiguous_ids: set[int] = set()
        for item in items:
            if (
                not isinstance(item, dict)
                or frozenset(item) != _TRANSLATION_CACHE_ITEM_KEYS
                or type(item.get("id")) is not int
            ):
                continue
            segment_id = item["id"]
            protected = protected_by_id.get(segment_id)
            if protected is None or not _translation_cache_identity_matches(
                item,
                protected,
            ):
                continue
            if segment_id in cached_by_id:
                ambiguous_ids.add(segment_id)
                cached_by_id.pop(segment_id, None)
            elif segment_id not in ambiguous_ids:
                cached_by_id[segment_id] = item

        result: dict[int, str] = {}
        for segment in transcript.segments:
            item = cached_by_id.get(segment.id)
            if item is None:
                continue
            expected_segment = _translation_cache_segment_input(segment)
            identity_matches = _translation_cache_identity_matches(
                item,
                expected_segment,
            )
            text = item["zh"]
            if identity_matches and isinstance(text, str) and text.strip():
                result[segment.id] = text.strip()
        return result, exact_input_match
    except Exception as exc:
        logger.warning("[TRANSLATE] 忽略损坏的缓存 %s: %s", path, exc)
        return {}, False


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
    translations, exact_cache_input = _load_cached_translations(
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
    if not output_path.exists() or not exact_cache_input:
        save_translation_cache(
            output_path,
            transcript,
            translations,
            provider,
            translator.model,
            target_language,
        )
    return updated
