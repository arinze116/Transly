"""Transcription and translation provider abstractions."""

from abc import ABC, abstractmethod
import asyncio
import json
import logging
from pathlib import Path

from models import NormalizedMedia, TranscriptResult, TranscriptSegment
from transcriber import GeminiTranscriber, TranscriptionError

LOGGER = logging.getLogger(__name__)


class TranscriptionProvider(ABC):
    @abstractmethod
    async def transcribe(self, media: NormalizedMedia) -> TranscriptResult: ...


class GeminiTranscriptionProvider(TranscriptionProvider):
    def __init__(self, api_key: str, model: str, retries: int) -> None:
        self.client = GeminiTranscriber(api_key, model, retries)

    async def transcribe(self, media: NormalizedMedia) -> TranscriptResult:
        result = await self.client.transcribe_structured(media.local_path, media.duration or 0)
        if result.language == "und" and result.segments:
            result.language = await self.client.detect_language(result.segments[0].text)
        return result


class WhisperProvider(TranscriptionProvider):
    def __init__(self, model: str, device: str, compute_type: str) -> None:
        self.model_name, self.device, self.compute_type = model, device, compute_type
        self._model = None

    async def transcribe(self, media: NormalizedMedia) -> TranscriptResult:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriptionError("Local Whisper fallback is not installed") from exc
        if self._model is None:
            device = "cuda" if self.device == "auto" else self.device
            compute = "float16" if self.compute_type == "auto" and device == "cuda" else "int8"
            self._model = await asyncio.to_thread(WhisperModel, self.model_name, device=device, compute_type=compute)
        segments, info = await asyncio.to_thread(lambda: self._model.transcribe(str(media.local_path), vad_filter=True))
        result = [TranscriptSegment(float(item.start), float(item.end), "speaker_1", item.text.strip()) for item in segments]
        return TranscriptResult(info.language or "und", float(media.duration or 0), result, "whisper")


class TranscriptionRouter:
    def __init__(self, primary: TranscriptionProvider, fallback: TranscriptionProvider) -> None:
        self.primary, self.fallback = primary, fallback

    async def transcribe(self, media: NormalizedMedia) -> TranscriptResult:
        try:
            return await self.primary.transcribe(media)
        except Exception as exc:
            LOGGER.warning("Primary transcription failed; using fallback: %s", exc)
            try:
                return await self.fallback.transcribe(media)
            except Exception as fallback_exc:
                raise TranscriptionError(
                    f"Primary provider failed ({exc}); fallback failed ({fallback_exc})"
                ) from fallback_exc


class TranslationProvider(ABC):
    @abstractmethod
    async def translate(self, segments: list[TranscriptSegment], source_language: str) -> list[TranscriptSegment]: ...


class GeminiTranslationProvider(TranslationProvider):
    def __init__(self, api_key: str, model: str, retries: int) -> None:
        self.client = GeminiTranscriber(api_key, model, retries)

    async def translate(self, segments, source_language):
        if source_language.lower().startswith("en"):
            return segments
        payload = json.dumps([{"start": s.start, "end": s.end, "speaker": s.speaker, "text": s.text} for s in segments], ensure_ascii=False)
        translations = await self.client.translate_segments(payload, source_language)
        for segment, translated in zip(segments, translations):
            segment.english_text = translated
        return segments
