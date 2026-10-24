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
import subprocess
import tempfile

import httpx

from models import TranscriptResult, TranscriptSegment

LOGGER = logging.getLogger(__name__)

GROQ_TRANSCRIBE_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

DEFAULT_WHISPER_MODEL = "whisper-large-v3"
TRANSLATION_LLM = "openai/gpt-oss-20b"  # supported replacement for retired Groq model
GROQ_MAX_BYTES = 25 * 1024 * 1024  # Groq per-request hard limit
GROQ_SAFE_BYTES = 24 * 1024 * 1024  # leave headroom below the provider limit
AUDIO_CHUNK_SECONDS = 20 * 60


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
        file_size = path.stat().st_size
        if file_size > GROQ_MAX_BYTES:
            mb = file_size / 1024 / 1024
            raise TranscriptionError(
                f"Audio file is {mb:.1f} MB — Groq's limit is 25 MB. "
                "The audio must be compressed or split before transcription."
            )

        file_bytes = path.read_bytes()

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
        """Transcribe compressed audio, splitting oversized files into ordered chunks."""
        try:
            if path.stat().st_size <= GROQ_SAFE_BYTES:
                data = await self._post_audio(path)
                return _result_from_data(data, duration)

            total_duration = duration or await asyncio.to_thread(_probe_duration, path)
            if total_duration <= 0:
                raise TranscriptionError("Could not determine audio duration for chunking")

            # Keep all temporary chunks beside the job audio so the job's parent
            # cleanup also covers them if the process is interrupted.
            with tempfile.TemporaryDirectory(prefix="transly-chunks-", dir=str(path.parent)) as temp_dir:
                chunks = await asyncio.to_thread(
                    _split_audio, path, Path(temp_dir), total_duration, AUDIO_CHUNK_SECONDS
                )
                combined_segments: list[TranscriptSegment] = []
                detected_language = "und"
                observed_duration = 0.0

                for index, chunk in enumerate(chunks):
                    offset = index * AUDIO_CHUNK_SECONDS
                    chunk_duration = min(AUDIO_CHUNK_SECONDS, max(0.0, total_duration - offset))
                    data = await self._post_audio(chunk)
                    result = _result_from_data(data, chunk_duration)
                    if detected_language == "und" and result.language != "und":
                        detected_language = result.language
                    observed_duration += result.duration
                    for segment in result.segments:
                        combined_segments.append(
                            TranscriptSegment(
                                start=segment.start + offset,
                                end=segment.end + offset,
                                speaker=segment.speaker,
                                text=segment.text,
                                english_text=segment.english_text,
                            )
                        )

            return TranscriptResult(
                detected_language,
                total_duration or observed_duration,
                combined_segments,
                "groq_whisper",
            )

        except TranscriptionError:
            raise
        except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
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


def _result_from_data(data: dict, duration: float = 0) -> TranscriptResult:
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
        segments = [TranscriptSegment(0, actual_duration, "speaker_1", data["text"].strip())]
    else:
        segments = []
    return TranscriptResult(detected_language, actual_duration, segments, "groq_whisper")


def _probe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise TranscriptionError("Could not read audio duration for chunking")
    try:
        return float(result.stdout.strip())
    except ValueError as exc:
        raise TranscriptionError("Could not read audio duration for chunking") from exc


def _split_audio(path: Path, output_dir: Path, duration: float, chunk_seconds: int) -> list[Path]:
    chunks: list[Path] = []
    count = max(1, int((duration + chunk_seconds - 1) // chunk_seconds))
    for index in range(count):
        start = index * chunk_seconds
        length = min(chunk_seconds, duration - start)
        output = output_dir / f"chunk-{index:04d}.mp3"
        result = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-i", str(path), "-ss", str(start),
                "-t", str(length), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "libmp3lame", "-b:a", "32k", str(output),
            ],
            capture_output=True, text=True, timeout=900,
        )
        if result.returncode != 0 or not output.exists() or output.stat().st_size == 0:
            detail = result.stderr.strip().splitlines()
            reason = detail[-1] if detail else "unknown FFmpeg error"
            raise TranscriptionError(f"Could not split oversized audio: {reason}")
        if output.stat().st_size > GROQ_MAX_BYTES:
            raise TranscriptionError("An audio chunk still exceeds Groq's 25 MiB request limit")
        chunks.append(output)
    return chunks


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