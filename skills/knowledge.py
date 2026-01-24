"""
Montana Feed Company - Knowledge Base Skills
Semantic search over company Q&A entries
"""

from config import supabase, openai_client, logger


def search_knowledge_base(query: str, top_k: int = 3) -> str:
    """Search knowledge base using semantic similarity."""
    if not supabase:
        return "Knowledge base unavailable."
    try:
        response = openai_client.embeddings.create(model="text-embedding-3-small", input=query)
        query_embedding = response.data[0].embedding

        result = supabase.rpc(
            "match_knowledge_base",
            {"query_embedding": query_embedding, "match_threshold": 0.7, "match_count": top_k}
        ).execute()

        if result.data:
            return "\n".join([f"• {item['content'][:500]}" for item in result.data])

        return "No relevant information found."
    except Exception as e:
        logger.error(f"Knowledge base search error: {e}")
        return "I'll connect you with a specialist who can help."
