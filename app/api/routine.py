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
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.db.connection import supabase
from app.core.services.token import get_current_user_id
from app.core.services import compatibility_service, routine_service, schedule_service
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


def _owned_step(step_id: str, user_id: str):
    """Like _owns_step but returns the step row (id, product_id, frequency,
    time_of_day) so a completion can denormalise those fields — or None if the
    step does not belong to the user."""
    step = (
        supabase.table("routine_steps")
        .select("id, routine_id, product_id, frequency, time_of_day")
        .eq("id", step_id).limit(1).execute()
    )
    if not step.data:
        return None
    s = step.data[0]
    routine = (
        supabase.table("routines").select("id")
        .eq("id", s["routine_id"]).eq("user_id", user_id).limit(1).execute()
    )
    return s if routine.data else None


# --- AM/PM sessions ---------------------------------------------------------
# A step's time_of_day says which session(s) it runs in. A completion's
# time_of_day says which session was actually ticked ("AM"/"PM", or the legacy
# "both" marker meaning the whole day). UC-22 / UC-27.

VALID_SESSIONS = {"AM", "PM"}


def _sessions_for(time_of_day) -> list:
    """The concrete sessions a step (or a completion) covers. "both"/unknown ->
    both AM and PM; "AM"/"PM" -> just that one."""
    t = (time_of_day or "both").strip().lower()
    if t == "am":
        return ["AM"]
    if t == "pm":
        return ["PM"]
    return ["AM", "PM"]


def _session_label(time_of_day):
    """"AM"/"PM" if the value names a single session, else None (e.g. legacy
    "both") — for display in the history detail."""
    t = (time_of_day or "").strip().upper()
    return t if t in VALID_SESSIONS else None


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

    # Attach today's completion state for each step, per session (UC-22 display).
    today = date.today().isoformat()
    step_ids = [s["id"] for s in steps]
    done_sessions: dict = {}  # step_id -> set of sessions completed today
    if step_ids:
        comp = (
            supabase.table("routine_step_completions")
            .select("step_id, time_of_day")
            .in_("step_id", step_ids)
            .eq("period_key", today)
            .execute()
        )
        for c in (comp.data or []):
            done_sessions.setdefault(c["step_id"], set()).update(_sessions_for(c.get("time_of_day")))
    for s in steps:
        done = done_sessions.get(s["id"], set())
        s["completed_am"] = "AM" in done
        s["completed_pm"] = "PM" in done
        # Whole-step done = every session it belongs to is ticked (back-compat).
        belongs = set(_sessions_for(s.get("time_of_day")))
        s["completed_today"] = bool(belongs) and belongs.issubset(done)

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
VALID_TIMES_OF_DAY = {"AM", "PM", "both"}


@router.patch("/steps/{step_id}/frequency")
async def update_frequency(step_id: str, req: FrequencyUpdateRequest, user_id: str = Depends(get_current_user_id)):
    if req.frequency not in VALID_FREQUENCIES:  # [E1] invalid
        raise HTTPException(status_code=400, detail="Invalid frequency option.")
    if req.time_of_day is not None and req.time_of_day not in VALID_TIMES_OF_DAY:  # [E1]
        raise HTTPException(status_code=400, detail="Invalid time of day option.")
    if not _owns_step(step_id, user_id):
        raise HTTPException(status_code=404, detail="Step not found.")

    payload = {"frequency": req.frequency}
    if req.time_of_day is not None:
        payload["time_of_day"] = req.time_of_day

    res = supabase.table("routine_steps").update(payload).eq("id", step_id).execute()
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
    step = _owned_step(step_id, user_id)
    if not step:
        raise HTTPException(status_code=404, detail="Step not found.")
    period_key = req.period_key or date.today().isoformat()

    # Which session is being ticked. A "both" step is two daily tasks, so the
    # client says which one; an AM- or PM-only step infers it; a "both" step
    # with no session marks the whole day via the legacy "both" marker.
    session = (req.time_of_day or "").strip().upper()
    if session and session not in VALID_SESSIONS:  # [E1]
        raise HTTPException(status_code=400, detail="Invalid session; expected AM or PM.")
    if not session:
        step_sessions = _sessions_for(step.get("time_of_day"))
        session = step_sessions[0] if len(step_sessions) == 1 else "both"

    try:
        supabase.table("routine_step_completions").upsert(
            {
                "step_id": step_id,
                "user_id": user_id,
                "period_key": period_key,
                "time_of_day": session,
                "product_id": step.get("product_id"),   # denormalised (survives step deletion, UC-27)
                "frequency": step.get("frequency"),
                "completed_at": datetime.utcnow().isoformat(),
            },
            on_conflict="step_id,period_key,time_of_day",
        ).execute()
        return {"message": "Completed", "period_key": period_key, "time_of_day": session}
    except Exception as e:  # [E2] save fails -> client reverts
        print("Complete step error:", e)
        raise HTTPException(status_code=500, detail="Failed to record completion.")


@router.delete("/steps/{step_id}/complete")
async def uncomplete_step(
    step_id: str,
    period_key: Optional[str] = None,
    time_of_day: Optional[str] = None,
    user_id: str = Depends(get_current_user_id),
):
    if not _owns_step(step_id, user_id):
        raise HTTPException(status_code=404, detail="Step not found.")
    pk = period_key or date.today().isoformat()
    query = (
        supabase.table("routine_step_completions").delete()
        .eq("step_id", step_id).eq("period_key", pk)
    )
    # Undo only the named session; with none given, clear the whole day (the
    # AM- or PM-only case, and legacy callers).
    session = (time_of_day or "").strip().upper()
    if session:
        query = query.eq("time_of_day", session)
    query.execute()
    return {"message": "Completion removed", "period_key": pk}


