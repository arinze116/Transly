"""Media acquisition and normalization through yt-dlp and ffmpeg."""

import asyncio
from pathlib import Path
import subprocess
import re
import uuid

import httpx
import yt_dlp
from security import validate_resolved_host


class DownloadError(Exception):
    pass


class NoAudioTrackError(DownloadError):
    pass


async def _download_direct(url: str, job_dir: Path, max_size: int) -> Path | None:
    """Download a URL when it points directly to a media file."""
    suffix = Path(url.split("?", 1)[0]).suffix.lower()
    allowed = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac", ".opus", ".mp4", ".mkv", ".avi", ".mov", ".webm", ".ts"}
    if suffix not in allowed:
        return None
    path = job_dir / f"direct{suffix}"
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=60) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                if response.headers.get("content-length") and int(response.headers["content-length"]) > max_size:
                    raise DownloadError("Download exceeds configured size limit")
                with path.open("wb") as output:
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_size:
                            raise DownloadError("Download exceeds configured size limit")
                        output.write(chunk)
    except DownloadError:
        path.unlink(missing_ok=True)
        raise
    except (httpx.HTTPError, OSError) as exc:
        path.unlink(missing_ok=True)
        raise DownloadError(f"Direct media download failed: {exc}") from exc
    return path


async def _download_x_post(url: str, job_dir: Path, max_size: int) -> Path | None:
    """Use X's public syndication response for posts yt-dlp cannot recognize."""
    match = re.search(r"(?:x\.com|twitter\.com)/[^/]+/status/(\d+)", url, re.IGNORECASE)
    if not match:
        return None
    endpoint = f"https://cdn.syndication.twimg.com/tweet-result?id={match.group(1)}&lang=en"
    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
            response = await client.get(endpoint)
            response.raise_for_status()
            payload = response.json()
            variants = []
            def collect(value):
                if isinstance(value, dict):
                    if value.get("content_type") == "video/mp4" and value.get("url"):
                        variants.append(value)
                    for child in value.values():
                        collect(child)
                elif isinstance(value, list):
                    for child in value:
                        collect(child)
            collect(payload)
            if not variants:
                return None
            selected = max(variants, key=lambda item: item.get("bitrate", 0))
            media_url = selected["url"]
            await validate_resolved_host(media_url)
            path = job_dir / "x-media.mp4"
            async with client.stream("GET", media_url) as media:
                media.raise_for_status()
                if media.headers.get("content-length") and int(media.headers["content-length"]) > max_size:
                    raise DownloadError("Download exceeds configured size limit")
                total = 0
                with path.open("wb") as output:
                    async for chunk in media.aiter_bytes():
                        total += len(chunk)
                        if total > max_size:
                            raise DownloadError("Download exceeds configured size limit")
                        output.write(chunk)
            return path
    except DownloadError:
        (job_dir / "x-media.mp4").unlink(missing_ok=True)
        raise
    except (httpx.HTTPError, ValueError, OSError) as exc:
        (job_dir / "x-media.mp4").unlink(missing_ok=True)
        raise DownloadError(f"X media download failed: {exc}") from exc


async def download_url(url: str, directory: Path, max_size_mb: int = 500, timeout: int = 600) -> Path:
    await validate_resolved_host(url)
    job_dir = directory / f"url-{uuid.uuid4().hex}"
    job_dir.mkdir(parents=True, exist_ok=True)

    direct = await _download_direct(url, job_dir, max_size_mb * 1024 * 1024)
    if direct is not None:
        return direct
    x_media = await _download_x_post(url, job_dir, max_size_mb * 1024 * 1024)
    if x_media is not None:
        return x_media

    def run() -> Path:
        output = job_dir / "media.%(ext)s"
        options = {
            "format": "bestaudio/best",
            "outtmpl": str(output),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": True,
            "socket_timeout": timeout,
            "max_filesize": max_size_mb * 1024 * 1024,
        }
        try:
            with yt_dlp.YoutubeDL(options) as client:
                client.download([url])
        except Exception as exc:
            message = str(exc)
            if "Unsupported URL" in message:
                raise DownloadError("This platform or URL is not supported by yt-dlp") from exc
            raise DownloadError(message) from exc
        files = [item for item in job_dir.iterdir() if item.is_file()]
        if not files:
            raise DownloadError("No media was downloaded")
        return files[0]

    return await asyncio.to_thread(run)


async def extract_audio(path: Path) -> Path:
    # Normalize every input to one speech-friendly format. This avoids relying
    # on the provider accepting every container/codec Telegram or yt-dlp emits.
    output = path.with_name("normalized.wav")

    def run() -> None:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=index", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0 or not probe.stdout.strip():
            raise NoAudioTrackError("media contains no audio stream")
        command = [
            "ffmpeg", "-nostdin", "-y", "-i", str(path), "-vn",
            "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(output),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=900)
        if result.returncode != 0:
            detail = result.stderr.strip().splitlines()
            reason = detail[-1] if detail else "unknown FFmpeg error"
            raise DownloadError(f"ffmpeg could not extract an audio track: {reason}")

    await asyncio.to_thread(run)
    return output
