from app.db.connection import supabase
from app.core.utils import create_slug
from app.core.services.ingredient_dictionary import parse_ingredient_data


def upsert_product(prod: dict) -> str:
    """Insert or update a product by its natural key (brand, name), preserving
    its id across reseeds/approvals so shelf_items/routine_steps FKs never break."""
    prod_payload = {
        "name": prod["name"], "brand": prod["brand"], "category": prod["category"],
        "description": prod.get("description"),
        "price_thb": prod.get("price_thb"), "price_usd": prod.get("price_usd"),
        "slug": create_slug(prod["brand"], prod["name"]),
    }

    # image_url is only written when we actually have one. Sending it
    # unconditionally meant any caller without an image - a reseed from
    # catalog.json, or an approved submission with no photo - would null out an
    # image the product already had. Same guard ingest_catalog.py already uses.
    if prod.get("image_url"):
        prod_payload["image_url"] = prod["image_url"]

    existing = supabase.table("products").select("id").eq("brand", prod["brand"]).eq("name", prod["name"]).execute()
    if existing.data:
        product_id = existing.data[0]["id"]
        supabase.table("products").update(prod_payload).eq("id", product_id).execute()
        return product_id
    return supabase.table("products").insert(prod_payload).execute().data[0]["id"]


def upsert_ingredient(ing_name: str) -> str:
    """Insert or update an ingredient by its name, preserving its id across reseeds/approvals."""
    profile = parse_ingredient_data(ing_name)
    ing_payload = {
        "name": ing_name, "functional_group": profile["group"],
        "awareness_tier": profile["tier"], "benefits": profile["benefits"],
        "good_for": profile["good_for"], "bad_for": profile["bad_for"],
        "source": profile["source"],
    }
    existing = supabase.table("ingredients").select("id").eq("name", ing_name).execute()
    if existing.data:
        ing_id = existing.data[0]["id"]
        supabase.table("ingredients").update(ing_payload).eq("id", ing_id).execute()
        return ing_id
    return supabase.table("ingredients").insert(ing_payload).execute().data[0]["id"]


def upsert_product_with_ingredients(prod: dict) -> str:
    """Upsert a product and all its ingredients, linking them via
    product_ingredients (skipping bridge rows that already exist). Used by
    the approved-product-submission flow; seed_db.py uses the lower-level
    upsert_product/upsert_ingredient directly since it also needs the
    ingredient name->id map for seeding conflict rules."""
    product_id = upsert_product(prod)
    for ing_name in prod.get("ingredients", []):
        ingredient_id = upsert_ingredient(ing_name)
        existing_link = (
            supabase.table("product_ingredients")
            .select("id")
            .eq("product_id", product_id)
            .eq("ingredient_id", ingredient_id)
            .execute()
        )
        if not existing_link.data:
            supabase.table("product_ingredients").insert({
                "product_id": product_id, "ingredient_id": ingredient_id
            }).execute()
    return product_id
