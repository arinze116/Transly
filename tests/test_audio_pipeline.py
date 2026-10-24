import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import downloader
import transcriber
from downloader import extract_audio
from transcriber import GROQ_MAX_BYTES, GROQ_SAFE_BYTES, GroqTranscriber, TranscriptionError


def test_extract_audio_uses_compressed_mono_mp3(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"media")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "ffprobe":
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        Path(command[-1]).write_bytes(b"compressed-audio")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(downloader.subprocess, "run", fake_run)
    output = asyncio.run(extract_audio(source))

    assert output.name == "normalized.mp3"
    ffmpeg = commands[1]
    assert "-ac" in ffmpeg and ffmpeg[ffmpeg.index("-ac") + 1] == "1"
    assert "-ar" in ffmpeg and ffmpeg[ffmpeg.index("-ar") + 1] == "16000"
    assert "-b:a" in ffmpeg and ffmpeg[ffmpeg.index("-b:a") + 1] == "32k"
    assert "pcm_s16le" not in ffmpeg


def test_post_audio_rejects_oversize_before_reading(tmp_path):
    path = tmp_path / "oversized.mp3"
    with path.open("wb") as output:
        output.truncate(GROQ_MAX_BYTES + 1)
    client = GroqTranscriber("not-a-real-key", retries=0)

    with pytest.raises(TranscriptionError, match="limit is 25 MB"):
        asyncio.run(client._post_audio(path))


def test_oversized_audio_is_chunked_in_order_and_cleaned_up(tmp_path, monkeypatch):
    source = tmp_path / "normalized.mp3"
    with source.open("wb") as output:
        output.truncate(GROQ_SAFE_BYTES + 1)

    monkeypatch.setattr(transcriber, "_probe_duration", lambda path: 2400.0)

    def fake_split(path, output_dir, duration, chunk_seconds):
        first = output_dir / "chunk-0000.mp3"
        second = output_dir / "chunk-0001.mp3"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        assert duration == 2400.0
        assert chunk_seconds == 20 * 60
        return [first, second]

    monkeypatch.setattr(transcriber, "_split_audio", fake_split)
    client = GroqTranscriber("not-a-real-key", retries=0)
    seen = []

    async def fake_post_audio(path):
        seen.append(path.name)
        text = "first chunk" if "0000" in path.name else "second chunk"
        return {
            "language": "en",
            "duration": 1200.0,
            "segments": [{"start": 2.0, "end": 3.0, "text": text}],
        }

    monkeypatch.setattr(client, "_post_audio", fake_post_audio)
    result = asyncio.run(client.transcribe_structured(source))

    assert seen == ["chunk-0000.mp3", "chunk-0001.mp3"]
    assert [segment.text for segment in result.segments] == ["first chunk", "second chunk"]
    assert [(segment.start, segment.end) for segment in result.segments] == [(2.0, 3.0), (1202.0, 1203.0)]
    assert result.duration == 2400.0
    assert result.language == "en"
    assert list(tmp_path.glob("transly-chunks-*")) == []


def test_split_audio_rejects_chunk_over_provider_limit(tmp_path, monkeypatch):
    source = tmp_path / "source.mp3"
    source.write_bytes(b"source")

    def fake_run(command, **kwargs):
        Path(command[-1]).write_bytes(b"x" * (GROQ_MAX_BYTES + 1))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(transcriber.subprocess, "run", fake_run)
    with pytest.raises(TranscriptionError, match="still exceeds Groq"):
        transcriber._split_audio(source, tmp_path / "chunks", 10, 20)
