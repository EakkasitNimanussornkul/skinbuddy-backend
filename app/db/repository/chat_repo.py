from app.db.connection import supabase

def get_user_skin_context(user_id: str) -> str:
    """Fetches the user's skin type from the quiz results."""
    quiz_res = supabase.table("quiz_results").select("*").eq("user_id", user_id).execute()
    return quiz_res.data[0].get("skin_type", "Unknown") if quiz_res.data else "Unknown"

def get_user_shelf_products(user_id: str) -> str:
    """Fetches the names of products currently on the user's shelf."""
    shelf_res = supabase.table("shelf_items").select("products(name)").eq("user_id", user_id).execute()
    
    if not shelf_res.data:
        return "None"
        
    current_products = [item['products']['name'] for item in shelf_res.data]
    return ", ".join(current_products)

def get_matching_facts(query_vector: list, threshold: float = 0.5, limit: int = 3) -> str:
    """Queries the pgvector database for relevant dermatology facts."""
    rag_res = supabase.rpc(
        "match_skincare_facts",
        {
            "query_embedding": query_vector,
            "match_threshold": threshold,
            "match_count": limit
        }
    ).execute()
    
    if not rag_res.data:
        return "No specific facts found."
        
    return "\n".join([item['content'] for item in rag_res.data])