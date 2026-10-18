import asyncio
import os
import unittest
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, HTTPException
import httpx
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import repository
from app.models import ChatState, History, ProactiveSchedule, User
from app.routers import admin
from app.services import proactive
from app.services import llm
from app.telegram import client as telegram
from types import SimpleNamespace


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

    async def test_http_schedule_lifecycle_and_validation(self):
        app = FastAPI()
        app.include_router(admin.router)
        body = {"name": "Exam check-in", "chat_id": 42, "intent": "Ask about the exam",
                "cron_expression": "0 9 * * *", "timezone": "Asia/Singapore", "repeat_times": 1}
        with patch.object(admin.client, "get_entity", AsyncMock()), patch.object(
            proactive, "send_proactive_message", AsyncMock()
        ):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for changes in ({"intent": " "}, {"cron_expression": "bad"}, {"timezone": "invalid/zone"}):
                    invalid = await client.post("/proactive-schedules", json={**body, **changes})
                    self.assertEqual(invalid.status_code, 422)
                created = await client.post("/proactive-schedules", json=body)
                self.assertEqual(created.status_code, 201)
                schedule_id = created.json()["id"]
                path = f"/proactive-schedules/{schedule_id}"
                run = await client.post(path + "/run")
                self.assertEqual(run.status_code, 200)
                self.assertEqual(run.json()["last_status"], "succeeded")
                self.assertEqual(run.json()["repeat_completed"], 1)
                self.assertEqual((await client.post(path + "/run")).status_code, 409)
                edited = await client.put(path, json={**body, "repeat_times": None})
                self.assertEqual(edited.status_code, 200)
                self.assertEqual(edited.json()["repeat_completed"], 1)
                self.assertEqual((await client.post(path + "/run")).status_code, 200)
                disabled = await client.patch(path + "/enabled", json={"enabled": False})
                self.assertEqual(disabled.status_code, 200)
                self.assertFalse(disabled.json()["enabled"])
                listed = await client.get("/proactive-schedules")
                self.assertEqual([row["id"] for row in listed.json()], [schedule_id])
                self.assertEqual((await client.delete(path)).status_code, 204)
                self.assertEqual((await client.post(path + "/run")).status_code, 404)

    async def test_scheduled_outreach_runs_agent_loop_sends_and_persists(self):
        # Exercise the entire DB -> scheduler -> Telegram -> graph -> history path.
        # Only Telegram, model completions and memory extraction are external.
        async with self.engine.begin() as conn:
            for table in (ChatState.__table__, User.__table__, History.__table__):
                await conn.run_sync(table.create)
        await repository.upsert_user(telegram_user_id=7, first_name="Jamie", last_name=None, username=None)
        await repository.upsert_user(telegram_user_id=99, first_name=llm.BOT_NAME, last_name=None, username=None)
        await repository.add_history_batch(chat_ids=[42], sender_user_ids=[7],
            contents=["My exam is tomorrow"], telegram_message_ids=[1], reasons=[None])
        await repository.set_summary(42, "Exam preparation", "excited")
        row = await self.create_schedule(intent="Ask how the exam went", repeat_times=1)

        class Model:
            def __init__(self, outputs):
                self.outputs, self.messages = iter(outputs), []

            def bind_tools(self, tools):
                return self

            async def ainvoke(self, messages):
                self.messages.append(messages)
                return next(self.outputs)

        fetcher = Model([AIMessage(content=""), AIMessage(content="")])
        responder = Model([
            llm.ResponseOutput(decision="fetch_more", retrieval_request="Exam context",
                reason="Need background", content="How was your exam?"),
            llm.ResponseOutput(decision="respond", retrieval_request="",
                reason="Following the outreach intent", content="Hey Jamie, how did your exam go?"),
        ])

        @asynccontextmanager
        async def typing(*args):
            yield

        from contextlib import ExitStack
        with ExitStack() as stack:
            for name in ("current_messages_buffer", "pending_summaries", "burst_sessions", "wait_tasks",
                         "flush_tasks", "last_message_time", "reply_locks", "buffer_locks", "responded_watermark",
                         "silent_until"):
                stack.enter_context(patch.dict(getattr(telegram, name), {}, clear=True))
            stack.enter_context(patch.dict(llm._chat_mood, {}, clear=True))
            stack.enter_context(patch.object(telegram.client, "is_connected", return_value=True))
            stack.enter_context(patch.object(telegram.client, "get_me", AsyncMock(return_value=SimpleNamespace(id=99))))
            stack.enter_context(patch.object(telegram.client, "action", side_effect=typing))
            sent = stack.enter_context(patch.object(telegram.client, "send_message", AsyncMock(return_value=SimpleNamespace(id=2))))
            stack.enter_context(patch.object(llm, "get_active_trait_prompts", AsyncMock(return_value="Friendly")))
            stack.enter_context(patch.object(llm, "_get_context_fetcher_llm", AsyncMock(return_value=fetcher)))
            stack.enter_context(patch.object(llm, "_get_responder_llm", AsyncMock(return_value=responder)))
            stack.enter_context(patch.object(llm.settings, "agent_max_context_passes", 3))
            stack.enter_context(patch("builtins.print"))
            try:
                self.assertTrue(await proactive.run_schedule(row["id"]))
                stored = await repository.get_proactive_schedule(row["id"])
                self.assertEqual((stored["repeat_completed"], stored["last_status"]), (1, "succeeded"))
                sent.assert_awaited_once_with(42, "Hey Jamie, how did your exam go?")
                self.assertEqual(len(fetcher.messages), 2)
                self.assertIn("Ask how the exam went", responder.messages[-1][0].content)
                self.assertIn("Jamie: My exam is tomorrow", responder.messages[-1][-1].content)
                self.assertNotIn(42, telegram.responded_watermark)
                await telegram._flush_chat(42)
                history = await repository.get_history(42, 40)
                self.assertEqual([m["content"] for m in history], ["My exam is tomorrow", "Hey Jamie, how did your exam go?"])
                self.assertEqual(history[-1]["reason"], "Following the outreach intent")
                self.assertEqual(await repository.get_summary_mood(42), ("Exam preparation", "excited"))
            finally:
                tasks = list(telegram.flush_tasks.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_scheduler_reloads_only_eligible_jobs_and_drains_active_send(self):
        eligible = await self.create_schedule(repeat_times=2)
        await self.create_schedule(enabled=False)
        await self.create_schedule(repeat_times=1, repeat_completed=1)
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args):
            entered.set()
            await release.wait()

        run = stop = None
        try:
            await proactive.start_proactive_scheduler()
            self.assertIsNotNone(proactive.next_run_at(eligible["id"]))
            self.assertEqual(len(proactive._scheduler.get_jobs()), 1)
            with patch.object(proactive, "send_proactive_message", send):
                run = asyncio.create_task(proactive.run_schedule(eligible["id"]))
                await entered.wait()
                self.assertFalse(await proactive.run_schedule(eligible["id"]))
                stop = asyncio.create_task(proactive.stop_proactive_scheduler())
                await asyncio.sleep(0)
                self.assertFalse(stop.done())
                self.assertFalse(await proactive.run_schedule(eligible["id"]))
                release.set()
                self.assertTrue(await run)
                await stop
            self.assertIsNone(proactive.next_run_at(eligible["id"]))
            stored = await repository.get_proactive_schedule(eligible["id"])
            self.assertEqual((stored["repeat_completed"], stored["last_status"]), (1, "succeeded"))
        finally:
            release.set()
            await asyncio.gather(*(task for task in (run, stop) if task is not None), return_exceptions=True)
            await proactive.stop_proactive_scheduler()


if __name__ == "__main__":
    unittest.main()
