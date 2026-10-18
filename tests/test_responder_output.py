import asyncio
import json
import unittest
from contextlib import ExitStack, redirect_stderr
from io import StringIO
from unittest.mock import AsyncMock, patch

import httpx
from openai import AsyncOpenAI
from openai import BadRequestError

from app.services import llm
from app.services.metrics import LLM_CALLS, LLM_ERRORS


def tool_message(arguments):
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{
            "id": "call-response", "type": "function",
            "function": {"name": "ResponseOutput", "arguments": arguments},
        }],
    }


VALID_MESSAGE = {"role": "assistant", "content": json.dumps({
    "decision": "respond", "retrieval_request": "",
    "content": "Hey, how was your day?", "reason": "Following the check-in intent.",
})}


class ResponderOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.messages = [VALID_MESSAGE]
        self.requests = []
        self.status = 200

        def handle_request(request):
            self.requests.append(json.loads(request.content))
            if self.status != 200:
                return httpx.Response(self.status, json={"error": {"message": "Bad request", "type": "invalid_request_error"}})
            message = self.messages.pop(0) if len(self.messages) > 1 else self.messages[0]
            return httpx.Response(200, json={
                "id": "chatcmpl-test", "object": "chat.completion", "created": 0,
                "model": "test-model", "choices": [{
                    "index": 0, "message": message,
                    "finish_reason": "tool_calls" if message.get("tool_calls") else "stop",
                }],
            })

        self.openai = AsyncOpenAI(
            api_key="test-key", base_url="https://gateway.example/v1/openai",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle_request)),
        )
        client = llm._base_chat("test-model", 0.2, reasoning_effort="medium")
        client.async_client = self.openai.chat.completions
        client.root_async_client = self.openai
        llm._client_cache.clear()
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(llm, "BOT_NAME", "Rachel"))
        self.stack.enter_context(patch.object(llm, "_base_chat", return_value=client))
        self.stack.enter_context(patch.object(llm, "get_active_models", AsyncMock(return_value={"main": "test-model"})))
        self.stack.enter_context(patch.object(llm, "RESPONDER_SYSTEM_PROMPT", "You are Rachel. {intent}"))
        self.stack.enter_context(patch.object(llm, "get_active_trait_prompts", AsyncMock(return_value="Friendly")))
        self.stack.enter_context(patch.object(llm, "_run_context_fetcher", AsyncMock()))
        self.stack.enter_context(patch("builtins.print"))
        self.stack.enter_context(patch.object(llm, "pprint"))
        self.stack.enter_context(redirect_stderr(StringIO()))

    async def asyncTearDown(self):
        self.stack.close()
        llm._client_cache.clear()
        await self.openai.close()

    async def outreach(self):
        return await llm.get_proactive_response([], "Check in about their day")

    async def test_invalid_output_falls_back_once_and_counts_failure(self):
        for message, kind in (
            (tool_message("{}"), "output_parse"),
            ({"role": "assistant", "content": "{}"}, "response_validation"),
        ):
            with self.subTest(message=message):
                self.requests.clear()
                self.messages = [message, VALID_MESSAGE]
                errors = LLM_ERRORS.labels(node="responder", kind=kind)._value.get()
                calls = LLM_CALLS.labels(node="responder")._value.get()
                content, reason, _ = await self.outreach()
                self.assertTrue(content.strip())
                self.assertIn("parse failure", reason)
                self.assertEqual(len(self.requests), 1)
                self.assertEqual(LLM_CALLS.labels(node="responder")._value.get() - calls, 1)
                self.assertEqual(LLM_ERRORS.labels(node="responder", kind=kind)._value.get() - errors, 1)
                self.assertIn("Check in about their day", self.requests[0]["messages"][0]["content"])

    async def test_malformed_payloads_never_leak_internal_fields(self):
        for body, expected in (
            ('{"reason":"INTERNAL"}', None),
            ('{"content":"Hey!"}', "Hey!"),
            ('{"reason":"INTERNAL","content":"   "}', None),
            ('{"reason":', None),
            ('{"reason":"INTERNAL","content":"Hey!","extra":true}', "Hey!"),
            ('"Hey!"', "Hey!"),
            ("null", None), ("true", None), ("42", None), ("[]", None),
            ("", None), ("{}", None),
            ('{"decision":"fetch_more","retrieval_request":"INTERNAL","content":"Hey!"}', "Hey!"),
        ):
            with self.subTest(body=body):
                self.requests.clear()
                self.messages = [{"role": "assistant", "content": body}, VALID_MESSAGE]
                content, _, _ = await self.outreach()
                self.assertTrue(content.strip())
                self.assertNotIn("INTERNAL", content)
                self.assertNotIn("retrieval_request", content)
                if expected is not None:
                    self.assertEqual(content, expected)
                self.assertEqual(len(self.requests), 1)

    async def test_truncated_and_fenced_json_recover_only_visible_content(self):
        for body, expected in (
            ('{"reason":"INTERNAL","content":"Hey, how was', None),
            ('{"reason":"INTERNAL","content":"Hey!"', "Hey!"),
            ('```json\n{"reason":"INTERNAL","content":"Hey!"}\n```', "Hey!"),
        ):
            with self.subTest(body=body):
                self.requests.clear()
                self.messages = [{"role": "assistant", "content": body}, VALID_MESSAGE]
                content, _, _ = await self.outreach()
                self.assertTrue(content.strip())
                self.assertNotIn("INTERNAL", content)
                self.assertNotIn("{", content)
                if expected:
                    self.assertEqual(content, expected)
                self.assertEqual(len(self.requests), 1)

    async def test_plain_prose_is_salvaged_without_an_extra_model_call(self):
        self.messages = [{"role": "assistant", "content": "Rachel: Hey, how was your day?"}]
        content, _, _ = await self.outreach()
        self.assertEqual(content, "Hey, how was your day?")
        self.assertEqual(len(self.requests), 1)

    async def test_valid_response_needs_only_one_request(self):
        content, _, _ = await self.outreach()
        self.assertEqual(content, "Hey, how was your day?")
        self.assertEqual(len(self.requests), 1)

    async def test_responder_requests_strict_json_schema_without_tools(self):
        await self.outreach()
        request = self.requests[0]
        self.assertNotIn("tools", request)
        self.assertNotIn("tool_choice", request)
        self.assertNotIn("parallel_tool_calls", request)
        self.assertEqual(request["response_format"]["type"], "json_schema")
        format_schema = request["response_format"]["json_schema"]
        self.assertIs(format_schema["strict"], True)
        self.assertEqual(format_schema["name"], "ResponseOutput")
        self.assertEqual(set(format_schema["schema"]["required"]), {"decision", "retrieval_request", "reason", "content"})
        self.assertIs(format_schema["schema"]["additionalProperties"], False)
        for field in ("reason", "content"):
            self.assertEqual(format_schema["schema"]["properties"][field]["type"], "string")
        self.assertEqual(request["reasoning_effort"], "medium")

    async def test_reactive_reply_recovers_invalid_output_without_retry(self):
        self.messages = [tool_message("{}"), VALID_MESSAGE]
        summarizer = AsyncMock()
        summarizer.ainvoke.return_value = llm.SummarizerOutput(summary="NIL", mood="default")
        with patch.object(llm, "_get_summarizer_llm", AsyncMock(return_value=summarizer)), patch.object(
            llm, "SUMMARIZER_SYSTEM_PROMPT", "Summarize"
        ):
            content, reason, _, _ = await llm.get_response(
                [{"sender": "Jamie", "content": "Hello"}], must_reply=True
            )
            self.assertTrue(content.strip())
            self.assertIn("parse failure", reason)
        self.assertEqual(len(self.requests), 1)

    async def test_transport_errors_are_not_treated_as_output_errors(self):
        self.status = 400
        errors = LLM_ERRORS.labels(node="responder", kind="client_400")._value.get()
        with self.assertRaises(BadRequestError):
            await self.outreach()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(LLM_ERRORS.labels(node="responder", kind="client_400")._value.get() - errors, 1)

    async def test_cancellation_is_not_retried_or_counted(self):
        errors = LLM_ERRORS.labels(node="responder", kind="other")._value.get()
        with patch.object(llm, "_get_responder_llm", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await llm.responder_node({"history": [], "intent": "Check in"})
        self.assertEqual(self.requests, [])
        self.assertEqual(LLM_ERRORS.labels(node="responder", kind="other")._value.get(), errors)


if __name__ == "__main__":
    unittest.main()
