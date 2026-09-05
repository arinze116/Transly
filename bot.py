"""Transly Telegram bot entry point."""

import asyncio
from contextlib import suppress
import logging
from pathlib import Path
import time
import uuid

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError
from telegram.request import HTTPXRequest
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import Settings, load_settings
from downloader import DownloadError, NoAudioTrackError, download_url, extract_audio
from security import UnsafeURL
from database import JobStore
from models import NormalizedMedia, TranscriptResult
from providers import GeminiTranscriptionProvider, GeminiTranslationProvider, TranscriptionError, TranscriptionRouter, WhisperProvider
from utils import chunk_text, extract_url, format_duration, is_supported_media, remove_tree

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
LOGGER = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

WELCOME = (
    "👋 <b>Transly</b> turns audio and video into accurate English text.\n\n"
    "Send me an audio/video file or paste a public media link. I detect the source language and translate it to English.\n\n"
    "Use /help for supported formats and details."
)
HELP = (
    "<b>How to use Transly</b>\n\n"
    "1. Upload audio/video, or paste a YouTube, TikTok, Instagram, X/Twitter, or other supported URL.\n"
    "2. Wait while I download and transcribe it with Groq Whisper.\n"
    "3. Receive clean English paragraphs. Long transcripts are split into messages and include a .txt download.\n\n"
    "Audio: MP3, WAV, OGG, FLAC, M4A, AAC, OPUS\n"
    "Video: MP4, MKV, AVI, MOV, WEBM, TS\n\n"
    "Telegram uploads are limited to 20 MB. For larger files, send a URL instead."
)


def get_services(context) -> tuple[Settings, TranscriptionRouter]:
    application = getattr(context, "application", context)
    return application.bot_data["settings"], application.bot_data["router"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode="HTML")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode="HTML")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _, router = get_services(context)
    alive = await router.primary.client.healthcheck()
    await update.message.reply_text("✅ Transly and Groq API are online." if alive else "⚠️ Groq API is currently unavailable.")


async def transcribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = extract_url(" ".join(context.args))
    if url:
        await process_media(update, context, source=url)
    else:
        await update.message.reply_text("Send an audio/video file or use `/transcribe <media URL>`.", parse_mode="Markdown")


async def incoming_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = extract_url(update.message.text)
    if url:
        await process_media(update, context, source=url)
    else:
        await update.message.reply_text("📎 Please send a video/audio file or a URL to a media page.")


async def incoming_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings, _ = get_services(context)
    media = update.message.document or update.message.audio or update.message.video or update.message.voice
    if media.file_size and media.file_size > settings.max_file_size_bytes:
        await update.message.reply_text(f"⚠️ File exceeds {settings.max_file_size_mb} MB. Please send a URL link instead.")
        return
    await process_media(update, context, source=media)


async def process_media(update: Update, context: ContextTypes.DEFAULT_TYPE, source) -> None:
    settings, _ = get_services(context)
    source_type = "url" if isinstance(source, str) else "telegram"
    job_id = await context.application.bot_data["jobs"].create(update.effective_chat.id, update.effective_user.id, source_type, source if isinstance(source, str) else None)
    message = await update.message.reply_text(f"⏳ <b>Transly received your media.</b>\n\nJob <code>{job_id[:8]}</code> added to the processing queue.", parse_mode="HTML")
    await context.application.bot_data["queue"].put((job_id, update, source, message))


