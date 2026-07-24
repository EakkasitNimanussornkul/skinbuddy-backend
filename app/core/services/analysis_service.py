"""
Weekly skin-log analysis (UC-24).

Invoked synchronously by POST /analysis/log when a weekly log is submitted.
Correlates the submitted log with recently-introduced routine products to flag
potential culprit ingredients, produce a trend verdict, and give recommendations.

Reuses the same LLM + embeddings + RAG facts as the chat/routine features.
"""
import json
import re
from datetime import date, timedelta
from typing import List, Optional

from langchain_core.messages import SystemMessage, HumanMessage

from app.db.connection import supabase
from app.core.services.chat_service import llm, embeddings
from app.db.repository import chat_repo

RECENT_WINDOW_DAYS = 21  # "recently introduced" = within ~2-3 weeks (SRS-92)


def _parse_json(text: str) -> Optional[dict]:
    if not text:
        return None
    try:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
    except Exception:
        return None
    return None


def _gather_routine_products(user_id: str):
    """Return (all_products, recently_introduced_products) for the active routine."""
    routine = (
        supabase.table("routines")
        .select("id")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .limit(1)
        .execute()
    )
    if not routine.data:
        return [], []

    routine_id = routine.data[0]["id"]
    steps = (
        supabase.table("routine_steps")
        .select("added_at, products(name, product_ingredients(ingredients(name)))")
        .eq("routine_id", routine_id)
        .execute()
    )

    cutoff = date.today() - timedelta(days=RECENT_WINDOW_DAYS)
    all_products, recent_products = [], []
    for st in (steps.data or []):
        prod = st.get("products")
        if not prod:
            continue
        ingredients = [
            pi["ingredients"]["name"]
            for pi in (prod.get("product_ingredients") or [])
            if pi.get("ingredients")
        ]
        entry = {"name": prod.get("name"), "ingredients": ingredients, "added_at": st.get("added_at")}
        all_products.append(entry)
        added = st.get("added_at")
        if added:
            try:
                if date.fromisoformat(str(added)[:10]) >= cutoff:
                    recent_products.append(entry)
            except ValueError:
                pass
    return all_products, recent_products


def analyze_log(log_row: dict, user_id: str) -> dict:
    """Produce a structured analysis report for a submitted weekly log.

    Returns: { flagged_ingredients: [...], trend_verdict: str,
               recommendations: [...], status: 'complete' | 'incomplete' }
    """
    week_start = log_row.get("week_start")
    all_products, recent_products = _gather_routine_products(user_id)

    # Prior logs for a week-over-week trend.
    prior = (
        supabase.table("skin_logs")
        .select("week_start, symptoms, notes")
        .eq("user_id", user_id)
        .neq("week_start", week_start)
        .order("week_start", desc=True)
        .limit(4)
        .execute()
    )
    prior_logs = prior.data or []
    insufficient = len(prior_logs) == 0  # [E1] first log / no baseline

    skin_type = chat_repo.get_user_skin_context(user_id)

    # RAG ingredient knowledge relevant to the reported symptoms (SRS-93).
    symptom_text = json.dumps(log_row.get("symptoms", [])) + " " + (log_row.get("notes") or "")
    try:
        query_vector = embeddings.embed_query(symptom_text)[:768]
        facts = chat_repo.get_matching_facts(query_vector)
    except Exception:
        facts = "No specific facts found."

    system = f"""You are the SkinBuddies weekly skin analyst. Correlate a user's weekly
skin self-report with the products recently introduced into their routine, and
assess the overall trend. Be scientifically careful and never alarmist.

USER CONTEXT
- Skin type (Baumann): {skin_type}

THIS WEEK'S LOG
- Symptoms (with 1-5 severity): {json.dumps(log_row.get("symptoms", []))}
- Affected areas: {json.dumps(log_row.get("affected_areas", []))}
- Notes: {log_row.get("notes") or "none"}

PRIOR WEEKS (most recent first): {json.dumps(prior_logs)}

RECENTLY INTRODUCED PRODUCTS (added within ~3 weeks): {json.dumps(recent_products)}
ALL CURRENT ROUTINE PRODUCTS: {json.dumps(all_products)}

RETRIEVED INGREDIENT FACTS
{facts}

INSTRUCTIONS
- Flag ingredients from RECENTLY INTRODUCED products that plausibly explain the reported symptoms. Explain the reasoning briefly.
- If there is no clear culprit, return an empty flagged_ingredients list.
- Give a short trend verdict comparing to prior weeks. {"Note that this is the first log, so baseline is limited." if insufficient else ""}
- Give 1-3 concrete recommendations.
- Respond in English.

Respond with STRICT JSON only (no markdown) in exactly this shape:
{{"flagged_ingredients":[{{"ingredient":"","product":"","reason":""}}],"trend_verdict":"","recommendations":[""]}}"""

    try:  # [E2] LLM failure -> incomplete, user can retry
        response = llm.invoke([SystemMessage(content=system), HumanMessage(content="Analyze now.")])
    except Exception as e:
        print("Analysis LLM error:", e)
        return {"flagged_ingredients": [], "trend_verdict": "Analysis could not be completed. Please retry.",
                "recommendations": [], "status": "incomplete"}

    data = _parse_json(response.content)
    if not data:
        return {"flagged_ingredients": [], "trend_verdict": "Analysis could not be completed. Please retry.",
                "recommendations": [], "status": "incomplete"}

    return {
        "flagged_ingredients": data.get("flagged_ingredients", []),
        "trend_verdict": data.get("trend_verdict", ""),
        "recommendations": data.get("recommendations", []),
        "status": "complete",
    }
