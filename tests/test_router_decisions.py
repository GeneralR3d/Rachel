import json
import unittest
from unittest.mock import patch

import httpx

from app.services import llm


class RouterDecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_router_sends_decision_request_and_uses_probability(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "object": "decision",
                    "model": "jev-1.13.0",
                    "vendor": "typesafe",
                    "answers": {"should_reply": {"type": "noul", "noul": 0.69}},
                    "usage": {"input_tokens": 20, "output_tokens": 1, "total_tokens": 21, "cost": 0.0},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        state = {
            "history": [
                {"sender": "Rachel", "content": "Hi", "telegram_message_id": 10},
                {"sender": "Alex", "content": "Thanks", "telegram_message_id": 11},
            ],
            "current_summary": "Planning dinner",
            "responded_watermark": 10,
            "is_private": False,
        }
        try:
            with (
                patch("httpx.AsyncClient", return_value=client),
                patch.object(llm.settings, "merge_gateway_base_url", "https://gateway.example/v1"),
                patch.object(llm.settings, "decisions_llm", "typesafe/jev-1.13"),
            ):
                result = await llm.router_node(state)
        finally:
            await client.aclose()

        self.assertEqual(result, {"should_reply": False})
        self.assertEqual(len(requests), 1)
        self.assertEqual(str(requests[0].url), "https://gateway.example/v1/decisions")
        self.assertEqual(
            requests[0].headers["Authorization"],
            f"Bearer {llm.settings.merge_gateway_api_key}",
        )
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["model"], "typesafe/jev-1.13")
        self.assertEqual(set(payload), {"model", "state", "questions"})
        self.assertEqual(payload["questions"]["should_reply"]["type"], "noul")
        self.assertIn("Planning dinner", payload["state"])
        self.assertIn(llm.ROUTER_NODE_DIVIDER, payload["state"])
        self.assertIn("Alex: Thanks", payload["state"])

    async def test_private_router_replies_at_cutoff(self) -> None:
        requests: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "object": "decision",
                    "model": "jev-1.13.0",
                    "vendor": "typesafe",
                    "answers": {"should_reply": {"type": "noul", "noul": 0.7}},
                    "usage": {"input_tokens": 20, "output_tokens": 1, "total_tokens": 21, "cost": 0.0},
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        try:
            with patch("httpx.AsyncClient", return_value=client):
                result = await llm.router_node(
                    {
                        "history": [{"sender": "Alex", "content": "How are you?"}],
                        "current_summary": None,
                        "responded_watermark": None,
                        "is_private": True,
                    }
                )
        finally:
            await client.aclose()

        self.assertEqual(result, {"should_reply": True})
        question = json.loads(requests[0].content)["questions"]["should_reply"]
        self.assertIn("private Telegram chat", question["instructions"])
        self.assertIn("brief acknowledgement", question["criteria"]["false"])

    async def test_invalid_answer_and_http_error_fail_open(self) -> None:
        for status, body in (
            (200, {"answers": {"should_reply": {"type": "noul", "noul": 1.5}}}),
            (200, {"answers": {"should_reply": {"type": "noul", "noul": "0.2"}}}),
            (200, {"answers": {}}),
            (503, {"error": "upstream unavailable"}),
        ):
            with self.subTest(status=status, body=body):
                client = httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda request: httpx.Response(status, json=body)
                    )
                )
                try:
                    with patch("httpx.AsyncClient", return_value=client):
                        result = await llm.router_node(
                            {
                                "history": [{"sender": "Alex", "content": "Thanks"}],
                                "current_summary": None,
                                "responded_watermark": None,
                                "is_private": True,
                            }
                        )
                finally:
                    await client.aclose()
                self.assertEqual(result, {"should_reply": True})

    def test_forced_reply_bypasses_router(self) -> None:
        route = llm._route_after_checker(
            {
                "history": [{"sender": "Alex", "content": "Rachel?"}],
                "responded_watermark": None,
                "must_reply": True,
            }
        )
        self.assertEqual(route, ["summarizer_node", "context_fetcher_node"])


if __name__ == "__main__":
    unittest.main()
