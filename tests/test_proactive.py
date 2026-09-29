import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import repository
from app.models import ProactiveSchedule
from app.routers import admin
from app.services import proactive


def schedule_row(**overrides):
    now = datetime.now(timezone.utc)
    return {
        "id": 1, "name": "Check-in", "chat_id": 42,
        "cron_expression": "0 9 * * *", "timezone": "Asia/Singapore",
        "intent": "Check in", "enabled": True,
        "repeat_times": 1, "repeat_completed": 1,
        "last_run_at": now, "last_status": "running", "last_error": None,
        "created_at": now, "updated_at": now, **overrides,
    }


class RepeatInputTests(unittest.TestCase):
    def test_limit_is_validated_and_counter_is_not_writable(self):
        data = schedule_row()
        for limit in (0, -1, 1.5, True, "3"):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                admin.ProactiveScheduleIn(**{**data, "repeat_times": limit})
        for limit in (None, 1, 3):
            body = admin.ProactiveScheduleIn(**{**data, "repeat_times": limit})
            self.assertEqual(body.repeat_times, limit)
            self.assertNotIn("repeat_completed", body.model_dump())


class ProactiveServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scheduler = AsyncIOScheduler(event_loop=asyncio.get_running_loop())
        self.scheduler.start(paused=True)
        self.scheduler_patch = patch.object(proactive, "_scheduler", self.scheduler)
        self.scheduler_patch.start()
        self.accepting_patch = patch.object(proactive, "_accepting_runs", True)
        self.accepting_patch.start()
        proactive._schedule_locks.clear()

    async def asyncTearDown(self):
        self.scheduler.shutdown(wait=False)
        self.scheduler_patch.stop()
        self.accepting_patch.stop()

    async def test_exhausted_schedule_is_not_registered_and_can_resume(self):
        proactive.sync_schedule_job(schedule_row())
        self.assertIsNone(proactive.next_run_at(1))
        proactive.sync_schedule_job(schedule_row(repeat_times=2))
        self.assertIsNotNone(proactive.next_run_at(1))
        proactive.sync_schedule_job(schedule_row(repeat_times=None))
        self.assertIsNotNone(proactive.next_run_at(1))
        proactive.sync_schedule_job(schedule_row(enabled=False))
        self.assertIsNone(proactive.next_run_at(1))

    async def test_final_attempt_is_consumed_and_unscheduled_before_send(self):
        row = schedule_row()
        observed = []
        proactive.sync_schedule_job(schedule_row(repeat_completed=0))
        with (
            patch.object(repository, "claim_proactive_schedule_run", AsyncMock(return_value=row), create=True) as claim,
            patch.object(repository, "get_proactive_schedule", AsyncMock(return_value=row)),
            patch.object(repository, "set_proactive_schedule_status", AsyncMock()),
        ):
            async def send(*args):
                observed.append((claim.await_count, proactive.next_run_at(1)))

            with patch.object(proactive, "send_proactive_message", send):
                self.assertTrue(await proactive.run_schedule(1))
        self.assertEqual(observed, [(1, None)])

    async def test_exhausted_schedule_never_sends(self):
        with (
            patch.object(repository, "claim_proactive_schedule_run", AsyncMock(return_value=None), create=True),
            patch.object(repository, "get_proactive_schedule", AsyncMock(return_value=schedule_row())),
            patch.object(repository, "set_proactive_schedule_status", AsyncMock()),
            patch.object(proactive, "send_proactive_message", AsyncMock()) as send,
        ):
            self.assertFalse(await proactive.run_schedule(1))
            send.assert_not_awaited()
            with self.assertRaises(HTTPException) as exc:
                await admin.run_proactive_schedule(1)
            self.assertEqual(exc.exception.status_code, 409)

    async def test_limit_increase_during_reconciliation_keeps_replaced_job(self):
        for claim_result in (schedule_row(), None):
            with self.subTest(claim_result=claim_result):
                proactive.sync_schedule_job(schedule_row(repeat_completed=0))

                async def stale_read(*args):
                    # SELECT saw exhaustion; while its transaction closes, an
                    # admin raises the limit and replaces the scheduled job.
                    proactive.sync_schedule_job(schedule_row(repeat_times=2))
                    return schedule_row()

                with (
                    patch.object(repository, "claim_proactive_schedule_run", AsyncMock(return_value=claim_result)),
                    patch.object(repository, "get_proactive_schedule", stale_read),
                    patch.object(repository, "set_proactive_schedule_status", AsyncMock()),
                    patch.object(proactive, "send_proactive_message", AsyncMock()),
                ):
                    await proactive.run_schedule(1)
                self.assertIsNotNone(proactive.next_run_at(1))


