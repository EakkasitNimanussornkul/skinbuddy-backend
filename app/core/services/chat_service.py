from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from app.config.setting import settings
from app.schemas import ChatRequest
from app.db.repository import chat_repo

# Initialize LangChain core models globally
llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=settings.GEMINI_API_KEY,
    temperature=0.7 # You can easily adjust creativity here now
)

embeddings = GoogleGenerativeAIEmbeddings(
    model="gemini-embedding-001", 
    google_api_key=settings.GEMINI_API_KEY
)

def generate_bot_response(request: ChatRequest, user_id: str) -> str:
    """Orchestrates data gathering and LLM generation using LangChain."""
    
    # 1. Get database context via the repository
    skin_type = chat_repo.get_user_skin_context(user_id)
    products_str = chat_repo.get_user_shelf_products(user_id)

    # 2. Embed the new message using LangChain's embedding tool
    query_vector = embeddings.embed_query(request.message)[:768]

    # 3. Get RAG facts via your existing repository
    facts = chat_repo.get_matching_facts(query_vector)

    # 4. Construct the LangChain System Prompt
    system_instruction = f"""
    You are the SkinBuddies AI assistant. Be helpful and scientifically accurate.
    You must strictly provide all responses in English.
    
    USER CONTEXT:
    - Skin Type: {skin_type}
    - Current products: {products_str}
    
    FACTS:
    {facts}
    """

    # Format the incoming history array into LangChain's native message objects
    langchain_history = []
    for msg in request.history:
        if msg.role == 'user':
            langchain_history.append(HumanMessage(content=msg.text))
        else:
            langchain_history.append(AIMessage(content=msg.text))

    # Build the dynamic Prompt Template
    prompt_template = ChatPromptTemplate.from_messages([
        ("system", system_instruction),
        MessagesPlaceholder(variable_name="history"),
        ("human", "{user_input}")
    ])

    # 5. Create the LangChain pipeline (Chain) and execute
    chain = prompt_template | llm
    
    response = chain.invoke({
        "history": langchain_history,
        "user_input": request.message
    })

    return response.content