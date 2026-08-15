import json

import pytest

from backend.models.domain import Segment, Transcript
from backend.pipeline.translator import (
    TranslationFormatError,
    Translator,
    parse_translation_json,
    translate_transcript,
)


class FakeTranslator(Translator):
    def __init__(self, model: str = "fake"):
        super().__init__(model, retries=1)
        self.calls = 0

    async def _request(self, system_prompt: str, user_prompt: str) -> str:
        self.calls += 1
        targets = json.loads(user_prompt.split("需要翻译的片段：\n", 1)[1])
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
    assert payload["cache_version"] == 2
    assert len(payload["input_fingerprint"]) == 64
    assert payload["input"]["segments"][0] == {
        "id": 1,
        "start": 0.0,
        "end": 2.0,
        "source": "Hello",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["text", "timing", "provider", "model", "target"])
async def test_translation_cache_invalidates_when_any_input_changes(tmp_path, changed):
    transcript = Transcript(
        language="en",
        segments=[Segment(id=1, start=0, end=2, text="Hello")],
    )
    cache = tmp_path / "translation.json"
    await translate_transcript(transcript, cache, FakeTranslator(), provider="fake")

    modified = transcript.model_copy(deep=True)
    provider = "fake"
    target = "zh-CN"
    model = "fake"
    if changed == "text":
        modified.segments[0].text = "Hello again"
    elif changed == "timing":
        modified.segments[0].end = 2.5
    elif changed == "provider":
        provider = "other"
    elif changed == "model":
        model = "fake-v2"
    else:
        target = "zh-Hans"

    translator = FakeTranslator(model)
    await translate_transcript(
        modified,
        cache,
        translator,
        provider=provider,
        target_language=target,
    )
    assert translator.calls == 1


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