# --- UC-27: routine completion history --------------------------------------

@router.get("/adherence")
async def get_adherence(weeks: int = 8, user_id: str = Depends(get_current_user_id)):
    """Completion history grouped by day (UC-27, SRS-103..SRS-108).

    Completions are read by user_id rather than by the active routine's step ids,
    so replacing a routine does not wipe the visible history or reset the streak.

    What was "due" on a given day is derived from the CURRENT routine and each
    step's CURRENT frequency. Editing a frequency therefore changes past due
    counts retroactively — a documented limitation. `routine_step_completions`
    records the frequency in force at completion time so this can be tightened
    later without another migration.
    """
    today = date.today()
    start = today - timedelta(weeks=max(1, min(weeks, 52)))

    # 1. What counts as "due": the steps of the active routine.
    routine = _get_active_routine(user_id)
    steps = []
    if routine:
        res = (
            supabase.table("routine_steps")
            .select("id, frequency, time_of_day, step_order, products(name)")
            .eq("routine_id", routine["id"])
            .order("step_order")
            .execute()
        )
        steps = res.data or []
    step_names = {st["id"]: (st.get("products") or {}).get("name") or "Product" for st in steps}

    # 2. Everything the user has actually ticked in the window.
    comps = (
        supabase.table("routine_step_completions")
        .select("step_id, product_id, period_key, time_of_day")
        .eq("user_id", user_id)
        .gte("period_key", start.isoformat())
        .lte("period_key", today.isoformat())
        .execute()
    )
    completions = comps.data or []

    # Names for completions whose step has since been deleted (step_id is null).
    orphan_product_ids = {
        c["product_id"] for c in completions
        if c.get("product_id") and c.get("step_id") not in step_names
    }
    orphan_names: dict = {}
    if orphan_product_ids:
        prods = (
            supabase.table("products")
            .select("id, name")
            .in_("id", list(orphan_product_ids))
            .execute()
        )
        orphan_names = {p["id"]: p.get("name") or "Product" for p in (prods.data or [])}

    by_day: dict = {}
    for c in completions:
        by_day.setdefault(c["period_key"], []).append(c)

    # 3. Classify every day in the window, per session. A "both" product is two
    #    daily tasks (AM + PM), so it is due twice and can be half-done — which is
    #    what makes "did the morning, skipped the evening" show as a partial day.
    days: dict = {}
    cursor = start
    while cursor <= today:
        key = cursor.isoformat()

        # Due (step, session) units for this day.
        due_units = []
        for st in steps:
            if not schedule_service.is_due(st.get("frequency"), cursor):
                continue
            for sess in _sessions_for(st.get("time_of_day")):
                due_units.append((st["id"], sess))

        # Sessions actually ticked, for steps still in the routine. A legacy
        # "both" completion satisfies both sessions.
        done_units = set()
        for c in by_day.get(key, []):
            if c.get("step_id") in step_names:
                for sess in _sessions_for(c.get("time_of_day")):
                    done_units.add((c["step_id"], sess))

        completed = [{"step_id": sid, "session": sess, "product_name": step_names[sid]}
                     for (sid, sess) in due_units if (sid, sess) in done_units]
        missed = [{"step_id": sid, "session": sess, "product_name": step_names[sid]}
                  for (sid, sess) in due_units if (sid, sess) not in done_units]

        # Ticked that day but no longer part of the routine — still real history.
        also = [
            {"step_id": None, "session": _session_label(c.get("time_of_day")),
             "product_name": orphan_names.get(c.get("product_id"), "Removed product")}
            for c in by_day.get(key, [])
            if c.get("step_id") not in step_names
        ]

        if not due_units:  # [SRS-104] a rest day is not a miss
            status = "none"
        elif not completed:
            status = "missed"
        elif len(completed) == len(due_units):
            status = "complete"
        else:
            status = "partial"

        days[key] = {
            "status": status,
            "due": len(due_units),
            "done": len(completed),
            "completed": completed,
            "missed": missed,
            "also_completed": also,
        }
        cursor += timedelta(days=1)

    # 4. Streak (SRS-107): consecutive complete days back from today. Rest days
    #    carry the streak; today is skipped while it is still in progress.
    streak = 0
    cursor = today
    if days.get(today.isoformat(), {}).get("status") != "complete":
        cursor = today - timedelta(days=1)
    while cursor >= start:
        status = days.get(cursor.isoformat(), {}).get("status")
        if status == "complete":
            streak += 1
        elif status != "none":
            break
        cursor -= timedelta(days=1)

    scheduled = [d for d in days.values() if d["status"] != "none"]
    completed_days = [d for d in scheduled if d["status"] == "complete"]

    return {
        "range": {"from": start.isoformat(), "to": today.isoformat()},
        "total_steps": len(steps),
        "streak": streak,
        "adherence_pct": round(100 * len(completed_days) / len(scheduled)) if scheduled else 0,
        "has_history": bool(completions),  # [SRS-108] empty state
        "days": days,
    }
