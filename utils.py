"""Small, dependency-free helpers used by the bot."""

from pathlib import Path
import re
import shutil
from typing import Iterable

TELEGRAM_MESSAGE_LIMIT = 4096
URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def extract_url(text: str | None) -> str | None:
    match = URL_RE.search(text or "")
    return match.group(0).rstrip(".,!?)") if match else None


def chunk_text(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    """Split on paragraph/word boundaries without exceeding Telegram's limit."""
    text = text.strip()
    chunks: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit + 1)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def remove_tree(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    elif path.exists():
        path.unlink(missing_ok=True)


def is_supported_media(path: Path) -> bool:
    return path.suffix.lower() in {
        ".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus",
        ".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts",
    }


def format_duration(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 60 else f"{seconds / 60:.1f}m"
