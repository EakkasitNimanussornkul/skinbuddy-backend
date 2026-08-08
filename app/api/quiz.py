from fastapi import APIRouter, Depends, HTTPException
from app.schemas import QuizResultCreate
from app.db.connection import supabase
from app.core.services.token import get_current_user_id

router = APIRouter()

@router.post("/save")
async def save_quiz_results(
    quiz_data: QuizResultCreate,
    user_id: str = Depends(get_current_user_id) 
):
    try:
        # 1. Insert into the quiz_results table
        supabase.table("quiz_results").insert({
            "user_id": user_id,
            "skin_type": quiz_data.skinType,
            "scores": quiz_data.scores
        }).execute()

        # 2. Update the user's main profile with their new skin type
        supabase.table("users").update({
            "skin_type": quiz_data.skinType
        }).eq("id", user_id).execute()

        return {"message": "Quiz results saved successfully!"}

    except Exception as e:
        print("POST /quiz/save error:", e)
        raise HTTPException(status_code=500, detail="Failed to save quiz results.")