@unittest.skipUnless(os.environ.get("TEST_DATABASE_URL"), "Set TEST_DATABASE_URL to run Postgres integration tests")
class ProactivePostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Each test owns a separate schema, never the application's tables.
        self.schema = "repeat_test_" + uuid4().hex
        url = os.environ["TEST_DATABASE_URL"]
        self.control = create_async_engine(url)
        async with self.control.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_async_engine(url, connect_args={"server_settings": {"search_path": self.schema}})
        async with self.engine.begin() as conn:
            await conn.run_sync(ProactiveSchedule.__table__.create)
        factory = async_sessionmaker(self.engine, expire_on_commit=False)

        @asynccontextmanager
        async def scope():
            async with factory() as session, session.begin():
                yield session

        self.scope_patch = patch.object(repository, "session_scope", scope)
        self.scope_patch.start()
        self.scheduler_patch = patch.object(proactive, "_scheduler", None)
        self.scheduler_patch.start()
        self.accepting_patch = patch.object(proactive, "_accepting_runs", True)
        self.accepting_patch.start()
        proactive._schedule_locks.clear()

    async def asyncTearDown(self):
        self.scope_patch.stop()
        self.scheduler_patch.stop()
        self.accepting_patch.stop()
        await self.engine.dispose()
        async with self.control.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        await self.control.dispose()

    async def create_schedule(self, **overrides):
        return await repository.create_proactive_schedule({
            "name": "Check-in", "chat_id": 42, "intent": "Check in",
            "cron_expression": "0 9 * * *", "timezone": "Asia/Singapore",
            **overrides,
        })

    async def test_concurrent_claims_cannot_exceed_limit(self):
        row = await self.create_schedule(repeat_times=3)
        results = await asyncio.gather(*[
            repository.claim_proactive_schedule_run(row["id"])
            for _ in range(10)
        ])
        self.assertEqual(sum(result is not None for result in results), 3)
        stored = await repository.get_proactive_schedule(row["id"])
        self.assertEqual(stored["repeat_completed"], 3)

    async def test_unlimited_counts_every_claim(self):
        row = await self.create_schedule()
        self.assertIsNone(row["repeat_times"])
        self.assertEqual(row["repeat_completed"], 0)
        for _ in range(5):
            await repository.claim_proactive_schedule_run(row["id"])
        stored = await repository.get_proactive_schedule(row["id"])
        self.assertEqual(stored["repeat_completed"], 5)

    async def test_failed_one_shot_is_committed_before_send_and_stays_consumed(self):
        row = await self.create_schedule(repeat_times=1)
        seen = []

        async def fail_send(*args):
            # A new session must see the increment before any send work starts.
            stored = await repository.get_proactive_schedule(row["id"])
            seen.append(stored["repeat_completed"])
            raise RuntimeError("send failed")

        with patch.object(proactive, "send_proactive_message", fail_send):
            with self.assertLogs("rachel.proactive", level="ERROR"):
                self.assertTrue(await proactive.run_schedule(row["id"]))
            proactive._schedule_locks.clear()  # Simulate losing process-local state.
            self.assertFalse(await proactive.run_schedule(row["id"]))
        self.assertEqual(seen, [1])
        stored = await repository.get_proactive_schedule(row["id"])
        self.assertEqual(stored["last_status"], "failed")
        self.assertEqual(stored["repeat_completed"], 1)

    async def test_cancellation_does_not_refund_attempt(self):
        row = await self.create_schedule(repeat_times=1)
        with patch.object(proactive, "send_proactive_message", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await proactive.run_schedule(row["id"])
        proactive._schedule_locks.clear()
        self.assertFalse(await proactive.run_schedule(row["id"]))
        stored = await repository.get_proactive_schedule(row["id"])
        self.assertEqual(stored["repeat_completed"], 1)

    async def test_edits_preserve_count_and_higher_limit_restores_eligibility(self):
        row = await self.create_schedule(repeat_times=1)
        await repository.claim_proactive_schedule_run(row["id"])
        await repository.update_proactive_schedule(row["id"], {"enabled": False, "name": "Edited"})
        edited = await repository.get_proactive_schedule(row["id"])
        self.assertEqual(edited["repeat_completed"], 1)
        self.assertIsNone(await repository.claim_proactive_schedule_run(row["id"]))
        await repository.update_proactive_schedule(row["id"], {"repeat_times": 2})
        claimed = await repository.claim_proactive_schedule_run(row["id"])
        self.assertEqual(claimed["repeat_completed"], 2)

    async def test_disabled_schedule_blocks_cron_but_allows_manual_run(self):
        row = await self.create_schedule(repeat_times=1, enabled=False)
        with patch.object(proactive, "send_proactive_message", AsyncMock()) as send:
            await proactive._scheduled_run(row["id"])
            send.assert_not_awaited()
            self.assertTrue(await proactive.run_schedule(row["id"]))
        stored = await repository.get_proactive_schedule(row["id"])
        self.assertEqual(stored["repeat_completed"], 1)

    async def test_api_omitted_limit_preserves_it_and_explicit_null_clears_it(self):
        row = await self.create_schedule(repeat_times=2)
        data = {key: row[key] for key in ("name", "chat_id", "cron_expression", "timezone", "intent", "enabled")}
        with patch.object(admin.client, "get_entity", AsyncMock()):
            saved = await admin.update_proactive_schedule(row["id"], admin.ProactiveScheduleIn(**data))
            self.assertEqual(saved.repeat_times, 2)
            cleared = await admin.update_proactive_schedule(row["id"], admin.ProactiveScheduleIn(**data, repeat_times=None))
            self.assertIsNone(cleared.repeat_times)


if __name__ == "__main__":
    unittest.main()
