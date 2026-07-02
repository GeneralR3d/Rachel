"""Rachel's persistent "world view" — durable, general facts she has learned.

This is a self-contained LangGraph pipeline that runs once per conversation,
after the CHAT_BLACKOUT_TIME flush (see app/telegram/client.py::finalize_conversation).

Two nodes run **sequentially**:

  START → fact_extractor_node → (any new facts?) → ingest_node → END
                                       └── no ──→ END

- fact_extractor_node: reads the just-finished dialogue and pulls out new,
  permanent facts about the world. Returns nothing if it learned nothing new
  (short-circuits straight to END — no graph write).
- ingest_node: writes each extracted fact into **Graphiti** (a temporal
  knowledge graph backed by Neo4j) as one episode per fact. Graphiti performs
  its own entity/edge extraction, de-duplication, and temporal conflict
  resolution on ingest, so there is no hand-rolled consolidation step anymore.

Retrieval: ``search_worldview()`` runs a Graphiti hybrid search and returns the
matching facts as a ``- ``-bulleted string; it is exposed to the context_fetcher
node as the ``search_world_view`` LangChain tool. The context_fetcher generates a
conversation-aware query and the result is threaded into the responder as
``{world_view}`` (see app/services/llm.py).
"""

import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

import tiktoken

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.tools import tool
from langchain_openrouter import ChatOpenRouter
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field, ValidationError
from typing_extensions import TypedDict

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.prompts.models import Message
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

from app.config import get_settings
from app.prompts import FACT_EXTRACTOR_SYSTEM_PROMPT

settings = get_settings()
BOT_NAME = settings.bot_name
_WORLDVIEW_PATH = Path(settings.worldview_path)

# Graphiti partition for Rachel's general world-view memory. All world-view
# episodes are written under this group_id and searches are scoped to it, so that
# future per-user memory graphs (each with their own group_id) never clash with
# it or each other.
_WORLDVIEW_GROUP_ID = "worldview"

SGT = timezone(timedelta(hours=8))

_tokenizer = tiktoken.get_encoding("cl100k_base")


def _count_tokens(text: str) -> int:
    return len(_tokenizer.encode(text))


# Graphiti mutates a single shared graph per add_episode (it dedups against and
# invalidates existing edges), so concurrent episode writes can race. This lock
# serialises both lazy client init and the per-conversation ingest loop across
# chats finishing near-simultaneously (single-process only).
_graphiti_lock = asyncio.Lock()
_graphiti: Graphiti | None = None


# --- Structured outputs ------------------------------------------------------


class ExtractorOutput(BaseModel):
    facts: List[str] = Field(
        default_factory=list,
        description="New, permanent facts as short standalone sentences. Empty if nothing new was learned.",
    )


class WorldviewState(TypedDict):
    conversation: List[Dict[str, Any]]
    extracted_facts: List[str]
    # Originating chat, threaded through purely so node logs can be tagged and
    # disambiguated when multiple chats finalize concurrently.
    chat_id: int | None


def _tag(chat_id: int | None) -> str:
    """Log prefix for worldview lines, with the chat id when we know it."""
    return f"[worldview][{chat_id}]" if chat_id is not None else "[worldview]"


# Uses the Graphiti key too: this extractor is the front half of the same
# world-view memory pipeline that feeds Graphiti, so all its cost is billed to
# the same separate OpenRouter key.
_extractor_llm = ChatOpenRouter(
    model=settings.openrouter_model,
    api_key=settings.graphiti_api_key,
    temperature=0.0,
).with_structured_output(ExtractorOutput)


# --- Graphiti client ---------------------------------------------------------


