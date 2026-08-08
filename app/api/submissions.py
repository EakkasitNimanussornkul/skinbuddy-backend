from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query

from app.db.connection import supabase
from app.core.services.token import get_current_user_id, get_current_admin_user_id
from app.core.services.catalog_service import upsert_product_with_ingredients
from app.schemas import ProductSubmissionCreate, ReviewDecision

router = APIRouter()


@router.post("/")
async def create_submission(payload: ProductSubmissionCreate, user_id: str = Depends(get_current_user_id)):
    try:
        response = supabase.table("product_submissions").insert({
            "submitted_by": user_id,
            "status": "pending",
            "payload": payload.model_dump(),
        }).execute()
        return response.data[0]
    except Exception as e:
        print("POST /submissions error:", e)
        raise HTTPException(status_code=500, detail="Failed to create submission.")


@router.get("/mine")
async def get_my_submissions(user_id: str = Depends(get_current_user_id)):
    try:
        response = (
            supabase.table("product_submissions")
            .select("*")
            .eq("submitted_by", user_id)
            .order("created_at", desc=True)
            .execute()
        )
        return response.data
    except Exception as e:
        print("GET /submissions/mine error:", e)
        raise HTTPException(status_code=500, detail="Failed to fetch your submissions.")


@router.get("/admin")
async def list_submissions_for_review(
    status: str = Query("pending", pattern="^(pending|approved|rejected)$"),
    admin_id: str = Depends(get_current_admin_user_id),
):
    try:
        response = (
            supabase.table("product_submissions")
            .select("*")
            .eq("status", status)
            .order("created_at")
            .execute()
        )
        return response.data
    except Exception as e:
        print("GET /submissions/admin error:", e)
        raise HTTPException(status_code=500, detail="Failed to fetch submissions.")


@router.post("/admin/{submission_id}/approve")
async def approve_submission(submission_id: str, admin_id: str = Depends(get_current_admin_user_id)):
    try:
        existing = supabase.table("product_submissions").select("*").eq("id", submission_id).single().execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Submission not found")
        if existing.data["status"] != "pending":
            raise HTTPException(status_code=400, detail="Submission has already been reviewed")

        product_id = upsert_product_with_ingredients(existing.data["payload"])

        response = supabase.table("product_submissions").update({
            "status": "approved",
            "reviewed_by": admin_id,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", submission_id).execute()
        return {**response.data[0], "product_id": product_id}
    except HTTPException as he:
        raise he
    except Exception as e:
        print("POST /submissions/admin/{id}/approve error:", e)
        raise HTTPException(status_code=500, detail="Failed to approve submission.")


@router.post("/admin/{submission_id}/reject")
async def reject_submission(submission_id: str, decision: ReviewDecision, admin_id: str = Depends(get_current_admin_user_id)):
    try:
        existing = supabase.table("product_submissions").select("id, status").eq("id", submission_id).single().execute()
        if not existing.data:
            raise HTTPException(status_code=404, detail="Submission not found")
        if existing.data["status"] != "pending":
            raise HTTPException(status_code=400, detail="Submission has already been reviewed")

        response = supabase.table("product_submissions").update({
            "status": "rejected",
            "review_notes": decision.review_notes,
            "reviewed_by": admin_id,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", submission_id).execute()
        return response.data[0]
    except HTTPException as he:
        raise he
    except Exception as e:
        print("POST /submissions/admin/{id}/reject error:", e)
        raise HTTPException(status_code=500, detail="Failed to reject submission.")
