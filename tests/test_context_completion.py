"""Fetcher-owned completion protocol and bounded retrieval contracts."""
import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from langchain_core.messages import AIMessage
from pydantic import ValidationError
from app.services import llm


def batch(*calls):
    return AIMessage(content='', tool_calls=[
        {'name': name, 'args': args, 'id': str(i)} for i, (name, args) in enumerate(calls)
    ])


def finish(reason='Enough evidence'):
    return batch(('finish_context', {'reason': reason}))


class CompletionTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('builtins.print'))
        self.model = SimpleNamespace(ainvoke=AsyncMock())
        self.model.bind_tools = lambda tools: self.model
        self.stack.enter_context(patch.object(llm, '_get_context_fetcher_llm', AsyncMock(return_value=self.model)))
        self.state = {'history': [], 'context_passes': 0, 'tool_observations': [],
                      'context_status': 'collecting', 'context_reason': '', 'chat_id': -123}

    async def test_finish_is_control_with_trimmed_reason_and_no_observation(self):
        self.model.ainvoke.return_value = finish('  sufficient 茶 {facts}  ')
        result = await llm.context_fetcher_node(self.state)
        self.assertEqual(result['context_reason'], 'sufficient 茶 {facts}')
        self.assertEqual(result['context_status'], 'finished')
        self.assertEqual(result['context_passes'], 1)
        self.assertEqual(result['loop_outcome'], 'first_pass')
        self.assertEqual(result['tool_observations'], [])
        tool = next(t for t in llm.CONTEXT_TOOLS if t.name == 'finish_context')
        self.assertEqual(tool.args_schema.model_validate({'reason': ' yes '}).reason, 'yes')
        for args in ({}, {'reason': ''}, {'reason': ' '}, {'reason': 'ok', 'extra': True}):
            with self.assertRaises(ValidationError):
                tool.args_schema.model_validate(args)

    async def test_invalid_batches_are_rejected_before_any_mutation(self):
        create_args = {'name': 'Exam', 'cron_expression': '0 10 * * *',
                       'intent': 'Ask about exam', 'repeat_times': 1}
        invalid = [AIMessage(content='All done'), batch(), finish(' '),
                   batch(('finish_context', {})),
                   batch(('finish_context', {'reason': 'ok', 'extra': 1})),
                   batch(('finish_context', {'reason': 'a'}), ('finish_context', {'reason': 'b'})),
                   batch(('create_proactive_schedule', create_args), ('finish_context', {'reason': 'done'})),
                   batch(('finish_context', {'reason': 'done'}), ('create_proactive_schedule', create_args)),
                   AIMessage(content='', invalid_tool_calls=[{
                       'name': 'finish_context', 'args': '{bad', 'id': 'broken', 'error': 'Invalid JSON'}])]
        mutation = AsyncMock()
        with patch.dict(llm._context_tools_by_name, {'create_proactive_schedule': SimpleNamespace(ainvoke=mutation)}):
            for output in invalid:
                with self.subTest(output=output):
                    self.model.ainvoke.return_value = output
                    result = await llm.context_fetcher_node(self.state)
                    self.assertEqual(result['context_status'], 'collecting')
                    self.assertEqual(len(result['tool_observations']), 1)
                    self.assertEqual(result['tool_observations'][0]['status'], 'error')
            mutation.assert_not_awaited()

    async def test_pass_and_step_caps_preserve_final_batch_and_completion_precedence(self):
        lookup = AsyncMock(return_value='last evidence')
        with patch.dict(llm._context_tools_by_name, {'lookup': SimpleNamespace(ainvoke=lookup)}):
            for budget in (1, 8, 10):
                with self.subTest(budget=budget), patch.object(llm.settings, 'agent_max_context_passes', budget):
                    self.model.ainvoke.return_value = batch(('lookup', {}))
                    result = await llm.context_fetcher_node({**self.state, 'context_passes': budget - 1})
                    self.assertEqual(result['context_status'], 'capped')
                    self.assertEqual(result['tool_observations'][0]['raw_result'], 'last evidence')
                    self.model.ainvoke.return_value = finish()
                    result = await llm.context_fetcher_node({**self.state, 'context_passes': budget - 1, 'remaining_steps': 1})
                    self.assertEqual(result['context_status'], 'finished')
            self.model.ainvoke.return_value = batch(('lookup', {}))
            result = await llm.context_fetcher_node({**self.state, 'remaining_steps': 2})
            self.assertEqual(result['context_status'], 'capped')

    async def test_exact_mutations_reuse_validated_defaults_including_failure_and_timeout(self):
        args = {'name': 'Exam', 'cron_expression': '0 10 * * *', 'intent': 'Ask about exam', 'repeat_times': 1}
        for failure in (None, ValueError('offline'), TimeoutError('unconfirmed')):
            with self.subTest(failure=failure):
                mutation = AsyncMock(return_value={'saved': True}, side_effect=failure)
                self.model.ainvoke.side_effect = [batch(('create_proactive_schedule', args)),
                    batch(('create_proactive_schedule', {**args, 'timezone': 'Asia/Singapore'}))]
                with patch.dict(llm._context_tools_by_name, {'create_proactive_schedule': SimpleNamespace(ainvoke=mutation)}):
                    first = await llm.context_fetcher_node(self.state)
                    second = await llm.context_fetcher_node({**self.state, **first})
                self.assertEqual(len(second['tool_observations']), 1)
                mutation.assert_awaited_once()

    async def test_cancellation_propagates(self):
        self.model.ainvoke.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await llm.context_fetcher_node(self.state)


class CollectionGraphTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('builtins.print'))
        self.stack.enter_context(patch.object(llm, 'get_active_trait_prompts', AsyncMock(return_value='friendly')))
        self.stack.enter_context(patch.object(llm, 'summarizer_node', AsyncMock(return_value={
            'current_summary': 'fresh 茶 {summary}', 'mood': 'excited'})))
        self.fetcher = SimpleNamespace(ainvoke=AsyncMock(return_value=finish()))
        self.fetcher.bind_tools = lambda tools: self.fetcher
        self.responder = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(content='Final reply', reason='Grounded')))
        self.stack.enter_context(patch.object(llm, '_get_context_fetcher_llm', AsyncMock(return_value=self.fetcher)))
        self.stack.enter_context(patch.object(llm, '_get_responder_llm', AsyncMock(return_value=self.responder)))
        self.state = {'history': [{'sender': 'Jamie', 'content': 'Hello', 'telegram_message_id': 2}],
                      'context_passes': 0, 'tool_observations': [], 'context_status': 'collecting',
                      'context_reason': '', 'current_summary': 'old', 'mood': 'default', 'must_reply': True,
                      'responded_watermark': 1, 'response_text': '', 'response_reason': ''}

    async def test_immediate_and_corrective_completion_both_graphs(self):
        for proactive in (False, True):
            for corrective in (False, True):
                with self.subTest(proactive=proactive, corrective=corrective):
                    self.fetcher.ainvoke.side_effect = ([batch(), finish()] if corrective else [finish()])
                    self.responder.ainvoke.reset_mock()
                    graph = llm._build_proactive_graph() if proactive else llm._build_graph()
                    result = await graph.ainvoke({**self.state, 'intent': 'Check in' if proactive else ''})
                    self.assertEqual(result['response_text'], 'Final reply')
                    self.assertEqual(result['context_status'], 'finished')
                    self.assertEqual(result['context_passes'], 2 if corrective else 1)
                    self.assertEqual(len(result['tool_observations']), int(corrective))
                    self.responder.ainvoke.assert_awaited_once()

    async def test_responder_waits_for_each_branch_and_uses_updated_summary(self):
        for slow in ('summary', 'collection'):
            with self.subTest(slow=slow):
                summary_ready, collection_ready, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
                async def summarize(state):
                    summary_ready.set()
                    if slow == 'summary':
                        await release.wait()
                    return {'current_summary': 'fresh 茶 {summary}', 'mood': 'excited'}
                async def collect(messages):
                    collection_ready.set()
                    if slow == 'collection':
                        await release.wait()
                    return finish('PRIVATE COMPLETION REASON')
                self.fetcher.ainvoke.side_effect = collect
                self.responder.ainvoke.reset_mock()
                with patch.object(llm, 'summarizer_node', summarize):
                    task = asyncio.create_task(llm._build_graph().ainvoke(self.state))
                    try:
                        await asyncio.wait_for(asyncio.gather(summary_ready.wait(), collection_ready.wait()), 2)
                        self.responder.ainvoke.assert_not_awaited()
                    finally:
                        release.set()
                    result = await task
                self.assertEqual(result['current_summary'], 'fresh 茶 {summary}')
                self.assertEqual(result['mood'], 'excited')
                self.responder.ainvoke.assert_awaited_once()
                final_messages = self.responder.ainvoke.call_args.args[0]
                self.assertIn('fresh 茶 {summary}', final_messages[0].content)
                self.assertNotIn('PRIVATE COMPLETION REASON', '\n'.join(m.content for m in final_messages))
                self.assertIn('old', self.fetcher.ainvoke.call_args.args[0][1].content)

    async def test_final_pass_results_reach_one_responder_in_both_graphs(self):
        lookup = AsyncMock(return_value='final-pass evidence')
        for proactive in (False, True):
            for budget in (1, 8, 10):
                with self.subTest(proactive=proactive, budget=budget), \
                        patch.object(llm.settings, 'agent_max_context_passes', budget), \
                        patch.dict(llm._context_tools_by_name, {'lookup': SimpleNamespace(ainvoke=lookup)}):
                    self.fetcher.ainvoke.side_effect = [batch(('lookup', {}))] * budget
                    self.responder.ainvoke.reset_mock()
                    graph = llm._build_proactive_graph() if proactive else llm._build_graph()
                    result = await graph.ainvoke({**self.state, 'intent': 'Check in' if proactive else ''})
                    self.assertEqual(result['context_passes'], budget)
                    self.assertEqual(result['loop_outcome'], 'capped')
                    self.assertEqual(len(result['tool_observations']), budget)
                    self.responder.ainvoke.assert_awaited_once()
                    self.assertIn('final-pass evidence', self.responder.ainvoke.call_args.args[0][0].content)


