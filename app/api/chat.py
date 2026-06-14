import traceback

from fastapi import APIRouter, HTTPException, Depends
from app.schemas import ChatRequest
from app.core.services import chat_service
from app.core.services.token import get_current_user_id 

router = APIRouter()

@router.post("/ask")
async def chat_endpoint(
    request: ChatRequest, 
    user_id: str = Depends(get_current_user_id)
):
    try:
        # Pass BOTH the request and the securely extracted user_id to your service
        bot_reply = chat_service.generate_bot_response(request, user_id)
        
        return {"answer": bot_reply}

    except Exception as e:
        # This will print the exact line number and error to your terminal!
        traceback.print_exc() 
        raise HTTPException(status_code=500, detail=str(e))