from typing import List

import httpx

from app.config.setting import settings

LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"


def _access_token():
    return getattr(settings, "LINE_CHANNEL_ACCESS_TOKEN", None)


def push_message(line_id: str, messages: List[dict]) -> bool:
    """Push up to 5 LINE message objects to a single user. Returns success bool."""
    token = _access_token()
    if not token:
        print("[line_service] LINE_CHANNEL_ACCESS_TOKEN not set; skipping push.")
        return False
    if not line_id:
        return False
    try:
        resp = httpx.post(
            LINE_PUSH_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": line_id, "messages": messages[:5]},
            timeout=10,
        )
        if resp.status_code != 200:
            print("[line_service] push failed:", resp.status_code, resp.text)
        return resp.status_code == 200
    except Exception as e:
        print("[line_service] push error:", e)
        return False


# --- Message builders --------------------------------------------------------

def build_routine_reminder(step_names: List[str], liff_url: str) -> List[dict]:
    """A simple text + button routine reminder (UC-21).

    Option B in the design: a single 'Open routine' button that deep-links to the
    routine page (checkboxes live in the web UI). Swap for a Flex message with
    per-step postback buttons if you later want in-chat checking (Option A).
    """
    steps_text = "\n".join(f"• {name}" for name in step_names) if step_names else "Your routine is ready."
    return [{
        "type": "template",
        "altText": "It's time for your skincare routine",
        "template": {
            "type": "buttons",
            "title": "SkinBuddy Routine",
            "text": (f"Time for your routine:\n{steps_text}")[:160],
            "actions": [
                {"type": "uri", "label": "Open my routine", "uri": liff_url}
            ],
        },
    }]


def build_weekly_checkin_reminder(liff_url: str) -> List[dict]:
    """Weekly check-in reminder (UC-27)."""
    return [{
        "type": "template",
        "altText": "Time for your weekly skin check-in",
        "template": {
            "type": "buttons",
            "title": "Weekly Skin Check-in",
            "text": "How has your skin been this week? Tap to log it.",
            "actions": [
                {"type": "uri", "label": "Start check-in", "uri": liff_url}
            ],
        },
    }]
