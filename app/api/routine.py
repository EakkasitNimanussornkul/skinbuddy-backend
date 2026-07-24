"""
Routine API (Feature #5, UC-15..UC-22, +UC-28 adherence).

Endpoints
  GET    /routine/                     -> active routine + ordered steps   (UC-16)
  GET    /routine/analyze/{product_id} -> compatibility scoped to routine  (UC-17 pre-check)
  POST   /routine/generate             -> propose routine via chatbot/RAG  (UC-15)
  POST   /routine/apply                -> set proposed routine as active   (UC-15)
  POST   /routine/steps                -> add a product as a step          (UC-17)
  DELETE /routine/steps/{step_id}      -> remove a step                    (UC-18)
  PATCH  /routine/steps/{id}/frequency -> edit step frequency              (UC-19)
  PATCH  /routine/reorder              -> persist new step order           (UC-20)
  POST   /routine/steps/{id}/complete  -> mark step done for a period      (UC-22)
  DELETE /routine/steps/{id}/complete  -> undo completion                  (UC-22 A1)
  GET    /routine/adherence            -> completion history by day        (UC-28)
"""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.db.connection import supabase
from app.core.services.token import get_current_user_id
from app.core.services import compatibility_service, routine_service
from app.schemas import (
    AnalysisResponse,
    RoutineGenerateRequest,
    ApplyRoutineRequest,
    RoutineStepCreate,
    FrequencyUpdateRequest,
    ReorderRequest,
    CompleteStepRequest,
)

router = APIRouter()


# --- helpers ----------------------------------------------------------------

