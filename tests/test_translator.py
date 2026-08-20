import json
from dataclasses import replace

import pytest

import backend.pipeline.translator as translator_module
from backend.config import Settings
from backend.models.domain import Segment, Transcript
from backend.pipeline.translator import (
    MIN_ZH_CHARS,
    OllamaTranslator,
    TRANSLATION_LENGTH_STRATEGY_VERSION,
    TRANSLATION_PROMPT_VERSION,
    ZH_CHARS_PER_SECOND,
    TranslationFormatError,
    Translator,
    _batches,
    build_translator,
    max_zh_chars_for_duration,
    parse_translation_json,
    translate_transcript,
)


class FakeTranslator(Translator):
    def __init__(self, model: str = "fake"):
        super().__init__(model, retries=1)
        self.calls = 0
        self.requested_ids: list[list[int]] = []

    async def _request(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        targets = json.loads(user_prompt.split("需要翻译的片段：\n", 1)[1])
        self.requested_ids.append([item["id"] for item in targets])
        return json.dumps(
            [{"id": item["id"], "zh": f"译文 {item['id']}"} for item in targets],
            ensure_ascii=False,
        )


def test_parse_translation_json_accepts_fenced_array():
    value = "```json\n[{\"id\": 7, \"zh\": \"自然中文\"}]\n```"
    assert parse_translation_json(value, [7]) == {7: "自然中文"}


def test_parse_translation_json_rejects_missing_ids():
    with pytest.raises(TranslationFormatError, match="ID 不匹配"):
        parse_translation_json('[{"id": 1, "zh": "一"}]', [1, 2])


def test_parse_translation_json_reports_every_over_budget_id():
    payload = [
        {"id": 4, "zh": "四" * 21},
        {"id": 1, "zh": "一" * 22},
        {"id": 5, "zh": "五" * 20},
    ]
    with pytest.raises(TranslationFormatError) as captured:
        parse_translation_json(
            payload,
            [4, 1, 5],
            max_chars_by_id={4: 20, 1: 19, 5: 18},
        )

    message = str(captured.value)
    assert "ID 4: 21>20" in message
    assert "ID 1: 22>19" in message
    assert "ID 5: 20>18" in message


def test_duration_budget_has_four_character_floor_and_uses_configured_cps():
    assert ZH_CHARS_PER_SECOND == 4.4
    assert max_zh_chars_for_duration(0.2) == MIN_ZH_CHARS == 4
    assert max_zh_chars_for_duration(1.01) == 5
    assert max_zh_chars_for_duration(2.5) == 11


def test_translation_batches_limit_targets_to_six():
    segments = [
        Segment(id=index, start=index, end=index + 1, text=f"segment {index}")
        for index in range(7)
    ]
    assert [len(batch) for batch in _batches(segments)] == [6, 1]


@pytest.mark.asyncio
async def test_translation_prompt_contains_per_segment_hard_budgets():
    captured: dict[str, str] = {}

    class CapturingTranslator(Translator):
        async def _request(self, system_prompt: str, user_prompt: str) -> str:
            captured["system"] = system_prompt
            captured["user"] = user_prompt
            targets = json.loads(user_prompt.split("需要翻译的片段：\n", 1)[1])
            return json.dumps(
                [{"id": item["id"], "zh": "中"} for item in targets],
                ensure_ascii=False,
            )

    segments = [
        Segment(id=10, start=0, end=0.2, text="Well"),
        Segment(id=11, start=0.2, end=1.21, text="hello"),
        Segment(id=12, start=1.21, end=3.71, text="world"),
    ]
    await CapturingTranslator("fake", retries=1).translate(segments)

    targets = json.loads(captured["user"].split("需要翻译的片段：\n", 1)[1])
    assert [item["max_zh_chars"] for item in targets] == [4, 5, 11]
    assert "本批总字符预算为 20" in captured["user"]
    assert "max_zh_chars 是硬上限" in captured["system"]
    assert "相邻 target" in captured["system"]
    assert "整批总语义不得遗漏、重复或新增" in captured["system"]


@pytest.mark.asyncio
async def test_batch_translation_rejects_wrong_ids():
    class InvalidBatchTranslator(Translator):
        def __init__(self, response: list[dict]):
            super().__init__("fake", retries=1)
            self.response = response

        async def _request(self, _system_prompt: str, _user_prompt: str) -> str:
            return json.dumps(self.response, ensure_ascii=False)

    segments = [
        Segment(id=1, start=0, end=1, text="Hello"),
        Segment(id=2, start=1, end=2, text="world"),
    ]
    with pytest.raises(RuntimeError, match="ID 不匹配"):
        await InvalidBatchTranslator(
            [{"id": 1, "zh": "你好"}, {"id": 999, "zh": "世界"}]
        ).translate(segments)


@pytest.mark.asyncio
async def test_retry_prompt_contains_all_specific_budget_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    prompts: list[str] = []

    async def no_sleep(_seconds: float) -> None:
        return None

    class FixingTranslator(Translator):
        async def _request(self, _system_prompt: str, user_prompt: str) -> str:
            prompts.append(user_prompt)
            if len(prompts) == 1:
                return json.dumps(
                    [
                        {"id": 4, "zh": "四" * 21},
                        {"id": 1, "zh": "一" * 22},
                    ],
                    ensure_ascii=False,
                )
            return '[{"id": 4, "zh": "短"}, {"id": 1, "zh": "短"}]'

    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    segments = [
        Segment(id=4, start=0, end=20 / 4.4, text="four"),
        Segment(id=1, start=5, end=5 + 19 / 4.4, text="one"),
    ]
    translator = FixingTranslator("fake", retries=2)

    assert await translator.translate(segments) == {4: "短", 1: "短"}
    assert len(prompts) == 2
    assert "上一次具体错误：译文超过 max_zh_chars" in prompts[1]
    assert "ID 4: 21>20" in prompts[1]
    assert "ID 1: 22>19" in prompts[1]
    assert "一次检查并修正上面列出的所有 ID" in prompts[1]


@pytest.mark.asyncio
async def test_over_budget_batch_falls_back_to_only_offending_segments(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    requests = 0
    compressed: list[tuple[str, str, float]] = []

    async def no_sleep(_seconds: float) -> None:
        return None

    class FallbackTranslator(Translator):
        async def _request(self, _system_prompt: str, _user_prompt: str) -> str:
            nonlocal requests
            requests += 1
            marker = "甲" if requests == 1 else "乙"
            return json.dumps(
                [
                    {"id": 1, "zh": marker * 6},
                    {"id": 2, "zh": "合格"},
                    {"id": 3, "zh": marker * 7},
                ],
                ensure_ascii=False,
            )

        async def compress(self, source: str, translated: str, duration: float) -> str:
            compressed.append((source, translated, duration))
            return {"first": "短一", "third": "短三"}[source]

    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    segments = [
        Segment(id=1, start=0, end=1, text="first"),
        Segment(id=2, start=1, end=2, text="second"),
        Segment(id=3, start=2, end=3, text="third"),
    ]
    translator = FallbackTranslator("fake", retries=2)

    with caplog.at_level("WARNING"):
        result = await translator.translate(segments)

    assert requests == 2
    assert result == {1: "短一", 2: "合格", 3: "短三"}
    assert [(source, text) for source, text, _duration in compressed] == [
        ("first", "乙" * 6),
        ("third", "乙" * 7),
    ]
    assert "fallback to per-segment compression ids=[1, 3]" in caplog.text


@pytest.mark.asyncio
async def test_invalid_ids_never_use_compression_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    requests = 0
    compression_calls = 0

    async def no_sleep(_seconds: float) -> None:
        return None

    class InvalidIdsTranslator(Translator):
        async def _request(self, _system_prompt: str, _user_prompt: str) -> str:
            nonlocal requests
            requests += 1
            return '[{"id": 999, "zh": "错误"}]'

        async def compress(self, source: str, translated: str, duration: float) -> str:
            nonlocal compression_calls
            compression_calls += 1
            return "不应调用"

    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    translator = InvalidIdsTranslator("fake")
    segments = [Segment(id=1, start=0, end=1, text="first")]

    with pytest.raises(RuntimeError, match="ID 不匹配"):
        await translator.translate(segments)

    assert requests == 4
    assert compression_calls == 0


@pytest.mark.asyncio
async def test_compression_fallback_still_fails_if_result_exceeds_budget():
    class StillLongTranslator(Translator):
        async def _request(self, _system_prompt: str, _user_prompt: str) -> str:
            return '[{"id": 1, "zh": "原始译文太长"}]'

        async def compress(self, source: str, translated: str, duration: float) -> str:
            return "压缩以后仍长"

    translator = StillLongTranslator("fake", retries=1)
    segments = [Segment(id=1, start=0, end=1, text="first")]

    with pytest.raises(RuntimeError, match="逐条压缩后的翻译仍不合规.*ID 1: 6>5"):
        await translator.translate(segments)


@pytest.mark.asyncio
async def test_builtin_compress_uses_array_contract_and_strict_budget():
    captured: dict[str, str] = {}

    class CompressionTranslator(Translator):
        async def _request(self, system_prompt: str, user_prompt: str) -> str:
            captured["system"] = system_prompt
            captured["user"] = user_prompt
            return '[{"id": 0, "zh": "精简译文"}]'

    translator = CompressionTranslator("fake", retries=1)

    assert await translator.compress("long source", "冗长的原始译文", 2.0) == "精简译文"
    assert '[{"id":0,"zh":"压缩后的译文"}]' in captured["system"]
    assert "max_zh_chars=9" in captured["user"]


@pytest.mark.asyncio
async def test_builtin_compress_retry_receives_specific_budget_error(
    monkeypatch: pytest.MonkeyPatch,
):
    prompts: list[str] = []

    async def no_sleep(_seconds: float) -> None:
        return None

    class CompressionTranslator(Translator):
        async def _request(self, _system_prompt: str, user_prompt: str) -> str:
            prompts.append(user_prompt)
            if len(prompts) == 1:
                return '[{"id": 0, "zh": "一二三四五六七八九十"}]'
            return '[{"id": 0, "zh": "精简译文"}]'

    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    translator = CompressionTranslator("fake", retries=2)

    assert await translator.compress("long source", "冗长的原始译文", 2.0) == "精简译文"
    assert len(prompts) == 2
    assert "上一次具体错误：译文超过 max_zh_chars：ID 0: 10>9" in prompts[1]


@pytest.mark.asyncio
async def test_builtin_compress_minimizes_real_17_to_16_punctuation_case(
    monkeypatch: pytest.MonkeyPatch,
):
    requests = 0
    candidate = "美国转向别处，任由国民党自生自灭。"
    assert len(candidate) == 17
    assert max_zh_chars_for_duration(3.42) == 16

    async def no_sleep(_seconds: float) -> None:
        return None

    class CompressionTranslator(Translator):
        async def _request(self, _system_prompt: str, _user_prompt: str) -> str:
            nonlocal requests
            requests += 1
            return json.dumps([{"id": 0, "zh": candidate}], ensure_ascii=False)

    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    translator = CompressionTranslator("fake", retries=4)

    assert await translator.compress(
        "the US decided to concentrate on other things and leave the "
        "nationalists to their fate.",
        "美国决定关注其他事务，任由国民党自生自灭。",
        3.42,
    ) == "美国转向别处，任由国民党自生自灭"
    assert requests == 4


@pytest.mark.asyncio
async def test_builtin_compress_does_not_truncate_over_budget_words(
    monkeypatch: pytest.MonkeyPatch,
):
    candidate = "甲" * 17

    async def no_sleep(_seconds: float) -> None:
        return None

    class CompressionTranslator(Translator):
        async def _request(self, _system_prompt: str, _user_prompt: str) -> str:
            return json.dumps([{"id": 0, "zh": candidate}], ensure_ascii=False)

    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    translator = CompressionTranslator("fake", retries=2)

    with pytest.raises(RuntimeError, match="仅移除标点/冗余空白仍无法满足 16 字上限"):
        await translator.compress("source", "translated", 3.42)


@pytest.mark.asyncio
async def test_builtin_compress_invalid_json_or_ids_never_use_formatting_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
    responses = ["not json", '[{"id": 7, "zh": "错误。"}]']

    async def no_sleep(_seconds: float) -> None:
        return None

    class CompressionTranslator(Translator):
        async def _request(self, _system_prompt: str, _user_prompt: str) -> str:
            return responses.pop(0)

    monkeypatch.setattr(translator_module.asyncio, "sleep", no_sleep)
    translator = CompressionTranslator("fake", retries=2)

    with pytest.raises(RuntimeError, match="ID 不匹配") as captured:
        await translator.compress("source", "translated", 1.0)
    assert "仅移除标点/冗余空白" not in str(captured.value)


@pytest.mark.asyncio
async def test_translation_cache_resumes_without_model_call(tmp_path):
    transcript = Transcript(
        language="en",
        segments=[
            Segment(id=1, start=0, end=2, text="Hello"),
            Segment(id=2, start=2.2, end=4, text="World"),
        ],
    )
    cache = tmp_path / "translation.json"
    first = FakeTranslator()
    translated = await translate_transcript(
        transcript, cache, first, provider="fake"
    )
    assert first.calls == 1
    assert translated.segments[1].translated_text == "译文 2"

    second = FakeTranslator()
    cached = await translate_transcript(
        transcript, cache, second, provider="fake"
    )
    assert second.calls == 0
    assert cached.segments[0].translated_text == "译文 1"

    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["cache_version"] == 3
    assert len(payload["input_fingerprint"]) == 64
    assert payload["input"]["prompt_version"] == TRANSLATION_PROMPT_VERSION
    assert payload["input"]["length_strategy"] == {
        "version": TRANSLATION_LENGTH_STRATEGY_VERSION,
        "zh_chars_per_second": ZH_CHARS_PER_SECOND,
        "minimum_zh_chars": MIN_ZH_CHARS,
    }
    assert payload["input"]["segments"][0] == {
        "id": 1,
        "start": 0.0,
        "end": 2.0,
        "source": "Hello",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["text", "timing"])
async def test_translation_cache_invalidates_when_any_input_changes(tmp_path, changed):
    transcript = Transcript(
        language="en",
        segments=[
            Segment(id=1, start=0, end=2, text="Hello"),
            Segment(id=2, start=2, end=4, text="World"),
        ],
    )
    cache = tmp_path / "translation.json"
    await translate_transcript(transcript, cache, FakeTranslator(), provider="fake")

    modified = transcript.model_copy(deep=True)
    if changed == "text":
        modified.segments[0].text = "Hello again"
    else:
        modified.segments[0].end = 2.5

    translator = FakeTranslator()
    await translate_transcript(
        modified,
        cache,
        translator,
        provider="fake",
    )
    assert translator.calls == 1
    assert translator.requested_ids == [[1]]


@pytest.mark.asyncio
async def test_translation_cache_reuses_same_id_timing_and_source(tmp_path):
    original = Transcript(
        language="en",
        segments=[
            Segment(id=1, start=0, end=2, text="Hello"),
            Segment(id=2, start=2, end=4, text="World"),
        ],
    )
    cache = tmp_path / "translation.json"
    await translate_transcript(original, cache, FakeTranslator(), provider="fake")

    extended = original.model_copy(deep=True)
    extended.segments.append(Segment(id=3, start=4, end=6, text="Again"))
    translator = FakeTranslator()
    translated = await translate_transcript(
        extended,
        cache,
        translator,
        provider="fake",
    )

    assert translator.requested_ids == [[3]]
    assert [segment.translated_text for segment in translated.segments] == [
        "译文 1",
        "译文 2",
        "译文 3",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changed",
    ["provider", "model", "source_language", "target_language"],
)
async def test_translation_cache_global_change_invalidates_every_segment(
    tmp_path,
    changed: str,
):
    original = Transcript(
        language="en",
        segments=[
            Segment(id=1, start=0, end=2, text="Hello"),
            Segment(id=2, start=2, end=4, text="World"),
        ],
    )
    cache = tmp_path / "translation.json"
    await translate_transcript(original, cache, FakeTranslator(), provider="fake")

    modified = original.model_copy(deep=True)
    provider = "fake"
    model = "fake"
    target_language = "zh-CN"
    if changed == "provider":
        provider = "other"
    elif changed == "model":
        model = "fake-v2"
    elif changed == "source_language":
        modified.language = "fr"
    else:
        target_language = "zh-Hans"

    translator = FakeTranslator(model)
    await translate_transcript(
        modified,
        cache,
        translator,
        provider=provider,
        target_language=target_language,
    )

    assert translator.requested_ids == [[1, 2]]


@pytest.mark.asyncio
async def test_translation_cache_removing_tail_reuses_prefix_and_rewrites_input(
    tmp_path,
):
    original = Transcript(
        language="en",
        segments=[
            Segment(id=index, start=index * 2, end=index * 2 + 2, text=f"Line {index}")
            for index in range(1, 9)
        ],
    )
    cache = tmp_path / "translation.json"
    await translate_transcript(original, cache, FakeTranslator(), provider="fake")

    without_credits = original.model_copy(deep=True)
    without_credits.segments = without_credits.segments[:6]
    translator = FakeTranslator()
    translated = await translate_transcript(
        without_credits,
        cache,
        translator,
        provider="fake",
    )

    assert translator.calls == 0
    assert [segment.translated_text for segment in translated.segments] == [
        f"译文 {index}" for index in range(1, 7)
    ]
    rewritten = json.loads(cache.read_text(encoding="utf-8"))
    assert [item["id"] for item in rewritten["input"]["segments"]] == list(
        range(1, 7)
    )
    assert [item["id"] for item in rewritten["segments"]] == list(range(1, 7))


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["prompt_version", "length_version", "cps"])
async def test_translation_cache_invalidates_when_strategy_changes(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
):
    transcript = Transcript(
        language="en",
        segments=[
            Segment(id=1, start=0, end=2, text="Hello"),
            Segment(id=2, start=2, end=4, text="World"),
        ],
    )
    cache = tmp_path / "translation.json"
    await translate_transcript(transcript, cache, FakeTranslator(), provider="fake")

    if changed == "prompt_version":
        monkeypatch.setattr(
            translator_module,
            "TRANSLATION_PROMPT_VERSION",
            TRANSLATION_PROMPT_VERSION + "-changed",
        )
    elif changed == "length_version":
        monkeypatch.setattr(
            translator_module,
            "TRANSLATION_LENGTH_STRATEGY_VERSION",
            TRANSLATION_LENGTH_STRATEGY_VERSION + "-changed",
        )
    else:
        monkeypatch.setattr(
            translator_module,
            "ZH_CHARS_PER_SECOND",
            ZH_CHARS_PER_SECOND + 0.25,
        )

    translator = FakeTranslator()
    await translate_transcript(transcript, cache, translator, provider="fake")
    assert translator.calls == 1
    assert translator.requested_ids == [[1, 2]]


@pytest.mark.asyncio
async def test_translation_cache_partial_reuse_requires_consistent_fingerprint(
    tmp_path,
):
    transcript = Transcript(
        language="en",
        segments=[
            Segment(id=1, start=0, end=2, text="Hello"),
            Segment(id=2, start=2, end=4, text="World"),
        ],
    )
    cache = tmp_path / "translation.json"
    await translate_transcript(transcript, cache, FakeTranslator(), provider="fake")

    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["input"]["segments"].append(
        {"id": 99, "start": 99.0, "end": 100.0, "source": "Tampered"}
    )
    cache.write_text(json.dumps(payload), encoding="utf-8")

    translator = FakeTranslator()
    await translate_transcript(transcript, cache, translator, provider="fake")

    assert translator.requested_ids == [[1, 2]]


@pytest.mark.asyncio
async def test_translation_cache_item_identity_must_match_fingerprinted_input(
    tmp_path,
):
    original = Transcript(
        language="en",
        segments=[Segment(id=1, start=0, end=2, text="Hello")],
    )
    cache = tmp_path / "translation.json"
    await translate_transcript(original, cache, FakeTranslator(), provider="fake")

    # The item itself is outside input_fingerprint.  It must not be possible to
    # relabel a cached translation as a changed source while leaving the
    # protected input identity untouched.
    payload = json.loads(cache.read_text(encoding="utf-8"))
    payload["segments"][0]["source"] = "Changed"
    cache.write_text(json.dumps(payload), encoding="utf-8")
    changed = original.model_copy(deep=True)
    changed.segments[0].text = "Changed"

    translator = FakeTranslator()
    await translate_transcript(changed, cache, translator, provider="fake")

    assert translator.requested_ids == [[1]]


@pytest.mark.asyncio
async def test_legacy_translation_cache_is_never_reused(tmp_path):
    transcript = Transcript(
        language="en",
        segments=[Segment(id=1, start=0, end=2, text="Hello")],
    )
    cache = tmp_path / "translation.json"
    cache.write_text(
        json.dumps({"segments": [{"id": 1, "zh": "过期译文"}]}),
        encoding="utf-8",
    )
    translator = FakeTranslator()

    translated = await translate_transcript(transcript, cache, translator, provider="fake")

    assert translator.calls == 1
    assert translated.segments[0].translated_text == "译文 1"


@pytest.mark.asyncio
async def test_ollama_disables_thinking_for_structured_subtitle_translation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict] = []

    class FakeResponse:
        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict:
            return {"message": {"content": "[]"}}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, _url: str, *, json: dict):
            requests.append(json)
            return FakeResponse()

    monkeypatch.setattr("backend.pipeline.translator.httpx.AsyncClient", FakeClient)
    translator = OllamaTranslator("http://127.0.0.1:11434", "gemma4:26b")

    assert translator.retries == 4
    assert await translator._request("system", "user") == "[]"
    assert requests[0]["stream"] is False
    assert requests[0]["think"] is False
    assert requests[0]["format"]["type"] == "array"


def test_build_translator_uses_configured_long_request_timeout() -> None:
    config = replace(Settings(), translation_timeout_seconds=777.0)

    ollama = build_translator(config, "ollama", "gemma4:26b")
    compatible = build_translator(config, "openai", "compatible-model")

    assert ollama.timeout == 777.0
    assert compatible.timeout == 777.0
