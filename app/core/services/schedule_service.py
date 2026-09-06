"""Routine scheduling — which days a step runs on, and whether it is due.

Single source of truth on the backend for turning a step's `frequency` into
concrete weekdays. Used by the reminder job (UC-21, SRS-81), by frequency edits
(UC-19, SRS-78) and by the completion history's missed-day logic (UC-27, SRS-104).

Mirrors `skinbuddy-frontend/src/utils/routineSchedule.ts`. The two must agree: if
they drift, the reminder a user receives will disagree with the checklist they see.

Weekday mapping (rationale in _working/DESIGN_DECISIONS.md D5.1):

    daily    -> every day
    3x_week  -> Mon / Wed / Fri     (standard every-other-night retinoid cadence)
    2x_week  -> Tue / Sat           (3-4 day spacing, the only pair that avoids
                                     collisions with the other cadences)
    weekly   -> Sun

The non-daily cadences are mutually exclusive by design, so no two periodic
actives ever land on the same night — an exfoliant and a retinoid should never be
applied together.

NOTE ON WEEKDAY NUMBERING. Python's date.weekday() is Mon=0 … Sun=6, while
JavaScript's Date.getDay() is Sun=0 … Sat=6. This module uses the Python
convention; the frontend util uses the JavaScript one. The mapped *days* are
identical — only the integers differ.
"""
from datetime import date
from typing import Optional, Set

# Python weekday(): Mon=0, Tue=1, Wed=2, Thu=3, Fri=4, Sat=5, Sun=6
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)

EVERY_DAY: Set[int] = {MON, TUE, WED, THU, FRI, SAT, SUN}

_WEEKDAYS_BY_FREQUENCY = {
    "daily": EVERY_DAY,
    "3x_week": {MON, WED, FRI},
    "2x_week": {TUE, SAT},
    "weekly": {SUN},
}

VALID_SESSIONS = {"am", "pm"}


def weekdays_for(frequency: Optional[str]) -> Set[int]:
    """The weekdays a cadence runs on. Unknown or missing frequency -> every day,
    matching the `frequency: str = "daily"` default on a routine step."""
    if not frequency:
        return EVERY_DAY
    return _WEEKDAYS_BY_FREQUENCY.get(frequency, EVERY_DAY)


def is_due(frequency: Optional[str], on: Optional[date] = None) -> bool:
    """Is a step with this cadence scheduled for the given day (default today)?"""
    day = on or date.today()
    return day.weekday() in weekdays_for(frequency)


def matches_session(time_of_day: Optional[str], session: Optional[str]) -> bool:
    """Does a step belong to the given session?

    `session` is "am" or "pm". A step marked "both" belongs to each. Passing no
    session matches everything, so callers that do not split by session (the
    routine page, the history view) get every step back.
    """
    if not session:
        return True
    normalized = (time_of_day or "both").strip().lower()
    return normalized in ("both", session.strip().lower())


def is_due_now(frequency: Optional[str], time_of_day: Optional[str],
               session: Optional[str] = None, on: Optional[date] = None) -> bool:
    """Both axes at once: due on this day AND part of this session."""
    return is_due(frequency, on) and matches_session(time_of_day, session)
