import json
import re
from typing import List, Optional

from langchain_core.messages import SystemMessage, HumanMessage

from app.db.connection import supabase
from app.core.services.chat_service import llm, embeddings
from app.core.services import compatibility_service
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


def _fetch_products_with_ingredients(product_ids: List[str]) -> dict:
    """product_id -> the products(...) row shape compatibility_service expects."""
    if not product_ids:
        return {}
    res = (
        supabase.table("products")
        .select("id, name, product_ingredients(ingredients(id, name, functional_group))")
        .in_("id", product_ids)
        .execute()
    )
    return {p["id"]: p for p in (res.data or [])}


def _run_compatibility_checks(user_id: str, steps: List[dict]) -> List[dict]:
    """Run the UC-06 conflict engine over the proposed routine.

    Each step is analysed against every OTHER step in the proposal, so the checks
    are scoped to the routine being proposed rather than the user's shelf. Returns
    one entry per step that produced warnings; an empty list means the engine found
    nothing. Deterministic — no LLM involved.
    """
    product_ids = [s["product_id"] for s in steps]
    product_map = _fetch_products_with_ingredients(product_ids)

    findings = []
    for step in steps:
        pid = step["product_id"]
        comparison = [
            compatibility_service.normalize_product(product_map[other])
            for other in product_ids
            if other != pid and other in product_map
        ]
        try:
            result = compatibility_service.analyze(pid, user_id, comparison)
        except Exception as e:  # one unanalysable product must not sink the pass
            print(f"Routine compatibility check failed for {pid}:", e)
            continue
        if not result.is_safe:
            findings.append({
                "product_id": pid,
                "product_name": step.get("product_name"),
                "warnings": [
                    {"type": w.alert_type, "severity": w.severity, "message": w.message}
                    for w in result.warnings
                ],
            })
    return findings


def _validate_routine(steps: List[dict], findings: List[dict], skin_type: str,
                      facts: str, valid: dict) -> Optional[List[dict]]:
    """Second LLM pass: revise the routine in light of the engine's findings.

    The model is not asked to grade its own work — it is given concrete, rule-based
    conflicts and asked to resolve them by separating steps across AM/PM, lowering a
    frequency, dropping a step, or strengthening its caution text.

    Returns the revised steps, or None if the pass could not be completed — callers
    fall back to the unvalidated routine rather than failing the request.
    """
    proposal = [
        {
            "product_id": s["product_id"],
            "product_name": s["product_name"],
            "step_order": s["step_order"],
            "time_of_day": s["time_of_day"],
            "frequency": s["frequency"],
            "caution": s.get("caution", ""),
        }
        for s in steps
    ]

    system = f"""You are the SkinBuddies routine safety reviewer. A routine has been proposed
and then checked by a deterministic ingredient-conflict engine. Your job is to resolve the
conflicts the engine found — not to re-invent the routine.

USER SKIN TYPE (Baumann): {skin_type}

PROPOSED ROUTINE
{json.dumps(proposal, indent=2)}

CONFLICTS FOUND BY THE ENGINE (authoritative — do not dismiss these)
{json.dumps(findings, indent=2) if findings else "None. The engine found no conflicts."}

DERMATOLOGY FACTS (retrieved)
{facts}

HOW TO RESOLVE, in order of preference
1. Separate the conflicting products into different sessions (one AM, one PM).
2. Lower the frequency of the harsher product so they are used on different days.
3. Strengthen the "caution" text so the user knows what to watch for.
4. Only drop a step if the conflict is severe and cannot be resolved any other way.

RULES
- Keep the same product_id values. Never introduce a product that was not proposed.
- Preserve correct application order: cleanser -> treatment/serum -> moisturizer -> SPF.
- SPF stays AM. Strong actives (retinoids, exfoliants) stay PM.
- Every step must keep a valid time_of_day ("AM"/"PM"/"both") and frequency
  ("daily"/"3x_week"/"2x_week"/"weekly").
- If the engine found no conflicts, return the routine unchanged apart from any
  caution text you can genuinely improve.
- Respond in English.

Respond with STRICT JSON only (no markdown, no prose) in exactly this shape:
{{"steps":[{{"product_id":"<id>","step_order":1,"time_of_day":"AM|PM|both","frequency":"daily|3x_week|2x_week|weekly","reason":"one short sentence","caution":"short warning or empty string"}}]}}"""

    try:
        response = llm.invoke([
            SystemMessage(content=system),
            HumanMessage(content="Review and return the corrected routine now."),
        ])
    except Exception as e:
        print("Routine validation LLM error:", e)
        return None

    data = _parse_json(response.content)
    if not data or "steps" not in data:
        print("Routine validation returned unparseable output; keeping the original.")
        return None

    revised = []
    for i, s in enumerate(data.get("steps", [])):
        pid = s.get("product_id")
        if pid not in valid:  # the second pass can hallucinate too
            continue
        revised.append({
            "product_id": pid,
            "product_name": valid[pid]["name"],
            "brand": valid[pid].get("brand"),
            "category": valid[pid].get("category"),
            "owned": valid[pid]["owned"],
            "step_order": s.get("step_order", i + 1),
            "time_of_day": s.get("time_of_day", "both"),
            "frequency": s.get("frequency", "daily"),
            "reason": s.get("reason", ""),
            "caution": s.get("caution", ""),
        })

    if not revised:  # a pass that empties the routine is a failed pass
        print("Routine validation dropped every step; keeping the original.")
        return None
    return revised


