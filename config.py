"""Application configuration loaded from environment variables."""

from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    groq_api_key: str
    max_file_size_mb: int
    download_dir: Path
    groq_model: str = "whisper-large-v3"
    telegram_api_mode: str = "standard"
    telegram_api_base_url: str = ""
    database_url: str = "sqlite:////tmp/transly.db"
    max_concurrent_jobs_per_user: int = 1
    max_audio_duration_minutes: int = 120
    max_download_time_seconds: int = 600
    max_download_size_mb: int = 500
    max_retries: int = 3
    media_retention_minutes: int = 30
    # Local Whisper fallback settings — kept for WhisperProvider in bot.py
    whisper_model: str = "small"
    whisper_device: str = "auto"
    whisper_compute_type: str = "auto"

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024

    # ------------------------------------------------------------------
    # Backward-compat shims — providers.py / bot.py may read these names
    # ------------------------------------------------------------------

    @property
    def gemini_api_key(self) -> str:
        """Alias kept so existing callers don't break. Points to groq_api_key."""
        return self.groq_api_key

    @property
    def gemini_model(self) -> str:
        """Alias kept so existing callers don't break. Points to groq_model."""
        return self.groq_model

    @property
    def max_gemini_retries(self) -> int:
        """Alias kept so existing callers don't break. Points to max_retries."""
        return self.max_retries


def load_settings() -> Settings:
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", os.getenv("BOT_TOKEN", "")).strip()

    # Accept both new GROQ_API_KEY and old GEMINI_API_KEY during transition
    groq_api_key = (
        os.getenv("GROQ_API_KEY", "")
        or os.getenv("GEMINI_API_KEY", "")
    ).strip()

    if not bot_token:
        raise RuntimeError("BOT_TOKEN (or TELEGRAM_BOT_TOKEN) must be set")
    if not groq_api_key:
        raise RuntimeError(
            "GROQ_API_KEY must be set. "
            "Get a free key at https://console.groq.com"
        )

    directory = Path(os.getenv("DOWNLOAD_DIR", "/tmp/transly")).expanduser()
    directory.mkdir(parents=True, exist_ok=True)

    return Settings(
        bot_token=bot_token,
        groq_api_key=groq_api_key,
        max_file_size_mb=int(os.getenv("MAX_FILE_SIZE_MB", "20")),
        download_dir=directory,
        groq_model=os.getenv("GROQ_MODEL", os.getenv("GEMINI_MODEL", "whisper-large-v3")),
        telegram_api_mode=os.getenv("TELEGRAM_API_MODE", "standard"),
        telegram_api_base_url=os.getenv("TELEGRAM_API_BASE_URL", ""),
        database_url=os.getenv("DATABASE_URL", "sqlite:////tmp/transly.db"),
        max_concurrent_jobs_per_user=int(os.getenv("MAX_CONCURRENT_JOBS_PER_USER", "1")),
        max_audio_duration_minutes=int(os.getenv("MAX_AUDIO_DURATION_MINUTES", "120")),
        max_download_time_seconds=int(os.getenv("MAX_DOWNLOAD_TIME_SECONDS", "600")),
        max_download_size_mb=int(os.getenv("MAX_DOWNLOAD_SIZE_MB", "500")),
        max_retries=int(os.getenv("MAX_RETRIES", os.getenv("MAX_GEMINI_RETRIES", "3"))),
        media_retention_minutes=int(os.getenv("MEDIA_RETENTION_MINUTES", "30")),
        whisper_model=os.getenv("WHISPER_MODEL", "small"),
        whisper_device=os.getenv("WHISPER_DEVICE", "auto"),
        whisper_compute_type=os.getenv("WHISPER_COMPUTE_TYPE", "auto"),
    )