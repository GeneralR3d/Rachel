"""The fetcher prompt follows the tools supplied to its model call."""

from copy import deepcopy
from datetime import datetime
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from app import prompts
from app.services import llm, prompt_builders


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
    def test_delegates_request_capabilities_and_runtime_context(self):
        tools = [llm.search_world_view, llm.CALENDAR_TOOLS[-1]]
        request = PromptRequest(tools)
        request.runtime.context = {"participants": "Jamie {茶}", "datetime": "Sunday {noon}"}
        template = "{participants}|{datetime}|{tools}|{tool_guidance}"
        with (
            patch.object(llm, "CONTEXT_FETCHER_SYSTEM_PROMPT", template),
            patch.object(llm, "build_context_fetcher_prompt",
                         wraps=prompt_builders.build_context_fetcher_prompt, create=True) as build,
        ):
            prompt = llm.context_fetcher_dynamic_prompt.wrap_model_call(
                request, lambda updated: updated.system_message.content
            )
        self.assertTrue(prompt.startswith("Jamie {茶}|Sunday {noon}|- search_world_view(query):"))
        self.assertIn("SCHEDULE\n", prompt)
        self.assertIn("WORLD-VIEW FACTS\n", prompt)
        self.assertNotIn("PER-USER FACTS\n", prompt)
        self.assertNotIn("- search_user_info(", prompt)
        build.assert_called_once_with(
            system_prompt=template, tools=tools, available_categories={"worldview", "schedule"},
            participants="Jamie {茶}", current_datetime="Sunday {noon}",
            intent="",
        )

    def test_guidance_registry_order_and_capability_selection(self):
        guidance = prompts.CONTEXT_FETCHER_TOOL_GUIDANCE
        self.assertEqual(list(guidance), ["schedule", "worldview", "user_info", "proactive_schedules"])
        for tools, categories in (
            (list(reversed(llm.CONTEXT_TOOLS)), ["schedule", "worldview", "user_info", "proactive_schedules"]),
            ([llm.search_world_view, llm.CALENDAR_TOOLS[-1]], ["schedule", "worldview"]),
            ([], []),
        ):
            with self.subTest(categories=categories):
                prompt = rendered_prompt(tools)
                for category, text in guidance.items():
                    if category in categories:
                        self.assertIn(text, prompt)
                    else:
                        self.assertNotIn(text, prompt)
                positions = [prompt.index(guidance[category]) for category in categories]
                self.assertEqual(positions, sorted(positions))

    def test_full_tool_set_preserves_guidance_and_tool_listing(self):
        prompt = rendered_prompt(llm.CONTEXT_TOOLS)
        self.assertLess(prompt.index("SCHEDULE\n"), prompt.index("WORLD-VIEW FACTS\n"))
        self.assertLess(prompt.index("WORLD-VIEW FACTS\n"), prompt.index("PER-USER FACTS\n"))
        self.assertIn("Use at most one focused lookup per relevant participant", prompt)
        self.assertIn(prompts.CONTEXT_FETCHER_TOOL_GUIDANCE["worldview"], prompt)
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

    def test_partial_calendar_set_uses_registered_category_guidance(self):
        prompt = rendered_prompt([llm.CALENDAR_TOOLS[-1], llm.search_world_view, llm.search_user_info_tool])
        for category in ("schedule", "worldview", "user_info"):
            guidance = prompts.CONTEXT_FETCHER_TOOL_GUIDANCE[category]
            self.assertIn(guidance, prompt)

    def test_proactive_policy_is_combined_and_only_added_with_capability(self):
        from app.services.proactive_tools import PROACTIVE_TOOLS

        prompt = rendered_prompt(PROACTIVE_TOOLS)
        self.assertIn(prompts.PROACTIVE_SCHEDULE_TOOL_PROMPT, prompt)
        for guidance in ("repeat_times=1", "unlimited", "follow-up", "current chat", "Asia/Singapore"):
            self.assertIn(guidance, prompt)
        self.assertNotIn(prompts.PROACTIVE_SCHEDULE_TOOL_PROMPT, rendered_prompt([]))
        for tool in PROACTIVE_TOOLS:
            self.assertIn(f"- {tool.name}(", prompt)


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
    async def test_handoff_and_history_remain_separate_ordered_messages(self):
        observations = [{
            "pass_number": 1, "tool_name": "search_world_view", "arguments": {"query": "茶"},
            "result_category": "worldview", "status": "error", "raw_result": "unavailable {value}",
        }]
        original = deepcopy(observations)
        model = SimpleNamespace(ainvoke=AsyncMock(return_value=AIMessage(content="")))
        model.bind_tools = lambda tools: model
        fixed_time = datetime(2026, 10, 18, 12, 0, tzinfo=llm.SGT)
        with (
            patch.object(llm, "_get_context_fetcher_llm", AsyncMock(return_value=model)),
            patch.object(llm.settings, "agent_max_context_passes", 3),
            patch.object(llm, "datetime") as clock,
            patch.object(llm, "build_context_fetcher_handoff",
                         wraps=prompt_builders.build_context_fetcher_handoff, create=True) as build,
        ):
            clock.now.return_value = fixed_time
            await llm._run_context_fetcher({
                "history": [
                    {"sender": "Jamie", "content": "Earlier", "telegram_message_id": 1},
                    {"sender": llm.BOT_NAME, "content": "hey", "telegram_message_id": 2},
                    {"sender": "Jamie", "content": "Free {茶}?", "telegram_message_id": 3},
                ],
                "senders": {7: "Jamie"}, "context_passes": 1,
                "retrieval_request": "Friday {value}", "responded_watermark": 2,
            }, observations)
        messages = model.ainvoke.call_args.args[0]
        self.assertEqual([msg.type for msg in messages], ["system", "system", "human", "ai", "human", "human"])
        self.assertIn("Jamie", messages[0].content)
        self.assertIn("The current date is 18 October 2026, the current day of week is Sunday, the current time is 12:00", messages[0].content)
        self.assertEqual(json.loads(messages[1].content.split("\n", 1)[1]), {
            "context_pass": 2, "max_context_passes": 3, "retrieval_request": "Friday {value}",
            "previous_observations": original,
        })
        self.assertEqual([msg.content for msg in messages[2:]], [
            "Jamie: Earlier", f"{llm.BOT_NAME}: hey", llm.CONTEXT_NODE_DIVIDER, "Jamie: Free {茶}?",
        ])
        self.assertEqual(observations, original)
        build.assert_called_once_with(
            context_pass=2, max_context_passes=3, retrieval_request="Friday {value}",
            observations=original,
        )

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
