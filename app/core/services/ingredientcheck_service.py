# app/core/services/ingredient_checker.py
import re

KNOWN_ALCOHOLS = ["alcohol denat", "sd alcohol", "isopropyl alcohol", "ethanol"]
KNOWN_PARABENS = ["methylparaben", "ethylparaben", "propylparaben", "butylparaben"]
KNOWN_SILICONES = ["dimethicone", "cyclomethicone", "cyclopentasiloxane"]
KNOWN_SULFATES = ["sodium lauryl sulfate", "sodium laureth sulfate", "sls", "sles"]
KNOWN_FRAGRANCES = ["parfum", "fragrance", "linalool", "limonene", "citronellol"]
KNOWN_NON_VEGAN = ["carmine", "lanolin", "beeswax", "honey", "collagen", "squalene"]
FUNGAL_ACNE_TRIGGERS = [
    "myristate", "palmitate", "laurate", "oleate", "linoleate",
    "isostearate", "stearate", "caprylate", "caprate", "polysorbate", "sorbitan",
]

# Single-word terms matched as bare substrings, e.g. "ethanol", would also match
# inside unrelated compound words like "phenoxyethanol". Match these on word
# boundaries instead so only the standalone ingredient name triggers the flag.
#
# "honey" is here for the same reason: it matched inside "Lonicera Japonica
# (Honeysuckle) Flower Extract", marking a plant extract non-vegan. \bhoney\b
# does not match "honeysuckle", while still matching "Honey" and "Honey Extract".
# BE-DEF-09.
_WORD_BOUNDARY_TERMS = {"ethanol", "honey"}


def _matches(term: str, name: str) -> bool:
    if term in _WORD_BOUNDARY_TERMS:
        return re.search(rf"\b{re.escape(term)}\b", name) is not None
    return term in name


def calculate_safety_flags(product_ingredients: list) -> dict:
    names = []
    for item in product_ingredients:
        ing = item.get("ingredients") or item
        if ing and "name" in ing:
            names.append(ing["name"].lower())

    return {
        "alcohol_free": not any(any(_matches(a, n) for a in KNOWN_ALCOHOLS) for n in names),
        "fragrance_free": not any(any(_matches(f, n) for f in KNOWN_FRAGRANCES) for n in names),
        "paraben_free": not any(any(_matches(p, n) for p in KNOWN_PARABENS) for n in names),
        "silicone_free": not any(any(_matches(s, n) for s in KNOWN_SILICONES) for n in names),
        "sulfate_free": not any(any(_matches(s, n) for s in KNOWN_SULFATES) for n in names),
        "vegan": not any(any(_matches(v, n) for v in KNOWN_NON_VEGAN) for n in names),
        "fungal_safe": not any(any(_matches(t, n) for t in FUNGAL_ACNE_TRIGGERS) for n in names),
    }
