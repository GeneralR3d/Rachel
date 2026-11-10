"""Exa tool contracts with the async SDK replaced only at its network boundary."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from exa_py.errors import ExaTimeoutError

from app.services import web_search


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.key = patch.object(web_search.settings, 'exa_api_key', 'test-secret-key')
        self.key.start()
        self.addCleanup(self.key.stop)
        self.requests = []
        self.clients = []
        self.handler = lambda req: httpx.Response(200, json={'results': [
            {'id': '1', 'url': 'https://example.com/one', 'title': 'Tea 茶', 'highlights': ['source {text}']},
            {'id': '2', 'url': 'https://example.com/two'},
        ]})
        real_client = httpx.AsyncClient

        def client(**kwargs):
            def handle(req):
                self.requests.append(req)
                return self.handler(req)
            result = real_client(**kwargs, transport=httpx.MockTransport(handle))
            self.clients.append(result)
            return result
        self.client_patch = patch('exa_py.api.httpx.AsyncClient', side_effect=client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    async def search(self, **args):
        return await web_search.search_web.ainvoke({'query': ' tea ', **args})

    async def test_default_urls_and_text_modes_use_sdk_and_preserve_sources(self):
        urls = await self.search()
        with patch.object(web_search, 'SEARCH_MODE', 'text'):
            text = await self.search(num_results=2)
        self.assertEqual(urls, {'query': 'tea', 'mode': 'urls', 'results': [
            'https://example.com/one', 'https://example.com/two']})
        self.assertEqual(text, {'query': 'tea', 'mode': 'text', 'results': [
            {'url': 'https://example.com/one', 'title': 'Tea 茶', 'highlights': ['source {text}'], 'truncated': False},
            {'url': 'https://example.com/two', 'highlights': [], 'truncated': False}]})
        for index, req in enumerate(self.requests):
            body = json.loads(req.content)
            self.assertEqual(body['query'], 'tea')
            self.assertEqual(body['type'], 'auto')
            self.assertEqual(body['numResults'], (5, 2)[index])
            self.assertEqual(req.headers['x-api-key'], 'test-secret-key')
            self.assertEqual(req.extensions['timeout']['read'], 15)
            if index:
                self.assertEqual(body['contents'], {'highlights': True})
            else:
                self.assertNotIn('contents', body)
        self.assertTrue(all(c.is_closed for c in self.clients))

    async def test_combined_excerpt_budget_and_missing_excerpts_keep_links(self):
        self.handler = lambda req: httpx.Response(200, json={'results': [
            {'id': '1', 'url': 'https://example.com', 'highlights': ['茶' * 1500, 'x' * 700, 'ignored']},
            {'id': '2', 'url': 'https://example.org', 'highlights': []},
            {'id': '3', 'url': 'https://example.net', 'highlights': ['x' * 2000]},
        ]})
        with patch.object(web_search, 'SEARCH_MODE', 'text'):
            result = await self.search()
        self.assertEqual(result['results'][0]['highlights'], ['茶' * 1500, 'x' * 500])
        self.assertTrue(result['results'][0]['truncated'])
        self.assertEqual(result['results'][1]['highlights'], [])
        self.assertFalse(result['results'][2]['truncated'])
        self.assertEqual(len(result['results']), 3)

    async def test_validation_prevents_requests_and_optional_key_allows_startup(self):
        for args in ({'query': '  '}, {'num_results': 0}, {'num_results': 11},
                     {'num_results': True}, {'num_results': 1.5}, {'mode': 'full'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                await self.search(**args)
        with patch.object(web_search.settings, 'exa_api_key', None):
            with self.assertRaisesRegex(ValueError, 'EXA_API_KEY'):
                await self.search()
        self.assertEqual(self.requests, [])
        for count in (1, 10):
            await self.search(num_results=count)
        self.assertEqual(len(self.requests), 2)

    async def test_mode_is_not_an_agent_argument(self):
        self.assertEqual(set(web_search.search_web.args), {'query', 'num_results'})
        for mode in ('urls', 'text'):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                await self.search(mode=mode)
        self.assertEqual(self.requests, [])

    async def test_empty_results(self):
        self.handler = lambda req: httpx.Response(200, json={'results': []})
        self.assertEqual((await self.search())['results'], [])

    async def test_http_and_malformed_errors_are_sanitized_without_retry(self):
        cases = [(httpx.Response(status, text='test-secret-key sensitive body'), f'HTTP {status}')
                 for status in (401, 403, 429, 500)]
        cases += [(httpx.Response(200, text='test-secret-key bad json'), 'malformed'),
                  (httpx.Response(200, json={'results': [{'id': '1'}]}), 'malformed')]
        for response, expected in cases:
            with self.subTest(expected=expected):
                self.handler = lambda req: response
                before = len(self.requests)
                with self.assertRaises(ValueError) as caught:
                    await self.search()
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn('test-secret-key', str(caught.exception))
                self.assertEqual(len(self.requests), before + 1)
                self.assertTrue(self.clients[-1].is_closed)

    async def test_invalid_sdk_result_shapes_are_rejected(self):
        for results in (None, 'bad', [SimpleNamespace(url=None)],
                        [SimpleNamespace(url='https://example.com', highlights='bad')],
                        [SimpleNamespace(url='https://example.com', highlights=[1])]):
            client = AsyncMock()
            client.search.return_value = SimpleNamespace(results=results)
            client.__aenter__.return_value = client
            with self.subTest(results=results), patch.object(web_search, 'AsyncExa', return_value=client), \
                    patch.object(web_search, 'SEARCH_MODE', 'text'):
                with self.assertRaisesRegex(ValueError, 'malformed'):
                    await self.search()

    async def test_sdk_timeout_translation_and_network_error_cleanup(self):
        for error, expected in ((httpx.ReadTimeout('secret'), TimeoutError),
                                (httpx.ConnectError('secret'), ValueError)):
            def fail(req):
                raise error
            self.handler = fail
            with self.assertRaises(expected) as caught:
                await self.search()
            self.assertNotIn('secret', str(caught.exception))
            self.assertTrue(self.clients[-1].is_closed)
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.search.side_effect = ExaTimeoutError('secret')
        with patch.object(web_search, 'AsyncExa', return_value=client), self.assertRaises(TimeoutError):
            await self.search()

    async def test_total_deadline_and_external_cancellation_close_client(self):
        started = asyncio.Event()
        async def wait(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        from exa_py import AsyncExa
        with patch.object(AsyncExa, 'search', side_effect=wait), \
                patch.object(AsyncExa, 'aclose', new_callable=AsyncMock) as close:
            with patch.object(web_search, 'SEARCH_TIMEOUT', 0.01), self.assertRaises(TimeoutError):
                await self.search()
            close.assert_awaited_once()
            close.reset_mock()
            started.clear()
            task = asyncio.create_task(self.search())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            close.assert_awaited_once()
