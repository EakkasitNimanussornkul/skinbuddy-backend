from datetime import date, timedelta

from fastapi import APIRouter, Header, HTTPException

from app.db.connection import supabase
from app.config.setting import settings
from app.core.services import line_service

router = APIRouter()


def _check_secret(secret: str | None):
    expected = getattr(settings, "CRON_SECRET", None)
    # If no secret is configured, allow (dev). If configured, require a match.
    if expected and secret != expected:
        raise HTTPException(status_code=401, detail="Invalid cron secret.")


def _routine_url() -> str:
    return getattr(settings, "LINE_LIFF_ROUTINE_URL", None) or f"{settings.FRONTEND_URL}/routine"


def _checkin_url() -> str:
    return getattr(settings, "LINE_LIFF_CHECKIN_URL", None) or f"{settings.FRONTEND_URL}/checkin"


# --- UC-21: routine step reminders ------------------------------------------

@router.post("/routine-reminders")
async def run_routine_reminders(x_cron_secret: str | None = Header(default=None)):
    _check_secret(x_cron_secret)
    today = date.today().isoformat()

    users = (
        supabase.table("users")
        .select("id, line_id, notifications_enabled")
        .eq("notifications_enabled", True)
        .execute()
    )

    sent, skipped = 0, 0
    for user in (users.data or []):
        line_id = user.get("line_id")
        if not line_id:
            skipped += 1
            continue

        routine = (
            supabase.table("routines")
            .select("id")
            .eq("user_id", user["id"])
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        if not routine.data:
            skipped += 1
            continue

        steps = (
            supabase.table("routine_steps")
            .select("id, products(name)")
            .eq("routine_id", routine.data[0]["id"])
            .order("step_order")
            .execute()
        )
        step_ids = [s["id"] for s in (steps.data or [])]
        if not step_ids:
            skipped += 1
            continue

        # [A2] Skip steps already completed for today.
        comp = (
            supabase.table("routine_step_completions")
            .select("step_id")
            .in_("step_id", step_ids)
            .eq("period_key", today)
            .execute()
        )
        done = {c["step_id"] for c in (comp.data or [])}
        pending = [
            (s.get("products") or {}).get("name", "Product")
            for s in (steps.data or []) if s["id"] not in done
        ]
        if not pending:
            skipped += 1
            continue

        if line_service.push_message(line_id, line_service.build_routine_reminder(pending, _routine_url())):
            sent += 1
        else:
            skipped += 1

    return {"sent": sent, "skipped": skipped}


# --- UC-27: weekly check-in reminders ---------------------------------------

@router.post("/weekly-checkin-reminders")
async def run_weekly_checkin_reminders(x_cron_secret: str | None = Header(default=None)):
    _check_secret(x_cron_secret)
    d = date.today()
    week_start = (d - timedelta(days=d.weekday())).isoformat()

    users = (
        supabase.table("users")
        .select("id, line_id, notifications_enabled")
        .eq("notifications_enabled", True)
        .execute()
    )

    sent, skipped = 0, 0
    for user in (users.data or []):
        line_id = user.get("line_id")
        if not line_id:
            skipped += 1
            continue

        # [A2] Already submitted this week -> no reminder.
        existing = (
            supabase.table("skin_logs")
            .select("id")
            .eq("user_id", user["id"])
            .eq("week_start", week_start)
            .limit(1)
            .execute()
        )
        if existing.data:
            skipped += 1
            continue

        if line_service.push_message(line_id, line_service.build_weekly_checkin_reminder(_checkin_url())):
            sent += 1
        else:
            skipped += 1

    return {"sent": sent, "skipped": skipped}
