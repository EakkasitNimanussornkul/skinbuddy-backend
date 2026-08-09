import asyncio
import httpx
import re
import os
import sys
from collections import defaultdict

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.db.connection import supabase
from app.core.utils import create_slug
from app.core.services.ingredient_dictionary import parse_ingredient_data
from app.core.services.image_service import fetch_and_store_product_image

OBF_SEARCH_URL = "https://world.openbeautyfacts.org/api/v2/search"
TARGET_BRANDS = ["COSRX", "CeraVe", "The Ordinary", "Paula's Choice", "Eucerin", "Laneige", "Innisfree"]

# 🌟 PRODUCT NAME TRANSLATION & SANITIZATION MAP 🌟
# Automatically converts localized non-English terms into pristine English context
PRODUCT_NAME_REPLACEMENTS = {
    "locao": "Lotion",
    "loção": "Lotion",
    "hidratante": "Moisturizer",
    "crema": "Cream",
    "crème": "Cream",
    "creme": "Cream",
    "gel nettoyant": "Cleansing Gel",
    "nettoyant": "Cleanser",
    "baume": "Balm",
    "balsamo": "Balm",
    "bálsamo": "Balm",
    "espuma": "Foam",
    "limpiador": "Cleanser",
    "agua micelar": "Micellar Water",
    "eau micellaire": "Micellar Water",
    "protezione solare": "Sunscreen",
    "protector solar": "Sunscreen",
    "solaire": "Sun Care"
}

INCI_ALIAS_MAP = {
    "aqua": "Water", "eau": "Water", "purified water": "Water",
    "glycerol": "Glycerin", "l ascorbic acid": "Ascorbic Acid",
    "vitamin c": "Ascorbic Acid", "vitamin e": "Tocopherol",
    "provitamin b5": "Panthenol", "d-panthenol": "Panthenol",
    "bha": "Salicylic Acid", "aha": "Glycolic Acid",
    "hyaluronate sodium": "Sodium Hyaluronate", "centella asiatica extract": "Centella Asiatica",
    "alcool cétylique": "Cetyl Alcohol", "cholestérol": "Cholesterol"
}

def sanitize_product_name(raw_name: str) -> str:
    """Cleans up non-English cosmetic terms from product names to ensure uniform terminology."""
    working_name = raw_name.lower()
    
    # Replace individual localized terms with unified English equivalents
    for foreign_term, english_term in PRODUCT_NAME_REPLACEMENTS.items():
        # Use regex boundary matching to avoid accidental middle-of-word alterations
        working_name = re.sub(rf'\b{foreign_term}\b', english_term.lower(), working_name)
        
    # Standard clean up and conversion to Title Case
    working_name = re.sub(r'\s+', ' ', working_name).strip()
    return working_name.title()

def clean_and_normalize_ingredient(raw_name: str) -> str:
    name = re.sub(r'[\(\)\*]', '', raw_name.replace("en:", "").replace("-", " ").strip().lower())
    return INCI_ALIAS_MAP.get(name, name.title())

def determine_smart_category(prod_name: str, raw_cat: str) -> str:
    combined_text = f"{prod_name} {raw_cat}".lower()
    if any(k in combined_text for k in ["sun", "spf", "uv", "solare", "screen"]): return "Sun Care"
    if any(k in combined_text for k in ["eye", "yeux"]): return "Eye Care"
    if any(k in combined_text for k in ["mask", "masque", "sheet"]): return "Masks"
    if any(k in combined_text for k in ["exfoliat", "scrub", "peel", "peeling"]): return "Exfoliators"
    if any(k in combined_text for k in ["cleans", "wash", "foam", "mouss", "nettoyant", "micellar", "soap"]): return "Cleansers"
    if any(k in combined_text for k in ["toner", "astringent", "lotion tonique"]): return "Toners"
    if any(k in combined_text for k in ["serum", "ampoule", "essence", "treatment", "booster"]): return "Treatments"
    if any(k in combined_text for k in ["cream", "lotion", "moisturiz", "hydrat", "balm", "baume", "gel"]): return "Moisturizers"
    return "Other Skincare"

