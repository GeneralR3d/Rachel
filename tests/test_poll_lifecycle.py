"""Poll side effects plus normal text replies share the existing lifecycle."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.exceptions import OutputParserException

from app.services import llm
from app.services.memory import build_extraction_history
from app.services.poll_tools import POLL_REASON
from app.telegram import client as telegram
from tests.test_agent_loop import FetcherModel, Model, calls, finish, reply
from tests.test_poll_graph import POLL
from tests import test_telegram_lifecycle as lifecycle

PLACEHOLDER = "[Poll sent]\nQuestion: Which language?\nOptions:\n1. Python\n2. Rust"
TEXT = "Have a vote!"


class PollLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        lifecycle.TelegramLifecycleTests.setUp(self)
        self.fetcher = FetcherModel([calls(POLL), finish()])
        self.responder = Model([reply(content=TEXT)])
        self.stack.enter_context(patch.object(llm, "_get_context_fetcher_llm", AsyncMock(return_value=self.fetcher)))
        self.stack.enter_context(patch.object(llm, "_get_responder_llm", AsyncMock(return_value=self.responder)))
        self.stack.enter_context(patch.object(llm, "get_active_trait_prompts", AsyncMock(return_value="friendly")))
        self.stack.enter_context(patch.object(llm.settings, "agent_max_context_passes", 4))
        self.stack.enter_context(patch.object(llm, "summarizer_node", AsyncMock(return_value={"current_summary": "updated", "mood": "excited"})))
        self.stack.enter_context(patch.object(llm, "_graph", llm._build_graph()))
        self.stack.enter_context(patch.object(llm, "_proactive_graph", llm._build_proactive_graph()))
        self.stack.enter_context(patch.object(telegram.client, "is_connected", return_value=True))
        self.stack.enter_context(patch.object(telegram.client, "action", side_effect=self.typing))
        self.send = self.stack.enter_context(patch.object(telegram.client, "send_file", AsyncMock(return_value=SimpleNamespace(id=123))))
        self.text = self.stack.enter_context(patch.object(telegram.client, "send_message", AsyncMock(return_value=SimpleNamespace(id=124))))
        telegram.current_messages_buffer[42] = [lifecycle.TelegramLifecycleTests.message(self, 1)]

    @asynccontextmanager
    async def typing(self, *args):
        yield

    async def asyncTearDown(self):
        tasks = list(telegram.flush_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def reactive(self):
        await telegram.reply(SimpleNamespace(chat_id=42, is_private=True, mentioned=True, chat=None))

    def assert_poll_and_text_buffer(self, responder_runs=1):
        messages = telegram.current_messages_buffer[42]
        self.assertEqual([m.telegram_message_id for m in messages], [1, 123, 124])
        poll, text = messages[1:]
        self.assertEqual(poll.sender_user_id, 99)
        self.assertEqual(poll.sender_name, telegram.BOT_NAME)
        self.assertEqual(poll.content, PLACEHOLDER)
        self.assertEqual(poll.reason, POLL_REASON)
        self.assertFalse(poll.is_persisted)
        self.assertEqual(text.content, TEXT)
        self.send.assert_awaited_once()
        self.text.assert_awaited_once_with(42, TEXT)
        self.assertEqual(len(self.responder.messages), responder_runs)
        self.assertFalse(telegram.reply_locks[42].locked())

    async def test_reactive_poll_and_text_persist_and_reload_into_memory_context(self):
        await self.reactive()
        self.assert_poll_and_text_buffer()
        self.assertEqual(telegram.pending_summaries[42], "updated")
        conversation, summary = await telegram._flush_chat(42, capture=True)
        self.assertEqual(conversation[1]["content"], PLACEHOLDER)
        self.assertEqual(summary, "updated")
        self.assertEqual(self.persist.await_args.kwargs["contents"], ["Message 1", PLACEHOLDER, TEXT])
        self.assertEqual(self.persist.await_args.kwargs["telegram_message_ids"], [1, 123, 124])
        extracted, has_new = build_extraction_history(conversation, 0)
        self.assertTrue(has_new)
        self.assertIn(PLACEHOLDER, extracted[1].content)
        stored = {"sender_user_id": 99, "sender": "Rachel", "content": PLACEHOLDER,
                  "telegram_message_id": 123, "reason": POLL_REASON}
        with patch.object(telegram, "get_history", AsyncMock(return_value=[stored])):
            rows, context, _ = await telegram._snapshot_context(42, 99)
        self.assertEqual(context[0]["content"], PLACEHOLDER)
        self.assertEqual(rows[0].reason, POLL_REASON)
        self.assertIn(PLACEHOLDER, llm._build_history_messages(context, divider_text=None)[0].content)

    async def test_outreach_sends_poll_and_text_and_updates_normal_timers(self):
        await telegram.send_proactive_message(42, "Send a language poll", 7)
        self.assert_poll_and_text_buffer()
        self.assertIn(42, telegram.last_message_time)
        self.assertIn(42, telegram.flush_tasks)
        self.assertNotIn(42, telegram.responded_watermark)

    async def test_outreach_uses_normal_parse_fallback_after_poll(self):
        self.responder.outputs = iter([OutputParserException("Empty content", llm_output='{"content":""}')])
        await telegram.send_proactive_message(42, "Send a language poll", 7)
        self.send.assert_awaited_once()
        self.text.assert_awaited_once_with(42, "Hey, how have you been?")
        self.assertEqual(telegram.current_messages_buffer[42][1].content, PLACEHOLDER)

    async def test_flush_during_send_persists_poll_and_text_directly(self):
        async def send(*args):
            self.assertTrue(telegram.reply_locks[42].locked())
            await telegram._flush_chat(42)
            return SimpleNamespace(id=123)
        self.send.side_effect = send
        await self.reactive()
        self.assertNotIn(42, telegram.current_messages_buffer)
        self.assertEqual(self.persist.await_args_list[-2].kwargs, {
            "chat_ids": [42], "sender_user_ids": [99], "contents": [PLACEHOLDER],
            "telegram_message_ids": [123], "reasons": [POLL_REASON],
        })
        self.assertEqual(self.persist.await_args_list[-1].kwargs["contents"], [TEXT])
        self.text.assert_awaited_once_with(42, TEXT)

    async def test_whole_graph_retry_reuses_poll_receipt_and_still_generates_text(self):
        self.fetcher.outputs = iter([calls(POLL), finish(), calls(POLL), finish()])
        self.responder.outputs = iter([RuntimeError("Retry after poll delivery"), reply(content=TEXT)])
        await self.reactive()
        self.assert_poll_and_text_buffer(responder_runs=2)
        self.assertEqual(len(self.fetcher.messages), 4)
        self.assertIn(PLACEHOLDER, self.responder.messages[-1][0].content)

    async def test_recording_retry_after_append_does_not_duplicate_buffer_entry(self):
        append = telegram._append_or_persist_bot_message
        attempts = 0
        async def append_then_fail(*args):
            nonlocal attempts
            await append(*args)
            if args[2] == 123:
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("Failure after append")
        self.fetcher.outputs = iter([calls(POLL), calls(POLL), finish()])
        with patch.object(telegram, "_append_or_persist_bot_message", append_then_fail):
            await self.reactive()
        self.assert_poll_and_text_buffer()
        self.assertEqual(attempts, 2)

    async def test_whole_graph_retry_does_not_resend_uncertain_poll(self):
        self.fetcher.outputs = iter([calls(POLL), finish(), calls(POLL), finish()])
        self.responder.outputs = iter([RuntimeError("Retry graph after uncertain send"),
                                      reply(content="I could not confirm the poll was sent.")])
        self.send.side_effect = TimeoutError("Telegram request timed out")
        await self.reactive()
        self.send.assert_awaited_once()
        self.text.assert_awaited_once_with(42, "I could not confirm the poll was sent.")
        self.assertEqual(len(self.fetcher.messages), 4)
        self.assertEqual(len(self.responder.messages), 2)
        self.assertEqual(len(telegram.current_messages_buffer[42]), 2)
        self.assertNotIn("[Poll sent]", telegram.current_messages_buffer[42][-1].content)
        self.assertIn("uncertain", "\n".join(message.content for message in self.responder.messages[-1]))

    async def test_recording_failure_recovery_keeps_normal_summary_and_mood(self):
        append = telegram._append_or_persist_bot_message
        attempts = 0
        async def unavailable_then_record(*args):
            nonlocal attempts
            if args[2] == 123:
                attempts += 1
                if attempts <= 2:
                    raise RuntimeError("History temporarily unavailable")
            await append(*args)
        self.fetcher.outputs = iter([calls(POLL), calls(POLL), calls(POLL), finish()])
        with patch.dict(llm._chat_mood, {42: "default"}, clear=True), patch.object(
            telegram, "_append_or_persist_bot_message", unavailable_then_record
        ):
            await self.reactive()
            self.assertEqual(telegram.pending_summaries[42], "updated")
            self.assertEqual(llm.get_chat_mood(42), "excited")
        self.assertEqual(attempts, 3)
        self.assert_poll_and_text_buffer()

    async def test_recording_failure_recovers_without_model_repeating_the_poll(self):
        append = telegram._append_or_persist_bot_message
        for proactive in (False, True):
            with self.subTest(proactive=proactive):
                telegram.current_messages_buffer[42] = [lifecycle.TelegramLifecycleTests.message(self, 1)]
                telegram.responded_watermark.clear()
                self.fetcher.outputs = iter([calls(POLL), finish()])
                self.responder.outputs = iter([reply(content=TEXT)])
                self.send.reset_mock()
                self.text.reset_mock()
                self.fetcher.messages.clear()
                self.responder.messages.clear()
                attempts = 0
                async def fail_poll_once(*args):
                    nonlocal attempts
                    if args[2] == 123:
                        attempts += 1
                        if attempts == 1:
                            raise RuntimeError("Temporary history failure")
                    await append(*args)
                with patch.object(telegram, "_append_or_persist_bot_message", fail_poll_once):
                    if proactive:
                        await telegram.send_proactive_message(42, "Send a poll", 7)
                    else:
                        await self.reactive()
                self.assert_poll_and_text_buffer()
                self.assertEqual(attempts, 2)
                self.assertEqual(len(self.fetcher.messages), 2)

    async def test_confirmed_poll_recording_cancelled_by_pass_deadline_is_drained(self):
        append = telegram._append_or_persist_bot_message
        attempts = 0
        async def slow_poll_once(*args):
            nonlocal attempts
            if args[2] == 123:
                attempts += 1
                if attempts == 1:
                    await asyncio.sleep(10)
            await append(*args)
        with patch.object(llm, "CONTEXT_FETCHER_TIMEOUT", 0.01), patch.object(
            telegram, "_append_or_persist_bot_message", slow_poll_once
        ):
            await self.reactive()
        self.assert_poll_and_text_buffer()
        self.assertEqual(attempts, 2)