async def process_media_now(job_id: str, update: Update, context: ContextTypes.DEFAULT_TYPE, source, message) -> None:
    application = getattr(context, "application", context)
    settings, transcriber = get_services(context)
    jobs: JobStore = application.bot_data["jobs"]
    job_dir = settings.download_dir / f"job-{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        await jobs.update(job_id, "VALIDATING")
        await update.message.chat.send_action(ChatAction.TYPING)
        await message.edit_text("⏳ Transly is processing your media...\n\n📥 Media received\n🎵 Preparing audio")
        if isinstance(source, str):
            path = await download_url(source, job_dir, settings.max_download_size_mb, settings.max_download_time_seconds)
        else:
            telegram_file = await source.get_file()
            suffix = Path(getattr(source, "file_name", "media.bin")).suffix or ".bin"
            path = job_dir / f"input{suffix}"
            await telegram_file.download_to_drive(path)
        await jobs.update(job_id, "EXTRACTING_AUDIO")
        audio = await extract_audio(path)
        if not is_supported_media(audio) and audio.suffix != ".wav":
            raise DownloadError("Unsupported media format")
        media = NormalizedMedia(audio, "audio", "audio/wav", size_bytes=audio.stat().st_size)
        await jobs.update(job_id, "TRANSCRIBING")
        result: TranscriptResult = await transcriber.transcribe(media)
        if not result.segments:
            await message.edit_text("🔇 No speech detected in this file.")
            return
        await jobs.update(job_id, "TRANSLATING")
        if not result.language.lower().startswith("en"):
            result.segments = await application.bot_data["translator"].translate(result.segments, result.language)
        formatted = format_result(result)
        prefix = "✅ <b>Transcription Complete</b>\n\n"
        parts = chunk_text(formatted, 4096 - len(prefix))
        header = f"{prefix}{parts[0]}"
        await message.edit_text(header)
        for part in parts[1:]:
            await update.message.reply_text(part)
        if len(parts) > 1:
            document = job_dir / "transcript.txt"
            document.write_text(formatted, encoding="utf-8")
            await update.message.reply_document(document, caption=f"⏱ Processed in {format_duration(time.monotonic() - started)}")
        else:
            await update.message.reply_text(f"⏱ Processed in {format_duration(time.monotonic() - started)}")
        await jobs.update(job_id, "COMPLETED")
    except DownloadError as exc:
        await jobs.update(job_id, "FAILED", str(exc))
        if isinstance(exc, NoAudioTrackError):
            response = "🔇 This video has no audio track, so Transly cannot detect speech in it. Please send a video with sound or an audio file."
        elif "ffmpeg" in str(exc):
            response = "❌ Transly couldn't read the audio track in that file. Please try another media file."
        elif "not supported" in str(exc):
            response = "❌ This link format is not supported. Try a direct media URL or YouTube, TikTok, or Instagram link."
        else:
            response = "❌ Transly couldn't download that media. Make sure the link is public and try again."
        await message.edit_text(response)
    except UnsafeURL:
        await jobs.update(job_id, "FAILED", "unsafe URL")
        await message.edit_text("❌ Transly couldn't retrieve media from this URL. Private or unsafe addresses are blocked.")
    except TranscriptionError as exc:
        LOGGER.error("Transly job %s transcription failed: %s", job_id, exc)
        await jobs.update(job_id, "FAILED", "transcription provider failed")
        if "invalid_api_key" in str(exc) or "Invalid API Key" in str(exc):
            response = "⚠️ Transly's Groq API key is invalid. The administrator needs to update it."
        elif "fallback failed" in str(exc) and "not installed" in str(exc):
            response = "⚠️ Transcription is temporarily unavailable. Please try again shortly."
        else:
            response = "⚠️ Transcription failed. Please try again in a moment."
        await message.edit_text(response)
    except Exception:
        LOGGER.exception("Media processing failed")
        await jobs.update(job_id, "FAILED", "unexpected processing failure")
        await message.edit_text("⚠️ Something went wrong while Transly was processing your media.")
    finally:
        remove_tree(job_dir)


def format_result(result: TranscriptResult) -> str:
    lines = [f"🌍 Language: {result.language}", f"⏱ Duration: {format_duration(result.duration)}", f"👥 Speakers: {result.speaker_count}", ""]
    if not result.language.lower().startswith("en"):
        lines.extend(["━━━━━━━━━━━━━━━━", "🇬🇧 ENGLISH TRANSLATION", "━━━━━━━━━━━━━━━━", ""])
    for segment in result.segments:
        text = segment.english_text if not result.language.lower().startswith("en") else segment.text
        lines.append(f"[{timestamp(segment.start)}] {segment.speaker.replace('_', ' ').title()}:")
        lines.extend([text or segment.text, ""])
    if not result.language.lower().startswith("en"):
        lines.extend(["━━━━━━━━━━━━━━━━", "🔤 ORIGINAL TRANSCRIPT", "━━━━━━━━━━━━━━━━", ""])
        for segment in result.segments:
            lines.extend([f"[{timestamp(segment.start)}] {segment.speaker.replace('_', ' ').title()}:", segment.text, ""])
    return "\n".join(lines).strip()


def timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


async def queue_worker(application: Application) -> None:
    while True:
        job_id, update, source, message = await application.bot_data["queue"].get()
        try:
            await process_media_now(job_id, update, application, source, message)
        finally:
            application.bot_data["queue"].task_done()


async def post_init(application: Application) -> None:
    application.bot_data["queue"] = asyncio.Queue()
    application.bot_data["queue_worker_task"] = asyncio.create_task(queue_worker(application))


async def post_shutdown(application: Application) -> None:
    task = application.bot_data.get("queue_worker_task")
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, NetworkError):
        LOGGER.warning("Telegram network request failed: %s", context.error)
    else:
        LOGGER.exception("Unhandled Telegram update error", exc_info=context.error)


def build_application(settings: Settings) -> Application:
    request = HTTPXRequest(
        connect_timeout=30,
        read_timeout=90,
        write_timeout=90,
        pool_timeout=30,
    )
    application = (Application.builder().token(settings.bot_token)
                   .request(request)
                   .post_init(post_init)
                   .post_shutdown(post_shutdown)
                   .build())
    application.bot_data["settings"] = settings
    primary = GeminiTranscriptionProvider(settings.gemini_api_key, settings.gemini_model, settings.max_gemini_retries)
    fallback = WhisperProvider(settings.whisper_model, settings.whisper_device, settings.whisper_compute_type)
    application.bot_data["router"] = TranscriptionRouter(primary, fallback)
    application.bot_data["translator"] = GeminiTranslationProvider(settings.gemini_api_key, settings.gemini_model, settings.max_gemini_retries)
    application.bot_data["jobs"] = JobStore(settings.database_url)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status))
    application.add_handler(CommandHandler("transcribe", transcribe_command))
    application.add_handler(MessageHandler(filters.ATTACHMENT, incoming_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, incoming_url))
    application.add_error_handler(error_handler)
    return application


def main() -> None:
    settings = load_settings()
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()