async def ingest_clean_brand(client: httpx.AsyncClient, brand_name: str, category_tracker: dict):
    print(f"🔄 Scanning catalog for: [{brand_name}]...")
    params = {"brands_tags_en": brand_name.lower(), "page_size": 40}

    try:
        res = await client.get(OBF_SEARCH_URL, params=params, timeout=20.0)
        products = res.json().get("products", [])
    except Exception as e:
        print(f"  ❌ Network error: {e}"); return

    for item in products:
        raw_name = item.get("product_name", "").strip()
        brand = (item.get("brands") or brand_name).split(",")[0].strip().title()
        raw_category = item.get("categories", "")
        raw_ingredients = item.get("ingredients", [])

        if not raw_name or len(raw_ingredients) < 4 or any(k in raw_name.lower() for k in ["www.", ".com", "&quot;"]): continue

        # 🌟 Intercept and Translate names into Clean English Context
        name = sanitize_product_name(raw_name)

        category = determine_smart_category(name, raw_category)
        if category_tracker[category] >= 3: continue

        slug = create_slug(brand, name)
        stored_image_url = await fetch_and_store_product_image(client, item.get("image_url"), slug)

        product_payload = {
            "name": name, "brand": brand, "category": category,
            "description": f"A specialized {category.lower()[:-1] if category.endswith('s') else category.lower()} formulated by {brand}.",
            "price_thb": 450, "price_usd": 15.00,
            "slug": slug
        }
        # Only set image_url when the fetch actually succeeded, so a transient
        # failure on a re-run never blanks out a previously-stored image.
        if stored_image_url:
            product_payload["image_url"] = stored_image_url

        existing_prod = supabase.table("products").select("id").eq("name", name).eq("brand", brand).execute()
        if existing_prod.data:
            product_id = existing_prod.data[0]["id"]
            supabase.table("products").update(product_payload).eq("id", product_id).execute()
        else:
            inserted_prod = supabase.table("products").insert(product_payload).execute()
            product_id = inserted_prod.data[0]["id"]
            
        category_tracker[category] += 1

        seen_ingredients = set()
        for ing_entry in raw_ingredients:
            ing_name = clean_and_normalize_ingredient(ing_entry.get("text") or ing_entry.get("id", ""))
            if not ing_name or len(ing_name) < 2 or ing_name in seen_ingredients or "www." in ing_name.lower(): continue
            seen_ingredients.add(ing_name)
            
            profile = parse_ingredient_data(ing_name)
            existing_ing = supabase.table("ingredients").select("id").eq("name", ing_name).execute()
            ing_payload = {
                "name": ing_name, "functional_group": profile["group"], "awareness_tier": profile["tier"],
                "benefits": profile["benefits"], "good_for": profile["good_for"], "bad_for": profile["bad_for"],
                "source": profile["source"],
            }

            if existing_ing.data:
                ing_id = existing_ing.data[0]["id"]
                supabase.table("ingredients").update(ing_payload).eq("id", ing_id).execute()
            else:
                ing_id = supabase.table("ingredients").insert(ing_payload).execute().data[0]["id"]

            check = supabase.table("product_ingredients").select("id").eq("product_id", product_id).eq("ingredient_id", ing_id).execute()
            if not check.data:
                supabase.table("product_ingredients").insert({"product_id": product_id, "ingredient_id": ing_id}).execute()

        print(f"  ✅ Added: {brand} - {name} ---> [Category: {category}]")

async def main():
    print("🚀 Running English Sanitized Ingestion Pipeline...")
    category_tracker = defaultdict(int)
    
    async with httpx.AsyncClient() as client:
        for brand in TARGET_BRANDS:
            await ingest_clean_brand(client, brand, category_tracker)
            await asyncio.sleep(1)
            
    print("\n📊 INGESTION SUMMARY (Target: max 3 per category):")
    for cat, count in category_tracker.items():
        print(f" - {cat}: {count} products")

if __name__ == "__main__":
    asyncio.run(main())