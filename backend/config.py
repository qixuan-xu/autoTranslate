from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / ".env")


def _path_env(name: str, default: str) -> Path:
    value = os.getenv(name, default)
    return Path(os.path.expanduser(value)).resolve()


def _project_executable(name: str) -> str:
    candidate = PROJECT_ROOT / ".venv" / "bin" / name
    return str(candidate) if candidate.is_file() else name


@dataclass(frozen=True)
class Settings:
    project_root: Path = PROJECT_ROOT
    work_dir: Path = _path_env("WORK_DIR", str(PROJECT_ROOT / "work"))
    whisper_bin: str = os.getenv("WHISPER_BIN", "whisper")
    whisper_model: str = os.getenv("WHISPER_MODEL", "turbo")
    translation_provider: str = os.getenv("TRANSLATION_PROVIDER", "ollama")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    ollama_model: str = os.getenv("OLLAMA_MODEL", "qwen2.5:14b")
    openai_base_url: str = os.getenv("OPENAI_BASE_URL", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    openai_model: str = os.getenv("OPENAI_MODEL", "")
    cosyvoice_root: Path = _path_env("COSYVOICE_ROOT", "~/CosyVoice")
    cosyvoice_model: Path = _path_env(
        "COSYVOICE_MODEL", "~/CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B"
    )
    cosyvoice_url: str = os.getenv("COSYVOICE_URL", "http://127.0.0.1:50001")
    ffmpeg_bin: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    ffprobe_bin: str = os.getenv("FFPROBE_BIN", "ffprobe")
    ytdlp_bin: str = os.getenv("YTDLP_BIN", _project_executable("yt-dlp"))
    ytdlp_proxy: str = os.getenv("YTDLP_PROXY", "")
    ytdlp_cookies: str = os.getenv("YTDLP_COOKIES", "")
    original_audio_volume: float = float(os.getenv("ORIGINAL_AUDIO_VOLUME", "0.14"))
    max_upload_gb: float = float(os.getenv("MAX_UPLOAD_GB", "20"))
    host: str = os.getenv("HOST", "127.0.0.1")
    port: int = int(os.getenv("PORT", "8000"))

    @property
    def database_path(self) -> Path:
        return self.work_dir / "jobs.sqlite3"


settings = Settings()
