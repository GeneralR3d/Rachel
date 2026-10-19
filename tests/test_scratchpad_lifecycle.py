"""Scratchpad ownership follows locked buffer snapshots and successful cleanup."""

import asyncio
import re
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import events

from app.services.scratchpad import scratchpads
from app.telegram import client as telegram
from app.telegram import bot
from app.routers import admin
from app import repository
from tests import test_telegram_lifecycle


class ScratchpadLifecycleTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_telegram_lifecycle.TelegramLifecycleTests.setUp
    message = test_telegram_lifecycle.TelegramLifecycleTests.message

    async def asyncSetUp(self):
        scratchpads.clear_all()
        self.addCleanup(scratchpads.clear_all)

    async def seed(self, chat=42):
        await telegram._append_to_buffer(chat, self.message(1), 99)
        snapshot = await telegram._snapshot_context(chat, 99)
        self.assertEqual(len(snapshot), 4)
        scope = snapshot[3]
        scratchpads.append(scope, "idea", "private notes")
        return scope

    async def test_snapshot_reuses_scope_and_successful_flush_invalidates_it(self):
        scope = await self.seed()
        self.assertEqual((await telegram._snapshot_context(42, 99))[3], scope)
        conversation, _ = await telegram._flush_chat(42, capture=True)
        self.assertNotIn("private notes", str(conversation))
        with self.assertRaisesRegex(ValueError, "scope"):
            scratchpads.read(scope)
        replacement = (await telegram._snapshot_context(42, 99))[3]
        self.assertNotEqual(scope, replacement)
        self.assertEqual(scratchpads.read(replacement)["notes"], [])

    async def test_reactive_and_proactive_generation_receive_the_same_captured_scope(self):
        scope = await self.seed()

        @asynccontextmanager
        async def typing(*args):
            yield

        async def reactive(**kwargs):
            scratchpads.append(kwargs["scratchpad_scope"], "reactive", "shared idea")
            return "Hi", "greeting", None, 0

        async def proactive(**kwargs):
            notes = scratchpads.read(kwargs["scratchpad_scope"])["notes"]
            self.assertEqual(notes[-1]["content"], "shared idea")
            return "Checking in", "follow up", 0

        with patch.object(telegram.client, "is_connected", return_value=True), patch.object(
            telegram.client, "action", side_effect=typing
        ), patch.object(telegram, "get_response", reactive), patch.object(
            telegram, "get_proactive_response", proactive
        ), patch.object(telegram, "_send_paragraphs", AsyncMock(side_effect=[10, 11])), patch.object(
            telegram, "_reset_flush_timer"
        ):
            await telegram._reply(SimpleNamespace(chat_id=42, is_private=True, mentioned=True, chat=None))
            await telegram.send_proactive_message(42, "Check in", 1)
        self.assertEqual(scratchpads.read(scope)["note_count"], 2)

    async def test_failed_history_or_summary_flush_preserves_notes_and_summary(self):
        scope = await self.seed()
        telegram.pending_summaries[42] = "keep me"
        for name in ("add_history_batch", "set_summary"):
            with self.subTest(name=name), patch.object(telegram, name, AsyncMock(side_effect=RuntimeError("offline"))):
                with self.assertRaises(RuntimeError):
                    await telegram._flush_chat(42)
            self.assertEqual(scratchpads.read(scope)["notes"][0]["content"], "private notes")
            self.assertEqual(telegram.pending_summaries[42], "keep me")
            self.assertEqual((await telegram._snapshot_context(42, 99))[3], scope)

    async def test_summary_arriving_during_persistence_is_kept_for_the_next_flush(self):
        await self.seed()
        telegram.pending_summaries[42] = "original"
        saved = []

        async def persist(chat_id, summary, mood):
            saved.append(summary)
            telegram.pending_summaries[42] = "newer summary"

        with patch.object(telegram, "set_summary", persist):
            await telegram._flush_chat(42)
        self.assertEqual(saved, ["original"])
        self.assertEqual(telegram.pending_summaries.get(42), "newer summary")

    async def test_flush_reseed_race_rejects_stale_mutations(self):
        scope = await self.seed()
        entered, release = asyncio.Event(), asyncio.Event()

        async def persist(**kwargs):
            entered.set()
            await release.wait()

        with patch.object(telegram, "add_history_batch", persist):
            flush = asyncio.create_task(telegram._flush_chat(42))
            await entered.wait()
            snapshot = asyncio.create_task(telegram._snapshot_context(42, 99))
            scratchpads.append(scope, "while saving", "allowed until success")
            release.set()
            await flush
            new = (await snapshot)[3]
        with self.assertRaisesRegex(ValueError, "scope"):
            scratchpads.append(scope, "stale", "no")
        self.assertEqual(scratchpads.read(new)["notes"], [])

    async def test_finalization_clears_notes_for_inactivity_and_size_without_extracting_them(self):
        for delay in (0, telegram.CHAT_BLACKOUT_TIME):
            scope = await self.seed()
            captured = []

            async def memories(conversation, *args, **kwargs):
                captured.extend(conversation)

            with patch.object(telegram.asyncio, "sleep", AsyncMock()), patch.object(
                telegram, "get_last_processed_message_id", AsyncMock(return_value=0)
            ), patch.object(telegram, "set_last_processed_message_id", AsyncMock()), patch.object(
                telegram, "update_memories", memories
            ):
                await telegram.finalize_conversation(42, delay)
            await asyncio.sleep(0)
            self.assertTrue(captured)
            self.assertNotIn("private notes", str(captured))
            with self.assertRaisesRegex(ValueError, "scope"):
                scratchpads.read(scope)

    async def test_private_reset_invalidates_only_its_chat(self):
        scope, other = await self.seed(), await self.seed(43)
        event = SimpleNamespace(chat_id=42, is_group=False, respond=AsyncMock())
        with patch.object(telegram, "clear_history", AsyncMock()), patch.object(telegram, "delete_summary", AsyncMock()):
            with self.assertRaises(events.StopPropagation):
                await telegram.on_clear_history(event)
        with self.assertRaisesRegex(ValueError, "scope"):
            scratchpads.read(scope)
        self.assertEqual(scratchpads.read(other)["note_count"], 1)

    async def test_admin_and_bot_history_resets_invalidate_the_buffer_session(self):
        for surface in ("http", "bot"):
            with self.subTest(surface=surface):
                scope = await self.seed()
                with patch.object(repository, "clear_history", AsyncMock()), patch.object(telegram, "clear_history", AsyncMock()):
                    if surface == "http":
                        await admin.delete_chat_history(42)
                    else:
                        event = SimpleNamespace(pattern_match=re.match(r"(/clear_history) (-?\d+)", "/clear_history 42"), reply=AsyncMock())
                        await bot.on_clear_history(event)
                with self.assertRaisesRegex(ValueError, "scope"):
                    scratchpads.read(scope)
                self.assertNotEqual((await telegram._snapshot_context(42, 99))[3], scope)

    async def test_shutdown_clears_successful_sessions_but_preserves_failed_flush(self):
        scope, other = await self.seed(), await self.seed(43)

        async def persist(**kwargs):
            if kwargs["chat_ids"][0] == 43:
                raise RuntimeError("offline")

        with patch.object(telegram, "add_history_batch", persist):
            await telegram.flush_all_buffers()
        with self.assertRaisesRegex(ValueError, "scope"):
            scratchpads.read(scope)
        self.assertEqual(scratchpads.read(other)["note_count"], 1)