def generate_routine(user_id: str, followup_answers: str = "") -> dict:
    """Generate a proposed routine.

    Runs in two stages: the LLM proposes a routine from the catalog, then the
    proposal is checked by the deterministic UC-06 conflict engine and handed back
    to the LLM to resolve anything it found.

    Returns either:
      { "steps": [ { product_id, product_name, category, step_order,
                     time_of_day, frequency, reason, caution } ],
        "validation": { status, conflicts_found, findings } }
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
- Set BOTH schedules per step:
  - time_of_day = "AM", "PM", or "both" (which session).
  - frequency = "daily", "3x_week", "2x_week", or "weekly" (which days).
    Cleanser/moisturizer/SPF are usually daily. Exfoliants ~2x_week.
    Retinoids ~3x_week. Strong weekly treatments/masks = weekly.
- For "caution": if the product has a notable irritant / sun-sensitising / "start
  slow" concern, give a short warning; otherwise use an empty string "".
- Respond in English.

Respond with STRICT JSON only (no markdown, no prose) in exactly this shape:
{{"steps":[{{"product_id":"<id>","step_order":1,"time_of_day":"AM|PM|both","frequency":"daily|3x_week|2x_week|weekly","reason":"one short sentence","caution":"short warning or empty string"}}]}}"""

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
            "caution": s.get("caution", ""),
        })

    if not steps:
        return {"error": "llm_failure", "message": "No valid products were selected. Please try again."}

    steps.sort(key=lambda x: x["step_order"])
    # Re-normalize step_order to 1..N
    for idx, step in enumerate(steps):
        step["step_order"] = idx + 1

    # --- Self-verification pass -------------------------------------------------
    # Check the proposal with the deterministic UC-06 conflict engine, then let the
    # LLM resolve whatever it found. Both halves are best-effort: if either fails,
    # the unvalidated routine is still returned rather than failing the request.
    findings = _run_compatibility_checks(user_id, steps)
    revised = _validate_routine(steps, findings, skin_type, facts, valid)

    if revised:
        revised.sort(key=lambda x: x["step_order"])
        for idx, step in enumerate(revised):
            step["step_order"] = idx + 1
        steps = revised
        validation_status = "validated"
    else:
        validation_status = "unvalidated"

    return {
        "steps": steps,
        "validation": {
            "status": validation_status,
            "conflicts_found": len(findings),
            "findings": findings,
        },
    }
