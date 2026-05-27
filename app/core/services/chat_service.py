from fastapi import types
from google import genai
from google.genai import types
from app.config.setting import settings
from app.schemas import ChatRequest
from app.db.repository import chat_repo

ai_client = genai.Client(api_key=settings.GEMINI_API_KEY)

def generate_bot_response(request: ChatRequest, user_id: str) -> str:
    """Orchestrates data gathering and LLM generation."""
    
    # 1. Get database context via the repository
    skin_type = chat_repo.get_user_skin_context(user_id)
    products_str = chat_repo.get_user_shelf_products(user_id)

    # 2. Embed the new message
    embed_response = ai_client.models.embed_content(
        model='gemini-embedding-001',
        contents=request.message,
        config=types.EmbedContentConfig(output_dimensionality=768)
    )
    query_vector = embed_response.embeddings[0].values

    # 3. Get RAG facts via the repository
    facts = chat_repo.get_matching_facts(query_vector)

    # 4. Construct the Prompt
    system_prompt = f"""
    You are the SkinBuddies AI assistant. Be helpful and scientifically accurate.
    
    USER CONTEXT:
    - Skin Type: {skin_type}
    - Current products: {products_str}
    
    FACTS:
    {facts}
    """

    formatted_history = "\n".join([f"{msg.role}: {msg.text}" for msg in request.history])
    final_prompt = f"{system_prompt}\n\nHISTORY:\n{formatted_history}\n\nUSER: {request.message}"

    # 5. Call Gemini
    response = ai_client.models.generate_content(
        model='gemini-2.5-flash',
        contents=final_prompt
    )

    return response.text