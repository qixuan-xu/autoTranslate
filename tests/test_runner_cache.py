from __future__ import annotations

from pathlib import Path

import pytest

from backend.pipeline.context import JobPaths
from backend.pipeline.runner import PipelineRunner


def test_tts_change_invalidates_only_exact_downstream_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = JobPaths.create(tmp_path, "job-1")
    unlink_calls: list[tuple[Path, bool]] = []

    def record_unlink(path: Path, missing_ok: bool = False) -> None:
        unlink_calls.append((path, missing_ok))

    monkeypatch.setattr(Path, "unlink", record_unlink)

    PipelineRunner._invalidate_downstream(paths)

    expected = [
        paths.mix / "dubbed_voice.wav",
        paths.mix / "mixed.wav",
        paths.output / "dubbed_voice.wav",
        paths.output / "mixed.wav",
        paths.output / "final_zh.mp4",
        paths.output / "final_zh_subtitle.mp4",
        paths.output / "final_zh_burned.mp4",
    ]
    assert [path for path, _ in unlink_calls] == expected
    assert all(missing_ok for _, missing_ok in unlink_calls)
    assert paths.tts / "0001.wav" not in expected
    assert paths.translation / "translation.json" not in expected
    assert paths.translation / "zh.srt" not in expected
    assert paths.asr / "transcript.json" not in expected
