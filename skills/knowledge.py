"""
Montana Feed Company - Knowledge Base Skills
Semantic search over company Q&A entries
"""

import asyncio

from config import supabase, logger


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
    try:
        result = await asyncio.to_thread(
            lambda: supabase.rpc(
                "match_knowledge_base",
                # text-embedding-3-small: strong matches top out ~0.65-0.70,
                # so 0.7 filtered out nearly everything (drought best = 0.691).
                # 0.4 admits relevant content while still rejecting true noise.
                {"query_text": query, "match_threshold": 0.4, "match_count": top_k},
            ).execute()
        )

        if result.data:
            return "\n".join([
                f"• Q: {item['question']}\n  A: {item['answer'][:500]}"
                for item in result.data
            ])

        return "No relevant information found."
    except Exception as e:
        logger.error(f"Knowledge base search error: {e}")
        return "I'll connect you with a specialist who can help."
