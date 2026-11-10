# Give Rachel web search through Exa

Planned on 2026-10-25. Updated on 2026-11-08 to use the official Python SDK.

## Summary

Add one context-fetcher tool, `search_web`, with two output modes: URLs or relevant source excerpts. The fetcher chooses per call, defaulting to URLs. Rachel can answer from sufficient excerpts or request another retrieval pass to read selected pages with `fetch_url`.

Use the official `exa-py` SDK with `from exa_py import AsyncExa` and `await exa.search(...)`. Rachel's Telegram clients, scheduler, and reply graphs share one asyncio event loop, so do not call the synchronous `Exa.search` directly inside the async tool. No new graph node or database migration is needed.

## Tool and configuration

- Add `app/services/web_search.py` with one async `@tool`: `search_web(query: str, mode: Literal["urls", "text"] = "urls", num_results: int = 5)`.
- Require a nonblank query and 1–10 results.
- Add `exa-py` to the project dependencies and update the lockfile during implementation. Use the SDK's async search interface with `type="auto"` and `num_results`.
- In URL mode, omit `contents` and return URL-only result entries. In text mode, request `contents={"highlights": True}` and return each result's URL, title, and source excerpts. Do not request Exa-generated summaries or answers.
- Return a structured envelope containing `query`, `mode`, and `results`. URL-mode results are URL strings. Text-mode results are records containing `url`, optional `title`, `highlights`, and `truncated`.
- Preserve result order and source attribution. Cap the combined excerpts at 2,000 characters per result and expose whether local truncation occurred. Retain links when excerpts are unavailable.
- Add optional `EXA_API_KEY` to settings and `template.env`. Pass the configured key explicitly to the SDK. Missing credentials must allow application startup and produce a clear tool error when called.
- Create the SDK client only when the tool is invoked and close it using the SDK's supported async lifecycle. Do not construct an authenticated client at import time.
- Apply a 15-second total tool deadline with `asyncio.timeout`, within the existing 30-second retrieval-pass deadline. Also set the SDK request timeout to 15 seconds; its network timeout alone is not a total wall-clock deadline.
- Surface sanitized authentication, HTTP, rate-limit, malformed-response, and timeout errors without application-level automatic retries. Translate SDK timeouts into the existing timeout observation path and preserve task cancellation.

## Retrieval and prompt integration

- Register `search_web` in `CONTEXT_TOOLS` under a `web_search` result category, shared by reactive and proactive graphs.
- Preserve existing observation statuses. An empty results list is `empty`; valid links remain successful results even when excerpts are missing.
- Generalize the URL observation helpers to handle both search and fetched-page observations. Supply their payloads, titles, and errors to both agents in a separate, explicitly untrusted data message, outside system instructions.
- Update the responder's main prompt and runtime loop instructions, which currently say web search is unavailable.
- Add fetcher guidance: search for relevant missing public facts, choose the output mode, reuse prior results, and fetch search-discovered URLs when necessary. Do not search for ordinary conversation or substitute public search for participant memory.
- Keep one tool-selection call per pass. Search results become available to model reasoning on the next responder invocation; dependent page fetches happen on a later pass. Do not add an internal search-and-fetch loop to the tool.
- Preserve the three-pass default and existing two-URL fetch allowance. Allow at most two Exa attempts per reply, deduplicating identical normalized argument sets across passes and counting failed or timed-out requests. Trim the query and resolve omitted defaults before comparing argument sets. Reserve attempts before awaiting the SDK so cancellation still consumes an attempt; invalid arguments do not consume an attempt.
- Keep successful search results and earlier observations when a subsequent tool or pass fails. Reuse identical earlier searches regardless of their outcome.
- Update README guidance and keep `AGENTS.md` and `CLAUDE.md` synchronized when documenting the implemented capability.

## Validation

- Mock the async SDK boundary to verify both search modes, query validation, result limits, excerpt bounds, missing credentials, empty results, missing excerpts, SDK failures, malformed responses, timeout translation, cancellation, and client cleanup. Tests must not require real Exa credentials or requests.
- Exercise both graphs: search → responder requests page details → URL fetch → grounded response.
- Verify excerpt-only answers and URL-only answers require no automatic page fetch.
- Verify deduplication, attempt reservation and limits, pass deadlines, preservation of partial work, and best-effort fallback at the pass cap.
- Extend prompt-boundary tests so search payloads remain outside system messages and appear once for each agent, including malicious-looking titles, excerpts, braces, errors, and Unicode.
- Update capability-prompt assertions and tool-registration tests to include search without losing existing tools.
- Run `uv run python -m unittest discover -s tests` and `uv run python -m compileall -q app scripts alembic`.
- With configured credentials, smoke-test both modes and one search-to-fetch conversation. Report explicitly if the live smoke test cannot run.

## Defaults and boundaries

URLs are the default; "text" means relevant excerpts. No Exa answer generation, full-page mode, search filters, persistent cache, dashboard controls, or automatic crawling in this version. The responder continues to request information in natural language; only the context fetcher calls tools.

The implementation uses the official async SDK instead of handwritten HTTP. Direct HTTP was originally proposed to reuse the existing client and avoid a dependency; the SDK now provides the preferred provider integration while the application retains control of tool output, deadlines, and retrieval budgets.

## References

- [Official Exa Python SDK: async usage and timeout behavior](https://github.com/exa-labs/exa-py#async)
- [Exa Search API](https://exa.ai/docs/reference/search)
- [Exa content guidance: source highlights](https://exa.ai/docs/contents/quickstart)
