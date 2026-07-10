"""Shared Graphiti (Neo4j temporal knowledge graph) infrastructure.

Everything purely Graphiti-related lives here: the lazily-initialised
process-wide client, the OpenRouter structured-output workaround, and the
generic ingest / search / episode-listing helpers. The memory pipelines build
on top of this module:

- app/services/worldview.py — Rachel's general facts (group_id "worldview").
- app/services/userfacts.py — per-user personal facts (one group_id per user).

Each partition is just a Graphiti group_id inside the single ``neo4j`` database
(an explicit group_id is a no-op for the DB name on the Neo4j driver, which
doesn't override ``clone``), so partitions never clash with each other.
"""

import asyncio
import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from pydantic import BaseModel, ValidationError

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType, EpisodicNode
from graphiti_core.llm_client.config import DEFAULT_MAX_TOKENS, LLMConfig, ModelSize
from graphiti_core.llm_client.openai_generic_client import OpenAIGenericClient
from graphiti_core.prompts.models import Message
from graphiti_core.search.search_config_recipes import COMBINED_HYBRID_SEARCH_RRF
from graphiti_core.embedder.openai import OpenAIEmbedder, OpenAIEmbedderConfig
from graphiti_core.cross_encoder.openai_reranker_client import OpenAIRerankerClient

from app.config import get_settings
from app.services.metrics import LLM_CALLS, record_llm_error

settings = get_settings()

SGT = timezone(timedelta(hours=8))

# Graphiti mutates a single shared graph per add_episode (it dedups against and
# invalidates existing edges), so concurrent episode writes can race. This lock
# serialises both lazy client init and the per-conversation ingest loops across
# chats finishing near-simultaneously (single-process only).
_graphiti_lock = asyncio.Lock()
_graphiti: Graphiti | None = None


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

    async def generate_response(self, *args, **kwargs) -> Dict[str, Any]:
        """Public entry point Graphiti calls for every LLM round-trip (node
        extraction, node dedup, edge extraction/invalidation, attribute
        hydration). Counts each logical call and records a classified error when
        one ultimately fails — i.e. survives the client's own tenacity retry
        (transport/rate-limit/5xx). Structured-output *shape* mismatches don't
        raise here (see _generate_response); they're counted there on exhaustion."""
        LLM_CALLS.labels(node="graphiti_llm").inc()
        try:
            return await super().generate_response(*args, **kwargs)
        except Exception as e:
            kind = record_llm_error("graphiti_llm", e)
            print(f"[graphiti] LLM call failed ({kind}: {type(e).__name__}: {e})")
            raise

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
                    # Exhausted the re-rolls — Graphiti will raise this same
                    # ValidationError downstream (outside this client), so it
                    # never reaches generate_response's except. Count it here.
                    record_llm_error("graphiti_llm", e)  # -> kind=response_validation
                    print(
                        f"[graphiti] structured-output still invalid after "
                        f"{attempt + 1} attempts ({name}); passing through"
                    )
                    return result
                print(
                    f"[graphiti] structured-output invalid ({name}, "
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


async def get_graphiti() -> Graphiti:
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
            print("[graphiti] Graphiti client initialised")
    return _graphiti


# --- Search --------------------------------------------------------------------


async def search_graph(query: str | None, group_id: str, tag: str) -> str:
    """Retrieve relevant facts from one Graphiti partition as a ``- ``-bulleted string.

    ``query`` is the search text (generated by the context_fetcher node — see
    app/services/llm.py); ``group_id`` scopes the search to a single partition
    (the world view or one user's facts). Uses Graphiti's advanced ``search_``
    with the COMBINED_HYBRID_SEARCH_RRF recipe, but we harvest only two of the
    layers it returns:

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
        graphiti = await get_graphiti()
        results = await graphiti.search_(
            query.strip(),
            config=COMBINED_HYBRID_SEARCH_RRF,
            group_ids=[group_id],
        )
    except Exception as e:
        print(f"{tag} search failed: {type(e).__name__}: {e}")
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

    print(f"{tag} search ({group_id}) for {query.strip()!r}:")
    print(f"{tag}   edges ({len(edge_facts)}):")
    print(f"{tag}   episodes ({len(episode_facts)}):")

    # De-duplicate while preserving order (edge facts first), then bullet.
    facts = list(dict.fromkeys([*edge_facts, *episode_facts]))
    return "\n".join(f"- {fact}" for fact in facts) if facts else ""


# --- Ingestion ------------------------------------------------------------------


async def ingest_facts(
    facts: List[str],
    group_id: str,
    source_description: str,
    name_prefix: str,
) -> None:
    """Write each fact into Graphiti as its own episode under ``group_id``.

    Episodes are added sequentially under ``_graphiti_lock`` so that conversations
    finishing at the same time don't race the shared graph. Graphiti does the
    dedup / conflict resolution itself, so no consolidation pass is needed.
    Shared by the world-view and per-user-facts pipelines.
    """
    graphiti = await get_graphiti()
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
                name=f"{name_prefix}-{int(now.timestamp())}-{i}",
                episode_body=fact,
                source=EpisodeType.text,
                source_description=source_description,
                reference_time=now,
                group_id=group_id,
            )


# --- Episode listing --------------------------------------------------------------


async def list_episodes(group_id: str) -> List[str]:
    """Return the body of every episode in one partition, oldest first.

    This is the raw ingested sentences (not the derived edges/nodes), read
    straight off the episodic layer — used by the admin surfaces to dump
    everything Rachel has stored for a partition. Raises on driver errors so
    admin callers can surface the failure.
    """
    graphiti = await get_graphiti()
    episodes = await EpisodicNode.get_by_group_ids(graphiti.driver, [group_id])
    episodes.sort(key=lambda e: e.valid_at)
    return [content for e in episodes if (content := (e.content or "").strip())]
