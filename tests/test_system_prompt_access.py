"""System prompts are code-owned, with read-only admin inspection."""

from contextlib import ExitStack
from pathlib import Path
import runpy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
import httpx

from app import prompts, repository
from app.routers import admin

ROOT = Path(__file__).resolve().parents[1]


class SystemPromptAccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_returns_constants_and_put_is_not_available(self):
        app = FastAPI()
        app.include_router(admin.router)
        with patch.object(repository, "session_scope", side_effect=AssertionError("Prompt inspection must not use the DB")):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                for endpoint, value in (
                    ("responder-system-prompt", prompts.RESPONDER_SYSTEM_PROMPT),
                    ("summarizer-system-prompt", prompts.SUMMARIZER_SYSTEM_PROMPT),
                ):
                    with self.subTest(endpoint=endpoint):
                        response = await client.get(f"/{endpoint}")
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json(), {"prompt": value})
                        response = await client.put(f"/{endpoint}", json={"prompt": "changed"})
                        self.assertEqual(response.status_code, 405)

    async def test_telegram_inspection_uses_constants_and_setters_are_absent(self):
        registered = []

        def on(event):
            def register(handler):
                registered.append(handler.__name__)
                return handler
            return register

        with patch("telethon.TelegramClient", return_value=SimpleNamespace(on=on)):
            handlers = runpy.run_path(str(ROOT / "app/telegram/bot.py"))
        for suffix, value in (
            ("responder", prompts.RESPONDER_SYSTEM_PROMPT),
            ("summarizer", prompts.SUMMARIZER_SYSTEM_PROMPT),
        ):
            self.assertNotIn(f"on_set_{suffix}_system_prompt", registered)
            event = SimpleNamespace(reply=AsyncMock())
            with patch.object(repository, "session_scope", side_effect=AssertionError("Prompt inspection must not use the DB")):
                await handlers[f"on_get_{suffix}_system_prompt"](event)
            self.assertEqual("".join(call.args[0] for call in event.reply.await_args_list), value)

    async def test_startup_does_not_seed_or_load_system_prompts(self):
        client = SimpleNamespace(start=AsyncMock(), disconnect=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(
            id=7, first_name="Test", last_name=None, username="test",
        )))
        bot = SimpleNamespace(start=AsyncMock(), disconnect=AsyncMock())
        with ExitStack() as stack:
            stack.enter_context(patch("phoenix.otel.register"))
            stack.enter_context(patch.dict("sys.modules", {
                "app.telegram.bot": SimpleNamespace(bot=bot),
                "app.telegram.client": SimpleNamespace(client=client, flush_all_buffers=AsyncMock()),
            }))
            stack.enter_context(patch.object(repository, "session_scope", side_effect=AssertionError("Unexpected system-prompt DB access")))
            for name in ("ensure_traits_seeded", "ensure_schedule_seeded", "ensure_models_seeded", "upsert_user"):
                stack.enter_context(patch.object(repository, name, AsyncMock()))
            stack.enter_context(patch("app.database.dispose_engine", AsyncMock()))
            stack.enter_context(patch("app.services.proactive.start_proactive_scheduler", AsyncMock()))
            stack.enter_context(patch("app.services.proactive.stop_proactive_scheduler", AsyncMock()))
            namespace = runpy.run_path(str(ROOT / "app/main.py"))
            async with namespace["lifespan"](namespace["app"]):
                client.start.assert_awaited_once()
                bot.start.assert_awaited_once()
        client.disconnect.assert_awaited_once()
        bot.disconnect.assert_awaited_once()

    def test_repository_has_no_system_prompt_accessors(self):
        for name in (
            "get_responder_system_prompt", "set_responder_system_prompt",
            "get_summarizer_system_prompt", "set_summarizer_system_prompt",
            "ensure_system_prompts_seeded",
        ):
            self.assertFalse(hasattr(repository, name), name)


if __name__ == "__main__":
    unittest.main()