class PromptOwnershipTests(IsolatedAsyncioTestCase):
    def test_responder_schema_is_final_only(self):
        self.assertEqual(set(llm.ResponseOutput.model_fields), {'content', 'reason'})
        self.assertEqual(llm.ResponseOutput(content='Hi', reason='Friendly').content, 'Hi')
        with self.assertRaises(ValidationError):
            llm.ResponseOutput(content='Hi', reason='Friendly', decision='fetch_more')

    def test_role_specific_trigger_and_dynamic_completion_schema(self):
        from app.services import prompt_builders as builders
        from app import prompts
        intent = 'Remind Jamie to check {茶} now; separately schedule a follow-up'
        fetcher = builders.build_proactive_trigger(intent, role='fetcher')
        responder = builders.build_proactive_trigger(intent, role='responder')
        for trigger in (fetcher, responder):
            self.assertIn(intent, trigger)
            self.assertIn('already elapsed', trigger)
            self.assertIn('prior context only', trigger)
            self.assertIn('current intent', trigger)
        self.assertIn('finish_context', fetcher)
        self.assertNotIn('finish_context', responder)
        self.assertIn('Do not compose', fetcher)
        self.assertIn('outgoing', responder)
        prompt = builders.build_context_fetcher_prompt(system_prompt=prompts.CONTEXT_FETCHER_SYSTEM_PROMPT,
            tools=[llm.finish_context], available_categories=[], participants='Jamie', current_datetime='now')
        self.assertIn('finish_context(reason)', prompt)
        self.assertIn('"additionalProperties": false', prompt)
        self.assertIn('"required": ["reason"]', prompt)
        self.assertIn('responder runs once', prompt)
        self.assertNotIn('RESPONDER HANDOFF', prompt)
        self.assertNotIn('retrieval_request', prompt)


