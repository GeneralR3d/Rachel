"""Exercise speech synthesis at the ElevenLabs streaming boundary."""

import asyncio
import unittest
from unittest.mock import patch

from app.services import voice


# First Ogg page with one 19-byte Opus identification packet.
OPUS_HEAD = b"OpusHead\x01\x01\x38\x01\x80\xbb\x00\x00\x00\x00\x00"
OPUS_TAGS = b"OpusTags" + bytes(8)


def ogg_page(flags, sequence, packet, *, granule=0):
    header = (b"OggS\x00" + bytes([flags]) + granule.to_bytes(8, "little")
              + bytes(4) + sequence.to_bytes(4, "little") + bytes(4))
    return header + bytes([1, len(packet)]) + packet


OGG_HEADER = ogg_page(2, 0, OPUS_HEAD)
OGG_TAGS = ogg_page(0, 1, OPUS_TAGS)
OPUS_PACKET = b"\xf8\xff\xfe"
AUDIO = OGG_HEADER + OGG_TAGS + ogg_page(4, 2, OPUS_PACKET, granule=48_313)
REPLY = "  First paragraph.\n\nSecond paragraph!  "


class OpusDurationTests(unittest.TestCase):
    def test_duration_uses_final_granule_and_pre_skip(self):
        audio = OGG_HEADER + OGG_TAGS + ogg_page(4, 2, OPUS_PACKET, granule=48_312)
        self.assertEqual(voice.ogg_opus_duration(audio), 1)
        self.assertEqual(voice.ogg_opus_duration(AUDIO), 2)

    def test_duration_requires_ogg_opus_headers_and_a_complete_final_page(self):
        missing_final_page = OGG_HEADER + OGG_TAGS + ogg_page(0, 2, OPUS_PACKET, granule=48_313)
        for audio in (b"", b"not audio", b"OggS OpusHead", OGG_HEADER, AUDIO[:-1],
                      missing_final_page, AUDIO.replace(b"OpusHead", b"OpusTags")):
            with self.subTest(audio=audio), self.assertRaises(voice.VoiceSynthesisError):
                voice.ogg_opus_duration(audio)


class VoiceSynthesisTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.http = self.enterContext(patch.object(voice.httpx, "AsyncClient"))
        self.sdk = self.enterContext(patch.object(voice, "AsyncElevenLabs"))
        self.stream = self.sdk.return_value.text_to_speech.stream
        self.stream_closed = False

    def provide_audio(self, chunks=(), *, error=None, wait=None):
        async def audio():
            try:
                for chunk in chunks:
                    yield chunk
                if error is not None:
                    raise error
                if wait is not None:
                    wait.set()
                    await asyncio.Future()
            finally:
                self.stream_closed = True

        self.stream.side_effect = lambda **kwargs: audio()

    async def synthesize(self):
        return await voice.synthesize_voice_reply(
            REPLY, api_key="key", voice_id="voice-id", model_id="model-id"
        )

    def assert_resources_closed(self):
        self.assertTrue(self.stream_closed)
        self.http.return_value.__aexit__.assert_awaited_once()

    async def test_streams_one_request_with_the_original_text(self):
        self.provide_audio([AUDIO[:2], AUDIO[2:32], AUDIO[32:]])
        self.assertEqual(await self.synthesize(), AUDIO)
        self.sdk.assert_called_once_with(
            api_key="key", httpx_client=self.http.return_value.__aenter__.return_value
        )
        self.stream.assert_called_once_with(
            text=REPLY, voice_id="voice-id", model_id="model-id",
            output_format="opus_48000_64", request_options={"max_retries": 0},
        )
        self.assert_resources_closed()

    async def test_rejects_empty_audio(self):
        self.provide_audio()
        with self.assertRaisesRegex(voice.VoiceSynthesisError, "empty audio"):
            await self.synthesize()
        self.assert_resources_closed()

    async def test_rejects_provider_failure_and_interrupted_stream(self):
        for chunks in ([], [AUDIO]):
            with self.subTest(chunks=chunks):
                self.http.reset_mock()
                self.stream_closed = False
                self.provide_audio(chunks, error=RuntimeError("provider failure"))
                with self.assertRaises(voice.VoiceSynthesisError):
                    await self.synthesize()
                self.assert_resources_closed()

    async def test_rejects_overflow_and_closes_stream_without_reading_more(self):
        self.provide_audio([AUDIO, b"x"], error=AssertionError("read after overflow"))
        with patch.object(voice, "MAX_AUDIO_BYTES", len(AUDIO)):
            with self.assertRaisesRegex(voice.VoiceSynthesisError, "size limit"):
                await self.synthesize()
        self.assert_resources_closed()

    async def test_times_out_even_after_audio_starts(self):
        waiting = asyncio.Event()
        self.provide_audio([AUDIO], wait=waiting)
        with patch.object(voice, "SYNTHESIS_TIMEOUT_SECONDS", 0.01):
            with self.assertRaisesRegex(voice.VoiceSynthesisError, "timed out"):
                await self.synthesize()
        self.assertTrue(waiting.is_set())
        self.assert_resources_closed()

    async def test_cancellation_propagates_and_closes_resources(self):
        waiting = asyncio.Event()
        self.provide_audio([AUDIO], wait=waiting)
        task = asyncio.create_task(self.synthesize())
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assert_resources_closed()
