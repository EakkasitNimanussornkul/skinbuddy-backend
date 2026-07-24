import json
import re
from typing import List, Optional

from langchain_core.messages import SystemMessage, HumanMessage

from app.db.connection import supabase
from app.core.services.chat_service import llm, embeddings
from app.db.repository import chat_repo


PRODUCT_LIMIT = 150  # cap products sent to the LLM to keep the prompt bounded


def _get_product_catalog(user_id: str) -> List[dict]:
    """The product catalog the chatbot can recommend from, each flagged with
    whether the user already owns it. The user does NOT need products in storage
    first — anything recommended is added to storage on apply."""
    products = (
        supabase.table("products")
        .select("id, brand, name, category")
        .limit(PRODUCT_LIMIT)
        .execute()
    )
    shelf = (
        supabase.table("shelf_items")
        .select("product_id")
        .eq("user_id", user_id)
        .execute()
    )
    owned = {item["product_id"] for item in (shelf.data or []) if item.get("product_id")}

    catalog = []
    for p in (products.data or []):
        catalog.append({
            "product_id": p["id"],
            "name": p.get("name"),
            "brand": p.get("brand"),
            "category": p.get("category"),
            "owned": p["id"] in owned,
        })
    return catalog


def _parse_json(text: str) -> Optional[dict]:
    """Best-effort extraction of the JSON object from an LLM reply."""
    if not text:
        return None
    try:
        match = re.search(r"\{.*\}", text, re.S)
        if match:
            return json.loads(match.group(0))
    except Exception:
        return None
    return None


def generate_routine(user_id: str, followup_answers: str = "") -> dict:
    """Generate a proposed routine.

    Returns either:
      { "steps": [ { product_id, product_name, category, step_order,
                     time_of_day, frequency, reason } ] }
    or an error dict: { "error": "no_products" | "llm_failure", "message": str }
    """
    catalog = _get_product_catalog(user_id)
    if not catalog:  # [E1] the product catalog itself is empty
        return {"error": "no_products",
                "message": "There are no products in the catalog yet, so a routine can't be built."}

    skin_type = chat_repo.get_user_skin_context(user_id)

    # RAG dermatology facts relevant to the user's stated concerns.
    query = followup_answers.strip() or "build a safe balanced skincare routine"
    try:
        query_vector = embeddings.embed_query(query)[:768]
        facts = chat_repo.get_matching_facts(query_vector)
    except Exception:
        facts = "No specific facts found."

    catalog_str = "\n".join(
        f'- id={p["product_id"]} | {p.get("brand") or ""} {p["name"]} ({p.get("category") or "unknown"})'
        f'{" [already in your storage]" if p["owned"] else ""}'
        for p in catalog
    )

    system = f"""You are the SkinBuddies routine planner. Build a safe, ordered skincare routine.
USER CONTEXT
- Skin type (Baumann): {skin_type}
- User concerns / notes: {followup_answers or "none provided"}

DERMATOLOGY FACTS (retrieved)
{facts}

AVAILABLE PRODUCTS (reference ONLY these, by their exact id):
{catalog_str}

RULES
- Only reference product ids from the list above. Never invent products or ids.
- PREFER products marked "[already in your storage]" when a suitable one exists,
  but you MAY include other catalog products when they meaningfully improve the routine.
- Build a complete routine (aim for cleanser -> treatment/serum -> moisturizer -> SPF);
  include a toner/exfoliant only if it fits the user's needs.
- Order steps by correct application order. SPF is AM only. Strong actives
  (retinoids, exfoliants) are usually PM.
- Respond in English.

Respond with STRICT JSON only (no markdown, no prose) in exactly this shape:
{{"steps":[{{"product_id":"<id>","step_order":1,"time_of_day":"AM|PM|both","frequency":"daily|3x_week|2x_week|weekly","reason":"one short sentence"}}]}}"""

    try:  # [E2] LLM/API failure
        response = llm.invoke([SystemMessage(content=system), HumanMessage(content="Generate the routine now.")])
    except Exception as e:
        print("Routine generation LLM error:", e)
        return {"error": "llm_failure", "message": "Could not generate a routine right now. Please try again."}

    data = _parse_json(response.content)
    if not data or "steps" not in data:
        return {"error": "llm_failure", "message": "Could not understand the generated routine. Please try again."}

    valid = {p["product_id"]: p for p in catalog}
    steps = []
    for i, s in enumerate(data.get("steps", [])):
        pid = s.get("product_id")
        if pid not in valid:  # drop any hallucinated product
            continue
        steps.append({
            "product_id": pid,
            "product_name": valid[pid]["name"],
            "brand": valid[pid].get("brand"),
            "category": valid[pid].get("category"),
            "owned": valid[pid]["owned"],
            "step_order": s.get("step_order", i + 1),
            "time_of_day": s.get("time_of_day", "both"),
            "frequency": s.get("frequency", "daily"),
            "reason": s.get("reason", ""),
        })

    if not steps:
        return {"error": "llm_failure", "message": "No valid products were selected. Please try again."}

    steps.sort(key=lambda x: x["step_order"])
    # Re-normalize step_order to 1..N
    for idx, step in enumerate(steps):
        step["step_order"] = idx + 1
    return {"steps": steps}
