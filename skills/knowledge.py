"""
Montana Feed Company - Knowledge Base Skills
Semantic search over company Q&A entries
"""


from config import (
    supabase,
    sb_exec,
    logger,
    ADVISORY_ENABLED,
    KB_NON_ADVISORY_CATEGORIES,
)


# Returned instead of an answer when the advisor is off and the only matching
# knowledge is nutrition / product content. Prefixed NO_MATCH: on purpose —
# every system prompt version already has a hard rule for that token ("do not
# guess, offer a specialist follow-up"), so the fallback behaves correctly even
# if a stale prompt version is ever republished.
ADVISORY_OFF_RESULT = (
    "NO_MATCH: Nutrition, feeding, health, and product-recommendation answers "
    "are turned off for this agent. Do NOT answer this from general knowledge "
    "and do NOT improvise. Tell the caller that's a question for their "
    "livestock specialist, and offer to take a message or set up a callback."
)


async def search_knowledge_base(query: str, top_k: int = 5) -> str:
    """Search knowledge base using semantic similarity.

    The Supabase RPC handles OpenAI embedding generation internally
    via the http extension, so no OpenAI client is needed here.

    The RPC call is synchronous in the Supabase Python client, so it's
    offloaded to a worker thread to keep the FastAPI event loop responsive
    under concurrent voice calls.
    """
    if not supabase:
        return "Knowledge base unavailable."

    logger.info(f"[KB_SEARCH] query={query!r}")
    try:
        # While the advisor is off we over-fetch, then drop everything that
        # isn't in the non-advisory allowlist. The semantic match doesn't know
        # about categories, so asking for exactly top_k here would routinely
        # return 5 nutrition rows that all get filtered to nothing on a
        # question the KB CAN legitimately answer ("who owns Montana Feed").
        fetch_k = top_k if ADVISORY_ENABLED else max(top_k * 6, 25)

        result = await sb_exec(
            lambda: supabase.rpc(
                "match_knowledge_base",
                # 0.4 stays, but the reasoning behind it changed on 2026-09-14
                # when rows moved to question-only embeddings. The old note here
                # ("strong matches top out ~0.65-0.70") described a compressed
                # scale caused by embedding rows as Question+Answer documents.
                # Decompressed, exact phrasings now score ~0.90+.
                #
                # 0.4 is NOT a clean separator and no threshold is -- measured,
                # the distributions interleave. Legitimate phrasings nobody wrote
                # a row for land 0.40-0.70; true noise lands 0.33-0.67 ("what
                # time does the bank close" hits 0.6675 against "What time do you
                # close?"). Raising to 0.7 rejected all 12 unseen-but-answerable
                # queries in the test set, so 0.4 stands: a false positive is
                # recoverable because the model sees the matched question text
                # and can judge it, while a false negative is the agent flatly
                # telling a caller it doesn't know.
                {"query_text": query, "match_threshold": 0.4, "match_count": fetch_k},
            ).execute(),
            what="match_knowledge_base rpc",
            idempotent=True,
        )

        if result.data and not ADVISORY_ENABLED:
            before = len(result.data)
            result.data = [
                item for item in result.data
                if item.get("category") in KB_NON_ADVISORY_CATEGORIES
            ][:top_k]
            logger.info(
                f"[KB_SEARCH] advisory OFF: {before} hits -> "
                f"{len(result.data)} after category allowlist"
            )
            if not result.data:
                return ADVISORY_OFF_RESULT

        if result.data:
            # Log what matched + how strongly, so retrieval quality is
            # visible in Railway logs without a live test call.
            hits = ", ".join(
                f"{item['question'][:40]!r}={item.get('similarity', 0):.3f}"
                for item in result.data
            )
            logger.info(f"[KB_SEARCH] {len(result.data)} hits: {hits}")
            return "\n".join([
                f"• Q: {item['question']}\n  A: {item['answer'][:500]}"
                for item in result.data
            ])

        # Nothing cleared the threshold. Return an explicit instruction the
        # model will read so it does NOT improvise a generic answer.
        logger.info("[KB_SEARCH] 0 hits")
        return (
            "NO_MATCH: The knowledge base has no entry covering this question. "
            "Do not guess or answer from general knowledge. Tell the caller you "
            "don't have that detail on hand and offer to have a livestock "
            "specialist follow up."
        )
    except Exception as e:
        logger.error(f"Knowledge base search error: {e}")
        # Same contract as NO_MATCH: an explicit instruction, not prose the
        # agent might mistake for an answer and repeat to the caller.
        return (
            "SEARCH_ERROR: The knowledge base could not be searched right now. "
            "Do not guess or answer from general knowledge. Tell the caller you "
            "can't pull that detail up at the moment and offer to have a "
            "livestock specialist follow up."
        )
