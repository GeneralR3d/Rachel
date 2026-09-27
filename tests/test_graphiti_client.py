import json
import unittest

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel

from app.services.graphiti import _RetryingOpenAIGenericClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.prompts.models import Message


class _StructuredReply(BaseModel):
    value: str


class GraphitiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_guided_json_does_not_request_gateway_structured_output(
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
                                "content": '{"value":"ok"}',
                            },
                            "finish_reason": "stop",
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
        client = _RetryingOpenAIGenericClient(
            config=LLMConfig(model="deepseek/deepseek-v4.1-flash"),
            client=openai_client,
            structured_output_mode="json_object",
        )

        try:
            result = await client.generate_response(
                [
                    Message(role="system", content="Extract the value."),
                    Message(role="user", content="The value is ok."),
                ],
                response_model=_StructuredReply,
            )
        finally:
            await openai_client.close()

        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(len(requests), 1)
        self.assertNotIn("response_format", requests[0])
        self.assertIn('"value"', requests[0]["messages"][-1]["content"])


if __name__ == "__main__":
    unittest.main()
