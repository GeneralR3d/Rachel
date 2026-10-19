"""RAM-only notes owned by active Telegram buffer sessions.

All operations are synchronous and contain no awaits: validation and mutation
are atomic on the application's single asyncio event loop. Only the buffer
lifecycle activates sessions; tool calls can never create or revive one.
"""

from dataclasses import asdict, dataclass, field
from uuid import uuid4


CAPACITY = 20_000


@dataclass(frozen=True)
class ScratchpadScope:
    chat_id: int
    session_id: str

    def __post_init__(self):
        if (type(self.chat_id) is not int or not self.chat_id
                or not isinstance(self.session_id, str) or not self.session_id.strip()):
            raise ValueError("Invalid scratchpad scope")


@dataclass(frozen=True)
class Note:
    id: str
    title: str
    content: str
    source_url: str | None = None

    @property
    def size(self) -> int:
        return len(self.title) + len(self.content) + len(self.source_url or "")


@dataclass
class _Pad:
    scope: ScratchpadScope
    notes: dict[str, Note] = field(default_factory=dict)


def _text(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Scratchpad text must be a non-empty string")


def _ids(note_ids: list[str] | None) -> None:
    if note_ids is not None:
        if not isinstance(note_ids, list) or not note_ids:
            raise ValueError("note_ids must be a non-empty list or null")
        for note_id in note_ids:
            _text(note_id)


class ScratchpadStore:
    def __init__(self):
        self._pads: dict[int, _Pad] = {}

    def activate(self, chat_id: int, session_id: str) -> ScratchpadScope:
        scope = ScratchpadScope(chat_id, session_id)
        pad = self._pads.get(chat_id)
        if pad is None or pad.scope != scope:
            self._pads[chat_id] = _Pad(scope)
        return scope

    def invalidate(self, chat_id: int) -> None:
        self._pads.pop(chat_id, None)

    def clear_all(self) -> None:
        self._pads.clear()

    def _pad(self, scope: ScratchpadScope) -> _Pad:
        if not isinstance(scope, ScratchpadScope):
            raise ValueError("Missing or invalid scratchpad scope")
        pad = self._pads.get(scope.chat_id)
        if pad is None or pad.scope != scope:
            raise ValueError("Scratchpad scope has expired")
        return pad

    def metadata(self, scope: ScratchpadScope) -> dict:
        pad = self._pad(scope)
        used = sum(note.size for note in pad.notes.values())
        return {"note_count": len(pad.notes), "used_characters": used,
                "capacity_characters": CAPACITY, "remaining_characters": CAPACITY - used}

    def read(self, scope: ScratchpadScope, note_ids: list[str] | None = None) -> dict:
        pad = self._pad(scope)
        _ids(note_ids)
        selected = None if note_ids is None else set(note_ids)
        return {**self.metadata(scope), "notes": [asdict(note) for note in pad.notes.values()
                if selected is None or note.id in selected]}

    def append(self, scope: ScratchpadScope, title: str, content: str,
               source_url: str | None = None) -> dict:
        pad = self._pad(scope)
        _text(title)
        _text(content)
        if source_url is not None:
            _text(source_url)
        note = Note(uuid4().hex, title, content, source_url)
        remaining = self.metadata(scope)["remaining_characters"]
        if note.size > remaining:
            raise ValueError(f"Scratchpad capacity exceeded; remaining capacity: {remaining} characters")
        pad.notes[note.id] = note
        return {"note_id": note.id, **self.metadata(scope)}

    def delete(self, scope: ScratchpadScope, note_ids: list[str] | None) -> dict:
        pad = self._pad(scope)
        _ids(note_ids)
        selected = None if note_ids is None else set(note_ids)
        deleted = [key for key in pad.notes if selected is None or key in selected]
        for key in deleted:
            del pad.notes[key]
        return {"deleted_note_ids": deleted, **self.metadata(scope)}


scratchpads = ScratchpadStore()
