"""Groq Whisper transcription — drop-in replacement for the Gemini backend.

Why Groq:
  - verbose_json always returns typed segments; no JSON-parsing fragility
  - Transcribes any language; LLaMA translates segments to English
  - 20 RPM / 7,200 req/day on the free tier (vs Gemini's 5 RPM)

Note: Groq only accepts model, response_format, language, prompt, temperature
on the /audio/transcriptions endpoint. task= and timestamp_granularities[]
are NOT supported and cause 404s.
"""

import asyncio
import json
import logging
from pathlib import Path

import httpx

from models import TranscriptResult, TranscriptSegment

LOGGER = logging.getLogger(__name__)

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

DEFAULT_WHISPER_MODEL = "whisper-large-v3"
TRANSLATION_LLM = "groq/compound-mini"  # confirmed available on this account
GROQ_MAX_BYTES = 25 * 1024 * 1024  # 25 MB hard limit


class TranscriptionError(Exception):
    pass


class GroqTranscriber:
    """Transcribes audio to English using Groq Whisper Large v3.

    Public interface is identical to the old GeminiTranscriber so
    providers.py / bot.py need zero changes.
    """

    def __init__(
        self,
        api_key: str,
        model_name: str = DEFAULT_WHISPER_MODEL,
        retries: int = 3,
    ) -> None:
        self.api_key = api_key
        self.model_name = model_name
        self.retries = max(0, retries)
        self._auth = {"Authorization": f"Bearer {api_key}"}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _post_audio(self, path: Path) -> dict:
        """Transcribe audio via Groq. Returns verbose_json dict."""
        file_bytes = path.read_bytes()

        if len(file_bytes) > GROQ_MAX_BYTES:
            mb = len(file_bytes) / 1024 / 1024
            raise TranscriptionError(
                f"Audio file is {mb:.1f} MB — Groq's limit is 25 MB. "
                "Re-encode with: ffmpeg -i input -ac 1 -ar 16000 -b:a 32k out.mp3"
            )

        # Only send parameters Groq actually accepts
        fields = {
            "model": self.model_name,
            "response_format": "verbose_json",
        }

        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=180) as client:
            for attempt in range(self.retries + 1):
                try:
                    resp = await client.post(
                        GROQ_TRANSCRIBE_URL,
                        headers=self._auth,
                        data=fields,
                        files={"file": (path.name, file_bytes, _mime(path))},
                    )
                    if resp.status_code in {429, 500, 502, 503, 504}:
                        wait = min(60, 5 * 2 ** attempt)
                        LOGGER.warning("Groq HTTP %s — retrying in %ss", resp.status_code, wait)
                        await asyncio.sleep(wait)
                        last_exc = TranscriptionError(f"Groq HTTP {resp.status_code}")
                        continue
                    resp.raise_for_status()
                    return resp.json()

                except (httpx.HTTPError, TranscriptionError) as exc:
                    last_exc = exc
                    if attempt < self.retries:
                        await asyncio.sleep(min(30, 2 ** attempt))

        raise TranscriptionError(str(last_exc)) from last_exc

    async def _chat(self, prompt: str, max_tokens: int = 2048) -> str:
        """Groq chat completions — used for translation and language detection."""
        last_exc: Exception | None = None
        async with httpx.AsyncClient(timeout=60) as client:
            for attempt in range(self.retries + 1):
                try:
                    resp = await client.post(
                        GROQ_CHAT_URL,
                        headers={**self._auth, "Content-Type": "application/json"},
                        json={
                            "model": TRANSLATION_LLM,
                            "messages": [{"role": "user", "content": prompt}],
                            "temperature": 0.1,
                            "max_tokens": max_tokens,
                        },
                    )
                    resp.raise_for_status()
                    return resp.json()["choices"][0]["message"]["content"].strip()
                except (httpx.HTTPError, KeyError) as exc:
                    last_exc = exc
                    if attempt < self.retries:
                        await asyncio.sleep(min(20, 2 ** attempt))

        raise TranscriptionError(f"Chat completion failed: {last_exc}") from last_exc

    # ------------------------------------------------------------------
    # Public API — same signatures as GeminiTranscriber
    # ------------------------------------------------------------------

    async def transcribe_structured(self, path: Path, duration: float = 0) -> TranscriptResult:
        """Transcribe audio and return a TranscriptResult with segments."""
        try:
            data = await self._post_audio(path)

            detected_language = data.get("language", "und")
            actual_duration = float(data.get("duration", duration))
            raw_segments = data.get("segments", [])

            if raw_segments:
                segments = [
                    TranscriptSegment(
                        start=float(seg.get("start", 0)),
                        end=float(seg.get("end", 0)),
                        speaker="speaker_1",
                        text=seg.get("text", "").strip(),
                    )
                    for seg in raw_segments
                    if seg.get("text", "").strip()
                ]
            elif data.get("text", "").strip():
                segments = [
                    TranscriptSegment(0, actual_duration, "speaker_1", data["text"].strip())
                ]
            else:
                segments = []

            return TranscriptResult(detected_language, actual_duration, segments, "groq_whisper")

        except TranscriptionError:
            raise
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise TranscriptionError(str(exc)) from exc

    async def translate_segments(self, payload: str, source_language: str) -> list[str]:
        """Translate pre-transcribed segments to English via Groq LLaMA."""
        prompt = (
            f"Translate these transcript segments from {source_language} to English. "
            "Return ONLY a valid JSON array of translated strings in the same order. "
            "Preserve meaning exactly. No markdown fences, no commentary.\n\n"
            f"Segments: {payload}"
        )
        raw = await self._chat(prompt)

        cleaned = raw
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise TranscriptionError(f"Translation returned invalid JSON: {exc}") from exc

        if not isinstance(parsed, list):
            raise TranscriptionError("Translation result is not a JSON array")

        return [str(item) for item in parsed]

    async def detect_language(self, text: str) -> str:
        """Return ISO 639-1 code for the given text sample."""
        prompt = (
            "Return only the ISO 639-1 two-letter language code for the following "
            "text. No punctuation, no explanation, just the code.\n\n"
            f"{text[:2000]}"
        )
        try:
            code = (await self._chat(prompt, max_tokens=5)).lower().strip()
            if len(code) == 2 and code.isalpha():
                return code
        except Exception as exc:
            LOGGER.warning("Language detection failed (non-fatal): %s", exc)
        return "und"

    async def healthcheck(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    "https://api.groq.com/openai/v1/models",
                    headers=self._auth,
                )
                return resp.is_success
        except httpx.HTTPError:
            return False

    async def transcribe(self, path: Path) -> str:
        """Convenience: transcribe and return plain English text string."""
        result = await self.transcribe_structured(path)
        return "\n\n".join(seg.text for seg in result.segments)


# Backward-compat alias — providers.py / bot.py import GeminiTranscriber
GeminiTranscriber = GroqTranscriber


def _mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
        ".opus": "audio/opus",
        ".webm": "audio/webm",
    }.get(ext, "audio/wav")