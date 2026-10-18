"""Reply-loop contracts; network, database and model calls are replaced at boundaries."""
import asyncio
from contextlib import ExitStack
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import END
from pydantic import ValidationError
from prometheus_client import REGISTRY

from app.config import Settings
from app.services import llm

REAL_SUMMARIZER = llm.summarizer_node


def state(**overrides):
    return {
        "history": [{"sender": "Jamie", "content": "Free on Friday?", "telegram_message_id": 2}],
        "current_summary": "old summary", "mood": "default", "senders": {7: "Jamie"},
        "must_reply": True, "is_private": True, "responded_watermark": 1,
        "should_reply": True, "context_passes": 0, "tool_observations": [],
        "retrieval_request": "",
        "response_text": "", "response_reason": "", "responder_decision": "respond",
        **overrides,
    }


def reply(decision="respond", content="Can meet at six!", request=""):
    return SimpleNamespace(decision=decision, content=content, reason="Grounded in context.", retrieval_request=request)


def calls(*items):
    return AIMessage(content="", tool_calls=[
        {"id": str(i), "name": name, "args": args} for i, (name, args) in enumerate(items)
    ])


class Model:
    def __init__(self, outputs):
        self.outputs = iter(outputs)
        self.messages = []

    async def ainvoke(self, messages):
        self.messages.append(messages)
        output = next(self.outputs)
        if isinstance(output, Exception):
            raise output
        return output


class FetcherModel(Model):
    def __init__(self, outputs):
        super().__init__(outputs)
        self.bound_tool_names = []

    def bind_tools(self, tools):
        self.bound_tool_names = [tool.name for tool in tools]
        return self


