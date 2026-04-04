"""
Montana Feed Company - Knowledge Base Skills
Semantic search over company Q&A entries
"""

from config import supabase, logger


def search_knowledge_base(query: str, top_k: int = 3) -> str:
    """Search knowledge base using semantic similarity.
    
    The Supabase RPC handles OpenAI embedding generation internally
    via the http extension, so no OpenAI client is needed here.
    """
    if not supabase:
        return "Knowledge base unavailable."
    try:
        result = supabase.rpc(
            "match_knowledge_base",
            {"query_text": query, "match_threshold": 0.7, "match_count": top_k}
        ).execute()

        if result.data:
            return "\n".join([
                f"• Q: {item['question']}\n  A: {item['answer'][:500]}"
                for item in result.data
            ])

        return "No relevant information found."
    except Exception as e:
        logger.error(f"Knowledge base search error: {e}")
        return "I'll connect you with a specialist who can help."
