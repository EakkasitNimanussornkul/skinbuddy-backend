"""Tests for resolve_product_record (app/api/products.py).

The helper behind GET /products/slug/{slug}, GET /products/{product_id} and both
arms of GET /products/compare, so it is the path the frontend takes to reach a
product page. It tries four strategies in order and returns the first hit:

  1.  a direct UUID lookup, when the identifier is shaped like one
  1b. an indexed exact match on the products.slug column
  2.  a scan of up to 2000 rows, comparing the identifier stripped to
      alphanumerics against each product's derived slug and its name, exactly
      or as a substring either way
  3.  a fuzzy fallback: every word of three or more characters, up to the first
      three, must appear somewhere in "brand name"

Every other test in the suite identifies products by UUID, so until this module
existed only strategy 1 and the final no-match return were ever executed.

Called directly rather than through a route: these are about which row comes
back for a given string, and the HTTP layer has nothing to do with that.
asyncio.run rather than a plugin, since the helper is async and the suite has no
async test support.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import asyncio

from app.api.products import resolve_product_record

UUID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def catalogue_row(pid, brand, name, slug=None):
    """The shape the helper's select returns, with the joined ingredient rows it
    reads through and the slug column the indexed lookup matches on."""
    return {
        "id": pid, "brand": brand, "name": name, "category": "Cleanser",
        "slug": slug, "description": None, "price_thb": 500, "price_usd": 15,
        "image_url": None, "product_ingredients": [],
    }


CERAVE = catalogue_row("id-cerave", "CeraVe", "Hydrating Facial Cleanser")
ORDINARY = catalogue_row("id-ordinary", "The Ordinary", "Niacinamide 10% + Zinc 1%")


def resolve(identifier):
    return asyncio.run(resolve_product_record(identifier))


def seed(patch_supabase, products):
    return patch_supabase({"products": products}, "app.api.products")


# --- Strategy 1b: the indexed slug column ------------------------------------

def test_resolves_by_the_indexed_slug_column_ahead_of_the_name_scan(patch_supabase):
    """Returns the product whose slug column equals the identifier, even when a
    different product would be found by the derived-name scan below it.

    Two products match the same string by different routes: the decoy carries it
    in products.slug, CeraVe would match it as the slug derived from its brand
    and name. The indexed column is authoritative and is tried first, so the
    decoy is the answer. CeraVe is seeded first, so a scan reaching it would
    return the wrong row."""
    decoy = catalogue_row("id-decoy", "Other Labs", "Decoy Serum",
                          slug="cerave-hydrating-facial-cleanser")
    seed(patch_supabase, [CERAVE, decoy])

    result = resolve("cerave-hydrating-facial-cleanser")

    assert result["id"] == "id-decoy"


# --- Strategy 2: the normalised name scan ------------------------------------

def test_resolves_by_a_normalised_product_name(patch_supabase):
    """Returns the product whose name, stripped to alphanumerics, equals the
    identifier stripped the same way - so punctuation and spacing in the
    identifier do not have to match the catalogue.

    "hydratingfacialcleanser" reaches only this strategy: no product carries it
    as a slug, and as a single unbroken word it cannot satisfy the fuzzy
    fallback, which needs each word to appear in the brand-and-name text."""
    seed(patch_supabase, [CERAVE, ORDINARY])

    result = resolve("hydratingfacialcleanser")

    assert result["id"] == "id-cerave"


# --- Strategy 3: the fuzzy keyword fallback ----------------------------------

def test_resolves_by_a_fuzzy_keyword_phrase_in_any_order(patch_supabase):
    """Returns the product whose brand and name together contain every word of
    the identifier, regardless of the order they appear in.

    "niacinamide zinc ordinary" matches no slug and survives no exact or
    substring comparison against a derived slug or name, so this is the only
    strategy that can answer it. The words appear across both the brand and the
    name, and out of order."""
    seed(patch_supabase, [CERAVE, ORDINARY])

    result = resolve("niacinamide zinc ordinary")

    assert result["id"] == "id-ordinary"


def test_a_fuzzy_phrase_matching_only_some_words_resolves_to_nothing(patch_supabase):
    """Returns None for a phrase whose words are split across two products, with
    no single product containing them all.

    "niacinamide zinc cerave" names an ingredient of one product and the brand of
    another. Every word appears somewhere in the catalogue and none of the three
    strategies above answers it, so the fallback decides - and it requires every
    word, not any word. Loosened to `any`, this would resolve to whichever
    product happened to be scanned first, which is how a search for two products
    at once silently returns one of them."""
    seed(patch_supabase, [CERAVE, ORDINARY])

    assert resolve("niacinamide zinc cerave") is None


# --- No strategy matches -----------------------------------------------------

def test_returns_none_when_no_strategy_matches_a_non_uuid_identifier(patch_supabase):
    """Returns None for a name-shaped identifier that no strategy can resolve,
    which is what the callers turn into a 404.

    The suite's other no-match case uses an unknown UUID, which is answered by
    strategy 1 alone and never reaches the three below it. This one runs the
    whole chain and falls off the end."""
    seed(patch_supabase, [CERAVE, ORDINARY])

    assert resolve("totally-unknown-product") is None


def test_returns_none_for_a_well_formed_uuid_matching_no_row(patch_supabase):
    """Returns None rather than raising for a UUID that matches nothing.

    Regression guard: this branch used .single(), which the real client raises
    PGRST116 on for zero rows, so the exception escaped into the callers'
    generic clauses and GET /products/compare answered 400 where it documents a
    404."""
    seed(patch_supabase, [CERAVE, ORDINARY])

    assert resolve(UUID_A) is None


# --- Precedence --------------------------------------------------------------

def test_a_uuid_resolves_by_id_even_when_another_product_matches_the_string(patch_supabase):
    """Returns the product whose id is the UUID, not the one carrying that UUID
    in its slug column.

    Both would match, by different strategies. The id lookup is tried first, and
    the slug-holder is seeded first so a fall-through would return it."""
    slug_holder = catalogue_row("id-slug-holder", "Other Labs", "Decoy Serum", slug=UUID_A)
    id_owner = catalogue_row(UUID_A, "CeraVe", "Hydrating Facial Cleanser")
    seed(patch_supabase, [slug_holder, id_owner])

    result = resolve(UUID_A)

    assert result["id"] == UUID_A
    assert result["brand"] == "CeraVe"
