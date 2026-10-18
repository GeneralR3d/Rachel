"""Contracts for chat-scoped proactive tools at the persistence boundary."""

import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import json
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pydantic import ValidationError

from app import repository
from app.services import proactive, proactive_tools
from app.telegram import client as telegram


def schedule_row(**changes):
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    return {
        "id": 1, "name": "Exam follow-up", "chat_id": -123,
        "cron_expression": "0 10 * * *", "timezone": "Asia/Singapore",
        "intent": "Ask Jamie how Monday's exam went.", "enabled": True,
        "repeat_times": 1, "repeat_completed": 0,
        "last_run_at": None, "last_status": None, "last_error": None,
        "created_at": now, "updated_at": now, **changes,
    }


def create_args(**changes):
    return {
        "name": "Exam follow-up", "cron_expression": "0 10 * * *",
        "intent": "Ask Jamie how Monday's exam went.", "repeat_times": 1,
        **changes,
    }


class ProactiveToolTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
        self.scheduler.start(paused=True)
        self.addCleanup(self.scheduler.shutdown, wait=False)
        self.stack.enter_context(patch.object(proactive, "_scheduler", self.scheduler))
        self.stack.enter_context(patch.object(telegram.client, "get_entity", AsyncMock()))
        self.config = {"configurable": {"current_chat_id": -123}}

    async def test_create_injects_scope_normalizes_and_registers_real_job(self):
        async def save(data):
            self.assertEqual(data["chat_id"], -123)
            self.assertIs(data["enabled"], True)
            self.assertEqual(data["name"], "Exam follow-up")
            self.assertEqual(data["cron_expression"], "0 10 * * *")
            return schedule_row(**data)

        with patch.object(repository, "create_proactive_schedule", side_effect=save):
            result = await proactive_tools.create_proactive_schedule.ainvoke(
                create_args(name="  Exam follow-up  ", cron_expression=" 0  10 * * * "),
                config=self.config,
            )
        self.assertEqual(result["chat_id"], -123)
        self.assertEqual(result["repeat_times"], 1)
        self.assertEqual(result["repeat_completed"], 0)
        self.assertIsNotNone(result["next_run_at"])
        self.assertIsNotNone(proactive.next_run_at(1))
        self.assertIsInstance(json.loads(json.dumps(result))["created_at"], str)

    async def test_update_is_scoped_preserves_counter_and_replaces_job(self):
        proactive.sync_schedule_job(schedule_row())

        async def save(schedule_id, data, *, chat_id):
            self.assertEqual((schedule_id, chat_id), (1, -123))
            self.assertNotIn("repeat_completed", data)
            return schedule_row(**data, repeat_completed=2)

        with patch.object(repository, "update_proactive_schedule", side_effect=save):
            result = await proactive_tools.update_proactive_schedule.ainvoke(
                {**create_args(repeat_times=5, cron_expression="0 11 * * *"), "schedule_id": 1},
                config=self.config,
            )
        self.assertEqual(result["repeat_completed"], 2)
        self.assertEqual(result["repeat_times"], 5)
        self.assertIs(result["enabled"], True)
        self.assertIn("hour='11'", str(self.scheduler.get_job("proactive-schedule-1").trigger))

    async def test_list_requests_only_current_chat_and_serializes_disabled_rows(self):
        async def read(*, chat_id):
            self.assertEqual(chat_id, -123)
            return [schedule_row(enabled=False)]

        with patch.object(repository, "get_proactive_schedules", side_effect=read):
            rows = await proactive_tools.list_proactive_schedules.ainvoke({}, config=self.config)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["enabled"])
        self.assertIsNone(rows[0]["next_run_at"])
        json.dumps(rows)

    async def test_delete_is_scoped_and_removes_job(self):
        proactive.sync_schedule_job(schedule_row())

        async def remove(schedule_id, *, chat_id):
            self.assertEqual((schedule_id, chat_id), (1, -123))
            return True

        with patch.object(repository, "delete_proactive_schedule", side_effect=remove):
            result = await proactive_tools.delete_proactive_schedule.ainvoke(
                {"schedule_id": 1}, config=self.config,
            )
        self.assertEqual(result, {"deleted": True, "schedule_id": 1})
        self.assertIsNone(proactive.next_run_at(1))

    async def test_missing_or_foreign_ids_are_errors_without_job_changes(self):
        proactive.sync_schedule_job(schedule_row())
        with patch.object(repository, "update_proactive_schedule", AsyncMock(return_value=None)), \
                patch.object(repository, "delete_proactive_schedule", AsyncMock(return_value=False)):
            for tool, args in (
                (proactive_tools.update_proactive_schedule, {**create_args(), "schedule_id": 1}),
                (proactive_tools.delete_proactive_schedule, {"schedule_id": 1}),
                (proactive_tools.set_proactive_schedule_enabled, {"schedule_id": 1, "enabled": False}),
            ):
                with self.subTest(tool=tool.name), self.assertRaisesRegex(ValueError, "current chat"):
                    await tool.ainvoke(args, config=self.config)
        self.assertIsNotNone(proactive.next_run_at(1))

    async def test_missing_scope_fails_for_every_tool(self):
        for tool, args in (
            (proactive_tools.create_proactive_schedule, create_args()),
            (proactive_tools.list_proactive_schedules, {}),
            (proactive_tools.update_proactive_schedule, {**create_args(), "schedule_id": 1}),
            (proactive_tools.delete_proactive_schedule, {"schedule_id": 1}),
            (proactive_tools.set_proactive_schedule_enabled, {"schedule_id": 1, "enabled": False}),
        ):
            for chat_id in (None, True, "-123", 0):
                with self.subTest(tool=tool.name, chat_id=chat_id), self.assertRaisesRegex(ValueError, "chat"):
                    await tool.ainvoke(args, config={"configurable": {"current_chat_id": chat_id}})

    async def test_forged_scope_and_enabled_arguments_are_rejected(self):
        for extra in ({"chat_id": 456}, {"enabled": False}, {"config": self.config}):
            with self.subTest(extra=extra), self.assertRaises(ValidationError):
                await proactive_tools.create_proactive_schedule.ainvoke(
                    create_args(**extra), config=self.config,
                )

    async def test_repeat_limit_is_required_strict_positive_or_null(self):
        for value in (0, -1, True, 1.5, "3", 9223372036854775808):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                await proactive_tools.create_proactive_schedule.ainvoke(
                    create_args(repeat_times=value), config=self.config,
                )
        args = create_args()
        del args["repeat_times"]
        with self.assertRaises(ValidationError):
            await proactive_tools.create_proactive_schedule.ainvoke(args, config=self.config)
        with patch.object(repository, "create_proactive_schedule", AsyncMock(return_value=schedule_row(repeat_times=None))):
            result = await proactive_tools.create_proactive_schedule.ainvoke(
                create_args(repeat_times=None), config=self.config,
            )
        self.assertIsNone(result["repeat_times"])

    async def test_invalid_schedule_details_never_persist(self):
        with patch.object(repository, "create_proactive_schedule", AsyncMock()) as save:
            for changes in ({"name": " "}, {"intent": " "}, {"cron_expression": "bad"}, {"timezone": "invalid/zone"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    await proactive_tools.create_proactive_schedule.ainvoke(create_args(**changes), config=self.config)
            with patch.object(telegram.client, "get_entity", AsyncMock(side_effect=ValueError("inaccessible"))):
                with self.assertRaisesRegex(ValueError, "cannot access"):
                    await proactive_tools.create_proactive_schedule.ainvoke(create_args(), config=self.config)
        save.assert_not_awaited()

    async def test_model_schema_hides_runtime_and_read_only_fields(self):
        for tool in proactive_tools.PROACTIVE_TOOLS:
            properties = tool.tool_call_schema.model_json_schema().get("properties", {})
            for hidden in ("chat_id", "config", "repeat_completed"):
                self.assertNotIn(hidden, properties)
            if tool.name != "set_proactive_schedule_enabled":
                self.assertNotIn("enabled", properties)

    async def test_toggle_rejects_invalid_or_extra_arguments_before_persistence(self):
        invalid = [
            {"schedule_id": value, "enabled": False}
            for value in (0, -1, True, 1.5, "1", None)
        ] + [
            {"schedule_id": 1, "enabled": value}
            for value in (0, 1, "false", "true", None)
        ] + [
            {"schedule_id": 1}, {"enabled": False},
            {"schedule_id": 1, "enabled": False, "chat_id": -456},
            {"schedule_id": 1, "enabled": False, "config": self.config},
            {"schedule_id": 1, "enabled": False, "repeat_completed": 0},
        ]
        with patch.object(repository, "update_proactive_schedule", AsyncMock()) as save:
            for args in invalid:
                with self.subTest(args=args), self.assertRaises(ValidationError):
                    await proactive_tools.set_proactive_schedule_enabled.ainvoke(args, config=self.config)
        save.assert_not_awaited()
