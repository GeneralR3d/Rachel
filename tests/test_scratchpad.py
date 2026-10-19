"""Scratchpad isolation, atomic mutations, and runtime-only ownership."""

import unittest
import asyncio
from pydantic import ValidationError

from app.services.scratchpad import ScratchpadScope, scratchpads
from app.services.scratchpad_tools import (
    SCRATCHPAD_TOOLS, append_scratchpad, delete_scratchpad, get_scratchpad,
)


class ScratchpadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        scratchpads.clear_all()
        self.addCleanup(scratchpads.clear_all)
        self.a = scratchpads.activate(42, "session-a")
        self.b = scratchpads.activate(43, "session-b")
        self.config = {"configurable": {"current_chat_id": 42, "scratchpad_scope": self.a}}

    async def append(self, **kwargs):
        return await append_scratchpad.ainvoke(kwargs, config=self.config)

    async def read(self, note_ids=None):
        return await get_scratchpad.ainvoke({"note_ids": note_ids}, config=self.config)

    async def test_cross_turn_retention_foreign_ids_and_clear_are_chat_local(self):
        saved = await self.append(title="Idea", content="Remember the tea")
        note_id = saved["note_id"]
        self.assertNotIn("Remember the tea", str(saved))
        self.assertEqual((await self.read())["notes"][0]["content"], "Remember the tea")
        other = scratchpads.append(self.b, "Other", "private")
        self.assertEqual((await self.read([other["note_id"]]))["notes"], [])
        deleted = await delete_scratchpad.ainvoke({"note_ids": [other["note_id"]]}, config=self.config)
        self.assertEqual(deleted["deleted_note_ids"], [])
        self.assertEqual((await self.read())["notes"][0]["id"], note_id)
        await delete_scratchpad.ainvoke({"note_ids": None}, config=self.config)
        self.assertEqual((await self.read())["notes"], [])
        self.assertEqual(scratchpads.read(self.b)["notes"][0]["content"], "private")

    async def test_exact_capacity_unicode_overflow_and_reclaimed_capacity(self):
        saved = await self.append(title="茶", content="🙂" * 19999)
        self.assertEqual(saved["used_characters"], 20000)
        self.assertEqual(saved["remaining_characters"], 0)
        with self.assertRaisesRegex(ValueError, "remaining capacity: 0"):
            await self.append(title="x", content="y")
        self.assertEqual(len((await self.read())["notes"]), 1)
        await delete_scratchpad.ainvoke({"note_ids": [saved["note_id"]]}, config=self.config)
        self.assertEqual((await self.append(title="x", content="y"))["remaining_characters"], 19998)

    async def test_invalid_arguments_never_mutate_storage(self):
        invalid = [
            {"title": "", "content": "hi"}, {"title": "hi", "content": " "},
            {"title": "hi"}, {"title": "hi", "content": "x", "from_url": "https://example.com/"},
            {"title": 2, "content": "x"}, {"title": "hi", "content": True},
        ]
        for args in invalid:
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.append(**args)
        for args in ({}, {"note_ids": []}, {"note_ids": "all"}, {"note_ids": [""]}, {"note_ids": [1]}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await delete_scratchpad.ainvoke(args, config=self.config)
        self.assertEqual((await self.read())["notes"], [])

    async def test_competing_appends_cannot_exceed_capacity(self):
        results = await asyncio.gather(self.append(title="a", content="x" * 12000),
                                       self.append(title="b", content="y" * 12000), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        self.assertEqual((await self.read())["used_characters"], 12001)
        self.assertEqual((await self.read())["note_count"], 1)

    async def test_url_overflow_is_atomic_and_read_results_are_detached(self):
        self.config["configurable"]["scratchpad_url_results"] = {"https://example.com/": "x" * 19999}
        with self.assertRaisesRegex(ValueError, "remaining capacity: 20000"):
            await self.append(title="Page", from_url="https://example.com/")
        self.assertEqual((await self.read())["note_count"], 0)
        saved = await self.append(title="a", content="b")
        result = await self.read()
        result["notes"][0]["content"] = "tampered"
        self.assertEqual((await self.read([saved["note_id"]]))["notes"][0]["content"], "b")

    async def test_all_tools_reject_missing_invalid_mismatched_and_forged_scope(self):
        for tool, args in ((get_scratchpad, {}), (append_scratchpad, {"title": "t", "content": "c"}),
                           (delete_scratchpad, {"note_ids": None})):
            properties = tool.tool_call_schema.model_json_schema()["properties"]
            for hidden in ("chat_id", "current_chat_id", "session_id", "scratchpad_scope", "scratchpad_url_results", "config", "configurable"):
                self.assertNotIn(hidden, properties)
                with self.subTest(tool=tool.name, hidden=hidden), self.assertRaises(ValidationError):
                    await tool.ainvoke({**args, hidden: "forged"}, config=self.config)
            for config in ({}, {"configurable": {}}, {"configurable": {"current_chat_id": 42}},
                           {"configurable": {"current_chat_id": 43, "scratchpad_scope": self.a}},
                           {"configurable": {"current_chat_id": True, "scratchpad_scope": self.a}},
                           {"configurable": {"current_chat_id": 42, "scratchpad_scope": {"chat_id": 42, "session_id": "session-a"}}}):
                with self.subTest(tool=tool.name, config=config), self.assertRaisesRegex(ValueError, "scope"):
                    await tool.ainvoke(args, config=config)

    async def test_expired_scope_cannot_recreate_or_modify_replacement_session(self):
        await self.append(title="old", content="body")
        scratchpads.invalidate(42)
        new = scratchpads.activate(42, "new")
        scratchpads.append(new, "new", "safe")
        for tool, args in ((get_scratchpad, {}), (append_scratchpad, {"title": "t", "content": "c"}),
                           (delete_scratchpad, {"note_ids": None})):
            with self.subTest(tool=tool.name), self.assertRaisesRegex(ValueError, "scope"):
                await tool.ainvoke(args, config=self.config)
        self.assertEqual(scratchpads.read(new)["notes"][0]["content"], "safe")

    async def test_url_copy_uses_only_injected_results_and_counts_source(self):
        self.config["configurable"]["scratchpad_url_results"] = {"https://example.com/": "raw 茶"}
        result = await self.append(title="Link", from_url="https://example.com/")
        self.assertEqual(result["used_characters"], 29)
        self.assertNotIn("raw 茶", str(result))
        note = (await self.read())["notes"][0]
        self.assertEqual((note["content"], note["source_url"]), ("raw 茶", "https://example.com/"))
        with self.assertRaisesRegex(ValueError, "current invocation"):
            await self.append(title="foreign", from_url="https://other.example/")
        self.assertEqual((await self.read())["note_count"], 1)

    def test_invalid_scope_values_are_rejected(self):
        for chat, session in ((True, "x"), (0, "x"), ("42", "x"), (42, ""), (42, None)):
            with self.subTest(chat=chat, session=session), self.assertRaises(ValueError):
                ScratchpadScope(chat, session)
