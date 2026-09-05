import asyncio

import pytest

from bot import format_result, timestamp
from models import TranscriptResult, TranscriptSegment
from security import UnsafeURL, validate_resolved_host, validate_url
from utils import chunk_text, extract_url


def test_url_and_chunking():
    assert extract_url("watch https://example.com/a.") == "https://example.com/a"
    assert all(len(part) <= 4096 for part in chunk_text("word " * 2000))


def test_structured_output_preserves_translation_and_original():
    result = TranscriptResult("fr", 12.2, [TranscriptSegment(2.4, 5, "speaker_1", "Bonjour", "Hello")])
    output = format_result(result)
    assert "ENGLISH TRANSLATION" in output
    assert "ORIGINAL TRANSCRIPT" in output
    assert "[00:00:02] Speaker 1:" in output
    assert "Bonjour" in output and "Hello" in output


def test_timestamp():
    assert timestamp(3723.9) == "01:02:03"


def test_private_urls_are_blocked():
    with pytest.raises(UnsafeURL):
        validate_url("http://127.0.0.1/file.mp3")
    with pytest.raises(UnsafeURL):
        validate_url("file:///etc/passwd")


def test_public_host_resolution():
    asyncio.run(validate_resolved_host("https://example.com/media.mp3"))
