"""Telegram entities and media render consistently before buffering or import."""
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telethon import events
from telethon.tl.types import Message, MessageEntityTextUrl, PeerUser

from app.telegram.message_content import render_message_content


def linked_message():
    return Message(id=7, peer_id=PeerUser(7), message='😀 read this', entities=[
        MessageEntityTextUrl(offset=3, length=9, url='https://example.com/Article#part'),
    ])


class MessageContentTests(unittest.TestCase):
    def test_hidden_link_after_emoji_survives_rendering(self):
        message = linked_message()
        self.assertEqual(render_message_content(message),
                         '😀 read this\n<https://example.com/Article#part>')

    def test_does_not_append_visible_or_duplicate_target(self):
        message = linked_message()
        message.message += ' https://example.com/Article#part'
        self.assertEqual(render_message_content(message), message.message)
        message = linked_message()
        message.message = render_message_content(message)
        self.assertEqual(render_message_content(message), message.message)

    def test_hidden_target_retains_query_punctuation(self):
        message = linked_message()
        message.entities[0].url = 'https://example.com/?q=hello!'
        rendered = render_message_content(message)
        self.assertEqual(rendered, '😀 read this\n<https://example.com/?q=hello!>')
        message.message = rendered
        self.assertEqual(render_message_content(message), rendered)

    def test_text_and_existing_media_placeholders(self):
        self.assertEqual(render_message_content(Message(id=1, peer_id=PeerUser(7), message='  caption {x}  ')), 'caption {x}')
        for field, expected in (
            ('sticker', '[sticker: 😀]'), ('gif', '[gif]'), ('video_note', '[video note]'),
            ('video', '[video]'), ('voice', '[voice message]'), ('audio', '[audio]'),
            ('photo', '[photo]'), ('document', '[file]'),
        ):
            attrs = dict.fromkeys(('sticker', 'gif', 'video_note', 'video', 'voice', 'audio', 'photo', 'document'))
            attrs.update(raw_text='', entities=[], file=SimpleNamespace(emoji='😀'))
            attrs[field] = object()
            with self.subTest(field=field):
                self.assertEqual(render_message_content(SimpleNamespace(**attrs)), expected)
        self.assertEqual(render_message_content(Message(id=1, peer_id=PeerUser(7), message='')), '')


class IngestionAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_incoming_and_import_use_same_renderer(self):
        from app.telegram import client as telegram
        message = linked_message()
        message.from_id = SimpleNamespace(user_id=7)
        event = SimpleNamespace(message=message, chat_id=42, is_group=False)
        self.assertEqual(telegram._message_content(event), '😀 read this\n<https://example.com/Article#part>')

        async def messages(*args, **kwargs):
            yield message

        with (
            patch.object(telegram.client, 'get_me', AsyncMock(return_value=SimpleNamespace(user_id=1))),
            patch.object(telegram.client, 'iter_messages', messages),
            patch.object(telegram.client, 'delete_messages', AsyncMock()),
            patch.object(telegram, 'get_history_min_id', AsyncMock(return_value=1)),
            patch.object(telegram, 'parse_history', return_value=[]) as parse,
            patch.object(telegram, 'rewrite_history', AsyncMock()),
        ):
            with self.assertRaises(events.StopPropagation):
                await telegram.on_update_history(event)
        self.assertEqual(parse.call_args.args[0], [(7, '😀 read this\n<https://example.com/Article#part>', 7)])