class CollectionBoundaryTests(IsolatedAsyncioTestCase):
    setUp = CollectionGraphTests.setUp

    async def test_completion_on_last_pass_finishes_normally_in_both_graphs(self):
        for proactive in (False, True):
            for budget in (1, 8, 10):
                with self.subTest(proactive=proactive, budget=budget), patch.object(llm.settings, 'agent_max_context_passes', budget):
                    self.fetcher.ainvoke.side_effect = [batch()] * (budget - 1) + [finish()]
                    self.responder.ainvoke.reset_mock()
                    graph = llm._build_proactive_graph() if proactive else llm._build_graph()
                    result = await graph.ainvoke({**self.state, 'intent': 'Check in' if proactive else ''})
                    self.assertEqual(result['context_status'], 'finished')
                    self.assertEqual(result['loop_outcome'], 'first_pass' if budget == 1 else 'retried')
                    self.assertEqual(result['context_passes'], budget)
                    self.responder.ainvoke.assert_awaited_once()

    async def test_summary_is_captured_across_collection_and_responder_waits_for_finish(self):
        summary_done, second_pass, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        fetcher_summaries = []
        import json
        async def summarize(state):
            summary_done.set()
            return {'current_summary': 'fresh', 'mood': 'excited'}
        async def collect(messages):
            fetcher_summaries.append(json.loads(messages[1].content.split('\n', 1)[1])['current_summary'])
            if len(fetcher_summaries) == 1:
                await summary_done.wait()
                return batch(('search_world_view', {'query': 'tea'}))
            second_pass.set()
            await release.wait()
            return finish()
        self.fetcher.ainvoke.side_effect = collect
        with patch.object(llm, 'summarizer_node', summarize), \
                patch.object(llm, 'search_worldview', AsyncMock(return_value='Tea facts')):
            task = asyncio.create_task(llm._build_graph().ainvoke(self.state))
            try:
                await asyncio.wait_for(second_pass.wait(), 2)
                self.responder.ainvoke.assert_not_awaited()
            finally:
                release.set()
            result = await task
        self.assertEqual(fetcher_summaries, ['old', 'old'])
        self.assertEqual(result['current_summary'], 'fresh')
        self.assertIn('Tea facts', self.responder.ainvoke.call_args.args[0][0].content)
        self.responder.ainvoke.assert_awaited_once()

    async def test_workflow_step_guard_leaves_room_for_responder_in_both_graphs(self):
        for proactive in (False, True):
            with self.subTest(proactive=proactive), \
                    patch.object(llm, 'search_worldview', AsyncMock(return_value='survived low steps')):
                self.fetcher.ainvoke.side_effect = [batch(('search_world_view', {'query': 'tea'})), finish()]
                self.responder.ainvoke.reset_mock()
                graph = llm._build_proactive_graph() if proactive else llm._build_graph()
                result = await graph.ainvoke(
                    {**self.state, 'intent': 'Check in' if proactive else ''},
                    {'recursion_limit': 3 if proactive else 4},
                )
                self.assertEqual(result['context_status'], 'capped')
                self.assertEqual(result['context_passes'], 1)
                self.assertEqual(result['response_text'], 'Final reply')
                self.responder.ainvoke.assert_awaited_once()
                self.assertIn('survived low steps', self.responder.ainvoke.call_args.args[0][0].content)

    async def test_collection_cancellation_propagates_without_responder(self):
        collecting = asyncio.Event()
        async def blocked(messages):
            collecting.set()
            await asyncio.Event().wait()
        self.fetcher.ainvoke.side_effect = blocked
        task = asyncio.create_task(llm._build_graph().ainvoke(self.state))
        await asyncio.wait_for(collecting.wait(), 2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.responder.ainvoke.assert_not_awaited()

    async def test_metrics_count_attempts_and_one_final_responder_once(self):
        from app.services.metrics import LLM_CALLS, AGENT_CONTEXT_PASSES, AGENT_LOOP_OUTCOMES
        for proactive in (False, True):
            with self.subTest(proactive=proactive):
                self.fetcher.ainvoke.side_effect = [TimeoutError('model deadline'), batch(), finish()]
                fetch_before = LLM_CALLS.labels(node='context_fetcher')._value.get()
                response_before = LLM_CALLS.labels(node='responder')._value.get()
                outcome_before = AGENT_LOOP_OUTCOMES.labels(outcome='retried')._value.get()
                from prometheus_client import REGISTRY
                count_before = REGISTRY.get_sample_value('rachel_agent_context_passes_count')
                graph = llm._build_proactive_graph() if proactive else llm._build_graph()
                await llm._run_response_graph(graph, {**self.state, 'intent': 'Check in' if proactive else ''}, run_name='MetricsContract')
                self.assertEqual(LLM_CALLS.labels(node='context_fetcher')._value.get() - fetch_before, 3)
                self.assertEqual(LLM_CALLS.labels(node='responder')._value.get() - response_before, 1)
                self.assertEqual(AGENT_LOOP_OUTCOMES.labels(outcome='retried')._value.get() - outcome_before, 1)
                self.assertEqual(REGISTRY.get_sample_value('rachel_agent_context_passes_count') - count_before, 1)
