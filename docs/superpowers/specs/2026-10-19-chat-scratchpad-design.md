# Temporary scratchpad with strict chat isolation

## Summary and research

Add a RAM-only scratchpad for each chat's current buffer session. **Rachel can operate only on the chat ID supplied by the application for that invocation.** The model cannot select or override the chat.

This follows an established agent pattern. [Deep Agents `StateBackend`](https://docs.langchain.com/oss/python/deepagents/backends) provides scratch files, [LangGraph `InMemoryStore`](https://reference.langchain.com/python/langgraph.store/memory/InMemoryStore) provides RAM-only storage, and [Anthropic's memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool) provides application-backed memory operations. Each would still require Rachel-specific limits and cleanup.

Use a small typed in-memory service with LangChain tools. No new dependency or database migration is needed.

## Storage and tool behavior

Each scratchpad holds ordered notes with generated IDs, titles, bodies, and optional source URLs.

- Limit each scratchpad to **20,000 characters**, counting titles, bodies, and source URLs using Python `len()`.
- Reject overflowing appends atomically and report remaining capacity.
- Read on demand. Automatically provide only note count and capacity usage to the fetcher and responder.
- Let Rachel choose which ideas or fetched content to save.

Expose these tools:

- **`get_scratchpad(note_ids=None)`** — read all notes or selected notes in the current chat's session.
- **`append_scratchpad(title, content=None, from_url=None)`** — append text or directly copy a successful URL result from the current graph invocation. Require exactly one content source.
- **`delete_scratchpad(note_ids)`** — delete selected notes; explicit `null` clears only the current chat's scratchpad.

Return note IDs and capacity information after mutations without echoing saved bodies. Reject empty notes and invalid arguments without changing storage.

20,000 characters is reasonable for storage: approximately 20–80 KB of string data before metadata. Its token cost varies substantially, so keep the contents out of prompts until requested. Rachel's existing 12,000-character URL limit means the pad fits one full fetched page plus notes, but may require excerpts for multiple pages.

## Mandatory isolation

Enforce ownership in code for every read, append, and delete:

- Derive the chat ID from the trusted Telegram event or proactive execution context, and capture the corresponding buffer-session ID.
- Supply that scope through application-controlled runtime configuration. **Neither ID appears in model-facing tool arguments.**
- Use strict tool schemas that reject extra arguments, including forged `chat_id`, session IDs, or configuration.
- Fail closed when trusted scope is missing or invalid. Never use a default chat or search globally.
- Resolve note IDs only inside the current chat and session. A foreign note ID behaves like a missing note without revealing whether it exists elsewhere.
- Resolve `from_url` only from successful observations belonging to the current scoped invocation.
- Expose no tool for listing chats, switching scope, or clearing multiple scratchpads.

The application may manage all scratchpads internally for lifecycle cleanup. Those capabilities remain unavailable to the model.

## Lifecycle and graph integration

Bind a scratchpad to each buffer session and capture its scope atomically with the history snapshot.

- Clear it after a successful inactivity or 150-message flush, explicit chat reset, and shutdown.
- Preserve it when buffer persistence fails.
- Invalidate the old session during cleanup. A model call continuing after a flush cannot recreate that session or modify the next session's scratchpad.
- Do not extend the inactivity timer for scratchpad operations.

Register the tools in both reactive and proactive graphs. Only the context fetcher executes them; the responder requests operations through the existing bounded loop. Preserve the current pass limit.

Render note content and text-bearing tool observations as untrusted data outside system messages. Do not directly persist scratchpad contents or feed them into memory extraction. Existing model/trace retention still applies; avoid console logging note bodies.

Update internal response interfaces to carry the captured session scope and synchronize `AGENTS.md` with `CLAUDE.md`. No new HTTP or dashboard surface is included.

## Verification

Test:

- Cross-turn retention and independent scratchpads for two chats.
- All three operations rejecting forged scope and missing runtime scope.
- Foreign note IDs being unreadable and unable to affect another chat.
- Clearing one chat leaving every other chat unchanged.
- Cross-chat URL-result references being unavailable.
- Exact capacity, Unicode counting, overflow rejection, and reclaimed capacity.
- Text saving and direct fetched-result copying without refetching.
- Inactivity/size flushes, reset, shutdown, and failed-flush retention.
- Concurrent flushes and stale calls being unable to alter a new session.
- Shared reactive/proactive behavior and untrusted prompt boundaries.

Run the Python regression suite and syntax checks.
