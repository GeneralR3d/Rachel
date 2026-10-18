import json
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError
from tenacity import wait_none

from app.services.graphiti import _RetryingOpenAIGenericClient
from graphiti_core.llm_client.client import LLMClient
from graphiti_core.llm_client.config import LLMConfig
from graphiti_core.prompts.dedupe_edges import EdgeDuplicate
from graphiti_core.prompts.models import Message


class _StructuredReply(BaseModel):
    value: str


@asynccontextmanager
async def _client_with_responses(contents):
    requests = []
    outputs = iter(contents)

    def handle_request(request):
        requests.append(json.loads(request.content))
        content = next(outputs)
        if isinstance(content, httpx.Response):
            return content
        return httpx.Response(200, json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle_request)) as http_client:
        async with AsyncOpenAI(
            api_key="test-key",
            base_url="https://gateway.example/v1/openai",
            http_client=http_client,
            max_retries=0,
        ) as openai_client:
            client = _RetryingOpenAIGenericClient(
                config=LLMConfig(model="test-model"),
                client=openai_client,
                structured_output_mode="json_object",
            )
            # Exercise the real retry policy without its production backoff delays.
            with patch.object(LLMClient._generate_response_with_retry.retry, "wait", wait_none()):
                yield client, requests


class GraphitiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_edge_duplicate_retry_uses_integer_array_schema(self):
        async with _client_with_responses([
            '{"duplicate_facts":[{"idx":0}],"contradicted_facts":[]}',
            '{"duplicate_facts":[0],"contradicted_facts":[]}',
        ]) as (client, requests):
            result = await client.generate_response(
                [Message(role="user", content="Find duplicate facts.")],
                response_model=EdgeDuplicate,
            )

        self.assertEqual(result, {"duplicate_facts": [0], "contradicted_facts": []})
        self.assertEqual(len(requests), 2)
        correction = requests[1]["messages"][-1]["content"]
        self.assertIn('"duplicate_facts"', correction)
        self.assertIn('"type": "integer"', correction)
        self.assertNotIn("Every array element must be a JSON object", correction)

    async def test_extra_json_gets_corrective_feedback_before_retry(self):
        async with _client_with_responses([
            '{"value":"first"}\n{"value":"second"}',
            '{"value":"ok"}',
        ]) as (client, requests):
            result = await client.generate_response(
                [Message(role="user", content="Extract the value.")],
                response_model=_StructuredReply,
            )

        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[1]["messages"][:-1], requests[0]["messages"])
        correction = requests[1]["messages"][-1]["content"]
        self.assertIn("Extra data", correction)
        self.assertIn("ONE JSON object", correction)
        for request in requests:
            self.assertNotIn("response_format", request)

    async def test_invalid_json_exhaustion_does_not_restart_format_retries(self):
        async with _client_with_responses(['{"value":"ok"} trailing'] * 4) as (client, requests):
            with self.assertRaises(ValueError) as caught:
                await client.generate_response(
                    [Message(role="user", content="Extract the value.")],
                    response_model=_StructuredReply,
                )

        self.assertEqual(len(requests), 4)
        self.assertNotIsInstance(caught.exception, json.JSONDecodeError)
        self.assertIsInstance(caught.exception.__cause__, json.JSONDecodeError)
        self.assertIn("4 attempts", str(caught.exception))

    async def test_json_and_schema_errors_share_corrective_retry_budget(self):
        async with _client_with_responses([
            '{"value":"first"} {"value":"second"}',
            '{"value":[]}',
            '{"value":',
            '{"value":"ok"}',
        ]) as (client, requests):
            result = await client.generate_response(
                [Message(role="user", content="Extract the value.")],
                response_model=_StructuredReply,
            )

        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(len(requests), 4)
        self.assertEqual([len(r["messages"]) for r in requests], [1, 2, 3, 4])

    async def test_schema_exhaustion_preserves_downstream_validation_failure(self):
        async with _client_with_responses(['{"value":[]}'] * 4) as (client, requests):
            result = await client.generate_response(
                [Message(role="user", content="Extract the value.")],
                response_model=_StructuredReply,
            )

        self.assertEqual(len(requests), 4)
        with self.assertRaises(ValidationError):
            _StructuredReply.model_validate(result)

    async def test_rate_limit_still_uses_transport_retry(self):
        async with _client_with_responses([
            httpx.Response(429, json={"error": {"message": "Rate limited", "type": "rate_limit"}}),
            '{"value":"ok"}',
        ]) as (client, requests):
            result = await client.generate_response(
                [Message(role="user", content="Extract the value.")],
                response_model=_StructuredReply,
            )

        self.assertEqual(result, {"value": "ok"})
        self.assertEqual(len(requests), 2)

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