class _RetryingOpenAIGenericClient(OpenAIGenericClient):
    """OpenAIGenericClient that re-rolls when the model returns valid JSON whose
    *shape* doesn't match the requested response_model.

    Graphiti validates the returned dict against its own pydantic models (e.g.
    ``ExtractedEdges`` / ``NodeResolutions``) **after** ``generate_response``
    returns, so the client's built-in tenacity retry — which only fires on
    transport errors (rate-limit / empty / 5xx / JSON-decode) — never sees these
    schema-*shape* mismatches. deepseek-v4-flash occasionally emits e.g. a
    pipe-delimited header string in place of a list of edge objects; a re-roll
    with a corrective note usually fixes it. We validate here (where the
    response_model is available) and retry a few times before giving up. On
    exhaustion the last result is returned unchanged, so behaviour is never worse
    than the un-wrapped client (Graphiti raises the same ValidationError it would
    have anyway).
    """

    _VALIDATION_RETRIES = 3

    async def _generate_response(
        self,
        messages: list[Message],
        response_model: type[BaseModel] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        model_size: ModelSize = ModelSize.medium,
    ) -> Dict[str, Any]:
        # Work on a copy so corrective appends don't accumulate across the outer
        # tenacity retries, which reuse the caller's message list.
        working = [Message(role=m.role, content=m.content) for m in messages]
        result: Dict[str, Any] = {}
        for attempt in range(self._VALIDATION_RETRIES + 1):
            result = await super()._generate_response(
                working, response_model, max_tokens, model_size
            )
            if response_model is None:
                return result
            try:
                response_model.model_validate(result)
                return result
            except ValidationError as e:
                name = getattr(response_model, "__name__", "response")
                if attempt == self._VALIDATION_RETRIES:
                    print(
                        f"[worldview] structured-output still invalid after "
                        f"{attempt + 1} attempts ({name}); passing through"
                    )
                    return result
                print(
                    f"[worldview] structured-output invalid ({name}, "
                    f"attempt {attempt + 1}); retrying"
                )
                working.append(
                    Message(
                        role="user",
                        content=(
                            "Your previous JSON did not match the required schema and "
                            f"failed validation with:\n{e}\n\nRespond again with ONE JSON "
                            "object that strictly matches the schema. Every array element "
                            "must be a JSON object with the specified fields — never a "
                            "string or a header line."
                        ),
                    )
                )
        return result


async def _get_graphiti() -> Graphiti:
    """Return the process-wide Graphiti client, initialising it on first use.

    Both the LLM and the embedder are pointed at OpenRouter's OpenAI-compatible
    endpoint, so a single OPENROUTER_API_KEY covers extraction, embeddings, and
    reranking. ``build_indices_and_constraints`` is idempotent and only needs to
    run once per database, so we call it as part of the one-time init. Guarded by
    ``_graphiti_lock`` (double-checked) so concurrent finalizations don't double-init.
    """
    global _graphiti
    if _graphiti is not None:
        return _graphiti
    async with _graphiti_lock:
        if _graphiti is None:
            g = Graphiti(
                settings.neo4j_uri,
                settings.neo4j_user,
                settings.neo4j_password,
                llm_client=_RetryingOpenAIGenericClient(
                    config=LLMConfig(
                        api_key=settings.graphiti_api_key,
                        model=settings.openrouter_model,
                        small_model=settings.openrouter_small_model,
                        base_url=settings.openrouter_base_url,
                    ),
                    # OpenRouter downgrades json_schema to a plain json_object for
                    # models without native constrained decoding, so the schema's
                    # field names are never enforced — the dedup model then emits
                    # `resolutions` instead of `entity_resolutions` and NodeResolutions
                    # fails to validate. In json_object mode Graphiti instead embeds
                    # the full schema (field names included) into the prompt text, so
                    # the model is explicitly told the required keys.
                    structured_output_mode="json_object",
                ),
                embedder=OpenAIEmbedder(
                    config=OpenAIEmbedderConfig(
                        api_key=settings.graphiti_api_key,
                        embedding_model=settings.openrouter_embedding_model,
                        base_url=settings.openrouter_base_url,
                    )
                ),
                cross_encoder=OpenAIRerankerClient(
                    config=LLMConfig(
                        api_key=settings.graphiti_api_key,
                        model=settings.openrouter_small_model,
                        base_url=settings.openrouter_base_url,
                    )
                ),
            )
            # build_indices_and_constraints re-issues CREATE INDEX ... IF NOT
            # EXISTS for every index. On an already-initialised DB Neo4j raises
            # EquivalentSchemaRuleAlreadyExists, which graphiti catches and
            # treats as safe — but the driver still logs each at ERROR first,
            # burying every restart in benign red. Silence just that driver
            # logger for the duration of this idempotent call; real query errors
            # during ingestion (which runs after init) still log normally.
            neo4j_logger = logging.getLogger("graphiti_core.driver.neo4j_driver")
            prev_level = neo4j_logger.level
            neo4j_logger.setLevel(logging.DEBUG)
            try:
                await g.build_indices_and_constraints()
            finally:
                neo4j_logger.setLevel(prev_level)
            _graphiti = g
            print("[worldview] Graphiti client initialised")
    return _graphiti


