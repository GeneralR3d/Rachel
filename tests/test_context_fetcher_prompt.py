"""The fetcher prompt follows the tools supplied to its model call."""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app.services import llm


class PromptRequest:
    def __init__(self, tools):
        self.tools = tools
        self.runtime = SimpleNamespace(context={
            "participants": "Jamie",
            "datetime": "The current date is 18 October 2026, the current day of week is Sunday, the current time is 12:00",
        })
        self.system_message = SystemMessage(content="")

    def override(self, **changes):
        updated = PromptRequest(self.tools)
        updated.__dict__.update(self.__dict__)
        updated.__dict__.update(changes)
        return updated


def rendered_prompt(tools):
    request = PromptRequest(tools)
    return llm.context_fetcher_dynamic_prompt.wrap_model_call(
        request, lambda updated: updated.system_message.content
    )


class ContextFetcherPromptTests(unittest.TestCase):
    def test_full_tool_set_preserves_guidance_and_tool_listing(self):
        prompt = rendered_prompt(llm.CONTEXT_TOOLS)
        self.assertLess(prompt.index("SCHEDULE\n"), prompt.index("WORLD-VIEW FACTS\n"))
        self.assertLess(prompt.index("WORLD-VIEW FACTS\n"), prompt.index("PER-USER FACTS\n"))
        self.assertIn("Use at most one focused lookup per relevant participant", prompt)
        self.assertIn("use at most one focused world-view search", prompt)
        self.assertIn("Pass the participant's name EXACTLY", prompt)
        self.assertIn("Jamie", prompt)
        self.assertIn("18 October 2026", prompt)
        for tool in llm.CONTEXT_TOOLS:
            self.assertIn(f"- {tool.name}(", prompt)

    def test_subset_only_includes_supported_guidance_and_tools(self):
        tools = [llm.CALENDAR_TOOLS[-1], llm.search_world_view]
        prompt = rendered_prompt(tools)
        self.assertIn("SCHEDULE\n", prompt)
        self.assertIn("specific time on some day", prompt)
        self.assertIn("RIGHT NOW and/or an overview of TODAY", prompt)
        self.assertIn("Fetch a DIFFERENT day", prompt)
        self.assertIn("Fetch the full detail of a particular day", prompt)
        self.assertIn("Resolve relative dates yourself", prompt)
        self.assertIn("WORLD-VIEW FACTS\n", prompt)
        self.assertNotIn("PER-USER FACTS\n", prompt)
        self.assertIn("- get_activity_at(day, hour):", prompt)
        self.assertIn("- search_world_view(query):", prompt)
        self.assertNotIn("- search_user_info(name, query):", prompt)

    def test_no_tools_has_no_tool_specific_guidance(self):
        prompt = rendered_prompt([])
        self.assertNotIn("SCHEDULE\n", prompt)
        self.assertNotIn("WORLD-VIEW FACTS\n", prompt)
        self.assertNotIn("PER-USER FACTS\n", prompt)
        self.assertNotIn("- get_activity_now(", prompt)
        self.assertIn("Each invocation is one retrieval pass", prompt)

    def test_partial_calendar_set_uses_full_category_overview(self):
        prompt = rendered_prompt([llm.CALENDAR_TOOLS[-1], llm.search_world_view, llm.search_user_info_tool])
        self.assertIn("There are four kinds of background you can gather: Rachel's weekly SCHEDULE", prompt)


class RecordingToolModel(BaseChatModel):
    calls: list = Field(default_factory=list)
    bound_tool_names: list[str] = Field(default_factory=list)

    @property
    def _llm_type(self):
        return "recording-tool-model"

    def bind_tools(self, tools, **kwargs):
        self.bound_tool_names = [tool.name for tool in tools]
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content="", tool_calls=[{
                "id": "call-1", "name": "get_day_overview", "args": {"day": "Friday"}
            }]
        ))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


class ContextFetcherNodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetcher_injects_every_tool_and_executes_call_once_in_existing_path(self):
        model = RecordingToolModel()
        with patch.dict(llm._client_cache, {}, clear=True), \
                patch.object(llm, "_current_main_model", AsyncMock(return_value="test-model")), \
                patch.object(llm, "_base_chat", return_value=model), \
                patch("app.calander.get_day_summary", new_callable=AsyncMock, return_value=[]) as schedule_lookup:
            observations = []
            self.assertIs(await llm._get_context_fetcher_llm(), model)
            await llm._run_context_fetcher({
                "history": [{"sender": "Jamie", "content": "Free Friday?", "telegram_message_id": 1}],
                "senders": {7: "Jamie"}, "context_passes": 0, "retrieval_request": "",
            }, observations)

        self.assertEqual(model.bound_tool_names, [tool.name for tool in llm.CONTEXT_TOOLS])
        self.assertEqual(len(model.calls), 1)
        self.assertIn("SCHEDULE\n", model.calls[0][0].content)
        self.assertIn("Jamie", model.calls[0][0].content)
        schedule_lookup.assert_awaited_once()
        self.assertEqual(observations[0]["tool_name"], "get_day_overview")
        self.assertEqual(observations[0]["status"], "success")


if __name__ == "__main__":
    unittest.main()
