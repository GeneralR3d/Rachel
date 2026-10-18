import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from openai import AsyncOpenAI

from app.services.llm import _base_chat
from app.services import llm, userfacts, worldview


@tool
def _lookup(query: str) -> str:
    """Look up context for a query."""
    return query


class ReplyPipelineClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_fetcher_sends_tool_schema_and_dynamic_prompt_once(self) -> None:
        requests: list[dict] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "chatcmpl-fetcher", "object": "chat.completion", "created": 0,
                "model": "deepseek/deepseek-v4.1-flash",
                "choices": [{"index": 0, "message": {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "call-1", "type": "function", "function": {
                        "name": "get_day_overview", "arguments": '{"day":"Friday"}',
                    }}],
                }, "finish_reason": "tool_calls"}],
            })

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle_request))
        openai_client = AsyncOpenAI(
            api_key="test-key", base_url="https://gateway.example/v1/openai",
            http_client=http_client,
        )
        client = _base_chat("deepseek/deepseek-v4.1-flash", 0.0, reasoning_effort="medium")
        client.async_client = openai_client.chat.completions
        client.root_async_client = openai_client

        try:
            with patch.dict(llm._client_cache, {}, clear=True), \
                    patch.object(llm, "_current_main_model", AsyncMock(return_value="deepseek/deepseek-v4.1-flash")), \
                    patch.object(llm, "_base_chat", return_value=client):
                self.assertIs(await llm._get_context_fetcher_llm(), client)
                with patch("app.calander.get_day_summary", new=AsyncMock(return_value=[])):
                    observations = []
                    await llm._run_context_fetcher({
                        "history": [{"sender": "Jamie", "content": "Free on Friday?", "telegram_message_id": 1}],
                        "senders": {7: "Jamie"}, "context_passes": 0, "retrieval_request": "",
                    }, observations)
        finally:
            await openai_client.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(observations[0]["tool_name"], "get_day_overview")
        request = requests[0]
        self.assertEqual([tool["function"]["name"] for tool in request["tools"]],
                         [tool.name for tool in llm.CONTEXT_TOOLS])
        by_name = {tool["function"]["name"]: tool["function"] for tool in request["tools"]}
        self.assertEqual(set(by_name["get_activity_at"]["parameters"]["properties"]), {"day", "hour"})
        self.assertTrue(all(tool["description"] for tool in by_name.values()))
        self.assertIn("query", by_name["search_user_info"]["parameters"]["properties"])
        for name in (
            "create_proactive_schedule", "list_proactive_schedules",
            "update_proactive_schedule", "delete_proactive_schedule",
        ):
            properties = by_name[name]["parameters"]["properties"]
            for hidden in ("chat_id", "enabled", "config", "repeat_completed"):
                self.assertNotIn(hidden, properties)
        self.assertIn("repeat_times", by_name["create_proactive_schedule"]["parameters"]["required"])
        toggle = by_name["set_proactive_schedule_enabled"]["parameters"]
        self.assertEqual(set(toggle["properties"]), {"schedule_id", "enabled"})
        self.assertEqual(set(toggle["required"]), {"schedule_id", "enabled"})
        self.assertEqual(toggle["properties"]["enabled"]["type"], "boolean")
        self.assertIn("PROACTIVE REACH-OUT SCHEDULES", request["messages"][0]["content"])
        self.assertIn("SCHEDULE\n", request["messages"][0]["content"])
        self.assertIn("WORLD-VIEW FACTS\n", request["messages"][0]["content"])
        self.assertIn("PER-USER FACTS\n", request["messages"][0]["content"])
        self.assertIn("Jamie", request["messages"][0]["content"])
        self.assertEqual(request.get("reasoning_effort"), "medium")

    async def test_responder_uses_medium_reasoning_with_json_schema_and_no_tools(self) -> None:
        requests: list[dict] = []
        outputs = iter([
            '{"decision":"respond","retrieval_request":"","content":"hey","reason":"I followed the friendly tone."}',
            '{"content":"Which cafe?","reason":"INTERNAL","retrieval_request":"INTERNAL"}',
            'Could you tell me which day?',
        ])

        def handle_request(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "deepseek/deepseek-v4.1-flash",
                    "choices": [{
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": next(outputs),
                        },
                        "finish_reason": "stop",
                    }],
                },
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle_request))
        openai_client = AsyncOpenAI(
            api_key="test-key",
            base_url="https://gateway.example/v1/openai",
            http_client=http_client,
        )
        llm.invalidate_model_clients()
        with patch("app.services.llm.get_active_models", new=AsyncMock(return_value={"main": "deepseek/deepseek-v4.1-flash"})):
            responder = await llm._get_responder_llm()
        model = responder.first.steps__["raw"].bound
        model.async_client = openai_client.chat.completions
        model.root_async_client = openai_client

        try:
            result = await responder.ainvoke([
                AIMessage(content="Rachel: an earlier persisted reply"),
                HumanMessage(content="Jamie: hi"),
            ])
            with (
                patch.object(llm, "_get_responder_llm", AsyncMock(return_value=responder)),
                patch.object(llm, "RESPONDER_SYSTEM_PROMPT", "Reply naturally."),
                patch.object(llm, "get_active_trait_prompts", AsyncMock(return_value="friendly")),
            ):
                for expected in ("Which cafe?", "Could you tell me which day?"):
                    fallback = await llm.responder_node({"history": [], "tool_observations": [], "context_passes": 1})
                    self.assertEqual(fallback["response_text"], expected)
                    self.assertEqual(fallback["responder_decision"], "respond")
                    self.assertEqual(fallback["responder_status"], "parse_fallback")
        finally:
            await openai_client.close()
            llm.invalidate_model_clients()

        self.assertEqual(result["parsed"]["content"], "hey")
        self.assertEqual(result["parsed"]["reason"], "I followed the friendly tone.")
        self.assertIsNone(result["parsing_error"])
        self.assertEqual(requests[0].get("reasoning_effort"), "medium")
        self.assertEqual(requests[0]["response_format"]["type"], "json_schema")
        self.assertNotIn("tools", requests[0])
        self.assertNotIn("reasoning_content", requests[0]["messages"][0])

    async def test_tool_request_disables_reasoning_for_replay_safe_history(
        self,
    ) -> None:
        requests: list[dict] = []

        def handle_request(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "deepseek/deepseek-v4.1-flash",
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "_lookup",
                                            "arguments": '{"query":"weather"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            )

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handle_request))
        openai_client = AsyncOpenAI(
            api_key="test-key",
            base_url="https://gateway.example/v1/openai",
            http_client=http_client,
        )
        client = _base_chat("deepseek/deepseek-v4.1-flash", 0.0)
        client.async_client = openai_client.chat.completions
        client.root_async_client = openai_client

        try:
            await client.bind_tools([_lookup]).ainvoke(
                [
                    AIMessage(content="Rachel: an earlier persisted reply"),
                    HumanMessage(content="Jamie: what is the weather?"),
                ]
            )
        finally:
            await openai_client.close()

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get("reasoning_effort"), "none")

    async def test_worldview_extractor_disables_reasoning(self) -> None:
        worldview.reset_extractor_clients()
        with patch(
            "app.repository.get_active_models",
            new=AsyncMock(return_value={"main": "deepseek/deepseek-v4.1-flash"}),
        ):
            client = await worldview._get_extractor_llm()

        self.assertEqual(client.first.bound.reasoning_effort, "none")

    async def test_userfacts_extractors_disable_reasoning(self) -> None:
        userfacts.reset_extractor_clients()
        with patch(
            "app.repository.get_active_models",
            new=AsyncMock(return_value={"main": "deepseek/deepseek-v4.1-flash"}),
        ):
            facts_client = await userfacts._get_extractor_llm()
            profile_client = await userfacts._get_profile_extractor_llm()

        self.assertEqual(facts_client.first.bound.reasoning_effort, "none")
        self.assertEqual(profile_client.first.bound.reasoning_effort, "none")


if __name__ == "__main__":
    unittest.main()