# --- Retrieval ---------------------------------------------------------------


async def search_worldview(query: str | None = None) -> str:
    """Retrieve relevant world-view facts from Graphiti as a ``- ``-bulleted string.

    ``query`` is the search text (now generated by the context_fetcher node — see
    app/services/llm.py). Uses Graphiti's advanced ``search_`` with the
    COMBINED_HYBRID_SEARCH_RRF recipe, but we harvest only two of the layers it
    returns:

    - **edges**: the atomic, complete, never-truncated relationship facts (BM25 +
      cosine similarity — the semantic-recall path).
    - **episodes**: the verbatim original fact sentences we ingested (BM25 only —
      the episode layer has no vector method — so keyword overlap, not semantics).

    We deliberately drop the recipe's **node** results: an entity node's summary is
    a lossy, 1000-char-truncated re-statement of the very edge facts we already
    read directly, so it only adds duplication.

    Returns the matched facts (edge facts first, then episode bodies) de-duplicated
    and joined into a ``- ``-bulleted markdown block, or "" when there's no query,
    no match, or on any error — retrieval must never crash the caller.
    """
    if not query or not query.strip():
        return ""
    try:
        graphiti = await _get_graphiti()
        results = await graphiti.search_(
            query.strip(),
            config=COMBINED_HYBRID_SEARCH_RRF,
            group_ids=[_WORLDVIEW_GROUP_ID],
        )
    except Exception as e:
        print(f"[worldview] search failed: {type(e).__name__}: {e}")
        traceback.print_exc()
        return ""

    edge_facts = [
        fact
        for edge in results.edges
        if (fact := (getattr(edge, "fact", "") or "").strip())
    ]
    episode_facts = [
        content
        for episode in results.episodes
        if (content := (getattr(episode, "content", "") or "").strip())
    ]

    print(f"[worldview] search for {query.strip()!r}:")
    print(f"[worldview]   edges ({len(edge_facts)}):")
    print(f"[worldview]   episodes ({len(episode_facts)}):")

    # De-duplicate while preserving order (edge facts first), then bullet.
    facts = list(dict.fromkeys([*edge_facts, *episode_facts]))
    return "\n".join(f"- {fact}" for fact in facts) if facts else ""


@tool
async def search_world_view(query: str) -> str:
    """Search Rachel's world-view knowledge base of general facts.

    The world-view database stores short, single-sentence general facts Rachel has
    learned about the world from past conversations (about brands, places, people,
    events. Use this to look up what Rachel already knows about whatever the conversation is touching on, so the
    responder can reply accurately instead of guessing.

    Args:
        query: A focused, natural-language description of what to look up, derived
            from what the conversation is about (e.g. "Chagee bubble tea brand").
            Do not just copy the last message verbatim — capture the topic/entity.
    """
    return await search_worldview(query) or "No relevant facts found."


# --- Nodes -------------------------------------------------------------------


async def fact_extractor_node(state: WorldviewState) -> Dict:
    tag = _tag(state.get("chat_id"))
    # History is built as concrete Message objects (not templated tuples) so any
    # literal '{' or '}' in user content is passed through verbatim rather than
    # parsed as an f-string placeholder, which would crash from_messages.
    history_msgs = [
        AIMessage(content=f"{entry['sender']}: {entry['content']}")
        if entry["sender"] == BOT_NAME
        else HumanMessage(content=f"{entry['sender']}: {entry['content']}")
        for entry in state["conversation"]
    ]
    system_msgs = ChatPromptTemplate.from_messages(
        [("system", FACT_EXTRACTOR_SYSTEM_PROMPT)]
    ).format_messages(bot_name=BOT_NAME)
    msgs = [*system_msgs, *history_msgs]
    msgs_tokens = sum(_count_tokens(str(m.content)) for m in msgs)
    print(f"{tag} extractor context: {len(msgs)} messages, {msgs_tokens} tokens")
    result: ExtractorOutput = await _extractor_llm.ainvoke(msgs)
    facts = [f.strip() for f in result.facts if f.strip()]
    print(f"{tag} extracted {len(facts)} new fact(s): {facts}")
    return {"extracted_facts": facts}


