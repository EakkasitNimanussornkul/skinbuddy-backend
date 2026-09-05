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
        # These two writes are not in a transaction, so their ORDER decides what
        # a failure between them leaves behind. The profile is written first
        # deliberately: it is what the rest of the app reads, while quiz_results
        # is history. Writing history first meant a failed profile update left a
        # stored result whose skin type never reached the profile - the user had
        # taken the quiz and the app still behaved as though they had not.
        # Reversed, the worst case is a profile that is correct but missing one
        # history row, which degrades nothing the user can see. BE-DEF-10.
        #
        # This narrows the window rather than closing it. Closing it needs both
        # writes in one database function; see DEFECTS_FOUND_BY_TESTS.md.

        # 1. Update the user's main profile with their new skin type
        profile = supabase.table("users").update({
            "skin_type": quiz_data.skinType
        }).eq("id", user_id).execute()

        # No match means there is no profile to attach this result to, so the
        # result must not be stored either.
        if not profile.data:
            raise HTTPException(status_code=404, detail="User profile not found.")

        # 2. Record the result now that the profile it belongs to is confirmed
        supabase.table("quiz_results").insert({
            "user_id": user_id,
            "skin_type": quiz_data.skinType,
            "scores": quiz_data.scores
        }).execute()

        return {"message": "Quiz results saved successfully!"}

    except HTTPException:
        raise
    except Exception as e:
        print("POST /quiz/save error:", e)
        raise HTTPException(status_code=500, detail="Failed to save quiz results.")