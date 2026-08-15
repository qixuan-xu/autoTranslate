from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class WordTimestamp(BaseModel):
    word: str
    start: float
    end: float
    probability: Optional[float] = None


class Segment(BaseModel):
    id: int
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str
    words: List[WordTimestamp] = Field(default_factory=list)
    confidence: Optional[float] = None
    translated_text: Optional[str] = None
    tts_file: Optional[str] = None
    tts_duration: Optional[float] = None

    @model_validator(mode="after")
    def validate_range(self) -> "Segment":
        if self.end <= self.start:
            raise ValueError("segment end must be after start")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


class Transcript(BaseModel):
    language: str = "unknown"
    segments: List[Segment] = Field(default_factory=list)
    duration: Optional[float] = None


class JobSettings(BaseModel):
    source_language: str = "auto"
    target_language: str = "zh-CN"
    whisper_model: str = "turbo"
    translation_provider: Literal["ollama", "openai"] = "ollama"
    translation_model: str = ""
    dubbing_enabled: bool = True
    keep_background: bool = True
    separation_mode: Literal["fast", "demucs"] = "fast"
    subtitle_mode: Literal["soft", "burn", "both"] = "both"
    reference_mode: Literal["auto", "segment", "upload"] = "auto"
    reference_segment_id: Optional[int] = None
    reference_text: str = ""
    subtitle_font: str = "PingFang SC"

    @field_validator("translation_model", mode="before")
    @classmethod
    def normalize_model(cls, value: Any) -> str:
        return str(value or "").strip()

    def public_dict(self) -> Dict[str, Any]:
        return self.model_dump()
