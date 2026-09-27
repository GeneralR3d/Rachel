import unittest

from telethon.tl.types import Channel, User

from app.routers.admin import enrich_chats_with_names, resolve_chat_name


class ResolveChatNameTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_telegram_display_name(self) -> None:
        async def get_entity(chat_id: int) -> Channel:
            self.assertEqual(chat_id, -1001234567890)
            return Channel(id=1234567890, title="Sunday Hikers", photo=None, date=None)

        self.assertEqual(
            await resolve_chat_name(-1001234567890, get_entity),
            "Sunday Hikers",
        )

    async def test_returns_full_name_for_private_chat(self) -> None:
        async def get_entity(chat_id: int) -> User:
            return User(id=chat_id, first_name="Jamie", last_name="Lim")

        self.assertEqual(await resolve_chat_name(84721, get_entity), "Jamie Lim")

    async def test_returns_none_when_telegram_cannot_resolve_chat(self) -> None:
        async def get_entity(chat_id: int) -> None:
            raise ValueError(f"unknown chat {chat_id}")

        self.assertIsNone(await resolve_chat_name(-404, get_entity))

    async def test_enriches_each_chat_without_dropping_unresolved_rows(self) -> None:
        chats = [
            {"chat_id": -1007, "message_count": 18},
            {"chat_id": 42, "message_count": 6},
        ]

        async def get_entity(chat_id: int) -> Channel:
            if chat_id == 42:
                raise ValueError("not cached by Telegram")
            return Channel(id=7, title="Project Lantern", photo=None, date=None)

        self.assertEqual(
            await enrich_chats_with_names(chats, get_entity),
            [
                {"chat_id": -1007, "message_count": 18, "chat_name": "Project Lantern"},
                {"chat_id": 42, "message_count": 6, "chat_name": None},
            ],
        )


if __name__ == "__main__":
    unittest.main()
