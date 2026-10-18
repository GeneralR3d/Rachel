"""Exercise repository SQL against SQLite, without a network database."""

import asyncio
from contextlib import asynccontextmanager
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import repository
from app.models import ProactiveSchedule
from app.services import proactive, proactive_tools
from app.telegram import client as telegram


class AsyncSessionAdapter:
    """Run production SQL locally; adapt only the async session boundary."""

    def __init__(self, session):
        self.session = session

    async def execute(self, statement):
        return self.session.execute(statement)

    def add(self, row):
        self.session.add(row)

    async def flush(self):
        self.session.flush()

    async def refresh(self, row):
        self.session.refresh(row)


class ScopedRepositoryTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine("sqlite://")
        self.addCleanup(self.engine.dispose)
        ProactiveSchedule.__table__.create(self.engine)
        with Session(self.engine) as session, session.begin():
            for schedule_id, chat_id, enabled in ((1, -123, True), (2, -456, True), (3, -123, False)):
                session.add(ProactiveSchedule(
                    id=schedule_id, name=f"Schedule {schedule_id}", chat_id=chat_id,
                    cron_expression="0 10 * * *", intent="Check in", enabled=enabled,
                    repeat_times=4, repeat_completed=2,
                ))

        @asynccontextmanager
        async def scope():
            with Session(self.engine, expire_on_commit=False) as session:
                with session.begin():
                    yield AsyncSessionAdapter(session)
                if self.after_commit is not None:
                    await self.after_commit()

        self.after_commit = None
        self.patch = patch.object(repository, "session_scope", scope)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_scoped_reads_include_disabled_and_global_reads_remain_global(self):
        self.assertEqual([r["id"] for r in await repository.get_proactive_schedules(chat_id=-123)], [1, 3])
        self.assertEqual([r["id"] for r in await repository.get_proactive_schedules(chat_id=-123, enabled_only=True)], [1])
        self.assertEqual([r["id"] for r in await repository.get_proactive_schedules()], [1, 2, 3])
        self.assertIsNone(await repository.get_proactive_schedule(2, chat_id=-123))
        self.assertEqual((await repository.get_proactive_schedule(2))["chat_id"], -456)

    async def test_update_and_delete_cannot_touch_foreign_rows(self):
        self.assertIsNone(await repository.update_proactive_schedule(2, {"name": "Wrong"}, chat_id=-123))
        self.assertFalse(await repository.delete_proactive_schedule(2, chat_id=-123))
        self.assertEqual((await repository.get_proactive_schedule(2))["name"], "Schedule 2")
        updated = await repository.update_proactive_schedule(1, {"name": "Edited"}, chat_id=-123)
        self.assertEqual((updated["name"], updated["repeat_completed"], updated["repeat_times"]), ("Edited", 2, 4))
        self.assertTrue(await repository.delete_proactive_schedule(1, chat_id=-123))
        self.assertIsNone(await repository.get_proactive_schedule(1, chat_id=-123))

    async def test_global_edits_remain_available_and_scoped_edits_cannot_move_chats(self):
        moved = await repository.update_proactive_schedule(2, {"chat_id": -789})
        self.assertEqual(moved["chat_id"], -789)
        with self.assertRaises(ValueError):
            await repository.update_proactive_schedule(1, {"chat_id": -789}, chat_id=-123)
        self.assertEqual((await repository.get_proactive_schedule(1))["chat_id"], -123)
        self.assertTrue(await repository.delete_proactive_schedule(2))

    async def test_toggle_preserves_details_and_synchronizes_repeated_requests(self):
        scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
        scheduler.start(paused=True)
        self.addCleanup(scheduler.shutdown, wait=False)
        before = await repository.get_proactive_schedule(1)
        with patch.object(proactive, "_scheduler", scheduler):
            proactive.sync_schedule_job(before)
            for enabled in (False, False, True, True):
                with self.subTest(enabled=enabled):
                    result = await proactive_tools.set_proactive_schedule_enabled.ainvoke(
                        {"schedule_id": 1, "enabled": enabled},
                        config={"configurable": {"current_chat_id": -123}},
                    )
                    saved = await repository.get_proactive_schedule(1)
                    self.assertIs(result["enabled"], enabled)
                    self.assertIs(saved["enabled"], enabled)
                    for key in before.keys() - {"enabled", "updated_at"}:
                        self.assertEqual(saved[key], before[key], key)
                    self.assertEqual(proactive.next_run_at(1) is not None, enabled)
                    self.assertEqual(result["next_run_at"] is not None, enabled)
                    self.assertEqual(len(scheduler.get_jobs()), int(enabled))

    async def test_toggle_rejects_missing_and_foreign_ids_without_changes(self):
        for schedule_id in (2, 999):
            with self.subTest(schedule_id=schedule_id), self.assertRaisesRegex(ValueError, "current chat"):
                await proactive_tools.set_proactive_schedule_enabled.ainvoke(
                    {"schedule_id": schedule_id, "enabled": False},
                    config={"configurable": {"current_chat_id": -123}},
                )
        self.assertTrue((await repository.get_proactive_schedule(2))["enabled"])

    async def test_enabling_exhausted_schedule_does_not_reset_or_register_it(self):
        await repository.update_proactive_schedule(3, {"repeat_times": 2})
        scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
        scheduler.start(paused=True)
        self.addCleanup(scheduler.shutdown, wait=False)
        with patch.object(proactive, "_scheduler", scheduler):
            result = await proactive_tools.set_proactive_schedule_enabled.ainvoke(
                {"schedule_id": 3, "enabled": True},
                config={"configurable": {"current_chat_id": -123}},
            )
            self.assertTrue(result["enabled"])
            self.assertEqual((result["repeat_times"], result["repeat_completed"]), (2, 2))
            self.assertIsNone(result["next_run_at"])
            self.assertEqual(scheduler.get_jobs(), [])

    async def test_cancel_after_commit_still_synchronizes_create_update_and_delete(self):
        scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
        scheduler.start(paused=True)
        self.addCleanup(scheduler.shutdown, wait=False)
        original_sync, original_remove = proactive.sync_schedule_job, proactive.remove_schedule_job
        with patch.object(proactive, "_scheduler", scheduler), \
                patch.object(telegram.client, "get_entity", AsyncMock()):
            original_sync(await repository.get_proactive_schedule(1))
            for operation in ("create", "update", "delete"):
                with self.subTest(operation=operation):
                    committed, release, synchronized = asyncio.Event(), asyncio.Event(), asyncio.Event()

                    async def pause_after_commit():
                        committed.set()
                        await release.wait()

                    def sync(row):
                        original_sync(row)
                        synchronized.set()

                    def remove(schedule_id):
                        original_remove(schedule_id)
                        synchronized.set()

                    args = {
                        "name": "Reminder", "cron_expression": "0 11 * * *",
                        "intent": "Follow up about the exam", "repeat_times": 5,
                    }
                    if operation == "update":
                        args["schedule_id"] = 1
                    elif operation == "delete":
                        args = {"schedule_id": 1}
                    selected = getattr(proactive_tools, f"{operation}_proactive_schedule")
                    self.after_commit = pause_after_commit
                    with patch.object(proactive, "sync_schedule_job", sync), \
                            patch.object(proactive, "remove_schedule_job", remove):
                        task = asyncio.create_task(selected.ainvoke(args, config={
                            "configurable": {"current_chat_id": -123},
                        }))
                        try:
                            await asyncio.wait_for(committed.wait(), timeout=1)
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await task
                            release.set()
                            await asyncio.wait_for(synchronized.wait(), timeout=1)
                        finally:
                            self.after_commit = None
                            release.set()
                            await asyncio.gather(task, return_exceptions=True)
                    if operation == "create":
                        self.assertIsNotNone(proactive.next_run_at(4))
                    elif operation == "update":
                        self.assertIn("hour='11'", str(scheduler.get_job("proactive-schedule-1").trigger))
                    else:
                        self.assertIsNone(proactive.next_run_at(1))
