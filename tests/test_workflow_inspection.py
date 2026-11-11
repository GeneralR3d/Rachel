"""Read-only workflow inspection and renderer source loading."""
from unittest import IsolatedAsyncioTestCase
from unittest.mock import Mock, patch
from types import SimpleNamespace

from app import prompts
from app.routers import admin
from scripts import draw_graphs


class WorkflowInspectionTests(IsolatedAsyncioTestCase):
    async def test_fetcher_system_and_loop_prompts_are_inspectable_read_only(self):
        result = await admin.read_workflow_prompts()
        by_key = {item.key: item.prompt for item in result}
        self.assertEqual(by_key['context_fetcher'], prompts.CONTEXT_FETCHER_SYSTEM_PROMPT)
        self.assertEqual(by_key['context_fetcher_loop'], prompts.CONTEXT_FETCHER_LOOP_INSTRUCTIONS)
        self.assertTrue(all(set(item.model_dump()) == {'key', 'label', 'prompt'} for item in result))

    def test_renderer_loads_restored_reactive_source_with_mocked_images(self):
        source = draw_graphs.LLM_MERMAID.read_text()
        self.assertIn('finish_context', source)
        self.assertIn('join', source)
        child_render = Mock(return_value=b'other-image')
        graph = SimpleNamespace(get_graph=lambda: SimpleNamespace(draw_mermaid_png=child_render))
        with patch.object(draw_graphs, 'draw_mermaid_png', return_value=b'reactive-image') as render, \
                patch.dict(draw_graphs.GRAPHS, {key: (label, lambda: graph) for key, (label, _) in draw_graphs.GRAPHS.items()}, clear=True):
            images = draw_graphs.render_graphs()
        render.assert_called_once_with(source)
        self.assertEqual(set(images), {'graph_llm.png', 'graph_proactive.png', 'graph_worldview.png', 'graph_userfacts.png'})
        self.assertEqual(images['graph_llm.png'], b'reactive-image')
        self.assertEqual(child_render.call_count, 3)
