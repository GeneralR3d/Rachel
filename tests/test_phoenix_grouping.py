import asyncio
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from langgraph.graph import END, START, StateGraph
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace.status import StatusCode
from openinference.instrumentation import get_attributes_from_context
from openinference.instrumentation.langchain import LangChainInstrumentor

from app.services import llm, memory
from app.services.tracing import traced_workflow
from app.telegram import client as telegram_client


class PhoenixGroupingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.provider = TracerProvider()
        self.provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.tracer = self.provider.get_tracer("test-rachel")
        self.tracer_patch = patch("opentelemetry.trace.get_tracer", return_value=self.tracer)
        self.tracer_patch.start()

    def tearDown(self):
        self.tracer_patch.stop()
        self.provider.shutdown()

    async def test_retry_and_parallel_children_share_one_response_trace(self):
        calls = 0

        async def fake_response(**kwargs):
            nonlocal calls
            calls += 1
            with self.tracer.start_as_current_span("graph"):
                async def branch(name):
                    with self.tracer.start_as_current_span(name):
                        self.assertEqual(trace.get_current_span().get_span_context().is_valid, True)
                await asyncio.gather(branch("summarizer"), branch("context_fetcher"))
                if calls == 1:
                    raise ValueError("first attempt failed")
            return "hello", "reason", None, 1.0

        with patch.object(telegram_client, "get_response", side_effect=fake_response), patch("builtins.print"):
            result = await telegram_client._generate_response_with_retry({}, "burst-1", 42, "Study Group")

        self.assertEqual(result[0], "hello")
        spans = self.exporter.get_finished_spans()
        roots = [span for span in spans if span.parent is None]
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].name, "telegram_reply")
        self.assertEqual(roots[0].attributes["session.id"], "burst-1")
        self.assertEqual(json.loads(roots[0].attributes["metadata"]), {
            "telegram_chat_id": 42, "telegram_chat_name": "Study Group"
        })
        self.assertEqual({span.name for span in spans}, {"telegram_reply", "graph", "summarizer", "context_fetcher"})
        self.assertEqual({span.context.trace_id for span in spans}, {roots[0].context.trace_id})

    async def test_memory_branches_have_one_root_and_the_burst_session(self):
        async def branch(name, *args):
            with self.tracer.start_as_current_span(name):
                await asyncio.sleep(0)

        conversation = [{"telegram_message_id": 1, "sender": "Jamie", "content": "Hi"}]
        async def worldview(*args):
            await branch("worldview", *args)

        async def userfacts(*args):
            await branch("userfacts", *args)

        with patch.object(memory, "update_worldview", new=worldview), patch.object(
            memory, "update_user_facts", new=userfacts
        ):
            await memory.update_memories(conversation, chat_id=42, session_id="burst-1", chat_name="Study Group")

        spans = self.exporter.get_finished_spans()
        roots = [span for span in spans if span.parent is None]
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0].name, "memory_update")
        self.assertEqual(roots[0].attributes["session.id"], "burst-1")
        self.assertEqual(json.loads(roots[0].attributes["metadata"]), {
            "telegram_chat_id": 42, "telegram_chat_name": "Study Group"
        })
        self.assertEqual({span.name for span in spans}, {"memory_update", "worldview", "userfacts"})
        self.assertEqual({span.context.trace_id for span in spans}, {roots[0].context.trace_id})

    async def test_reply_graph_has_a_stable_name(self):
        fake_graph = AsyncMock()
        fake_graph.ainvoke.return_value = {"mood": "default", "current_summary": "", "response_text": "hi", "response_reason": "ok"}
        with patch.object(llm, "_graph", fake_graph):
            await llm.get_response([], current_summary="")
        self.assertEqual(fake_graph.ainvoke.call_args.kwargs["config"]["run_name"], "RachelReplyGraph")

    async def test_workflow_starts_a_new_trace_even_with_an_active_parent(self):
        with self.tracer.start_as_current_span("unrelated_event"):
            with traced_workflow("telegram_reply", "burst-1"):
                with self.tracer.start_as_current_span("graph"):
                    pass
        spans = {span.name: span for span in self.exporter.get_finished_spans()}
        self.assertNotEqual(spans["unrelated_event"].context.trace_id, spans["telegram_reply"].context.trace_id)
        self.assertEqual(spans["graph"].context.trace_id, spans["telegram_reply"].context.trace_id)

    async def test_chat_metadata_is_available_to_instrumented_child_spans(self):
        with traced_workflow("telegram_reply", "burst-1", chat_id=-42, chat_name=None):
            attributes = dict(get_attributes_from_context())
        self.assertEqual(json.loads(attributes["metadata"]), {"telegram_chat_id": -42})
        root = self.exporter.get_finished_spans()[0]
        self.assertEqual(json.loads(root.attributes["metadata"]), {"telegram_chat_id": -42})

    async def test_jev_decision_is_visible_in_reply_trace(self):
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            200,
            json={
                "object": "decision", "model": "jev-1.13.0", "vendor": "typesafe",
                "answers": {"should_reply": {"type": "noul", "noul": 0.69}},
                "usage": {"input_tokens": 20, "output_tokens": 1, "total_tokens": 21, "cost": 0.002},
            },
        )))
        try:
            with patch("httpx.AsyncClient", return_value=client), traced_workflow("telegram_reply", "burst-1"):
                result = await llm.router_node({
                    "history": [{"sender": "Alex", "content": "Thanks"}],
                    "current_summary": "Planning dinner", "responded_watermark": None,
                    "is_private": False,
                })
        finally:
            await client.aclose()

        self.assertEqual(result, {"should_reply": False})
        spans = {span.name: span for span in self.exporter.get_finished_spans()}
        self.assertEqual(len(spans), 2)
        root, decision = spans["telegram_reply"], spans["JEV decision"]
        self.assertEqual(decision.parent.span_id, root.context.span_id)
        self.assertEqual(decision.context.trace_id, root.context.trace_id)
        attrs = decision.attributes
        self.assertEqual(attrs["openinference.span.kind"], "LLM")
        self.assertEqual(attrs["llm.model_name"], "jev-1.13.0")
        self.assertEqual(attrs["llm.request.model_name"], llm.settings.decisions_llm)
        self.assertEqual(attrs["llm.response.model_name"], "jev-1.13.0")
        self.assertEqual(attrs["llm.provider"], "typesafe")
        self.assertEqual(attrs["input.mime_type"], "application/json")
        traced_input = json.loads(attrs["input.value"])
        self.assertIn("Planning dinner", traced_input["state"])
        self.assertEqual(traced_input["questions"]["should_reply"], llm.ROUTER_GROUP_QUESTION)
        self.assertEqual(json.loads(attrs["output.value"]), {"probability": 0.69, "should_reply": False})
        self.assertEqual(attrs["llm.token_count.prompt"], 20)
        self.assertEqual(attrs["llm.token_count.completion"], 1)
        self.assertEqual(attrs["llm.token_count.total"], 21)
        self.assertEqual(attrs["llm.cost.total"], 0.002)

    async def test_jev_decision_is_nested_under_instrumented_router(self):
        instrumentor = LangChainInstrumentor()
        instrumentor.instrument(tracer_provider=self.provider)
        self.addCleanup(instrumentor.uninstrument)
        graph = StateGraph(llm.GraphState)
        graph.add_node("router_node", llm.router_node)
        graph.add_edge(START, "router_node")
        graph.add_edge("router_node", END)
        compiled = graph.compile()

        for status, body, should_reply in (
            (200, {"answers": {"should_reply": {"type": "noul", "noul": 0.69}}}, False),
            (200, {"answers": {"should_reply": {"type": "noul", "noul": 0.8}}}, True),
            (503, {"error": "upstream unavailable"}, True),
            (200, {"answers": {"should_reply": {"type": "noul", "noul": 1.5}}}, True),
        ):
            with self.subTest(status=status, body=body):
                self.exporter.clear()
                client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, json=body)
                ))
                try:
                    with patch("httpx.AsyncClient", return_value=client), traced_workflow("telegram_reply", "burst-1"):
                        result = await compiled.ainvoke({
                            "history": [{"sender": "Alex", "content": "Thanks"}],
                            "current_summary": None, "responded_watermark": None,
                            "is_private": False,
                        }, config={"run_name": "RachelReplyGraph"})
                        self.assertEqual(trace.get_current_span().name, "telegram_reply")
                finally:
                    await client.aclose()

                self.assertEqual(result["should_reply"], should_reply)
                spans = {span.name: span for span in self.exporter.get_finished_spans()}
                root, graph_span, router, decision = (
                    spans[name] for name in ("telegram_reply", "RachelReplyGraph", "router_node", "JEV decision")
                )
                self.assertIsNone(root.parent)
                self.assertEqual(graph_span.parent.span_id, root.context.span_id)
                self.assertEqual(router.parent.span_id, graph_span.context.span_id)
                self.assertEqual(decision.parent.span_id, router.context.span_id)
                self.assertEqual({span.context.trace_id for span in spans.values()}, {root.context.trace_id})
                self.assertEqual(router.status.status_code, StatusCode.OK)
                if status == 503 or body["answers"]["should_reply"]["noul"] == 1.5:
                    self.assertEqual(decision.status.status_code, StatusCode.ERROR)
                    self.assertEqual(json.loads(decision.attributes["output.value"]), {"should_reply": True})
                    self.assertIn("exception", [event.name for event in decision.events])
                else:
                    self.assertNotEqual(decision.status.status_code, StatusCode.ERROR)
                    self.assertEqual(json.loads(decision.attributes["output.value"]), {
                        "probability": body["answers"]["should_reply"]["noul"], "should_reply": should_reply,
                    })

    async def test_jev_http_and_answer_errors_are_traced_while_router_fails_open(self):
        for status, body in (
            (503, {"error": "upstream unavailable"}),
            (200, {"answers": {"should_reply": {"type": "noul", "noul": 1.5}}}),
        ):
            with self.subTest(status=status):
                self.exporter.clear()
                client = httpx.AsyncClient(transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, json=body)
                ))
                try:
                    with patch("httpx.AsyncClient", return_value=client), traced_workflow("telegram_reply", "burst-1"):
                        result = await llm.router_node({
                            "history": [{"sender": "Alex", "content": "Thanks"}],
                            "current_summary": None, "responded_watermark": None,
                            "is_private": False,
                        })
                finally:
                    await client.aclose()

                self.assertEqual(result, {"should_reply": True})
                spans = {span.name: span for span in self.exporter.get_finished_spans()}
                self.assertEqual(set(spans), {"telegram_reply", "JEV decision"})
                self.assertEqual(spans["JEV decision"].parent.span_id, spans["telegram_reply"].context.span_id)
                self.assertEqual(spans["JEV decision"].status.status_code, StatusCode.ERROR)
                self.assertEqual(json.loads(spans["JEV decision"].attributes["output.value"]), {"should_reply": True})
                self.assertIn("exception", [event.name for event in spans["JEV decision"].events])

    async def test_chat_display_name_uses_group_title_or_user_name(self):
        self.assertEqual(
            telegram_client._chat_display_name(SimpleNamespace(title="Study Group")), "Study Group"
        )
        self.assertEqual(
            telegram_client._chat_display_name(SimpleNamespace(first_name="Jamie", last_name="Lee")), "Jamie Lee"
        )
        self.assertEqual(
            telegram_client._chat_display_name(SimpleNamespace(username="jamie")), "@jamie"
        )
        self.assertIsNone(telegram_client._chat_display_name(None))

    async def test_reply_uses_name_from_current_event(self):
        chat_id = 998875
        telegram_client.current_messages_buffer[chat_id] = [
            telegram_client.BufferedMessage(
                telegram_message_id=1, sender_user_id=7, sender_name="Jamie", content="Hi"
            )
        ]
        telegram_client.pending_summaries[chat_id] = ""
        event = SimpleNamespace(
            chat_id=chat_id, chat=SimpleNamespace(title="Renamed Group"),
            is_private=False, mentioned=False,
        )

        @asynccontextmanager
        async def typing(*args):
            yield

        try:
            with patch.object(telegram_client.client, "get_me", AsyncMock(return_value=SimpleNamespace(id=99))), patch.object(
                telegram_client.client, "is_connected", return_value=True
            ), patch.object(telegram_client.client, "action", side_effect=typing), patch.object(
                telegram_client, "_generate_response_with_retry", AsyncMock(return_value=("", "", None, 0.0))
            ) as generate, patch("builtins.print"):
                await telegram_client._reply(event)
            self.assertEqual(generate.call_args.args[3], "Renamed Group")
        finally:
            for state in (
                telegram_client.current_messages_buffer, telegram_client.pending_summaries,
                telegram_client.responded_watermark,
            ):
                state.pop(chat_id, None)

    async def test_memory_graphs_have_distinct_names(self):
        from app.services import worldview, userfacts

        world_graph = AsyncMock()
        world_graph.ainvoke.return_value = {"extracted_facts": []}
        user_graph = AsyncMock()
        with patch.object(worldview, "_graph", world_graph), patch.object(userfacts, "_graph", user_graph), patch("builtins.print"):
            await worldview.update_worldview(["Hi"], True, 42)
            await userfacts.update_user_facts(
                [{"sender": "Jamie", "sender_user_id": 7, "content": "Hi"}], ["Hi"], True, chat_id=42
            )
        self.assertEqual(world_graph.ainvoke.call_args.kwargs["config"]["run_name"], "WorldviewMemoryGraph")
        self.assertEqual(user_graph.ainvoke.call_args.kwargs["config"]["run_name"], "UserFactsMemoryGraph")

    async def test_burst_session_reuses_id_then_rotates_after_flush(self):
        chat_id = 998877
        sender = SimpleNamespace(id=7, first_name="Jamie", user_name="jamie", last_name=None, username="jamie")

        def event(message_id, title="Study Group"):
            return SimpleNamespace(
                chat_id=chat_id, raw_text="Hi", sender_id=7,
                message=SimpleNamespace(id=message_id), is_private=True,
                mentioned=False, get_sender=AsyncMock(return_value=sender),
                chat=SimpleNamespace(title=title),
            )

        def discard_task(coro):
            coro.close()
            return Mock(done=Mock(return_value=False), cancel=Mock())

        try:
            with patch.object(telegram_client.client, "get_me", AsyncMock(return_value=SimpleNamespace(id=99))), patch.object(
                telegram_client, "get_history", AsyncMock(return_value=[])
            ), patch.object(telegram_client, "upsert_user", AsyncMock()), patch.object(
                telegram_client, "_message_content", return_value="Hi"
            ), patch.object(telegram_client, "_handle_group_speech_control", return_value="muted"), patch.object(
                telegram_client.asyncio, "create_task", side_effect=discard_task
            ), patch.object(telegram_client, "add_history_batch", AsyncMock()), patch.object(
                telegram_client, "finalize_conversation", new_callable=AsyncMock
            ) as finalize, patch("builtins.print"):
                await telegram_client.new_message(event(1))
                first = telegram_client.burst_sessions[chat_id]
                self.assertEqual(finalize.call_args.args, (chat_id, telegram_client.CHAT_BLACKOUT_TIME, "Study Group"))
                await telegram_client.new_message(event(2, "Renamed Group"))
                self.assertEqual(finalize.call_args.args, (chat_id, telegram_client.CHAT_BLACKOUT_TIME, "Renamed Group"))
                self.assertEqual(telegram_client.burst_sessions[chat_id], first)
                await telegram_client._flush_chat(chat_id)
                self.assertNotIn(chat_id, telegram_client.burst_sessions)
                await telegram_client.new_message(event(3))
                self.assertNotEqual(telegram_client.burst_sessions[chat_id], first)
        finally:
            for state in (
                telegram_client.current_messages_buffer, telegram_client.burst_sessions,
                telegram_client.flush_tasks, telegram_client.wait_tasks,
                telegram_client.last_message_time,
            ):
                state.pop(chat_id, None)

    async def test_memory_task_keeps_closed_burst_session(self):
        chat_id = 998876
        telegram_client.burst_sessions[chat_id] = "closed-burst"
        telegram_client.current_messages_buffer[chat_id] = [
            telegram_client.BufferedMessage(
                telegram_message_id=1, sender_user_id=7, sender_name="Jamie", content="Hi"
            )
        ]
        captured = []

        def capture_task(coro):
            captured.append(coro)
            return Mock()

        try:
            with patch.object(telegram_client.asyncio, "sleep", AsyncMock()), patch.object(
                telegram_client, "get_summary", AsyncMock(return_value="")
            ), patch.object(telegram_client, "get_last_processed_message_id", AsyncMock(return_value=0)), patch.object(
                telegram_client, "add_history_batch", AsyncMock()
            ), patch.object(telegram_client, "set_last_processed_message_id", AsyncMock()), patch.object(
                telegram_client, "update_memories", AsyncMock()
            ) as update, patch.object(telegram_client.asyncio, "create_task", side_effect=capture_task), patch("builtins.print"):
                await telegram_client.finalize_conversation(chat_id, 0, "Study Group")
                self.assertNotIn(chat_id, telegram_client.burst_sessions)
                self.assertEqual(len(captured), 1)
                await captured[0]
                self.assertEqual(update.call_args.kwargs["session_id"], "closed-burst")
                self.assertEqual(update.call_args.kwargs["chat_name"], "Study Group")
        finally:
            for state in (
                telegram_client.current_messages_buffer, telegram_client.burst_sessions,
                telegram_client.pending_summaries, telegram_client.last_message_time,
            ):
                state.pop(chat_id, None)
            for coro in captured:
                if coro.cr_await is None and coro.cr_frame is not None:
                    coro.close()


if __name__ == "__main__":
    unittest.main()
