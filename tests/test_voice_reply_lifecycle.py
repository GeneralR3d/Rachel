"""Reactive delivery keeps one transcript across voice and text modes."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call, patch

from app.services.voice import VoiceSynthesisError
from app.telegram import client as telegram
from tests import test_telegram_lifecycle as lifecycle
from tests.test_voice import AUDIO

CHAT_ID = 42
REPLY = "First paragraph.\n\nSecond paragraph!"
REASON = "Answers Jamie's question."
ACCENT_PREFIX = "[Strong Singapore Accent] "


class VoiceReplyLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lifecycle.TelegramLifecycleTests.setUp(self)
        self.stack.enter_context(patch.object(telegram.settings, "elevenlabs_api_key", "test-key"))
        self.stack.enter_context(patch.object(telegram.settings, "elevenlabs_voice_id", "test-voice"))
        self.stack.enter_context(patch.object(telegram.settings, "elevenlabs_model_id", "eleven_v4"))
        self.stack.enter_context(patch.object(telegram.client, "is_connected", return_value=True))
        self.stack.enter_context(patch.object(telegram.client, "action", side_effect=self.action))
        self.draw = self.stack.enter_context(patch.object(telegram.random, "random", return_value=0.149))
        self.generate = self.stack.enter_context(patch.object(
            telegram, "get_response", AsyncMock(return_value=(REPLY, REASON, "Updated summary", 0))
        ))
        self.synthesize = AsyncMock(return_value=AUDIO, side_effect=self.synthesize_while_recording)
        self.stack.enter_context(patch.object(telegram, "synthesize_voice_reply", self.synthesize))
        self.voice = self.stack.enter_context(patch.object(
            telegram.client, "send_file", AsyncMock(side_effect=self.upload)
        ))
        self.text = self.stack.enter_context(patch.object(telegram.client, "send_message", AsyncMock()))
        self.actions = []
        self.active_action = None
        self.uploaded = None
        self.seed_chat()

    def seed_chat(self):
        telegram.current_messages_buffer[CHAT_ID] = [lifecycle.TelegramLifecycleTests.message(self, 1)]
        telegram.responded_watermark.clear()
        self.text.side_effect = [SimpleNamespace(id=10), SimpleNamespace(id=11)]

    @asynccontextmanager
    async def action(self, chat_id, action):
        self.assertEqual(chat_id, CHAT_ID)
        self.assertIsNone(self.active_action)
        self.actions.append(action)
        self.active_action = action
        try:
            yield
        finally:
            self.active_action = None

    async def synthesize_while_recording(self, *args, **kwargs):
        self.assertEqual(self.active_action, "record-voice")
        self.assertTrue(telegram.reply_locks[CHAT_ID].locked())
        self.assertFalse(telegram.buffer_locks[CHAT_ID].locked())
        return AUDIO

    async def upload(self, chat_id, file, **kwargs):
        self.assertIsNone(self.active_action)
        self.assertTrue(telegram.reply_locks[CHAT_ID].locked())
        self.assertEqual(chat_id, CHAT_ID)
        self.assertEqual(set(kwargs), {"voice_note", "attributes"})
        self.assertIs(kwargs["voice_note"], True)
        attribute, = kwargs["attributes"]
        self.assertTrue(attribute.voice)
        self.assertEqual(attribute.duration, 2)
        self.assertEqual(file.name, "voice.ogg")
        self.assertEqual(file.tell(), 0)
        self.assertEqual(file.read(), AUDIO)
        self.uploaded = file
        return SimpleNamespace(id=12)

    async def reactive(self, *, is_private=True):
        # A latched mention keeps admission independent of the delivery draw.
        telegram.pending_mention[CHAT_ID] = True
        await telegram.reply(SimpleNamespace(chat_id=CHAT_ID, is_private=is_private, mentioned=False, chat=None))

    def assert_transcript(self, message_id, content=REPLY):
        messages = telegram.current_messages_buffer[CHAT_ID]
        self.assertEqual([m.telegram_message_id for m in messages], [1, message_id])
        self.assertEqual((messages[-1].content, messages[-1].reason), (content, REASON))

    async def test_private_and_group_replies_each_send_one_captionless_voice_note(self):
        for is_private in (True, False):
            with self.subTest(is_private=is_private):
                await self.reactive(is_private=is_private)
                self.assertEqual(self.generate.await_args.kwargs["is_private"], is_private)
                self.draw.assert_called_once_with()
                self.synthesize.assert_awaited_once_with(
                    ACCENT_PREFIX + REPLY, api_key="test-key", voice_id="test-voice", model_id="eleven_v4"
                )
                self.assertEqual(self.actions, ["typing", "record-voice"])
                self.voice.assert_awaited_once()
                self.assertTrue(self.uploaded.closed)
                self.text.assert_not_awaited()
                self.assert_transcript(12)
                self.seed_chat()
                self.actions.clear()
                for mock in (self.draw, self.synthesize, self.voice):
                    mock.reset_mock()

    async def test_text_selection_at_fifteen_percent_and_with_no_key(self):
        for draw, key in ((0.15, "test-key"), (0, None), (0, ""), (0, " \t ")):
            with self.subTest(draw=draw, key=key), patch.object(telegram.settings, "elevenlabs_api_key", key):
                self.draw.return_value = draw
                await self.reactive()
                self.draw.assert_called_once_with()
                self.synthesize.assert_not_awaited()
                self.voice.assert_not_awaited()
                self.assertEqual(self.text.await_args_list, [
                    call(CHAT_ID, "First paragraph."), call(CHAT_ID, "Second paragraph!")
                ])
                self.assert_transcript(10)
                self.seed_chat()
                self.draw.reset_mock()
                self.text.reset_mock()

    async def test_urls_in_any_paragraph_use_the_original_text(self):
        self.draw.return_value = 0
        for link in (
            "<http://example.com>",
            "[the site](HTTPS://example.com/path)",
            "www.example.com",
            "example.com/path",
            "tg://resolve?domain=example",
        ):
            with self.subTest(link=link):
                self.seed_chat()
                self.text.reset_mock()
                self.actions.clear()
                paragraph = f"See {link}."
                response = f"First paragraph.\n\n{paragraph}"
                self.generate.return_value = (response, REASON, None, 0)
                with patch.object(telegram.asyncio, "sleep", AsyncMock()):
                    await self.reactive()
                self.synthesize.assert_not_awaited()
                self.voice.assert_not_awaited()
                self.assertEqual(self.actions, ["typing", "typing"])
                self.assertEqual(self.text.await_args_list, [
                    call(CHAT_ID, "First paragraph."), call(CHAT_ID, paragraph),
                ])
                self.assert_transcript(10, response)

    async def test_blank_replies_do_not_draw_or_send(self):
        for response in ("", " \t\n\n"):
            with self.subTest(response=response):
                self.generate.return_value = (response, REASON, None, 0)
                await self.reactive()
                self.draw.assert_not_called()
                self.synthesize.assert_not_awaited()
                self.voice.assert_not_awaited()
                self.text.assert_not_awaited()
                self.assertEqual(len(telegram.current_messages_buffer[CHAT_ID]), 1)
                self.seed_chat()

    async def test_voice_requires_fewer_than_650_characters_before_accent_tag(self):
        response = "x" * 649
        self.generate.return_value = (response, REASON, None, 0)
        await self.reactive()
        self.synthesize.assert_awaited_once()
        self.assertEqual(self.synthesize.await_args.args[0], ACCENT_PREFIX + response)
        self.assert_transcript(12, response)
        self.seed_chat()
        self.synthesize.reset_mock()
        self.voice.reset_mock()
        response += "x"
        self.generate.return_value = (response, REASON, None, 0)
        await self.reactive()
        self.synthesize.assert_not_awaited()
        self.voice.assert_not_awaited()
        self.text.assert_awaited_once_with(CHAT_ID, response)
        self.assert_transcript(10, response)

    async def test_typed_reply_keeps_one_send_per_paragraph(self):
        self.draw.return_value = 0.5
        paragraph = "x" * 4095 + "😀" * 10 + " "
        response = paragraph + "\n\nSecond paragraph!"
        self.generate.return_value = (response, REASON, None, 0)
        with patch.object(telegram.asyncio, "sleep", AsyncMock()) as sleep:
            await self.reactive()
        self.assertEqual(self.text.await_args_list, [
            call(CHAT_ID, paragraph), call(CHAT_ID, "Second paragraph!"),
        ])
        sleep.assert_awaited_once_with(len("Second paragraph!") / telegram.TYPING_SPEED)
        self.assert_transcript(10, response)

    async def test_overridden_model_can_try_short_text_and_falls_back_on_rejection(self):
        response = "Try this instead."
        self.generate.return_value = (response, REASON, None, 0)
        self.synthesize.side_effect = VoiceSynthesisError("Rejected input")
        with patch.object(telegram.settings, "elevenlabs_model_id", "custom"):
            await self.reactive()
        self.synthesize.assert_awaited_once_with(
            ACCENT_PREFIX + response, api_key="test-key", voice_id="test-voice", model_id="custom"
        )
        self.voice.assert_not_awaited()
        self.text.assert_awaited_once_with(CHAT_ID, response)
        self.assert_transcript(10, response)

    async def test_cancellation_during_synthesis_propagates_without_sending(self):
        started = asyncio.Event()

        async def synthesize(*args, **kwargs):
            started.set()
            await asyncio.Future()

        self.synthesize.side_effect = synthesize
        task = asyncio.create_task(self.reactive())
        try:
            await asyncio.wait_for(started.wait(), 1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.voice.assert_not_awaited()
        self.text.assert_not_awaited()
        self.assertFalse(telegram.reply_locks[CHAT_ID].locked())

    async def test_audio_without_duration_metadata_falls_back_to_text(self):
        self.synthesize.side_effect = None
        self.synthesize.return_value = b"not Ogg audio"
        self.generate.return_value = ("Try this instead.", REASON, None, 0)
        await self.reactive()
        self.voice.assert_not_awaited()
        self.text.assert_awaited_once_with(CHAT_ID, "Try this instead.")
        self.assert_transcript(10, "Try this instead.")

    async def test_ambiguous_voice_upload_does_not_fall_back_or_record(self):
        async def upload(*args, **kwargs):
            await self.upload(*args, **kwargs)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        self.voice.side_effect = upload
        for outcome, error in (
            (TimeoutError("No confirmation"), TimeoutError),
            (asyncio.CancelledError(), asyncio.CancelledError),
            (None, RuntimeError),
            (SimpleNamespace(id=0), RuntimeError),
            (SimpleNamespace(id=True), RuntimeError),
        ):
            with self.subTest(outcome=outcome):
                with self.assertRaises(error):
                    await self.reactive()
                self.voice.assert_awaited_once()
                self.assertTrue(self.uploaded.closed)
                self.text.assert_not_awaited()
                self.assertEqual(len(telegram.current_messages_buffer[CHAT_ID]), 1)
                self.persist.assert_not_awaited()
                self.assertFalse(telegram.reply_locks[CHAT_ID].locked())
                self.voice.reset_mock()
                self.seed_chat()

    async def test_flush_during_synthesis_persists_voice_transcript_directly(self):
        async def synthesize(*args, **kwargs):
            await self.synthesize_while_recording()
            await telegram._flush_chat(CHAT_ID)
            return AUDIO

        self.synthesize.side_effect = synthesize
        await self.reactive()
        self.assertNotIn(CHAT_ID, telegram.current_messages_buffer)
        self.assertEqual(self.persist.await_args.kwargs, {
            "chat_ids": [CHAT_ID], "sender_user_ids": [99], "contents": [REPLY],
            "telegram_message_ids": [12], "reasons": [REASON],
        })

    async def test_buffer_flush_reseed_and_context_keep_transcript_for_every_delivery_mode(self):
        for mode, message_id in (("voice", 12), ("text", 10), ("fallback", 10)):
            with self.subTest(mode=mode):
                self.seed_chat()
                self.draw.return_value = 0.5 if mode == "text" else 0
                self.synthesize.side_effect = (
                    VoiceSynthesisError("Synthesis timed out") if mode == "fallback"
                    else self.synthesize_while_recording
                )
                late_message = lifecycle.TelegramLifecycleTests.message(self, 20)

                async def generate(**kwargs):
                    await telegram._append_to_buffer(CHAT_ID, late_message, 99)
                    return REPLY, REASON, "Updated summary", 0

                self.generate.side_effect = generate
                await self.reactive()
                buffer = telegram.current_messages_buffer[CHAT_ID]
                self.assertEqual([m.telegram_message_id for m in buffer], [1, message_id, 20])
                self.assertEqual((buffer[1].content, buffer[1].reason), (REPLY, REASON))
                memories = AsyncMock()
                with (
                    patch.object(telegram, "get_last_processed_message_id", AsyncMock(return_value=0)),
                    patch.object(telegram, "set_last_processed_message_id", AsyncMock()),
                    patch.object(telegram, "update_memories", memories),
                ):
                    await telegram.finalize_conversation(CHAT_ID, 0)
                    await asyncio.sleep(0)
                self.assertEqual(memories.await_args.args[0][1]["content"], REPLY)
                batch = self.persist.await_args.kwargs
                self.assertEqual(batch["telegram_message_ids"], [1, message_id, 20])
                self.assertEqual(batch["contents"][1], REPLY)
                self.assertEqual(batch["reasons"][1], REASON)
                stored = {"sender_user_id": 99, "sender": "Rachel", "content": batch["contents"][1],
                          "telegram_message_id": message_id, "reason": batch["reasons"][1]}
                with patch.object(telegram, "get_history", AsyncMock(return_value=[stored])):
                    rows, context, _ = await telegram._snapshot_context(CHAT_ID, 99)
                self.assertEqual(context[0]["content"], REPLY)
                self.assertEqual(rows[0].reason, REASON)

    async def test_queued_reply_waits_for_synthesis_and_sees_the_voice_transcript(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def synthesize(*args, **kwargs):
            started.set()
            await release.wait()
            return AUDIO

        self.synthesize.side_effect = synthesize
        first = asyncio.create_task(self.reactive())
        second = None
        try:
            await asyncio.wait_for(started.wait(), 1)
            await telegram._append_to_buffer(CHAT_ID, lifecycle.TelegramLifecycleTests.message(self, 20), 99)
            second = asyncio.create_task(self.reactive())
            await asyncio.sleep(0)
            self.generate.assert_awaited_once()
            self.assertFalse(second.done())
            self.draw.return_value = 0.5
        finally:
            release.set()
            await asyncio.gather(*(task for task in (first, second) if task is not None))
        self.assertEqual(self.generate.await_args.kwargs["history"][1]["content"], REPLY)
        self.synthesize.assert_awaited_once()

    async def test_proactive_outreach_uses_text_with_voice_configured(self):
        with (
            patch.object(telegram, "get_proactive_response", AsyncMock(return_value=(REPLY, REASON, 0))),
            patch.object(telegram, "_reset_flush_timer"),
        ):
            await telegram.send_proactive_message(CHAT_ID, "Check in with Jamie", 2)
        self.draw.assert_not_called()
        self.synthesize.assert_not_awaited()
        self.voice.assert_not_awaited()
        self.assertEqual(self.text.await_count, 2)
        self.assert_transcript(10)
