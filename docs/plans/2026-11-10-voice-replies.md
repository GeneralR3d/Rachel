# Automatic voice replies for Rachel

## Summary

Give each non-empty reactive reply a 50% chance of becoming one Telegram voice note, replacing all its text paragraphs. Apply this to private and group chats. Scheduled outreach, commands, and confirmations remain text.

The choice happens automatically after response generation. The agent receives no voice tool or control over delivery mode. Only the sending portion changes: the full generated text must still be buffered, saved to the database, and processed through the normal conversation lifecycle in every delivery mode.

## Implementation

- Add a separate speech service using the official `AsyncElevenLabs` SDK and its [async streaming interface](https://github.com/elevenlabs/elevenlabs-python/blob/main/src/elevenlabs/text_to_speech/client.py). Add the dependency and update the lockfile.
- Configure through Rachel’s existing `.env`:
  - `ELEVENLABS_API_KEY`: optional; absent means text delivery.
  - `ELEVENLABS_VOICE_ID`: default `SEWXl8lPSO01tdGbWECX`.
  - `ELEVENLABS_MODEL_ID`: default `eleven_v4`.
- Request `opus_48000_64`, a [supported output format](https://github.com/elevenlabs/elevenlabs-python/blob/main/src/elevenlabs/text_to_speech/types/text_to_speech_stream_request_output_format.py). Consume chunks asynchronously into memory, then verify OGG/Opus headers. Bound generation to 60 seconds and 10 MiB; close streams and network resources on every exit.
- Add a reactive delivery helper in `app/telegram/client.py`. Make one random draw per complete reply: values below `0.5` select voice.
- For voice delivery, pass the entire generated response unchanged to the separate ElevenLabs speech function in one request, preserving all paragraphs and newlines. Bypass the text sender's paragraph splitting and simulated typing delays; send one voice note for the whole response. Text delivery and text fallback continue using the existing paragraph sender.
- Send successful audio through a rewound `BytesIO` named `voice.ogg`, using `voice_note=True` and no caption. Show the recording action during synthesis.
- Keep generation, delivery, and history insertion under the existing reply lock. Route the sent message ID back through the normal `_append_or_persist_bot_message` path, using the original full response and reason. For voice delivery, use the voice message's Telegram ID; for text delivery, retain the first text message's ID.
- Update setup documentation and synchronize the relevant guidance in `AGENTS.md` and `CLAUDE.md`.

## Text storage and processing invariants

- Preserve the exact full generated response as `BufferedMessage.content` and subsequently `History.content`, including all newlines, in voice, text, and text-fallback cases. Audio bytes, filenames, and media placeholders must never replace the generated text in conversation history.
- Preserve normal buffer insertion and ordering, database flushes, direct persistence if a concurrent flush removed the buffer, and history reloads. Voice delivery does not bypass or create a separate persistence path.
- Continue using the original full text for subsequent reply context, summarization, memory extraction, and admin history inspection. Audio is only an alternative outbound representation of the same response.

## Failure behavior and defaults

- Speech errors, timeouts, empty or invalid audio fall back to the existing text sender without regenerating the reply.
- Replies exceeding `eleven_v4`’s documented 10,000-character limit use text without truncating either delivery text or stored history. ([Model limits](https://elevenlabs.io/docs/overview/models/))
- Cancellation propagates. Telegram upload/send errors follow existing failure handling; they do not trigger a second text message that might duplicate a delivered voice note.
- No new agent schema, HTTP endpoint, database migration, audio storage, or conversion dependency is needed. Streaming ends in a completed file upload, rather than live playback.

## Validation

- Mock randomness to test both delivery branches and the exact `0.5` boundary.
- Verify multi-paragraph replies produce one captionless voice note with the exact full text, including newlines, passed to ElevenLabs in one request; the paragraph sender and simulated typing delays are not used for voice delivery.
- Test missing credentials, oversized text, SDK errors, interrupted streams, invalid audio, timeout, cancellation, and resource cleanup.
- Verify voice, text, and text-fallback delivery all retain the exact original response and reason in the buffer and database. Cover normal flush, concurrent-flush direct persistence, history reload, and subsequent context/memory processing; use the voice message ID for voice history.
- Confirm scheduled outreach stays text and reactive replies remain serialized.
- Run the Python regression suite and syntax checks. Automated tests mock ElevenLabs and Telegram.
