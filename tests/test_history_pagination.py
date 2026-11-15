"""Exercise the history API and its pagination query without a network database."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch

from fastapi import FastAPI
import httpx
import sqlalchemy as sa

from app import repository
from app.models import History, User
from app.routers import admin


class HistoryPaginationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        engine = sa.create_engine("sqlite://")
        self.addCleanup(engine.dispose)
        User.__table__.create(engine)
        History.__table__.create(engine)
        with engine.begin() as connection:
            connection.execute(sa.insert(User), [{"telegram_user_id": 7, "first_name": "Jamie"}])
            connection.execute(sa.insert(History), [
                {
                    "chat_id": chat_id,
                    "telegram_message_id": number * 2,
                    "sender_user_id": 7,
                    "content": f"{chat_id}/{number}",
                    "reason": "A reason" if number == 205 else None,
                }
                for chat_id, numbers in ((-123, range(1, 206)), (-456, range(51, 251)))
                for number in numbers
            ])

        @asynccontextmanager
        async def scope():
            with engine.begin() as connection:
                async def execute(statement):
                    return connection.execute(statement)

                yield SimpleNamespace(execute=execute)

        self.enterContext(patch.object(repository, "session_scope", scope))
        app = FastAPI()
        app.include_router(admin.router)
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ))

    async def test_default_page_and_older_pages_cover_only_the_requested_chat(self):
        first = await self.client.get("/history/-123")
        explicit = await self.client.get("/history/-123?page=1")
        self.assertEqual(first.json(), explicit.json())
        second = await self.client.get("/history/-123?page=2")
        third = await self.client.get("/history/-123?page=3")

        for response, numbers, has_more in (
            (first, range(106, 206), "true"),
            (second, range(6, 106), "true"),
            (third, range(1, 6), "false"),
        ):
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), [
                {
                    "sender": "Jamie", "content": f"-123/{number}",
                    "telegram_message_id": number * 2,
                    "reason": "A reason" if number == 205 else None,
                }
                for number in numbers
            ])
            self.assertEqual(response.headers.get("X-Has-More"), has_more)

    async def test_a_full_final_page_has_no_older_page(self):
        response = await self.client.get("/history/-456?page=2")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 100)
        self.assertEqual(response.headers.get("X-Has-More"), "false")

    async def test_empty_chat_and_out_of_range_pages_return_empty_arrays(self):
        for path in ("/history/-789", "/history/-123?page=4", "/history/-123?page=21474838"):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), [])
                self.assertEqual(response.headers.get("X-Has-More"), "false")

    async def test_invalid_pages_are_rejected(self):
        for page in ("", "0", "-1", "1.5", "text", str((2**63 - 1) // 100 + 2)):
            with self.subTest(page=page):
                self.assertEqual((await self.client.get(f"/history/-123?page={page}")).status_code, 422)

    async def test_openapi_documents_the_page_parameter_and_header(self):
        response = await self.client.get("/openapi.json")
        operation = response.json()["paths"]["/history/{chat_id}"]["get"]
        page = next(parameter for parameter in operation["parameters"] if parameter["name"] == "page")
        self.assertEqual(page["schema"]["default"], 1)
        self.assertIn("X-Has-More", operation["responses"]["200"]["headers"])

    async def test_existing_repository_callers_keep_unpaged_and_bounded_reads(self):
        all_messages = await repository.get_history(-123)
        recent = await repository.get_history(-123, 40)
        self.assertEqual([row["telegram_message_id"] for row in all_messages], list(range(2, 411, 2)))
        self.assertEqual(recent, all_messages[-40:])
