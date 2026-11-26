"""Exercise the moderation tool with mocked Telegram requests."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.messages import HumanMessage
from telethon.errors import UsernameNotOccupiedError
from telethon.tl import functions, types

from app.services import llm
from tests.test_agent_loop import FetcherModel, Model, calls, finish, reply, state


CHAT_ID = -1000000000042
TARGET = types.User(id=74, access_hash=174, first_name="Jamie", username="jamie")
PEER = types.InputPeerUser(user_id=74, access_hash=174)
MEMBER = SimpleNamespace(is_admin=False, is_banned=False, has_left=False)


class KickTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = AsyncMock()
        self.client.return_value = types.contacts.ResolvedPeer(
            peer=types.PeerUser(74), chats=[], users=[TARGET]
        )
        self.client.get_input_entity.return_value = PEER
        self.client.get_permissions.side_effect = lambda chat, user: (
            SimpleNamespace(is_creator=False, ban_users=True) if user == "me" else MEMBER
        )

    def turn(self, request="Rachel, kick @Jamie or user 75", recent_user_id=None):
        from app.services.kick_tools import KickRuntime

        return KickRuntime(CHAT_ID, 99, self.client, request, recent_user_id)

    async def invoke(self, runtime, **args):
        from app.services.kick_tools import kick_user

        return await kick_user.ainvoke(args, config={"configurable": {
            "current_chat_id": CHAT_ID, "kick_runtime": runtime,
        }})

    async def test_username_resolves_live_and_shares_receipt_with_numeric_id(self):
        runtime = self.turn("Rachel, kick @Jamie (user 74)")
        first = await self.invoke(runtime, username="@Jamie")
        second = await self.invoke(runtime, user_id=74)
        self.assertIn("74", first)
        self.assertEqual(first, second)
        request = self.client.await_args.args[0]
        self.assertIsInstance(request, functions.contacts.ResolveUsernameRequest)
        self.assertEqual(request.username.casefold(), "jamie")
        self.client.kick_participant.assert_awaited_once_with(CHAT_ID, PEER)

    async def test_bare_username_and_recent_id_each_work_without_prior_dialogue(self):
        await self.invoke(self.turn("Could you remove Jamie, Rachel?"), username="Jamie")
        await self.invoke(self.turn("Kick the person just added", 74), user_id=74)
        self.assertEqual(self.client.kick_participant.await_count, 2)

    async def test_unrelated_identifier_cannot_replace_the_requested_target(self):
        with self.assertRaisesRegex(ValueError, "request"):
            await self.invoke(self.turn("Could you kick @Other, Rachel?"), username="jamie")
        self.client.assert_not_awaited()
        self.client.kick_participant.assert_not_awaited()

    async def test_invalid_arguments_never_reach_telegram(self):
        for args in ({}, {"user_id": 0}, {"user_id": "74"}, {"username": "t.me/jamie"},
                     {"username": "Jamie", "user_id": 74}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.invoke(self.turn(), **args)
        self.client.assert_not_awaited()
        self.client.kick_participant.assert_not_awaited()

    async def test_unknown_username_and_channel_are_rejected(self):
        self.client.side_effect = [
            UsernameNotOccupiedError(None),
            types.contacts.ResolvedPeer(peer=types.PeerChannel(74), chats=[], users=[]),
        ]
        for _ in range(2):
            with self.assertRaises(ValueError):
                await self.invoke(self.turn(), username="jamie")
        self.client.kick_participant.assert_not_awaited()

    async def test_departed_banned_and_admin_members_are_protected(self):
        for member in (
            SimpleNamespace(is_admin=True, is_banned=False, has_left=False),
            SimpleNamespace(is_admin=False, is_banned=True, has_left=False),
            SimpleNamespace(is_admin=False, is_banned=False, has_left=True),
        ):
            with self.subTest(member=member), self.assertRaises(ValueError):
                self.client.get_permissions.side_effect = [
                    SimpleNamespace(is_creator=False, ban_users=True), member,
                ]
                await self.invoke(self.turn(), username="jamie")
        self.client.kick_participant.assert_not_awaited()

    async def test_missing_removal_permission_prevents_the_kick(self):
        self.client.get_permissions.side_effect = [
            SimpleNamespace(is_creator=False, ban_users=False), MEMBER,
        ]
        with self.assertRaisesRegex(ValueError, "Ban Users"):
            await self.invoke(self.turn(), username="jamie")
        self.client.kick_participant.assert_not_awaited()

    async def test_username_resolving_to_rachel_is_protected(self):
        self.client.return_value = types.contacts.ResolvedPeer(
            peer=types.PeerUser(99), chats=[],
            users=[types.User(id=99, access_hash=199, is_self=True)],
        )
        with self.assertRaisesRegex(ValueError, "herself"):
            await self.invoke(self.turn("Rachel, kick @rachel"), username="rachel")
        self.client.kick_participant.assert_not_awaited()

    async def test_uncertain_kick_blocks_other_attempts_in_the_same_turn(self):
        runtime = self.turn()
        self.client.kick_participant.side_effect = TimeoutError
        for args in ({"username": "jamie"}, {"user_id": 75}):
            with self.assertRaisesRegex(RuntimeError, "uncertain"):
                await self.invoke(runtime, **args)
        self.client.kick_participant.assert_awaited_once_with(CHAT_ID, PEER)

    async def test_response_graph_runs_kick_once_and_surfaces_receipt_to_responder(self):
        kick_call = calls(("kick_user", {"username": "jamie"}))
        fetcher = FetcherModel([kick_call, kick_call, finish()])
        responder = Model([reply(content="Jamie has been removed.")])
        with patch.object(llm, "summarizer_node", AsyncMock(return_value={})), patch.object(
            llm, "_get_context_fetcher_llm", AsyncMock(return_value=fetcher),
        ), patch.object(llm, "_get_responder_llm", AsyncMock(return_value=responder)), patch.object(
            llm, "get_active_trait_prompts", AsyncMock(return_value=""),
        ), patch.object(llm, "print", create=True):
            graph = llm._build_graph()
            result = await graph.ainvoke(state(
                chat_id=CHAT_ID, is_private=False, kick_runtime=self.turn(),
            ), {"recursion_limit": 24})
        self.assertEqual(result["response_text"], "Jamie has been removed.")
        self.assertIn("[Member kicked]", responder.messages[0][0].content)
        self.client.kick_participant.assert_awaited_once_with(CHAT_ID, PEER)

    async def test_context_fetcher_uses_tool_and_keeps_proactive_calls_out(self):
        for is_proactive in (False, True):
            with self.subTest(is_proactive=is_proactive):
                fetcher = FetcherModel([calls(("kick_user", {"username": "jamie"}))])
                observations = []
                with patch.object(llm, "_get_context_fetcher_llm", AsyncMock(return_value=fetcher)):
                    await llm._run_context_fetcher(state(
                        chat_id=CHAT_ID, is_private=False, kick_runtime=self.turn(),
                        intent="Send an update" if is_proactive else "",
                    ), observations)
                expected = "error" if is_proactive else "success"
                self.assertEqual(observations[0]["status"], expected)
                self.assertEqual("kick_user" in fetcher.bound_tool_names, not is_proactive)
                if not is_proactive:
                    self.assertTrue(any(
                        "@Jamie" in message.content for message in fetcher.messages[0]
                        if isinstance(message, HumanMessage)
                    ))
        self.client.kick_participant.assert_awaited_once_with(CHAT_ID, PEER)


class RecentAddsTests(unittest.TestCase):
    def test_latest_add_is_scoped_to_group_and_precedes_request(self):
        from app.services.kick_tools import RecentMemberAdds

        adds = RecentMemberAdds()
        now = datetime.now(timezone.utc)
        adds.record(CHAT_ID, (74,), now - timedelta(seconds=1), 9)
        adds.record(CHAT_ID, (75,), now, 11)
        adds.record(-43, (76,), now - timedelta(seconds=1), 9)
        self.assertEqual(adds.latest_before(CHAT_ID, now, 10), 74)
        self.assertEqual(adds.latest_before(-43, now, 10), 76)

    def test_multiple_members_in_the_latest_add_are_ambiguous(self):
        from app.services.kick_tools import RecentMemberAdds

        adds = RecentMemberAdds()
        now = datetime.now(timezone.utc)
        adds.record(CHAT_ID, (72,), now - timedelta(seconds=2), 8)
        adds.record(CHAT_ID, (73, 74), now - timedelta(seconds=1), 9)
        self.assertIsNone(adds.latest_before(CHAT_ID, now, 10))

    def test_unordered_same_second_add_does_not_select_an_older_member(self):
        from app.services.kick_tools import RecentMemberAdds

        adds = RecentMemberAdds()
        now = datetime.now(timezone.utc)
        adds.record(CHAT_ID, (74,), now - timedelta(seconds=1), 9)
        adds.record(CHAT_ID, (75,), now, None)
        self.assertIsNone(adds.latest_before(CHAT_ID, now, 10))
