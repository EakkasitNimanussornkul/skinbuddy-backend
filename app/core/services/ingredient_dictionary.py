# Shared ingredient reference data used by both seed_db.py and ingest_catalog.py.
#
# SOURCING NOTE: entries below are manually authored from well-established,
# uncontroversial cosmetic-chemistry/dermatology knowledge (ingredient function,
# common usage guidance). They are NOT pulled from a live, per-ingredient
# citation (e.g. a specific CosIng substance ID or CIR report number) - automated
# fetching from those sources wasn't reliable to verify at the time this was
# written. Each entry's "source" field says so explicitly rather than implying a
# verified citation that doesn't exist. Treat this as a starting point for
# further manual review against CosIng/CIR/PubChem, not a finished citation.
GENERAL_SOURCE_NOTE = (
    "General cosmetic-chemistry/dermatology consensus (not independently "
    "verified against a specific citation) - cross-reference CosIng, CIR, "
    "or PubChem before relying on this for clinical guidance."
)
HEURISTIC_SOURCE_NOTE = "uncategorized-heuristic"

FRAGRANCE_ALLERGENS = ["geraniol", "linalool", "limonene", "citronellol", "eugenol", "citral", "farnesol"]

# "bad_for" values that reference a Baumann skin-type axis MUST include the
# literal "(<letter>)" marker (O/D, S/R, P/N, W/T) - the skin-type conflict
# check in compatibility_service.py matches on that exact substring.
CLINICAL_DICTIONARY = {
    "water": {
        "group": "Solvent", "tier": "low",
        "benefits": "The fundamental fluid base that delivers active ingredients into the skin.",
        "good_for": "All Skin Types", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "glycerin": {
        "group": "Humectant", "tier": "low",
        "benefits": "A powerful natural moisture-magnet that pulls water into the outer layer of the skin.",
        "good_for": "Dry Skin, Dehydrated Skin", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "niacinamide": {
        "group": "Vitamin B3", "tier": "low",
        "benefits": "Regulates sebum, improves barrier function, and fades hyperpigmentation.",
        "good_for": "Oily Skin, Acne-Prone", "bad_for": "Highly Sensitive Skin (S)",
        "source": GENERAL_SOURCE_NOTE,
    },
    "salicylic acid": {
        "group": "Beta Hydroxy Acid (BHA)", "tier": "high",
        "benefits": "Oil-soluble acid that penetrates pores to dissolve clogs and clear blackheads.",
        "good_for": "Acne-Prone Skin", "bad_for": "Extremely Dry Skin (D)",
        "source": GENERAL_SOURCE_NOTE,
    },
    "glycolic acid": {
        "group": "Alpha Hydroxy Acid (AHA)", "tier": "high",
        "benefits": "Melts dead surface cells to reveal brighter, smoother skin texture.",
        "good_for": "Dull Skin, Rough Texture", "bad_for": "Sensitive Skin (S), Active Rosacea",
        "source": GENERAL_SOURCE_NOTE,
    },
    "retinol": {
        "group": "Retinoid", "tier": "high",
        "benefits": "Gold-standard anti-aging active that accelerates cell turnover and boosts collagen.",
        "good_for": "Aging Skin", "bad_for": "Sensitive Skin (S), Pregnancy",
        "source": GENERAL_SOURCE_NOTE,
    },
    "ascorbic acid": {
        "group": "Vitamin C", "tier": "high",
        "benefits": "Potent antioxidant that neutralizes free radicals and brightens dark spots.",
        "good_for": "Dull Skin", "bad_for": "Active Acne, Highly Sensitive Skin (S)",
        "source": GENERAL_SOURCE_NOTE,
    },
    "panthenol": {
        "group": "Pro-Vitamin B5", "tier": "low",
        "benefits": "Deeply soothes irritation, reduces moisture loss, and accelerates skin healing.",
        "good_for": "Compromised Barriers, Dry Skin", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "sodium hyaluronate": {
        "group": "Humectant", "tier": "low",
        "benefits": "Salt form of hyaluronic acid that easily penetrates to plump fine lines with hydration.",
        "good_for": "Dehydrated Skin", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "cholesterol": {
        "group": "Skin-Identical Lipid", "tier": "low",
        "benefits": "Makes up roughly a quarter of the skin's natural barrier lipids; crucial for repairing dry, damaged skin.",
        "good_for": "Dry Skin, Eczema", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "cetyl alcohol": {
        "group": "Fatty Alcohol", "tier": "low",
        "benefits": "A non-drying fatty alcohol that acts as a rich emollient to soften the skin (distinct from drying alcohols like ethanol).",
        "good_for": "Dry Skin", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "cetearyl alcohol": {
        "group": "Fatty Alcohol", "tier": "low",
        "benefits": "Softens the skin and thickens formulas without clogging pores.",
        "good_for": "Dry Skin", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "ceramide np": {
        "group": "Skin-Identical Lipid", "tier": "low",
        "benefits": "Directly replenishes the skin's protective barrier to stop moisture loss and block environmental stress.",
        "good_for": "Dry Skin, Damaged Barriers", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "ceramide ap": {
        "group": "Skin-Identical Lipid", "tier": "low",
        "benefits": "Works synergistically with other ceramides to fortify the lipid barrier.",
        "good_for": "Dry Skin, Damaged Barriers", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "ceramide eop": {
        "group": "Skin-Identical Lipid", "tier": "low",
        "benefits": "Crucial for binding skin cells together to prevent environmental damage.",
        "good_for": "Dry Skin, Damaged Barriers", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "squalane": {
        "group": "Emollient", "tier": "low",
        "benefits": "A lightweight, highly stable oil that mimics natural sebum to prevent moisture loss.",
        "good_for": "All Skin Types", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "sodium chloride": {
        "group": "Viscosity Controller", "tier": "low",
        "benefits": "Standard table salt used in chemistry to thicken water-based formulas like cleansers.",
        "good_for": "All Skin Types", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "cellulose gum": {
        "group": "Thickener", "tier": "low",
        "benefits": "Derived from plant cell walls, it creates a smooth, gel-like texture for easy application.",
        "good_for": "All Skin Types", "bad_for": "None",
        "source": GENERAL_SOURCE_NOTE,
    },
    "phenoxyethanol": {
        "group": "Preservative", "tier": "medium",
        "benefits": "A broadly-used preservative that keeps the product safe from mold and bacteria; distinct from drying alcohols like ethanol.",
        "good_for": "All Skin Types", "bad_for": "Extremely Sensitive Skin (S)",
        "source": GENERAL_SOURCE_NOTE,
    },
}


def parse_ingredient_data(ing_name: str) -> dict:
    lower_name = ing_name.lower()
    if lower_name in CLINICAL_DICTIONARY:
        return CLINICAL_DICTIONARY[lower_name]
    if lower_name in FRAGRANCE_ALLERGENS or "fragrance" in lower_name or "parfum" in lower_name:
        return {
            "group": "Fragrance Component", "tier": "high",
            "benefits": "Adds scent to the formulation but provides no direct skincare benefit.",
            "good_for": "None", "bad_for": "Sensitive Skin (S), Rosacea, Eczema",
            "source": HEURISTIC_SOURCE_NOTE,
        }
    if "ceramide" in lower_name:
        return {
            "group": "Skin-Identical Lipid", "tier": "low",
            "benefits": "Directly replenishes the skin's protective barrier to stop moisture loss and block environmental stress.",
            "good_for": "Dry Skin, Damaged Barriers", "bad_for": "None",
            "source": HEURISTIC_SOURCE_NOTE,
        }
    if lower_name.endswith("gum") or lower_name.endswith("crosspolymer"):
        return {
            "group": "Texture Enhancer", "tier": "low",
            "benefits": "Thickens the formula to give it a smooth, spreadable texture.",
            "good_for": "All Skin Types", "bad_for": "None",
            "source": HEURISTIC_SOURCE_NOTE,
        }
    if "extract" in lower_name or "oil" in lower_name or "water" in lower_name:
        return {
            "group": "Botanical Nutrient", "tier": "medium",
            "benefits": "Natural plant-derived compound offering localized antioxidant properties.",
            "good_for": "Dry Skin", "bad_for": "Allergy-Prone Skin",
            "source": HEURISTIC_SOURCE_NOTE,
        }
    if lower_name.endswith("glycol") or lower_name.endswith("anediol"):
        return {
            "group": "Solvent & Humectant", "tier": "low",
            "benefits": "Helps active ingredients absorb deeply while pulling moisture into the skin.",
            "good_for": "Dehydrated Skin", "bad_for": "None",
            "source": HEURISTIC_SOURCE_NOTE,
        }
    if lower_name.endswith("cone") or lower_name.endswith("siloxane"):
        return {
            "group": "Silicone", "tier": "medium",
            "benefits": "Creates a breathable, silky film on the skin.",
            "good_for": "Dry Skin", "bad_for": "Acne-Prone Skin (if prone to clogging)",
            "source": HEURISTIC_SOURCE_NOTE,
        }
    if lower_name.endswith("paraben"):
        return {
            "group": "Preservative", "tier": "high",
            "benefits": "Prevents microbial growth to keep the product safe.",
            "good_for": "All Skin Types", "bad_for": "Sensitive Skin (S)",
            "source": HEURISTIC_SOURCE_NOTE,
        }

    return {
        "group": "Formulation Stabilizer", "tier": "medium",
        "benefits": "Supports the overall formula by balancing pH or maintaining shelf life.",
        "good_for": "All Skin Types", "bad_for": "None",
        "source": HEURISTIC_SOURCE_NOTE,
    }
