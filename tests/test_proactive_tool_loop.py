"""Real graph/tool integration with model and persistence boundaries controlled."""

import asyncio
from contextlib import ExitStack
import json
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage

from app import repository
from app.services import llm, proactive
from app.telegram import client as telegram
from test_proactive_tools import create_args, schedule_row


def tool_call(name, args):
    return AIMessage(content="", tool_calls=[{"id": "call-1", "name": name, "args": args}])


def response(decision="respond", request=""):
    return SimpleNamespace(
        decision=decision, retrieval_request=request,
        content="Okay, I'll check in!", reason="Follow up on the exam.",
    )


class Model:
    def __init__(self, results):
        self.results = iter(results)
        self.messages = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        self.messages.append(messages)
        result = next(self.results)
        if isinstance(result, Exception):
            raise result
        return result


class ProactiveLoopTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("builtins.print"))
        self.stack.enter_context(patch.object(llm, "get_active_trait_prompts", AsyncMock(return_value="friendly")))
        self.stack.enter_context(patch.object(llm, "summarizer_node", AsyncMock(return_value={
            "current_summary": "Exam on Monday", "mood": "default",
        })))
        self.stack.enter_context(patch.object(telegram.client, "get_entity", AsyncMock()))
        self.stack.enter_context(patch("app.services.proactive._scheduler", None))
        self.stack.enter_context(patch.object(llm, "_graph", llm._build_graph()))
        self.stack.enter_context(patch.object(llm, "_proactive_graph", llm._build_proactive_graph()))
        self.history = [{"sender": "Jamie", "content": "Remind me tomorrow at 10am", "telegram_message_id": 5}]

    def models(self, fetcher_results, responder_results):
        fetcher, responder = Model(fetcher_results), Model(responder_results)
        self.stack.enter_context(patch.object(llm, "_get_context_fetcher_llm", AsyncMock(return_value=fetcher)))
        self.stack.enter_context(patch.object(llm, "_get_responder_llm", AsyncMock(return_value=responder)))
        return fetcher, responder

    async def test_list_then_update_and_delete_reaches_responder_with_scoped_results(self):
        for operation in ("update", "delete"):
            with self.subTest(operation=operation):
                name = f"{operation}_proactive_schedule"
                args = {"schedule_id": 1}
                if operation == "update":
                    args.update(create_args(cron_expression="0 11 * * *"))
                _, responder = self.models([
                    tool_call("list_proactive_schedules", {}), tool_call(name, args),
                ], [response("fetch_more", f"{operation} Jamie's listed reminder"), response()])
                with patch.object(repository, "get_proactive_schedules", AsyncMock(return_value=[schedule_row()])), \
                        patch.object(repository, "update_proactive_schedule", AsyncMock(return_value=schedule_row(cron_expression="0 11 * * *"))) as update, \
                        patch.object(repository, "delete_proactive_schedule", AsyncMock(return_value=True)) as delete:
                    await llm.get_response(self.history, chat_id=-123, must_reply=True)
                prompt = json.loads(responder.messages[-1][2].content.split("\n", 1)[1])["tool_results"]
                self.assertIn(name, prompt)
                if operation == "update":
                    self.assertEqual(update.await_args.kwargs["chat_id"], -123)
                    self.assertIn("0 11 * * *", prompt)
                else:
                    self.assertEqual(delete.await_args.kwargs["chat_id"], -123)
                    self.assertIn('"deleted": true', prompt)

    async def test_requested_tool_calls_execute_on_each_context_pass(self):
        self.models([
            tool_call("create_proactive_schedule", create_args()),
            tool_call("create_proactive_schedule", create_args()),
        ], [response("fetch_more", "Confirm the reminder is saved"), response()])
        with patch.object(repository, "create_proactive_schedule", AsyncMock(return_value=schedule_row())) as create:
            await llm.get_response(self.history, chat_id=-123, must_reply=True)
        self.assertEqual(create.await_count, 2)

    async def test_list_then_toggle_reaches_responder_with_saved_enabled_state(self):
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                _, responder = self.models([
                    tool_call("list_proactive_schedules", {}),
                    tool_call("set_proactive_schedule_enabled", {"schedule_id": 1, "enabled": enabled}),
                ], [response("fetch_more", "Pause or resume Jamie's listed reminder"), response()])

                async def save(schedule_id, data, *, chat_id):
                    self.assertEqual((schedule_id, chat_id), (1, -123))
                    self.assertEqual(data, {"enabled": enabled})
                    return schedule_row(**data)

                with patch.object(repository, "get_proactive_schedules", AsyncMock(return_value=[schedule_row(enabled=not enabled)])), \
                        patch.object(repository, "update_proactive_schedule", side_effect=save):
                    await llm.get_response(self.history, chat_id=-123, must_reply=True)
                prompt = json.loads(responder.messages[-1][2].content.split("\n", 1)[1])["tool_results"]
                self.assertIn("set_proactive_schedule_enabled", prompt)
                self.assertIn(f'"enabled": {json.dumps(enabled)}', prompt)

    async def test_each_reply_supplies_only_its_current_chat(self):
        fetcher, _ = self.models([
            tool_call("create_proactive_schedule", create_args()),
            tool_call("create_proactive_schedule", create_args()),
        ], [response(), response()])
        with patch.object(repository, "create_proactive_schedule", AsyncMock(return_value=schedule_row())) as create:
            for chat_id in (-123, -456):
                await llm.get_response(self.history, chat_id=chat_id, must_reply=True)
        self.assertEqual([call.args[0]["chat_id"] for call in create.await_args_list], [-123, -456])
        second_handoff = json.loads(fetcher.messages[1][1].content.split("\n", 1)[1])
        self.assertEqual(second_handoff["previous_observations"], [])

    async def test_timed_out_action_is_reported_to_responder_as_unconfirmed(self):
        _, responder = self.models([
            tool_call("create_proactive_schedule", create_args()),
        ], [response()])
        writes = []
        release = asyncio.Event()

        async def uncertain_write(data):
            writes.append(data)
            await release.wait()
            return schedule_row(**data)

        with patch.object(repository, "create_proactive_schedule", side_effect=uncertain_write), \
                patch.object(llm, "CONTEXT_FETCHER_TIMEOUT", 0.05):
            try:
                await llm.get_response(self.history, chat_id=-123, must_reply=True)
            finally:
                release.set()
                await asyncio.gather(*list(proactive._active_tasks), return_exceptions=True)
        self.assertEqual(len(writes), 1)
        context = json.loads(responder.messages[-1][2].content.split("\n", 1)[1])
        observation = json.loads(context["retrieval_status"])
        self.assertEqual(observation["status"], "timeout")
        self.assertIn("No proactive schedule action or listing has been confirmed", context["tool_results"])

    async def test_proactive_graph_also_supplies_chat_scope_and_intent(self):
        fetcher, _ = self.models([tool_call("create_proactive_schedule", create_args())], [response()])
        with patch.object(repository, "create_proactive_schedule", AsyncMock(return_value=schedule_row())) as create:
            await llm.get_proactive_response(self.history, "Set a follow-up for the exam", chat_id=-123)
        self.assertEqual(create.await_args.args[0]["chat_id"], -123)
        self.assertIn("Set a follow-up for the exam", fetcher.messages[0][0].content)

    async def test_tool_error_is_not_presented_as_successful_schedule_context(self):
        _, responder = self.models([tool_call("delete_proactive_schedule", {"schedule_id": 2})], [response()])
        with patch.object(repository, "delete_proactive_schedule", AsyncMock(return_value=False)):
            await llm.get_response(self.history, chat_id=-123, must_reply=True)
        context = json.loads(responder.messages[-1][2].content.split("\n", 1)[1])
        observation = json.loads(context["retrieval_status"])
        self.assertEqual(observation["status"], "error")
        self.assertIn("not found in the current chat", observation["raw_result"])
        self.assertNotIn('"deleted": true', responder.messages[-1][0].content)
