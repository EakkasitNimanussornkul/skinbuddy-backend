from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query

from app.db.connection import supabase
from app.config.setting import settings
from app.core.services import line_service, schedule_service

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
async def run_routine_reminders(
    session: Optional[str] = Query(
        default=None,
        description='Which routine session to remind about: "am" or "pm". '
                    'Omit to include every step due today.',
    ),
    x_cron_secret: str | None = Header(default=None),
):
    """Push a reminder listing the steps due right now (UC-21).

    Scheduled twice a day: `?session=am` at 06:00 and `?session=pm` at 22:00.
    Only steps whose cadence falls on today are included, so a 2x/week step is not
    pushed seven days a week (SRS-81), and editing a step's frequency changes what
    gets sent from the next run onward (SRS-78).
    """
    _check_secret(x_cron_secret)
    if session is not None and session.strip().lower() not in schedule_service.VALID_SESSIONS:
        raise HTTPException(status_code=400, detail='session must be "am" or "pm".')

    today_date = date.today()
    today = today_date.isoformat()

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
            .select("id, frequency, time_of_day, products(name)")
            .eq("routine_id", routine.data[0]["id"])
            .order("step_order")
            .execute()
        )

        # [SRS-81] Only steps actually scheduled for today, in this session.
        due_steps = [
            st for st in (steps.data or [])
            if schedule_service.is_due_now(
                st.get("frequency"), st.get("time_of_day"), session, today_date
            )
        ]
        step_ids = [st["id"] for st in due_steps]
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
            (st.get("products") or {}).get("name", "Product")
            for st in due_steps if st["id"] not in done
        ]
        if not pending:
            skipped += 1
            continue

        if line_service.push_message(line_id, line_service.build_routine_reminder(pending, _routine_url())):
            sent += 1
        else:
            skipped += 1

    return {"sent": sent, "skipped": skipped}


# --- UC-26: weekly check-in reminders ---------------------------------------

@router.post("/weekly-checkin-reminders")
async def run_weekly_checkin_reminders(x_cron_secret: str | None = Header(default=None)):
    """Push a reminder to submit this week's skin log (UC-26).

    Scheduled once a week (e.g. Sunday 18:00). Mirrors the routine reminder
    (UC-21): only notification-enabled, friended users are pushed, and a user
    who already submitted this week's log is skipped so no duplicate nag is sent
    (SRS-83-style de-dup; UC-23 [A1]).
    """
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
        # No LINE friendship / id -> cannot push.
        line_id = user.get("line_id")
        if not line_id:
            skipped += 1
            continue

        # [UC-23 A1] Already submitted this week -> no reminder.
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
