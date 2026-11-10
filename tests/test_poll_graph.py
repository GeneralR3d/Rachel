"""Poll sending is a side effect with an ordinary text tool observation."""
import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon.errors import RPCError

from app.services import llm
from app.services.poll_tools import PollDeliveryRuntime
from tests.test_agent_loop import FetcherModel, Model, calls, finish, reply, state

POLL = ("send_poll", {"question": "Which language?", "options": ["Python", "Rust"]})
POLL_TEXT = "[Poll sent]\nQuestion: Which language?\nOptions:\n1. Python\n2. Rust"


class PollGraphTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("builtins.print"))
        self.stack.enter_context(patch.object(llm, "get_active_trait_prompts", AsyncMock(return_value="friendly")))
        self.client = SimpleNamespace(send_file=AsyncMock(return_value=SimpleNamespace(id=123)))
        self.record = AsyncMock()
        self.runtime = PollDeliveryRuntime(42, 99, self.client, self.record)

    async def graph(self, proactive=False, fetches=None, replies=None, budget=3):
        self.fetcher = FetcherModel(fetches if fetches is not None else [calls(POLL), finish()])
        self.responder = Model(replies if replies is not None else [reply(content="Have a vote!")])
        with patch.object(llm, "_get_context_fetcher_llm", AsyncMock(return_value=self.fetcher)), patch.object(
            llm, "_get_responder_llm", AsyncMock(return_value=self.responder)
        ), patch.object(llm.settings, "agent_max_context_passes", budget):
            graph = llm._build_proactive_graph() if proactive else llm._build_graph()
            return await graph.ainvoke(state(chat_id=42, poll_delivery=self.runtime,
                intent="Send a poll" if proactive else ""), {"recursion_limit": 2 * budget + 8})

    async def test_initial_poll_reaches_responder_after_both_summary_completion_orders(self):
        for delay_summary in (False, True):
            with self.subTest(delay_summary=delay_summary):
                async def summarize(_state):
                    if delay_summary:
                        await asyncio.sleep(0.02)
                    return {"current_summary": "updated", "mood": "excited"}
                with patch.object(llm, "summarizer_node", summarize):
                    result = await self.graph()
                self.assertEqual(result["response_text"], "Have a vote!")
                self.assertEqual(result["context_passes"], 2)
                self.assertEqual(result["context_status"], "finished")
                self.assertEqual(result["loop_outcome"], "retried")
                self.assertEqual(result["current_summary"], "updated")
                self.assertEqual(result["mood"], "excited")
                self.assertEqual(len(self.responder.messages), 1)
                self.assertEqual(result["tool_observations"][0]["raw_result"], POLL_TEXT)
                self.assertIn(POLL_TEXT, self.responder.messages[0][0].content)

    async def test_successful_poll_keeps_later_fetcher_passes_in_both_graphs(self):
        for proactive in (False, True):
            with self.subTest(proactive=proactive), patch.object(llm, "summarizer_node", AsyncMock(return_value={})), patch.object(
                llm, "search_worldview", AsyncMock(return_value="Cafe closes at nine")
            ):
                result = await self.graph(proactive,
                    [calls(POLL), calls(("search_world_view", {"query": "cafe"})), finish()],
                    [reply(content="Vote before nine!")])
                self.assertEqual(result["response_text"], "Vote before nine!")
                self.assertEqual(result["context_passes"], 3)
                self.assertEqual(result["loop_outcome"], "retried")
                self.assertEqual(len(self.responder.messages), 1)
                self.assertEqual(result["tool_observations"][0]["raw_result"], POLL_TEXT)
                self.assertEqual(result["tool_observations"][1]["raw_result"], "Cafe closes at nine")

    async def test_later_pass_poll_reaches_the_single_responder_call(self):
        for proactive in (False, True):
            with self.subTest(proactive=proactive), patch.object(
                llm, "summarizer_node", AsyncMock(return_value={})
            ), patch.object(llm, "search_worldview", AsyncMock(return_value="Cafe closes at nine")):
                result = await self.graph(proactive,
                    [calls(("search_world_view", {"query": "cafe"})), calls(POLL), finish()],
                    [reply(content="I've opened the poll.")])
                self.assertEqual(result["response_text"], "I've opened the poll.")
                self.assertEqual(result["context_passes"], 3)
                self.assertEqual(len(self.responder.messages), 1)
                self.assertEqual(result["tool_observations"][1]["pass_number"], 2)
                self.assertIn(POLL_TEXT, self.responder.messages[-1][0].content)

    async def test_all_tools_in_batch_are_awaited_in_order_after_poll(self):
        order = []
        async def lookup(query, **kwargs):
            await asyncio.sleep(0)
            order.append(query)
            return query
        async def send(*args):
            self.assertEqual(order, ["before"])
            order.append("poll")
            return SimpleNamespace(id=123)
        self.client.send_file.side_effect = send
        with patch.object(llm, "search_worldview", lookup), patch.object(llm, "summarizer_node", AsyncMock(return_value={})):
            result = await self.graph(fetches=[calls(("search_world_view", {"query": "before"}), POLL,
                                                     ("search_world_view", {"query": "after"})), finish()])
        self.assertEqual(order, ["before", "poll", "after"])
        self.assertEqual([o["status"] for o in result["tool_observations"]], ["success"] * 3)
        self.assertEqual(len(self.responder.messages), 1)

    async def test_distinct_polls_are_independent_tools_and_identical_calls_reuse_results(self):
        other = ("send_poll", {"question": "Which day?", "options": ["Friday", "Saturday"]})
        self.client.send_file.side_effect = [SimpleNamespace(id=123), SimpleNamespace(id=124)]
        with patch.object(llm, "summarizer_node", AsyncMock(return_value={})):
            result = await self.graph(fetches=[calls(POLL, other), calls(POLL), finish()])
        self.assertEqual(self.client.send_file.await_count, 2)
        self.assertEqual(self.record.await_count, 2)
        observations = result["tool_observations"]
        self.assertEqual([o["status"] for o in observations], ["success"] * 3)
        self.assertEqual(observations[0]["raw_result"], POLL_TEXT)
        self.assertIn("Question: Which day?", observations[1]["raw_result"])
        self.assertEqual(observations[2]["raw_result"], POLL_TEXT)
        self.assertEqual(len(self.responder.messages), 1)

    async def test_failed_poll_keeps_normal_batch_and_completion_behavior(self):
        for proactive in (False, True):
            for failure in ("invalid", "rejection", "timeout"):
                with self.subTest(proactive=proactive, failure=failure):
                    self.runtime = PollDeliveryRuntime(42, 99, self.client, self.record)
                    args = POLL if failure != "invalid" else ("send_poll", {"question": "", "options": ["a", "b"]})
                    self.client.send_file.side_effect = TimeoutError("uncertain") if failure == "timeout" else RPCError(None, "POLL_INVALID", 400)
                    with patch.object(llm, "summarizer_node", AsyncMock(return_value={})), patch.object(
                        llm, "search_worldview", AsyncMock(return_value="fact")
                    ):
                        result = await self.graph(proactive,
                            [calls(args, ("search_world_view", {"query": "after failure"})), finish()],
                            [reply(content="final text")])
                    self.assertEqual(result["response_text"], "final text")
                    self.assertEqual(result["context_passes"], 2)
                    self.assertEqual(len(self.responder.messages), 1)
                    self.assertEqual(result["tool_observations"][0]["status"],
                                     "timeout" if failure == "timeout" else "error")
                    self.assertEqual(result["tool_observations"][1]["raw_result"], "fact")
                    self.record.assert_not_awaited()

    async def test_real_send_and_pass_deadlines_remain_uncertain_in_both_graphs(self):
        from app.services import poll_tools
        for proactive in (False, True):
            for pass_deadline in (False, True):
                with self.subTest(proactive=proactive, pass_deadline=pass_deadline):
                    self.runtime = PollDeliveryRuntime(42, 99, self.client, self.record)
                    self.client.send_file.reset_mock()
                    async def hang(*args):
                        await asyncio.sleep(10)
                    self.client.send_file.side_effect = hang
                    with patch.object(llm, "summarizer_node", AsyncMock(return_value={})), patch.object(
                        poll_tools, "POLL_SEND_TIMEOUT", 1 if pass_deadline else 0.01
                    ), patch.object(llm, "CONTEXT_FETCHER_TIMEOUT", 0.01 if pass_deadline else 1):
                        result = await self.graph(proactive, [calls(POLL), calls(POLL), finish()],
                            [reply(content="Unable to confirm delivery")])
                    observation = result["tool_observations"][0]
                    self.assertEqual(observation["status"], "timeout")
                    self.assertIn("uncertain", observation["raw_result"].lower())
                    self.client.send_file.assert_awaited_once()
                    self.record.assert_not_awaited()
                    self.assertEqual(result["response_text"], "Unable to confirm delivery")

    async def test_recording_failure_is_an_ordinary_error_and_can_recover_on_next_pass(self):
        self.record.side_effect = [RuntimeError("write failed"), None]
        with patch.object(llm, "summarizer_node", AsyncMock(return_value={})):
            result = await self.graph(fetches=[calls(POLL), calls(POLL), finish()],
                replies=[reply(content="Vote now!")])
        self.assertEqual(result["response_text"], "Vote now!")
        self.assertEqual([o["status"] for o in result["tool_observations"]], ["error", "success"])
        self.assertEqual(result["tool_observations"][1]["raw_result"], POLL_TEXT)
        self.client.send_file.assert_awaited_once()
        self.assertEqual(len(self.responder.messages), 1)