def _get_active_routine(user_id: str):
    res = (
        supabase.table("routines")
        .select("*")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    return res.data[0] if res.data else None


def _get_or_create_active_routine(user_id: str, source: str = "manual"):
    routine = _get_active_routine(user_id)
    if routine:
        return routine
    created = (
        supabase.table("routines")
        .insert({"user_id": user_id, "is_active": True, "source": source})
        .execute()
    )
    return created.data[0]


def _owns_step(step_id: str, user_id: str) -> bool:
    """Verify a step belongs to one of the user's routines."""
    step = supabase.table("routine_steps").select("routine_id").eq("id", step_id).limit(1).execute()
    if not step.data:
        return False
    routine = (
        supabase.table("routines")
        .select("id")
        .eq("id", step.data[0]["routine_id"])
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    return bool(routine.data)


# --- UC-16: view routine ----------------------------------------------------

@router.get("/")
async def get_active_routine(user_id: str = Depends(get_current_user_id)):
    routine = _get_active_routine(user_id)
    if not routine:
        return {"routine": None, "steps": []}  # [A1] empty state handled on the client

    steps_res = (
        supabase.table("routine_steps")
        .select("*, products(*, product_ingredients(ingredients(*)))")
        .eq("routine_id", routine["id"])
        .order("step_order")
        .execute()
    )
    steps = steps_res.data or []

    # Attach today's completion state for each step (UC-22 display).
    today = date.today().isoformat()
    step_ids = [s["id"] for s in steps]
    completed_ids = set()
    if step_ids:
        comp = (
            supabase.table("routine_step_completions")
            .select("step_id")
            .in_("step_id", step_ids)
            .eq("period_key", today)
            .execute()
        )
        completed_ids = {c["step_id"] for c in (comp.data or [])}
    for s in steps:
        s["completed_today"] = s["id"] in completed_ids

    return {"routine": routine, "steps": steps}


# --- UC-17 pre-check: compatibility scoped to the routine -------------------

@router.get("/analyze/{product_id}", response_model=AnalysisResponse)
async def analyze_for_routine(product_id: str, user_id: str = Depends(get_current_user_id)):
    try:
        comparison = compatibility_service.get_routine_products(user_id, exclude_product_id=product_id)
        return compatibility_service.analyze(product_id, user_id, comparison)
    except Exception as e:
        print("Routine analysis error:", e)
        raise HTTPException(status_code=500, detail="Failed to analyze compatibility.")


# --- UC-15: generate + apply ------------------------------------------------

@router.post("/generate")
async def generate_routine(req: RoutineGenerateRequest, user_id: str = Depends(get_current_user_id)):
    result = routine_service.generate_routine(user_id, req.followup_answers)
    if result.get("error") == "no_products":
        raise HTTPException(status_code=422, detail=result["message"])  # [E1]
    if result.get("error"):
        raise HTTPException(status_code=500, detail=result["message"])  # [E2]
    return result


@router.post("/apply")
async def apply_routine(req: ApplyRoutineRequest, user_id: str = Depends(get_current_user_id)):
    if not req.steps:
        raise HTTPException(status_code=400, detail="Cannot apply an empty routine.")

    # [A1] Archive any existing active routine as a snapshot.
    supabase.table("routines").update(
        {"is_active": False, "archived_at": datetime.utcnow().isoformat()}
    ).eq("user_id", user_id).eq("is_active", True).execute()

    routine = (
        supabase.table("routines")
        .insert({"user_id": user_id, "is_active": True, "source": "chatbot"})
        .execute()
    )
    routine_id = routine.data[0]["id"]

    # Ensure every recommended product is in the user's storage (UC-15 / UC-05):
    # map existing shelf items, add any missing ones as active products.
    product_ids = [s.product_id for s in req.steps]
    existing = (
        supabase.table("shelf_items")
        .select("id, product_id")
        .eq("user_id", user_id)
        .in_("product_id", product_ids)
        .execute()
    )
    shelf_map = {}
    for item in (existing.data or []):
        shelf_map.setdefault(item["product_id"], item["id"])

    now = datetime.utcnow().isoformat()
    rows = []
    for s in req.steps:
        shelf_item_id = shelf_map.get(s.product_id)
        if not shelf_item_id:
            added = (
                supabase.table("shelf_items")
                .insert({"user_id": user_id, "product_id": s.product_id, "usage_state": "active"})
                .execute()
            )
            shelf_item_id = added.data[0]["id"]
            shelf_map[s.product_id] = shelf_item_id
        rows.append({
            "routine_id": routine_id,
            "product_id": s.product_id,
            "shelf_item_id": shelf_item_id,
            "step_order": s.step_order,
            "time_of_day": s.time_of_day,
            "frequency": s.frequency,
            "added_at": now,
        })
    supabase.table("routine_steps").insert(rows).execute()

    return {"message": "Routine applied", "routine_id": routine_id}


# --- UC-17: add step --------------------------------------------------------

@router.post("/steps")
async def add_step(req: RoutineStepCreate, user_id: str = Depends(get_current_user_id)):
    """Add a product as a routine step. The client is expected to have already
    called GET /routine/analyze/{product_id} and shown any warnings first."""
    try:
        routine = _get_or_create_active_routine(user_id)
        routine_id = routine["id"]

        existing = (
            supabase.table("routine_steps")
            .select("step_order")
            .eq("routine_id", routine_id)
            .order("step_order", desc=True)
            .limit(1)
            .execute()
        )
        next_order = (existing.data[0]["step_order"] + 1) if existing.data else 1

        row = {
            "routine_id": routine_id,
            "product_id": req.product_id,
            "shelf_item_id": req.shelf_item_id,
            "step_order": next_order,
            "frequency": req.frequency,
            "time_of_day": req.time_of_day,
            "added_at": datetime.utcnow().isoformat(),
        }
        res = supabase.table("routine_steps").insert(row).execute()
        return res.data[0]
    except Exception as e:  # [E1] add fails
        print("Add step error:", e)
        raise HTTPException(status_code=500, detail="Failed to add product to routine.")


# --- UC-18: remove step -----------------------------------------------------

@router.delete("/steps/{step_id}")
async def remove_step(step_id: str, user_id: str = Depends(get_current_user_id)):
    if not _owns_step(step_id, user_id):
        raise HTTPException(status_code=404, detail="Step not found.")
    try:
        supabase.table("routine_steps").delete().eq("id", step_id).execute()
        return {"message": "Step removed"}
    except Exception as e:  # [E1] remove fails
        print("Remove step error:", e)
        raise HTTPException(status_code=500, detail="Failed to remove step.")


# --- UC-19: edit frequency --------------------------------------------------

VALID_FREQUENCIES = {"daily", "3x_week", "2x_week", "weekly"}


@router.patch("/steps/{step_id}/frequency")
async def update_frequency(step_id: str, req: FrequencyUpdateRequest, user_id: str = Depends(get_current_user_id)):
    if req.frequency not in VALID_FREQUENCIES:  # [E1] invalid
        raise HTTPException(status_code=400, detail="Invalid frequency option.")
    if not _owns_step(step_id, user_id):
        raise HTTPException(status_code=404, detail="Step not found.")
    res = supabase.table("routine_steps").update({"frequency": req.frequency}).eq("id", step_id).execute()
    return res.data[0] if res.data else {}


# --- UC-20: reorder ---------------------------------------------------------

@router.patch("/reorder")
async def reorder_steps(req: ReorderRequest, user_id: str = Depends(get_current_user_id)):
    routine = _get_active_routine(user_id)
    if not routine:
        raise HTTPException(status_code=404, detail="No active routine.")
    try:
        for index, step_id in enumerate(req.step_ids):
            supabase.table("routine_steps").update({"step_order": index + 1}) \
                .eq("id", step_id).eq("routine_id", routine["id"]).execute()
        return {"message": "Reordered", "order": req.step_ids}
    except Exception as e:  # [E1] save fails -> client reverts
        print("Reorder error:", e)
        raise HTTPException(status_code=500, detail="Failed to save new order.")


# --- UC-22: complete / undo -------------------------------------------------

@router.post("/steps/{step_id}/complete")
async def complete_step(step_id: str, req: CompleteStepRequest, user_id: str = Depends(get_current_user_id)):
    if not _owns_step(step_id, user_id):
        raise HTTPException(status_code=404, detail="Step not found.")
    period_key = req.period_key or date.today().isoformat()
    try:
        supabase.table("routine_step_completions").upsert(
            {
                "step_id": step_id,
                "user_id": user_id,
                "period_key": period_key,
                "completed_at": datetime.utcnow().isoformat(),
            },
            on_conflict="step_id,period_key",
        ).execute()
        return {"message": "Completed", "period_key": period_key}
    except Exception as e:  # [E1] save fails -> client reverts
        print("Complete step error:", e)
        raise HTTPException(status_code=500, detail="Failed to record completion.")


@router.delete("/steps/{step_id}/complete")
async def uncomplete_step(step_id: str, period_key: Optional[str] = None, user_id: str = Depends(get_current_user_id)):
    if not _owns_step(step_id, user_id):
        raise HTTPException(status_code=404, detail="Step not found.")
    pk = period_key or date.today().isoformat()
    supabase.table("routine_step_completions").delete() \
        .eq("step_id", step_id).eq("period_key", pk).execute()
    return {"message": "Completion removed", "period_key": pk}


# --- UC-28: adherence history by day ----------------------------------------

@router.get("/adherence")
async def get_adherence(user_id: str = Depends(get_current_user_id)):
    routine = _get_active_routine(user_id)
    if not routine:
        return {"total_steps": 0, "days": {}}
    steps = supabase.table("routine_steps").select("id").eq("routine_id", routine["id"]).execute()
    step_ids = [s["id"] for s in (steps.data or [])]
    if not step_ids:
        return {"total_steps": 0, "days": {}}
    comps = (
        supabase.table("routine_step_completions")
        .select("step_id, period_key, completed_at")
        .in_("step_id", step_ids)
        .execute()
    )
    days: dict = {}
    for c in (comps.data or []):
        days.setdefault(c["period_key"], []).append(c["step_id"])
    return {"total_steps": len(step_ids), "days": days}