async def ingest_node(state: WorldviewState) -> Dict:
    """Write each extracted fact into Graphiti as its own episode.

    Episodes are added sequentially under ``_graphiti_lock`` so that conversations
    finishing at the same time don't race the shared graph. Graphiti does the
    dedup / conflict resolution itself, so no consolidation pass is needed.
    """
    tag = _tag(state.get("chat_id"))
    facts = state["extracted_facts"]
    graphiti = await _get_graphiti()
    # World-view facts are general knowledge, not chat-scoped, so the episode
    # name is deliberately chat-agnostic — only timestamped to stay unique.
    now = datetime.now(SGT)
    async with _graphiti_lock:
        for i, fact in enumerate(facts):
            # What graphiti.add_episode() does under the hood (see
            # graphiti_core/graphiti.py::add_episode). A single call runs this
            # pipeline synchronously against the shared Neo4j graph:
            #
            #   1. retrieve_episodes         — pull the last N previous episodes
            #                                  (same group_id) as context for the
            #                                  extraction LLM calls below.
            #   2. extract_nodes             — LLM reads episode_body + context and
            #                                  extracts candidate entity nodes.
            #   3. resolve_extracted_nodes   — dedup those candidates against nodes
            #                                  already in the graph (existing node
            #                                  reused instead of a duplicate created).
            #   4. _extract_and_resolve_edges— LLM extracts relationships (edges)
            #                                  between the nodes, then resolves them
            #                                  against existing edges AND invalidates
            #                                  (temporally expires) any that this new
            #                                  fact contradicts — "newer info wins".
            #   5. extract_attributes_from_nodes — hydrate each node's summary /
            #                                  attributes from the new edges.
            #   6. _process_episode_data     — persist the episode, nodes, edges and
            #                                  their embeddings to Neo4j.
            #   7. update_communities        — (off; we pass update_communities=False)
            #                                  optional clustering of nodes into
            #                                  community super-nodes.
            #   8. logs "Completed add_episode in <ms> ms" at INFO and returns
            #      AddEpisodeResults(episode, episodic_edges, nodes, edges, ...).
            #
            # Steps 2/4/5 are LLM calls, so each add_episode is several round-trips —
            # this is why ingestion is slow and must stay sequential + awaited.
            # Every step emits DEBUG logs under the "graphiti_core" logger namespace.
            await graphiti.add_episode(
                name=f"worldview-{int(now.timestamp())}-{i}",
                episode_body=fact,
                source=EpisodeType.text,
                source_description="Rachel worldview fact",
                reference_time=now,
                group_id=_WORLDVIEW_GROUP_ID,
            )
    print(f"{tag} ingested {len(facts)} fact(s) into Graphiti")
    return {}


def _route_after_extraction(state: WorldviewState) -> str:
    """Skip ingestion when nothing new was learned."""
    return "ingest_node" if state["extracted_facts"] else END


def _build_graph():
    graph: StateGraph = StateGraph(WorldviewState)
    graph.add_node("fact_extractor_node", fact_extractor_node)
    graph.add_node("ingest_node", ingest_node)
    graph.add_edge(START, "fact_extractor_node")
    graph.add_conditional_edges(
        "fact_extractor_node",
        _route_after_extraction,
        {"ingest_node": "ingest_node", END: END},
    )
    graph.add_edge("ingest_node", END)
    return graph.compile()


_graph = _build_graph()


async def update_worldview(
    conversation: List[Dict[str, str]], chat_id: int | None = None
) -> List[str] | None:
    """Extract facts from a finished conversation and ingest them into Graphiti.

    Returns the list of newly ingested facts, or None when nothing new was
    learned (or on error). Never raises — memory upkeep must not crash the caller.
    """
    if not conversation:
        return None

    tag = _tag(chat_id)
    state: WorldviewState = {
        "conversation": conversation,
        "extracted_facts": [],
        "chat_id": chat_id,
    }
    try:
        result = await _graph.ainvoke(state)
    except Exception as e:  # never let memory upkeep crash the caller
        print(f"{tag} update failed: {e}")
        traceback.print_exc()
        return None

    facts = result["extracted_facts"]
    if not facts:
        print(f"{tag} nothing new from chat; graph unchanged")
        return None

    print(f"{tag} graph updated ({len(facts)} fact(s)) after chat")
    return facts