class AgentContractTests(unittest.TestCase):
    def test_response_requires_decision_and_request(self):
        with self.assertRaises(ValidationError):
            llm.ResponseOutput(content="hey", reason="friendly")
        with self.assertRaises(ValidationError):
            llm.ResponseOutput(decision="tool_call", content="hey", reason="friendly", retrieval_request="")
        output = llm.ResponseOutput(decision="fetch_more", content="Which time?", reason="Missing time", retrieval_request="Friday evening activities")
        self.assertEqual(output.decision, "fetch_more")

    def test_budget_default_and_inclusive_bounds(self):
        credentials = dict(telegram_api_id=1, telegram_api_hash="test", telegram_bot_token="test", merge_gateway_api_key="test", _env_file=None)
        self.assertEqual(Settings(**credentials).agent_max_context_passes, 3)
        for count in (1, 5, 10):
            self.assertEqual(Settings(**credentials, agent_max_context_passes=count).agent_max_context_passes, count)
        for count in (0, 11):
            with self.assertRaises(ValidationError):
                Settings(**credentials, agent_max_context_passes=count)


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(llm, "get_responder_system_prompt", AsyncMock(return_value="Schedule: {schedule_context}\nWorld: {world_view}\nFacts: {user_facts}\nProfiles: {user_profiles}")))
        self.stack.enter_context(patch.object(llm, "get_active_trait_prompts", AsyncMock(return_value="friendly")))
        self.summary_runs = 0

        async def summarize(_state):
            self.summary_runs += 1
            return {"current_summary": "updated summary", "mood": "excited"}

        self.stack.enter_context(patch.object(llm, "summarizer_node", summarize))
        self.stack.enter_context(patch.object(llm, "search_worldview", AsyncMock(return_value="Cafe closes at 9.")))
        self.stack.enter_context(patch.object(llm, "search_user_info", AsyncMock(return_value=("Jamie likes tea.", {"life_stage": "student"}))))
        self.schedule_calls = []

        async def schedule(args):
            self.schedule_calls.append(args)
            return "Friday: class until 5."

        self.stack.enter_context(patch.dict(llm._context_tools_by_name, {
            "get_day_overview": SimpleNamespace(ainvoke=schedule),
            "get_schedule_for_day": SimpleNamespace(ainvoke=schedule),
        }))

    def models(self, fetch_outputs, response_outputs):
        fetcher, responder = FetcherModel(fetch_outputs), Model(response_outputs)
        self.stack.enter_context(patch.object(llm, "_get_context_fetcher_llm", AsyncMock(return_value=fetcher)))
        self.stack.enter_context(patch.object(llm, "_get_responder_llm", AsyncMock(return_value=responder)))
        return fetcher, responder

    async def run_graph(self, budget=2, recursion_limit=30, **overrides):
        # Compile anew so every scenario exercises the actual registered edges.
        with patch.object(llm.settings, "agent_max_context_passes", budget, create=True):
            return await llm._build_graph().ainvoke(state(**overrides), {"recursion_limit": recursion_limit})

    async def test_first_pass_answer_and_raw_state(self):
        self.models([calls(("get_day_overview", {"day": "Friday"}))], [reply()])
        result = await self.run_graph()
        self.assertEqual(result["response_text"], "Can meet at six!")
        self.assertEqual(result["context_passes"], 1)
        self.assertEqual(result["loop_outcome"], "first_pass")
        self.assertEqual(self.summary_runs, 1)
        self.assertNotIn("schedule_context", result)
        self.assertEqual(result["tool_observations"][0]["raw_result"], "Friday: class until 5.")

    async def test_refinement_retains_every_category_and_handoff(self):
        fetcher, responder = self.models([
            calls(("get_day_overview", {"day": "Friday"}), ("search_world_view", {"query": "cafe hours"}), ("search_user_info", {"name": "Jamie", "query": "drinks"})),
            calls(("get_schedule_for_day", {"day": "Friday"})),
        ], [reply("fetch_more", "Which time were you thinking?", "Friday companions and locations"), reply()])
        result = await self.run_graph()
        self.assertEqual(result["context_passes"], 2)
        self.assertEqual(result["loop_outcome"], "retried")
        self.assertEqual(self.summary_runs, 1)
        self.assertEqual(len(result["tool_observations"]), 4)
        prompt = "\n".join(str(m.content) for m in responder.messages[-1])
        for evidence in ("Friday: class until 5.", "Cafe closes at 9.", "Jamie likes tea.", "student", "Jamie (id 7)"):
            self.assertIn(evidence, prompt)
        handoff = "\n".join(str(m.content) for m in fetcher.messages[-1])
        for evidence in ("Friday companions and locations", "get_day_overview", "cafe hours", "Jamie", "Free on Friday?", "Friday: class until 5."):
            self.assertIn(evidence, handoff)

    async def test_exact_pass_caps_one_through_ten(self):
        for budget in range(1, 11):
            with self.subTest(budget=budget):
                fetcher, responder = self.models([
                    calls(("search_world_view", {"query": f"distinct gap {i}"})) for i in range(budget)
                ], [reply("fetch_more", f"fallback {i}", f"gap {i}") for i in range(budget)])
                result = await self.run_graph(budget)
                self.assertEqual(result["context_passes"], budget)
                self.assertEqual(result["response_text"], f"fallback {budget - 1}")
                self.assertEqual(result["loop_outcome"], "capped")
                self.assertEqual(len(fetcher.messages), budget)
                self.assertEqual(len(responder.messages), budget)

    async def test_repeated_calls_execute_within_and_across_passes(self):
        self.models([
            calls(("get_day_overview", {"day": " Friday "}), ("get_day_overview", {"day": " Friday "}))
            for _ in range(3)
        ], [reply("fetch_more", f"fallback {i}", "Friday details") for i in range(3)])
        result = await self.run_graph(3)
        self.assertEqual(self.schedule_calls, [{"day": " Friday "}] * 6)
        self.assertEqual(result["context_passes"], 3)
        self.assertEqual(result["loop_outcome"], "capped")
        self.assertEqual(result["response_text"], "fallback 2")
        self.assertEqual([o["status"] for o in result["tool_observations"]], ["success"] * 6)

    async def test_empty_passes_continue_until_configured_cap(self):
        for budget in range(1, 11):
            for output in (calls(), calls(("search_world_view", {"query": "unknown"}))):
                with self.subTest(budget=budget, output=output):
                    with patch.object(llm, "search_worldview", AsyncMock(return_value="")):
                        fetcher, responder = self.models([output] * budget, [
                            reply("fetch_more", f"fallback {i}", "unknown detail") for i in range(budget)
                        ])
                        result = await self.run_graph(budget)
                    self.assertEqual(result["context_passes"], budget)
                    self.assertEqual(result["loop_outcome"], "capped")
                    self.assertEqual(result["response_text"], f"fallback {budget - 1}")
                    self.assertEqual(len(fetcher.messages), budget)
                    self.assertEqual(len(responder.messages), budget)

    async def test_backend_failures_remain_errors_through_memory_wrappers(self):
        from app.services import graphiti, userfacts, worldview

        for name, args, attr, real in (
            ("search_world_view", {"query": "cafe"}, "search_worldview", worldview.search_worldview),
            ("search_user_info", {"name": "Jamie", "query": "drinks"}, "search_user_info", userfacts.search_user_info),
        ):
            with self.subTest(name=name):
                self.models([calls((name, args))], [reply()])
                with patch.object(llm, attr, real), patch.object(graphiti, "get_graphiti", AsyncMock(side_effect=RuntimeError("backend unavailable"))), patch.object(llm, "get_user_profiles_cached", AsyncMock(return_value={})):
                    result = await self.run_graph()
                observation = result["tool_observations"][0]
                self.assertEqual(observation["status"], "error")
                self.assertIn("backend unavailable", observation["raw_result"])

    async def test_errors_visible_without_discarding_successes(self):
        _, responder = self.models([calls(("get_day_overview", {"day": "Friday"}), ("search_world_view", {"query": "cafe"}), ("search_user_info", {"name": "Unknown", "query": "drinks"}), ("unknown_tool", {}))], [reply()])
        with patch.object(llm, "search_worldview", AsyncMock(side_effect=RuntimeError("search unavailable"))):
            result = await self.run_graph()
        prompt = "\n".join(str(m.content) for m in responder.messages[0])
        self.assertIn("search unavailable", prompt)
        self.assertIn("Friday: class until 5.", prompt)
        self.assertIn("error", [o["status"] for o in result["tool_observations"]])
        self.assertEqual(result["context_passes"], 1)

    async def test_timeout_preserves_partial_pass(self):
        async def hang(_query, **kwargs):
            await asyncio.sleep(10)

        _, responder = self.models([calls(("get_day_overview", {"day": "Friday"}), ("search_world_view", {"query": "cafe"}))], [reply()])
        with patch.object(llm, "search_worldview", hang), patch.object(llm, "CONTEXT_FETCHER_TIMEOUT", 0.02):
            result = await self.run_graph()
        self.assertEqual([o["status"] for o in result["tool_observations"]], ["success", "timeout"])
        prompt = "\n".join(str(m.content) for m in responder.messages[0])
        self.assertIn("Friday: class until 5.", prompt)
        self.assertIn("timeout", prompt)

    async def test_fetcher_model_timeout_is_visible_and_can_retry(self):
        self.models([TimeoutError("gateway timeout"), calls(("search_world_view", {"query": "cafe hours"}))],
                    [reply("fetch_more", "Could you clarify?", "cafe hours"), reply()])
        result = await self.run_graph(5)
        self.assertEqual([o["status"] for o in result["tool_observations"]], ["timeout", "success"])
        self.assertEqual(result["context_passes"], 2)
        self.assertEqual(result["loop_outcome"], "retried")

    async def test_unsupported_request_responds_without_looping(self):
        _, responder = self.models([calls()], [reply(content="I can't check live flights; which flight?")])
        result = await self.run_graph()
        self.assertEqual(result["context_passes"], 1)
        self.assertIn("live flights", result["response_text"])
        prompt = "\n".join(str(m.content) for m in responder.messages[0])
        self.assertIn("capabilities", prompt.lower())
        self.assertIn("not web search", prompt.lower())

    async def test_recursion_guard_uses_prepared_reply(self):
        self.models([calls(("get_day_overview", {"day": "Friday"}))], [reply("fetch_more", "Which time?", "Friday details")])
        result = await self.run_graph(5, recursion_limit=5)
        self.assertEqual(result["response_text"], "Which time?")
        self.assertEqual(result["context_passes"], 1)
        self.assertEqual(result["loop_outcome"], "capped")

    async def test_router_rejection_and_no_new_message_bypass(self):
        for overrides in ({"must_reply": False}, {"history": [{"sender": llm.BOT_NAME, "content": "Already answered", "telegram_message_id": 3}]}):
            with self.subTest(overrides=overrides):
                with patch.object(llm, "router_node", AsyncMock(return_value={"should_reply": False})):
                    result = await self.run_graph(**overrides)
                self.assertEqual(result["response_text"], "")
                self.assertEqual(result["context_passes"], 0)
                self.assertEqual(self.summary_runs, 0)

    async def test_parse_failure_salvages_prose_and_json_content_only(self):
        for raw, expected in ((f"{llm.BOT_NAME}: Not sure, what time?", "Not sure, what time?"), (json.dumps({"decision": "fetch_more", "content": "Which cafe?", "retrieval_request": "INTERNAL"}), "Which cafe?"),
                              ('Here is my response: {"decision":"fetch_more","content":"Which cafe?","retrieval_request":"INTERNAL"}', "Which cafe?")):
            with self.subTest(raw=raw):
                self.models([calls()], [OutputParserException("bad output", llm_output=raw)])
                result = await self.run_graph()
                self.assertEqual(result["response_text"], expected)
                self.assertEqual(result["responder_decision"], "respond")
                self.assertEqual(result["retrieval_request"], "")

    async def test_raw_structured_envelope_salvages_and_terminates(self):
        for raw, expected in (("Hey, which day?", "Hey, which day?"),
                              ('{"content":"Which cafe?","retrieval_request":"private"}', "Which cafe?")):
            with self.subTest(raw=raw):
                self.models([calls()], [{"parsed": None, "raw": AIMessage(content=raw), "parsing_error": ValueError("missing decision")}])
                result = await self.run_graph()
                self.assertEqual(result["response_text"], expected)
                self.assertEqual(result["responder_decision"], "respond")
                self.assertEqual(result["responder_status"], "parse_fallback")

    async def test_unusable_parse_keeps_previous_best_effort_reply(self):
        for raw in ('{"decision":"fetch_more"}', r'{"content":"Which cafe\q?","decision":"fetch_more"'):
            with self.subTest(raw=raw):
                self.models([calls(("get_day_overview", {"day": "Friday"})), calls(("get_schedule_for_day", {"day": "Friday"}))],
                            [reply("fetch_more", "Which time suits you?", "Friday details"), OutputParserException("bad output", llm_output=raw)])
                result = await self.run_graph()
                self.assertEqual(result["response_text"], "Which time suits you?")
                self.assertEqual(result["responder_decision"], "respond")

    async def test_empty_refinement_can_be_followed_by_answer(self):
        self.models([calls(("get_day_overview", {"day": "Friday"})), calls()],
                    [reply("fetch_more", "Which time?", "Friday details"), reply(content="Still unsure; which time?")])
        result = await self.run_graph(5)
        self.assertEqual(result["response_text"], "Still unsure; which time?")
        self.assertEqual(result["context_passes"], 2)
        self.assertEqual(result["loop_outcome"], "retried")

    async def test_failed_calls_can_be_reexecuted(self):
        self.models([calls(("search_world_view", {"query": "cafe"}), ("get_day_overview", {"day": "Friday"})),
                     calls(("search_world_view", {"query": "cafe"}))],
                    [reply("fetch_more", "Which cafe?", "Cafe hours"), reply("fetch_more", "Still unsure, which cafe?", "Cafe hours")])
        queries = []

        async def fail(query, **kwargs):
            queries.append(query)
            raise RuntimeError("offline")

        with patch.object(llm, "search_worldview", fail):
            result = await self.run_graph(2)
        self.assertEqual(queries, ["cafe", "cafe"])
        self.assertEqual(result["loop_outcome"], "capped")
        self.assertEqual([o["status"] for o in result["tool_observations"]], ["error", "success", "error"])

    async def test_ambiguous_participant_names_are_visible_errors(self):
        self.models([calls(("search_user_info", {"name": "Jamie", "query": "drinks"}))], [reply()])
        result = await self.run_graph(senders={7: "Jamie", 8: " Jamie "})
        self.assertEqual(result["tool_observations"][0]["status"], "error")
        self.assertIn("ambiguous", result["tool_observations"][0]["raw_result"])

    async def test_empty_request_terminates_and_respond_clears_request(self):
        for output in (reply("fetch_more", "Which time?", " "), reply(request="obsolete request")):
            self.models([calls(("get_day_overview", {"day": "Friday"}))], [output])
            result = await self.run_graph()
            self.assertEqual(result["responder_decision"], "respond")
            self.assertEqual(result["retrieval_request"], "")
            self.assertEqual(result["context_passes"], 1)

    async def test_emergency_recursion_preserves_last_reply_and_summary(self):
        class ExhaustedGraph:
            async def astream(self, initial_state, **kwargs):
                yield {**initial_state, "context_passes": 1, "response_text": "What time?",
                       "response_reason": "Missing details", "current_summary": "fresh", "mood": "excited"}
                raise GraphRecursionError("emergency")

        with patch.object(llm, "_graph", ExhaustedGraph()):
            result = await llm.get_response(state()["history"], "old summary", chat_id=-124)
        self.addCleanup(llm._chat_mood.pop, -124, None)
        self.assertEqual(result[:3], ("What time?", "Missing details", "fresh"))
        self.assertEqual(llm.get_chat_mood(-124), "excited")

    async def test_nil_summary_and_skipped_loop_keep_public_contract(self):
        self.models([calls()], [reply()])
        self.stack.enter_context(patch.object(llm, "get_summarizer_system_prompt", AsyncMock(return_value="Summarize.")))
        self.stack.enter_context(patch.object(llm, "_get_summarizer_llm", AsyncMock(return_value=Model([llm.SummarizerOutput(summary="NIL", mood="excited")]))))
        with patch.object(llm, "summarizer_node", REAL_SUMMARIZER), patch.object(llm, "_graph", llm._build_graph()):
            result = await llm.get_response(state()["history"], "old summary", chat_id=-125, must_reply=True)
        self.addCleanup(llm._chat_mood.pop, -125, None)
        self.assertIsNone(result[2])
        self.assertEqual(llm.get_chat_mood(-125), "excited")
        passes_before = REGISTRY.get_sample_value("rachel_agent_context_passes_count")
        with patch.object(llm, "_graph", llm._build_graph()):
            result = await llm.get_response([], "old summary", must_reply=True, responded_watermark=1)
        self.assertEqual(result[:3], ("", "", None))
        self.assertEqual(REGISTRY.get_sample_value("rachel_agent_context_passes_count"), passes_before)

    async def test_public_tuple_and_mood_summary_survive_retry(self):
        self.models([calls(("get_day_overview", {"day": "Friday"})), calls(("get_schedule_for_day", {"day": "Friday"}))], [reply("fetch_more", "What time?", "Friday details"), reply()])
        outcome_before = REGISTRY.get_sample_value("rachel_agent_loop_outcomes_total", {"outcome": "retried"}) or 0
        passes_before = REGISTRY.get_sample_value("rachel_agent_context_passes_sum")
        with patch.object(llm, "_graph", llm._build_graph()):
            result = await llm.get_response(state()["history"], "old summary", chat_id=-123, must_reply=True)
        self.assertEqual(REGISTRY.get_sample_value("rachel_agent_loop_outcomes_total", {"outcome": "retried"}), outcome_before + 1)
        self.assertEqual(REGISTRY.get_sample_value("rachel_agent_context_passes_sum"), passes_before + 2)
        self.addCleanup(llm._chat_mood.pop, -123, None)
        self.assertEqual(len(result), 4)
        self.assertEqual(result[:3], ("Can meet at six!", "Grounded in context.", "updated summary"))
        self.assertGreaterEqual(result[3], 0)
        self.assertEqual(llm.get_chat_mood(-123), "excited")
        self.assertEqual(self.summary_runs, 1)


if __name__ == "__main__":
    unittest.main()
