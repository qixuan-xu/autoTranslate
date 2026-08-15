from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class JobPaths:
    root: Path
    source: Path
    audio: Path
    asr: Path
    translation: Path
    tts: Path
    mix: Path
    output: Path

    @classmethod
    def create(cls, work_root: Path, job_id: str) -> "JobPaths":
        root = (work_root / job_id).resolve()
        paths = cls(
            root=root,
            source=root / "source",
            audio=root / "audio",
            asr=root / "asr",
            translation=root / "translation",
            tts=root / "tts",
            mix=root / "mix",
            output=root / "output",
        )
        for path in (
            paths.source,
            paths.audio,
            paths.asr,
            paths.translation,
            paths.tts,
            paths.mix,
            paths.output,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return paths
