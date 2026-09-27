import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from openai import AsyncOpenAI

from app.services.llm import _base_chat
from app.services import userfacts, worldview


@tool
def _lookup(query: str) -> str:
    """Look up context for a query."""
    return query


class ReplyPipelineClientTests(unittest.IsolatedAsyncioTestCase):
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
