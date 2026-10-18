"""Regression coverage for shared proactive/reactive buffers and flushes."""

import asyncio
from contextlib import ExitStack, asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.telegram import client as telegram


class TelegramLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in (
            "current_messages_buffer", "pending_summaries", "burst_sessions", "wait_tasks",
            "flush_tasks", "last_message_time", "reply_locks", "buffer_locks", "responded_watermark",
            "pending_mention", "silent_until", "muted_chats",
        ):
            self.stack.enter_context(patch.dict(getattr(telegram, name), {}, clear=True))
        self.stack.enter_context(patch.object(telegram.client, "get_me", AsyncMock(return_value=SimpleNamespace(id=99))))
        self.stack.enter_context(patch.object(telegram, "get_history", AsyncMock(return_value=[])))
        self.stack.enter_context(patch.object(telegram, "upsert_user", AsyncMock()))
        self.stack.enter_context(patch.object(telegram, "get_summary", AsyncMock(return_value="")))
        self.stack.enter_context(patch.object(telegram, "get_summary_mood", AsyncMock(return_value=("", "default"))))
        self.persist = self.stack.enter_context(patch.object(telegram, "add_history_batch", AsyncMock()))
        self.stack.enter_context(patch.object(telegram, "set_summary", AsyncMock()))
        self.stack.enter_context(patch("builtins.print"))

    def message(self, message_id, sender_id=7):
        return telegram.BufferedMessage(telegram_message_id=message_id, sender_user_id=sender_id,
                                        sender_name="Jamie", content=f"Message {message_id}")

    async def test_flush_returns_exact_memory_snapshot_and_clears_session(self):
        telegram.current_messages_buffer[42] = [self.message(1)]
        telegram.pending_summaries[42] = "Exam next week"
        telegram.burst_sessions[42] = "burst-1"
        conversation, summary = await telegram._flush_chat(42, capture=True)
        self.assertEqual(conversation, [{"sender": "Jamie", "sender_user_id": 7,
                                         "content": "Message 1", "telegram_message_id": 1}])
        self.assertEqual(summary, "Exam next week")
        self.assertNotIn(42, telegram.current_messages_buffer)
        self.assertNotIn(42, telegram.burst_sessions)
        self.assertEqual(self.persist.await_args.kwargs["telegram_message_ids"], [1])

    async def test_message_waiting_for_flush_survives_in_new_buffer(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def persist(**kwargs):
            entered.set()
            await release.wait()

        telegram.current_messages_buffer[42] = [self.message(1)]
        with patch.object(telegram, "add_history_batch", persist):
            flush = asyncio.create_task(telegram._flush_chat(42, capture=True))
            await entered.wait()
            append = asyncio.create_task(telegram._append_to_buffer(42, self.message(2), 99))
            release.set()
            snapshot, _ = await flush
            await append
        self.assertEqual([m["telegram_message_id"] for m in snapshot], [1])
        self.assertEqual([m.telegram_message_id for m in telegram.current_messages_buffer[42]], [2])

    async def test_outreach_seed_then_inbound_message_reuses_buffer_and_session(self):
        # Outreach can initialize a buffer before new_message has ever run.
        await telegram._snapshot_context(42, 99)
        self.assertIn(42, telegram.burst_sessions)
        session = telegram.burst_sessions[42]
        sender = SimpleNamespace(id=7, first_name="Jamie", last_name=None, username="jamie")

        def event(message_id):
            return SimpleNamespace(chat_id=42, raw_text="Hi", sender_id=7, mentioned=False,
                message=SimpleNamespace(id=message_id), is_private=True,
                get_sender=AsyncMock(return_value=sender), chat=SimpleNamespace(title="Study Group"))

        def discard(coro):
            coro.close()
            return Mock(done=Mock(return_value=False), cancel=Mock())

        with patch.object(telegram, "_message_content", return_value="Hi"), patch.object(
            telegram, "_handle_group_speech_control", return_value="muted"
        ), patch.object(telegram.asyncio, "create_task", side_effect=discard):
            await telegram.new_message(event(1))
            await telegram.new_message(event(2))
        self.assertEqual([m.telegram_message_id for m in telegram.current_messages_buffer[42]], [1, 2])
        self.assertEqual(telegram.burst_sessions[42], session)

    async def test_reply_after_flush_reseeds_instead_of_crashing(self):
        @asynccontextmanager
        async def typing(*args):
            yield

        with patch.object(telegram.client, "is_connected", return_value=True), patch.object(
            telegram.client, "action", side_effect=typing
        ):
            await telegram._reply(SimpleNamespace(chat_id=42, is_private=True, mentioned=False, chat=None))
        self.assertIn(42, telegram.current_messages_buffer)

    async def test_outreach_sends_and_arms_memory_flush_without_advancing_reply_watermark(self):
        @asynccontextmanager
        async def typing(*args):
            yield

        with patch.object(telegram.client, "is_connected", return_value=True), patch.object(
            telegram.client, "action", side_effect=typing
        ), patch.object(telegram, "get_proactive_response", AsyncMock(return_value=("How was the exam?", "Check in", 0))), patch.object(
            telegram.client, "send_message", AsyncMock(return_value=SimpleNamespace(id=10))
        ):
            await telegram.send_proactive_message(42, "Ask about the exam", 1)
        timer = telegram.flush_tasks[42]
        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        self.assertEqual(telegram.current_messages_buffer[42][0].content, "How was the exam?")
        self.assertNotIn(42, telegram.responded_watermark)
        self.assertFalse(telegram.reply_locks[42].locked())

    async def test_muted_chat_rejects_outreach_without_generation(self):
        telegram.muted_chats[42] = object()
        with patch.object(telegram, "get_proactive_response", AsyncMock()) as generate:
            with self.assertRaisesRegex(RuntimeError, "muted"):
                await telegram.send_proactive_message(42, "Check in", 1)
        generate.assert_not_awaited()

    async def test_mute_while_waiting_for_reply_lock_rejects_outreach(self):
        lock = telegram.reply_locks.setdefault(42, asyncio.Lock())
        await lock.acquire()
        checked = asyncio.Event()

        async def ready(chat_id):
            checked.set()

        with patch.object(telegram, "_wait_for_reactive_and_silence", ready), patch.object(
            telegram, "get_proactive_response", AsyncMock()
        ) as generate:
            task = asyncio.create_task(telegram.send_proactive_message(42, "Check in", 1))
            await checked.wait()
            telegram.muted_chats[42] = object()
            lock.release()
            with self.assertRaisesRegex(RuntimeError, "muted"):
                await task
        generate.assert_not_awaited()
        self.assertFalse(lock.locked())


if __name__ == "__main__":
    unittest.main()
