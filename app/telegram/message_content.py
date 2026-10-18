"""Render Telegram text, hidden hyperlinks and media without a live client."""

from telethon.tl.types import MessageEntityTextUrl


def render_message_content(message) -> str:
    """Preserve usable text and link targets before buffering or persistence.
    Exposes URLs hidden in hyperlinks, and indicates the presence of media without downloading it. 
    """
    text = (message.raw_text or '').strip()
    targets = set(text.split())
    # Telethon's helper interprets entity offsets as UTF-16, including emoji.
    for entity, label in message.get_entities_text(MessageEntityTextUrl) if getattr(message, 'entities', None) else ():
        target = entity.url
        if target != label and target not in targets and f'<{target}>' not in targets:
            text += ('\n' if text else '') + f'<{target}>'
            targets.add(target)
    if text:
        return text
    if getattr(message, 'sticker', None) is not None:
        emoji = getattr(getattr(message, 'file', None), 'emoji', None)
        return f'[sticker: {emoji}]' if emoji else '[sticker]'
    for field, label in (
        ('gif', 'gif'), ('video_note', 'video note'), ('video', 'video'),
        ('voice', 'voice message'), ('audio', 'audio'), ('photo', 'photo'), ('document', 'file'),
    ):
        if getattr(message, field, None) is not None:
            return f'[{label}]'
    return ''
