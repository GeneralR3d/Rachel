# Ordered silence confirmation

## Problem

In `app/telegram/client.py`, the "Rachel shush" branch in `new_message`
(lines ~614–618) arms silent mode and then immediately does
`await event.respond("Rachel has been silenced …")` inline.

If an earlier reply is still being produced when the shush arrives, the
confirmation can appear out of order:

- A reply may be **actively typing/sending** — it holds the chat's
  `reply_locks[chat_id]` entry across its multi-paragraph send with typing
  delays. An inline `event.respond` from `new_message` does not wait for that
  lock, so the "silenced" message jumps ahead of a reply Rachel is mid-way
  through delivering.
- A reply may be **scheduled but not yet firing** — sitting in its
  `REPLY_DELAY` (7s) sleep inside `wait_before_reply`.

## Goal

The "silenced" confirmation must never appear before an in-flight reply that
was already committed, and a reply that is still only scheduled should be
cancelled so Rachel goes quiet immediately.

## Approach

Reuse the existing per-chat serializer `reply_locks` — no new primitive.
`asyncio.Lock` acquisition is FIFO, so awaiting the lock places the
confirmation behind any in-flight reply.

Changes are confined to the shush branch of `new_message`:

1. **Skip if already silenced.** If `_is_silenced(chat_id)` is already true,
   do nothing (don't re-arm, don't re-confirm). Prevents spamming "shush" from
   queuing multiple confirmations.

2. **Cancel the pending reply timer.** If `wait_tasks[chat_id]` exists and is
   not done, cancel it. This aborts a reply still in its `REPLY_DELAY` sleep so
   Rachel goes quiet at once. A reply already past the sleep is
   `asyncio.shield`-ed and keeps sending — that is correct; the confirmation
   orders behind it via step 4.

3. **Arm `silent_until`** as today.

4. **Queue the confirmation behind the lock.** Instead of sending inline,
   spawn a detached task:

   ```python
   async def _send_silence_confirmation(event, chat_id, minutes):
       async with reply_locks.setdefault(chat_id, asyncio.Lock()):
           await event.respond(f"Rachel has been silenced for {minutes} minutes")

   asyncio.create_task(_send_silence_confirmation(event, chat_id, minutes))
   ```

   If a reply holds the lock, the confirmation waits for it to finish sending
   and appending its bot message; otherwise it sends immediately.

## Out of scope / unchanged

- `reply()`, `_reply()`, `wait_before_reply()` — untouched.
- Silent-mode checks (`_is_silenced`, `_is_silence_trigger`) and the
  mention-override path — untouched.
- Buffering/flush/memory pipelines for the trigger message — the shush message
  is still buffered and flushed exactly as before.

## Known limitation

There is a sub-millisecond window where a reply has passed its `REPLY_DELAY`
sleep and entered its shielded `reply()` but has not yet acquired the lock. If
the confirmation task acquires the lock first in that window, it could still
send ahead of that reply. This is rare (the common case — reply still
sleeping — is cancelled outright in step 2) and not worth heavier machinery.
Documented with a comment at the call site rather than solved.
