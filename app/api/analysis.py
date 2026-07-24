from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.db.connection import supabase
from app.core.services.token import get_current_user_id
from app.core.services import analysis_service
from app.schemas import SkinLogCreate

router = APIRouter()


def _week_start(d: Optional[date] = None) -> str:
    """Monday of the given (or current) week, as an ISO date string."""
    d = d or date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


@router.get("/log/current")
async def get_current_log(user_id: str = Depends(get_current_user_id)):
    ws = _week_start()
    res = (
        supabase.table("skin_logs")
        .select("*")
        .eq("user_id", user_id)
        .eq("week_start", ws)
        .limit(1)
        .execute()
    )
    return {"week_start": ws, "log": res.data[0] if res.data else None}


@router.post("/log")
async def submit_log(req: SkinLogCreate, user_id: str = Depends(get_current_user_id)):
    ws = req.week_start or _week_start()
    payload = {
        "user_id": user_id,
        "week_start": ws,
        "symptoms": [s.dict() for s in req.symptoms],
        "affected_areas": req.affected_areas,
        "notes": req.notes,
    }
    try:
        # [A1] upsert -> updates the existing log instead of duplicating.
        log = supabase.table("skin_logs").upsert(payload, on_conflict="user_id,week_start").execute()
        log_row = log.data[0]
    except Exception as e:  # [E1] save fails
        print("Log save error:", e)
        raise HTTPException(status_code=500, detail="Failed to save your log. Please try again.")

    # UC-24: run analysis and store the report linked to the log.
    report = analysis_service.analyze_log(log_row, user_id)
    stored = supabase.table("skin_analysis_reports").upsert(
        {
            "log_id": log_row["id"],
            "user_id": user_id,
            "week_start": ws,
            "flagged_ingredients": report.get("flagged_ingredients", []),
            "trend_verdict": report.get("trend_verdict", ""),
            "recommendations": report.get("recommendations", []),
            "status": report.get("status", "complete"),
        },
        on_conflict="log_id",
    ).execute()

    return {"log": log_row, "report": stored.data[0] if stored.data else report}


@router.get("/report")
async def get_report(week: Optional[str] = None, user_id: str = Depends(get_current_user_id)):
    ws = week or _week_start()
    res = (
        supabase.table("skin_analysis_reports")
        .select("*")
        .eq("user_id", user_id)
        .eq("week_start", ws)
        .limit(1)
        .execute()
    )
    # [E1] none -> client shows a "no log submitted" state.
    return {"week_start": ws, "report": res.data[0] if res.data else None}


@router.get("/history")
async def get_history(user_id: str = Depends(get_current_user_id)):
    logs = (
        supabase.table("skin_logs")
        .select("*")
        .eq("user_id", user_id)
        .order("week_start", desc=True)
        .execute()
    )
    reports = (
        supabase.table("skin_analysis_reports")
        .select("*")
        .eq("user_id", user_id)
        .execute()
    )
    report_by_week = {r["week_start"]: r for r in (reports.data or [])}

    timeline = []
    for lg in (logs.data or []):
        timeline.append({
            "week_start": lg["week_start"],
            "log": lg,
            "report": report_by_week.get(lg["week_start"]),
        })
    return {"timeline": timeline}
