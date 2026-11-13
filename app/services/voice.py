"""Synthesize one in-memory Ogg Opus voice reply with ElevenLabs."""

import asyncio
from contextlib import aclosing
from math import ceil

import httpx
from elevenlabs.client import AsyncElevenLabs

SYNTHESIS_TIMEOUT_SECONDS = 60
MAX_AUDIO_BYTES = 10 * 1024 * 1024
OPUS_SAMPLE_RATE = 48_000


class VoiceSynthesisError(RuntimeError):
    """Speech synthesis failed before Telegram delivery could start."""


def ogg_opus_duration(audio: bytes | bytearray) -> int:
    """Read the Opus pre-skip and final Ogg page's sample position."""
    if len(audio) < 28 or not audio.startswith(b"OggS\x00"):
        raise VoiceSynthesisError("ElevenLabs returned invalid Ogg Opus audio")
    header_start = 27 + audio[26]
    if (
        not audio[26]
        or audio[27] < 19
        or header_start + 19 > len(audio)
        or not audio.startswith(b"OpusHead", header_start)
    ):
        raise VoiceSynthesisError("ElevenLabs returned invalid Ogg Opus audio")

    pre_skip = int.from_bytes(audio[header_start + 10:header_start + 12], "little")
    offset = 0
    while offset < len(audio):
        segment_table = offset + 27
        if segment_table > len(audio) or not audio.startswith(b"OggS\x00", offset):
            break
        payload_start = segment_table + audio[offset + 26]
        page_end = payload_start + sum(audio[segment_table:payload_start])
        if page_end > len(audio):
            break
        if audio[offset + 5] & 4:
            granule = int.from_bytes(audio[offset + 6:offset + 14], "little", signed=True)
            if offset and page_end == len(audio) and granule > pre_skip:
                return ceil((granule - pre_skip) / OPUS_SAMPLE_RATE)
            break
        offset = page_end
    raise VoiceSynthesisError("ElevenLabs returned incomplete or invalid Ogg Opus audio")


async def synthesize_voice_reply(
    text: str, *, api_key: str, voice_id: str, model_id: str
) -> bytes:
    """Return the complete audio or raise a sanitized synthesis error."""
    audio = bytearray()
    try:
        async with asyncio.timeout(SYNTHESIS_TIMEOUT_SECONDS), httpx.AsyncClient() as http_client:
            sdk = AsyncElevenLabs(api_key=api_key, httpx_client=http_client)
            stream = sdk.text_to_speech.stream(
                text=text,
                voice_id=voice_id,
                model_id=model_id,
                output_format="opus_48000_64",
                request_options={"max_retries": 0},
            )
            async with aclosing(stream):
                async for chunk in stream:
                    if len(audio) + len(chunk) > MAX_AUDIO_BYTES:
                        raise VoiceSynthesisError("ElevenLabs audio exceeded the size limit")
                    audio.extend(chunk)
            if not audio:
                raise VoiceSynthesisError("ElevenLabs returned empty audio")
            return bytes(audio)
    except VoiceSynthesisError:
        raise
    except TimeoutError:
        raise VoiceSynthesisError("ElevenLabs synthesis timed out") from None
    except Exception as exc:
        raise VoiceSynthesisError(f"ElevenLabs synthesis failed ({type(exc).__name__})") from None
