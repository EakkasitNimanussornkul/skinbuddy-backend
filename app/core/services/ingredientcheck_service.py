# app/core/services/ingredient_checker.py

KNOWN_ALCOHOLS = ["alcohol denat", "sd alcohol", "isopropyl alcohol", "ethanol"]
KNOWN_PARABENS = ["methylparaben", "ethylparaben", "propylparaben", "butylparaben"]
KNOWN_SILICONES = ["dimethicone", "cyclomethicone", "cyclopentasiloxane"]
KNOWN_SULFATES = ["sodium lauryl sulfate", "sodium laureth sulfate", "sls", "sles"]
KNOWN_FRAGRANCES = ["parfum", "fragrance", "linalool", "limonene", "citronellol"]
KNOWN_NON_VEGAN = ["carmine", "lanolin", "beeswax", "honey", "collagen", "squalene"]

def calculate_safety_flags(product_ingredients: list) -> dict:
    names = []
    for item in product_ingredients:
        ing = item.get("ingredients") or item
        if ing and "name" in ing:
            names.append(ing["name"].lower())

    return {
        "alcohol_free": not any(any(a in n for a in KNOWN_ALCOHOLS) for n in names),
        "fragrance_free": not any(any(f in n for f in KNOWN_FRAGRANCES) for n in names),
        "paraben_free": not any(any(p in n for p in KNOWN_PARABENS) for n in names),
        "silicone_free": not any(any(s in n for s in KNOWN_SILICONES) for n in names),
        "sulfate_free": not any(any(s in n for s in KNOWN_SULFATES) for n in names),
        "vegan": not any(any(v in n for v in KNOWN_NON_VEGAN) for n in names),
        "fungal_safe": not any("polysorbate" in n or "ester" in n for n in names),
    }