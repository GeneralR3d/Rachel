"""Bounded fetching with controlled DNS, HTTP responses and connection boundary."""
import asyncio
from contextlib import ExitStack
import gzip
import socket
import threading
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.services import url_fetch as fetching


class UrlNormalizationTests(unittest.TestCase):
    def test_normalization_preserves_path_query_and_encoding(self):
        self.assertEqual(fetching.normalize_url('HTTPS://ExAmPle.COM:443/Case/%2f?b=2&a=%2F&a=3#x'),
                         'https://example.com/Case/%2f?b=2&a=%2F&a=3')
        self.assertEqual(fetching.normalize_url('www.example.com'), 'https://www.example.com/')
        self.assertEqual(fetching.normalize_url('http://[2606:4700:4700::1111]:80/#a'),
                         'http://[2606:4700:4700::1111]/')

    def test_rejects_malformed_and_unsupported_targets(self):
        for url in ('ftp://example.com', '//example.com', 'https://',
                    'https://example.com:bad', 'https://user:pass@example.com',
                    'https://example.com/\nsecret', 'https://example.com\\@127.0.0.1'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fetching.normalize_url(url)


class Body(httpx.AsyncByteStream):
    def __init__(self, chunks, wait=False):
        self.chunks, self.wait, self.closed = chunks, wait, False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk
        if self.wait:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closed = True


class RecordingTransport(httpx.MockTransport):
    closed = False

    async def aclose(self):
        self.closed = True


class UrlFetchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.dns = self.stack.enter_context(patch.object(
            asyncio.get_running_loop(), 'getaddrinfo', AsyncMock(return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443)),
            ])))
        self.requests, self.transports = [], []

    def serve(self, handler):
        def factory(*args, **kwargs):
            def record(request):
                self.requests.append(request)
                return handler(request)
            transport = RecordingTransport(record)
            self.transports.append(transport)
            return transport
        self.stack.enter_context(patch.object(fetching, '_PinnedTransport', side_effect=factory))

    async def fetch(self, url='https://example.com/article'):
        return await fetching.fetch_url.ainvoke({'url': url})

    async def test_direct_text_types_charset_and_headers(self):
        for media, body, expected in (
            ('text/markdown; charset=utf-8', '# 茶 {value}'.encode(), '# 茶 {value}'),
            ('text/x-markdown', b'## tea', '## tea'),
            ('text/markdown', b'Read [the guide](https://example.org/tea).',
             'Read [the guide](https://example.org/tea).'),
            ('text/plain', b'Read https://example.org/tea for more.',
             'Read https://example.org/tea for more.'),
            ('text/plain; charset=iso-8859-1', b'caf\xe9', 'café'),
            ('text/plain; charset=unknown-charset', '茶'.encode(), '茶'),
            ('application/json', b'{"tea": true}', '{"tea": true}'),
            ('application/problem+json; charset=utf-8', b'{"error": 1}', '{"error": 1}'),
        ):
            with self.subTest(media=media):
                with patch.object(fetching, '_PinnedTransport', return_value=httpx.MockTransport(
                    lambda req: httpx.Response(200, headers={'content-type': media}, content=body)
                )):
                    result = await self.fetch()
                self.assertEqual(result['content'], expected)
                self.assertEqual(result['content_type'], media.split(';')[0])
                self.assertFalse(result['truncated'])
        self.serve(lambda req: httpx.Response(200, headers={'content-type': 'text/plain'}, text='ok'))
        await self.fetch()
        request = self.requests[0]
        self.assertEqual(request.method, 'GET')
        self.assertTrue(request.headers['accept'].startswith('text/markdown'))
        self.assertNotIn('authorization', request.headers)
        self.assertNotIn('cookie', request.headers)
        self.assertTrue(all(t.closed for t in self.transports))

    async def test_extracts_html_markdown_tables_title_and_excludes_scripts(self):
        html = '<html><head><title>Tea 茶</title></head><body><article><h1>Tea 茶</h1>' + (
            '<p>Tea is a drink brewed from leaves. Friends share tea and stories at the cafe.</p>' * 5
        ) + '<table><tr><th>Drink</th><th>Price</th></tr><tr><td>Tea</td><td>$3</td></tr></table>' \
            '<script>secret-script-marker</script><img src="x"/>' \
            '</article></body></html>'
        self.serve(lambda req: httpx.Response(200, headers={'content-type': 'Text/HTML; charset=utf-8'}, text=html))
        result = await self.fetch('HTTPS://EXAMPLE.COM:443/article#part')
        self.assertEqual(result['requested_url'], 'https://example.com/article')
        self.assertEqual(result['final_url'], 'https://example.com/article')
        self.assertEqual(result['title'], 'Tea 茶')
        self.assertIn('Tea is a drink', result['content'])
        self.assertIn('$3', result['content'])
        self.assertNotIn('secret-script-marker', result['content'])
        self.assertNotIn('<p>', result['content'])

    async def test_preserves_html_links_and_visible_urls_without_fetching_targets(self):
        html = '''<html><body><article><h1>A guide to tea</h1>
            <p>Tea is brewed from leaves using warm water. Read the
            <a href="https://example.org/tea?type=green&amp;size=2#brewing">brewing guide</a>
            to learn how water temperature and steeping time affect the flavour.</p>
            <p>Friends share tea and stories at the cafe. More tasting notes are
            available at https://example.net/tasting for anyone interested in tea.</p>
            </article></body></html>'''
        self.serve(lambda req: httpx.Response(200, headers={'content-type': 'text/html'}, text=html))
        result = await self.fetch()
        self.assertEqual([str(req.url) for req in self.requests], ['https://example.com/article'])
        self.assertIn('[brewing guide](https://example.org/tea?type=green&size=2#brewing)', result['content'])
        self.assertIn('https://example.net/tasting', result['content'])

    async def test_resolves_html_links_against_final_redirect_url(self):
        html = '''<html><body><article><h1>A guide to tea</h1>
            <p>Tea is brewed from leaves using warm water. Read our
            <a href="../guides/brewing?type=green&amp;size=2#steps">brewing instructions</a>
            to learn how water temperature and steeping time affect the flavour.</p>
            <p>Friends share tea and stories at the cafe. Visit our
            <a href="/shops">tea shops</a> to find a comfortable place to try a new drink.</p>
            </article></body></html>'''

        def handler(req):
            if req.url.path == '/article':
                return httpx.Response(302, headers={'location': '/tea/articles/current'})
            return httpx.Response(200, headers={'content-type': 'text/html'}, text=html)

        self.serve(handler)
        result = await self.fetch()
        self.assertEqual(result['final_url'], 'https://example.com/tea/articles/current')
        self.assertEqual([req.url.path for req in self.requests], ['/article', '/tea/articles/current'])
        self.assertIn('[brewing instructions](https://example.com/tea/guides/brewing?type=green&size=2#steps)',
                      result['content'])
        self.assertIn('[tea shops](https://example.com/shops)', result['content'])

    async def test_resolves_html_links_using_document_base(self):
        for base, target in (
            ('../docs/', 'https://example.com/tea/docs/brewing'),
            ('https://example.org/guides/', 'https://example.org/guides/brewing'),
        ):
            with self.subTest(base=base):
                html = f'''<html><head><base href="{base}"></head><body><article>
                    <h1>A guide to tea</h1>
                    <p>Tea is brewed from leaves using warm water. Read our
                    <a href="brewing">brewing instructions</a> to learn how water
                    temperature and steeping time affect the flavour.</p>
                    </article></body></html>'''
                self.requests.clear()
                self.serve(lambda req: httpx.Response(200, headers={'content-type': 'text/html'}, text=html))
                result = await self.fetch('https://example.com/tea/articles/current')
                self.assertEqual([req.url.path for req in self.requests], ['/tea/articles/current'])
                self.assertIn(f'[brewing instructions]({target})', result['content'])

    async def test_empty_html_does_not_turn_metadata_into_content(self):
        self.serve(lambda req: httpx.Response(200, headers={'content-type': 'application/xhtml+xml'},
                                             text='<html><head><title>Metadata only</title></head><body></body></html>'))
        result = await self.fetch()
        self.assertEqual(result['content'], '')
        self.assertFalse(result['truncated'])

    async def test_truncates_extracted_text_at_character_limit(self):
        self.serve(lambda req: httpx.Response(200, headers={'content-type': 'text/plain'}, text='茶' * 12001))
        result = await self.fetch()
        self.assertEqual(result['content'], '茶' * 12000)
        self.assertTrue(result['truncated'])

    async def test_relative_redirects_revalidated_and_no_cookies_forwarded(self):
        def handler(req):
            if req.url.path == '/article':
                return httpx.Response(302, headers={'location': '/next#section', 'set-cookie': 'session=secret'})
            return httpx.Response(200, headers={'content-type': 'text/plain'}, text='redirected')
        self.serve(handler)
        result = await self.fetch()
        self.assertEqual(result['final_url'], 'https://example.com/next')
        self.assertEqual(result['content'], 'redirected')
        self.assertEqual(self.dns.await_count, 2)
        self.assertTrue(all('cookie' not in r.headers for r in self.requests))
        self.assertTrue(all(t.closed for t in self.transports))

    async def test_redirect_limit_and_redirect_to_private_rejected(self):
        self.serve(lambda req: httpx.Response(302, headers={'location': '/again'}))
        with self.assertRaisesRegex(ValueError, 'redirect'):
            await self.fetch()
        self.assertEqual(len(self.requests), 4)
        with patch.object(fetching, '_PinnedTransport', return_value=httpx.MockTransport(
            lambda req: httpx.Response(302, headers={'location': 'http://127.0.0.1/private'})
        )):
            with self.assertRaisesRegex(ValueError, 'public'):
                await self.fetch()

    async def test_invalid_destinations_never_reach_transport(self):
        self.serve(lambda req: self.fail('unsafe destination reached transport'))
        for url in (
            'http://127.0.0.1', 'http://10.1.2.3', 'http://169.254.169.254/latest',
            'http://[fec0::1]', 'http://[::1]', 'http://[fc00::1]', 'http://[fe80::1]', 'http://[::ffff:127.0.0.1]',
            'http://0.0.0.0', 'http://224.0.0.1', 'http://100.64.0.1', 'http://192.0.0.8',
            'http://2130706433', 'http://0177.0.0.1', 'http://0x7f000001', 'http://127.1',
            'http://localhost', 'http://metadata.google.internal',
            'https://user:pass@example.com', 'https://example.com:0', 'https://example.com:8443', 'http://example.com:443',
            'file:///etc/passwd', 'http://[fe80::1%25en0]',
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                await self.fetch(url)
        self.assertEqual(self.requests, [])

    async def test_mixed_or_empty_dns_answers_rejected(self):
        self.serve(lambda req: self.fail('unsafe DNS reached transport'))
        for addresses in ([], ['93.184.216.34', '127.0.0.1'], ['::1'], ['169.254.169.254']):
            self.dns.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (ip, 443)) for ip in addresses]
            with self.subTest(addresses=addresses), self.assertRaisesRegex(ValueError, 'public'):
                await self.fetch()

    async def test_http_missing_unsupported_and_extraction_errors_close_resources(self):
        for status, headers, expected in (
            (403, {}, 'HTTP 403'), (500, {}, 'HTTP 500'),
            (200, {}, 'content type'), (200, {'content-type': 'application/pdf'}, 'content type'),
            (302, {}, 'redirect'),
        ):
            body = Body([b'failure body'])
            self.serve(lambda req: httpx.Response(status, headers=headers, stream=body))
            with self.subTest(status=status, headers=headers), self.assertRaisesRegex(ValueError, expected):
                await self.fetch()
            self.assertTrue(body.closed)
            self.assertTrue(self.transports[-1].closed)
        self.serve(lambda req: httpx.Response(200, headers={'content-type': 'text/html'}, text='<p>hello</p>'))
        with patch.object(fetching, '_extract_html', side_effect=RuntimeError('internal-secret')):
            with self.assertRaisesRegex(ValueError, '^Page extraction failed$'):
                await self.fetch()

    async def test_oversized_decoded_stream_is_rejected_and_closed(self):
        for chunks, headers in (
            ([b'a' * (1024 * 1024), b'b' * (1024 * 1024), b'c'], {}),
            ([gzip.compress(b'x' * (2 * 1024 * 1024 + 1))], {'content-encoding': 'gzip'}),
        ):
            body = Body(chunks)
            self.serve(lambda req: httpx.Response(200, headers={'content-type': 'text/plain', **headers}, stream=body))
            with self.assertRaisesRegex(ValueError, 'too large'):
                await self.fetch()
            self.assertTrue(body.closed)
            self.assertTrue(self.transports[-1].closed)

    async def test_timeout_and_cancellation_close_stream_and_client(self):
        for cancel in (False, True):
            body = Body([b'partial'], wait=True)
            self.serve(lambda req: httpx.Response(200, headers={'content-type': 'text/plain'}, stream=body))
            with patch.object(fetching, 'URL_TIMEOUT', 0.03):
                if cancel:
                    task = asyncio.create_task(self.fetch())
                    await asyncio.sleep(0.01)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaises(TimeoutError):
                        await self.fetch()
            self.assertTrue(body.closed)
            self.assertTrue(self.transports[-1].closed)

    async def test_dns_and_extraction_wait_are_inside_deadline(self):
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        with patch.object(asyncio.get_running_loop(), 'getaddrinfo', hang), patch.object(fetching, 'URL_TIMEOUT', 0.01):
            with self.assertRaises(TimeoutError):
                await self.fetch()
        released = threading.Event()
        self.addCleanup(released.set)
        self.serve(lambda req: httpx.Response(200, headers={'content-type': 'text/html'}, text='<p>hello</p>'))
        with patch.object(fetching, '_extract_html', side_effect=lambda *a: released.wait(1)), patch.object(fetching, 'URL_TIMEOUT', 0.03):
            with self.assertRaises(TimeoutError):
                await self.fetch()
        released.set()
        self.assertTrue(self.transports[-1].closed)


class PinnedTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_uses_validated_ip_with_original_host_and_tls_hostname(self):
        # Exercise HTTPX + HTTPCore down to its socket boundary, without a socket.
        import httpcore
        for address in ('93.184.216.34', '2606:4700:4700::1111'):
            stream = AsyncMock(spec=httpcore.AsyncNetworkStream)
            stream.start_tls.return_value = stream
            stream.get_extra_info.return_value = None
            stream.read.return_value = b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok'
            backend = AsyncMock(spec=httpcore.AsyncNetworkBackend)
            backend.connect_tcp.return_value = stream
            with patch('httpcore._backends.auto.AutoBackend.connect_tcp', backend.connect_tcp):
                async with httpx.AsyncClient(transport=fetching._PinnedTransport(address), trust_env=False) as client:
                    response = await client.get('https://example.com/Case?q=1')
            self.assertEqual(response.text, 'ok')
            self.assertEqual(backend.connect_tcp.call_args.kwargs['host'], address)
            self.assertEqual(stream.start_tls.call_args.kwargs['server_hostname'], 'example.com')
            written = b''.join(c.args[0] for c in stream.write.call_args_list)
            self.assertIn(b'Host: example.com\r\n', written)
            self.assertIn(b'GET /Case?q=1 HTTP/1.1', written)
