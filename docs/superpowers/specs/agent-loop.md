 # Rachel’s Bounded Context–Responder Agent Loop

  ## Summary

  Convert the reply pipeline into LangGraph’s evaluator–optimizer pattern:

  checker/router
        ↓
  summarizer ───────┐
  context_fetcher ──┴→ responder
                         ├─ sufficient → END
                         └─ fetch_more → context_fetcher
                                            ↑       │
                                            └───────┘

  The responder evaluates context sufficiency, but only the context fetcher may select and execute tools. The loop accumulates observations and stops gracefully when it succeeds or reaches its
  configured budget.

  This follows LangGraph’s documented evaluator–optimizer workflow (https://docs.langchain.com/oss/python/langgraph/workflows-agents), raw-state guidance, and proactive recursion-limit handling
  (https://docs.langchain.com/oss/python/langgraph/graph-api).

  ## Implementation Changes

  - Extend ResponseOutput with:
      - decision: Literal["respond", "fetch_more"]
      - content: always required; for fetch_more, this is the best-effort clarification reply used if the loop cannot continue.
      - reason: existing traceability explanation.
      - retrieval_request: precise description of missing information; empty for respond.

  - Replace the four overwrite-prone context strings with accumulated raw ToolObservation records containing pass number, tool name, arguments, result category, status, and raw result. Format these into
    schedule, worldview, user-fact, and profile prompt sections only inside the responder. This follows LangGraph’s recommendation to keep state raw and format prompts inside nodes
    (https://docs.langchain.com/oss/python/langgraph/thinking-in-langgraph).

  - Add loop state:
      - context_passes
      - retrieval_request
      - tool_observations
      - remaining_steps: RemainingSteps
      - responder decision/status fields

  - Update the context fetcher so every subsequent pass receives:
      - The responder’s retrieval request.
      - Previously collected observations.
      - The original conversation and participant list.

  - Preserve all successful results across passes. Record tool errors and timeouts as observations so the responder can adapt instead of seeing silently empty context.
  - Allow the agent to repeat tool calls and retry empty, failed, or timed-out lookups. Every pass counts toward the configured budget regardless of its results; the application does not attempt to detect retrieval progress.

  - Teach the responder about available capability categories without letting it choose tool calls directly. It must request another pass only when an existing tool could plausibly supply the missing
    information; unsupported questions should immediately receive an honest answer or clarification request.

  - Add a conditional edge after responder_node:
      - respond → END
      - fetch_more with budget remaining → context_fetcher_node
      - fetch_more at the cap, near the LangGraph recursion limit → END using the already-generated fallback content

  - Keep the summarizer outside the loop: it runs once and is not repeated during retrieval refinement.
  - Add AGENT_MAX_CONTEXT_PASSES to Settings, validated from 1–10 and defaulting to 3. Count the initial context fetch as pass 1.
  - Invoke the graph with an explicit recursion_limit safely above the configured pass budget. Use RemainingSteps for graceful internal termination; retain GraphRecursionError handling only as an emergency
    fallback. LangGraph explicitly recommends this proactive approach.

  - Keep the public get_response() tuple unchanged. No database migration, checkpointer, or Telegram-client contract change is required.
  - Add low-cardinality observability for loop outcome (first_pass, retried, capped) and context-pass count, alongside the existing per-node LLM metrics.

  ## LangGraph Version and Resources

  - Tighten the dependency from langgraph>=0.2 to langgraph>=1.2,<2 and update the lockfile from 1.2.4 to the latest tested 1.2 patch. The current official release listing shows LangGraph 1.2.12
    (https://github.com/langchain-ai/langgraph/releases).

  - Use conditional edges for the responder decision because routing remains deterministic Python logic. Command remains useful later when a node must atomically update state and choose a destination; its
    current semantics are documented in the Graph API (https://docs.langchain.com/oss/python/langgraph/graph-api).

  - Relevant newer capabilities to consider separately:
      - Node retry_policy, timeout, and error_handler for infrastructure failures; error_handler requires LangGraph 1.2.
      - set_node_defaults() for graph-wide retry and timeout policies.
      - Typed v2 streaming events if Telegram typing/progress updates are added later.
      - Checkpointers and durable execution if agent runs must survive process restarts.
      - Structured interrupt() responses for a future human-approval or user-clarification workflow.

  - Use LangGraph’s custom graph testing patterns (https://docs.langchain.com/oss/python/langgraph/test), compiling a fresh graph with mocked nodes for each routing scenario.

  ## Test Plan

  - Responder answers after the initial context pass.
  - Responder requests one refinement, receives accumulated results, then answers.
  - Maximum passes are enforced exactly and produce the best-effort clarification reply.
  - Repeated calls execute within and across passes, including previously failed calls. Empty or failed retrieval passes may continue until the responder answers or the configured pass cap is reached.
  - Tool errors and timeouts remain visible to the responder without crashing the run.
  - Unsupported information requests do not loop.
  - Previously collected schedule, worldview, facts, and profiles survive later passes.
  - Summarizer runs exactly once while context fetcher and responder may repeat.
  - Router rejection and no-new-message paths still bypass the entire agent loop.
  - Structured-output parse failures salvage a usable reply and terminate safely.
  - get_response() retains its existing return shape and mood/summary behavior.
  - Regenerate the LangGraph PNG and verify the new back-edge and exit paths.

  ## Assumptions

  - Version one uses only Rachel’s existing schedule, worldview, and per-user-memory tools.
  - Default budget is three total context-fetch passes, configurable through the environment.
  - At the cap, Rachel sends a natural best-effort response that acknowledges uncertainty and asks one focused follow-up question.
  - Agent state is ephemeral within one Telegram reply; durable checkpointing and web search remain separate future features.
