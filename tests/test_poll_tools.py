"""Poll validation, Telegram serialization and per-turn delivery safety."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon.errors import RPCError
from telethon.extensions.binaryreader import BinaryReader
from telethon.tl.types import InputMediaPoll

from app.services import poll_tools as polls


class PollToolTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, **kwargs):
        self.client = SimpleNamespace(send_file=AsyncMock(return_value=SimpleNamespace(id=123)))
        self.record = AsyncMock()
        return polls.PollDeliveryRuntime(chat_id=42, sender_id=99, client=self.client,
                                         record_message=self.record, **kwargs)

    async def invoke(self, runtime, **args):
        return await polls.send_poll.ainvoke(args or {"question": " Pick\na language? ", "options": [" Python ", "Rust\nGo"]},
            config={"configurable": {"current_chat_id": 42, "poll_delivery": runtime}})

    async def test_schema_normalization_serialization_and_history_receipt(self):
        schema = polls.send_poll.get_input_schema().model_json_schema()
        self.assertEqual(set(schema["properties"]), {"question", "options"})
        self.assertFalse(schema["additionalProperties"])
        runtime = self.runtime()
        result = await self.invoke(runtime)
        self.assertEqual(result, "[Poll sent]\nQuestion: Pick\na language?\nOptions:\n1. Python\n2. Rust\nGo")
        media = self.client.send_file.await_args.args[1]
        self.assertIsInstance(media, InputMediaPoll)
        with BinaryReader(bytes(media)) as reader:
            wire = reader.tgread_object()
        self.assertEqual(wire.poll.question.text, "Pick\na language?")
        self.assertEqual([a.text.text for a in wire.poll.answers], ["Python", "Rust\nGo"])
        self.assertTrue(wire.poll.public_voters)
        self.assertTrue(wire.poll.multiple_choice)
        self.assertFalse(wire.poll.quiz)
        self.assertFalse(wire.poll.closed)
        self.assertEqual(wire.poll.hash, 0)
        self.assertLess(wire.poll.id, 2**60)
        self.assertEqual(len({a.option for a in wire.poll.answers}), 2)
        self.record.assert_awaited_once_with(42, 99, 123,
            "[Poll sent]\nQuestion: Pick\na language?\nOptions:\n1. Python\n2. Rust\nGo",
            "Sent a poll requested by the context fetcher.")
        self.assertEqual(len(runtime.receipts), 1)

    async def test_invalid_inputs_never_send(self):
        runtime = self.runtime()
        invalid = [
            {"question": " ", "options": ["a", "b"]},
            {"question": 3, "options": ["a", "b"]},
            {"question": "a" * 301, "options": ["a", "b"]},
            {"question": "q", "options": ["a"]},
            {"question": "q", "options": [str(i) for i in range(11)]},
            {"question": "q", "options": ["a", " "]},
            {"question": "q", "options": ["a", 1]},
            {"question": "q", "options": ["a", " A "]},
            {"question": "q", "options": ["a", "b" * 101]},
            {"question": "q", "options": ("a", "b")},
        ]
        for extra in ("chat_id", "public_voters", "multiple_choice", "quiz", "closed"):
            invalid.append({"question": "q", "options": ["a", "b"], extra: False})
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.invoke(runtime, **args)
        self.client.send_file.assert_not_awaited()
        self.record.assert_not_awaited()

    async def test_inclusive_boundaries(self):
        await self.invoke(self.runtime(), question="q" * 300, options=[str(i) * 100 for i in range(10)])

    async def test_confirmed_receipt_reused_after_recording_failure(self):
        runtime = self.runtime()
        self.record.side_effect = [RuntimeError("database unavailable"), None]
        with self.assertRaisesRegex(RuntimeError, "database unavailable"):
            await self.invoke(runtime)
        self.assertTrue(runtime.receipts)
        await self.invoke(runtime)
        await self.invoke(runtime)
        self.client.send_file.assert_awaited_once()
        self.assertEqual(self.record.await_count, 2)

    async def test_unconfirmed_or_timed_out_send_is_uncertain_and_never_resent(self):
        for mode in ("none", "timeout", "cancel"):
            with self.subTest(mode=mode):
                runtime = self.runtime()
                if mode == "none":
                    self.client.send_file.return_value = None
                else:
                    entered = asyncio.Event()
                    async def hang(*args, **kwargs):
                        entered.set()
                        await asyncio.sleep(10)
                    self.client.send_file.side_effect = hang
                with patch.object(polls, "POLL_SEND_TIMEOUT", 0.01):
                    if mode == "cancel":
                        task = asyncio.create_task(self.invoke(runtime))
                        await entered.wait()
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        with self.assertRaisesRegex((RuntimeError, TimeoutError), "uncertain"):
                            await self.invoke(runtime)
                with self.assertRaisesRegex(RuntimeError, "uncertain"):
                    await self.invoke(runtime)
                self.assertFalse(runtime.receipts)
                self.record.assert_not_awaited()
                self.assertLessEqual(self.client.send_file.await_count, 1)

    async def test_telegram_rejection_is_not_a_confirmed_delivery(self):
        runtime = self.runtime()
        self.client.send_file.side_effect = RPCError(None, "POLL_INVALID", 400)
        with self.assertRaisesRegex(RPCError, "POLL_INVALID"):
            await self.invoke(runtime)
        self.assertFalse(runtime.receipts)
        self.record.assert_not_awaited()

    async def test_application_runtime_is_required_and_chat_scoped(self):
        runtime = self.runtime()
        for config in ({}, {"configurable": {"current_chat_id": 43, "poll_delivery": runtime}}):
            with self.assertRaises(ValueError):
                await polls.send_poll.ainvoke({"question": "q", "options": ["a", "b"]}, config=config)
        self.client.send_file.assert_not_awaited()

    async def test_network_failure_is_reported_as_uncertain(self):
        runtime = self.runtime()
        self.client.send_file.side_effect = ConnectionError("Connection closed")
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            await self.invoke(runtime)
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            await self.invoke(runtime)
        self.client.send_file.assert_awaited_once()
        self.record.assert_not_awaited()

    async def test_server_rpc_errors_are_uncertain_and_not_resent(self):
        from telethon.errors import ServerError, TimedOutError
        for error in (ServerError(None, "INTERNAL", 500), TimedOutError(None, "TIMEOUT", -503),
                      RPCError(None, "UNKNOWN", 999)):
            with self.subTest(error=error):
                runtime = self.runtime()
                self.client.send_file.side_effect = error
                with self.assertRaisesRegex(RuntimeError, "uncertain"):
                    await self.invoke(runtime)
                with self.assertRaisesRegex(RuntimeError, "uncertain"):
                    await self.invoke(runtime)
                self.client.send_file.assert_awaited_once()
                self.record.assert_not_awaited()
