"""Load a catalogue expansion: new products with their ingredients, concerns,
conflict rules and sources, from the JSON file the documentation session
prepares (spec: docs/DATA_EXPANSION_SPEC.md in the backend repo).

Nothing here writes to the database. It validates the whole file against the
live catalogue, then generates a SQL file the owner reviews and runs in the
Supabase SQL editor, like migrations 0008-0010. The SQL only ADDS rows - it
never updates or deletes an existing one - and every insert is guarded, so
running it twice adds nothing.

    python -m app.db.catalog_expansion check  <file.json>
    python -m app.db.catalog_expansion sql    <file.json> [--out path.sql]
    python -m app.db.catalog_expansion images <file.json>     # after the SQL has run

`images` is the one command that writes: it copies each new product's Open
Beauty Facts image into our storage bucket and sets image_url where it is
still empty. SQL cannot fetch images, so this is a separate, explicit step.

Why not seed_db.py: it rewrites every existing ingredient's profile from the
code dictionary and deletes then rebuilds all conflict rules from
seed_conflict.json, which holds 11 of the 17 live rules. Both would destroy
live data.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from app.api.products import GOOD_FOR_TRAITS, NEUTRAL_GOOD_FOR
from app.core.utils import create_slug

CATEGORIES = {"Cleansers", "Toners", "Treatments", "Moisturizers", "Sun Care", "Exfoliators"}
SOURCE_TYPES = {"regulatory_register", "safety_review", "chemical_database",
                "peer_reviewed", "reference_book", "product_database"}
INGREDIENT_CLAIMS = {"function", "benefits", "good_for", "bad_for"}
PRODUCT_CLAIMS = {"listing", "price", "image", "description"}
CONCERN_SEVERITIES = {"High", "Moderate", "Low"}
RULE_SEVERITIES = {"high", "medium", "low"}
AWARENESS_TIERS = {"low", "medium", "high"}
TOP_LEVEL = {"sources", "products", "ingredients", "concerns", "conflict_rules", "category_rules"}
OBF_IMAGE_HOST = "https://images.openbeautyfacts.org/"
MARKER = re.compile(r"\(([ODSRPNWT])\)")
SOURCE_KEY = re.compile(r"^S(\d+)$")
FIRST_KEY = 101   # S01-S80 belong to the owner's sources sheet

# Written to ingredients.source for rows this loader adds, so the verification
# note says where the row came from; the claims themselves link to sources.
INGREDIENT_SOURCE_NOTE = "catalogue-expansion: claims link to checked sources in ingredient_sources where found"


@dataclass
class Snapshot:
    """What already exists, read from the live database. Tests build one by hand."""
    products: Set[Tuple[str, str]] = field(default_factory=set)        # (brand, name)
    slugs: Set[str] = field(default_factory=set)
    ingredients: Dict[str, int] = field(default_factory=dict)           # name -> number of rows with it
    ingredient_bad_for: Dict[str, str] = field(default_factory=dict)
    functional_groups: Set[str] = field(default_factory=set)
    source_urls: Set[str] = field(default_factory=set)
    concerns: Set[Tuple[str, str]] = field(default_factory=set)         # (ingredient name, target_profile)
    rule_pairs: Set[FrozenSet[str]] = field(default_factory=set)        # ingredient names
    category_pairs: Set[FrozenSet[str]] = field(default_factory=set)    # group names
    has_product_sources: bool = False                                   # migration 0010 applied


@dataclass
class Report:
    errors: List[str] = field(default_factory=list)     # the file cannot be loaded until these are fixed
    warnings: List[str] = field(default_factory=list)   # loadable, but worth a look

    def error(self, where: str, message: str) -> None:
        self.errors.append(f"{where}: {message}")

    def warn(self, where: str, message: str) -> None:
        self.warnings.append(f"{where}: {message}")


# --- Validation ----------------------------------------------------------------

def _text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _markers(text: Optional[str]) -> Set[str]:
    return set(MARKER.findall(text or ""))


def _check_source_refs(report: Report, where: str, refs: Any, keys: Set[str], used: Set[str]) -> None:
    if not isinstance(refs, list):
        report.error(where, "must be a list of source keys")
        return
    for ref in refs:
        if ref not in keys:
            report.error(where, f"source key {ref!r} is not in this file's sources list")
        else:
            used.add(ref)


def _check_claim_sources(report: Report, where: str, sources: Any, claims: Set[str],
                         keys: Set[str], used: Set[str]) -> None:
    if sources is None:
        return
    if not isinstance(sources, dict):
        report.error(where, f"must be an object keyed by claim ({', '.join(sorted(claims))})")
        return
    for claim, refs in sources.items():
        if claim not in claims:
            report.error(where, f"claim {claim!r} is not one of {sorted(claims)}")
        else:
            _check_source_refs(report, f"{where}.{claim}", refs, keys, used)


def validate(data: Any, snap: Snapshot) -> Report:
    """Check the whole file against the spec and the live catalogue. Every
    problem is reported with where it is; nothing is guessed or fixed."""
    report = Report()
    if not isinstance(data, dict):
        report.error("file", "must be a JSON object")
        return report
    for key in set(data) - TOP_LEVEL:
        report.error("file", f"unknown top-level key {key!r}")
    for key in TOP_LEVEL & set(data):
        if not isinstance(data[key], list):
            report.error(key, "must be a list")
    if report.errors:
        return report

    # Sources --------------------------------------------------------------
    keys: Set[str] = set()
    used: Set[str] = set()
    urls_seen: Set[str] = set()
    untitled_seen: Set[str] = set()
    for i, s in enumerate(data.get("sources", [])):
        where = f"sources[{i}]"
        key = s.get("key")
        m = SOURCE_KEY.match(key or "")
        if not m:
            report.error(where, f"key {key!r} must look like S101")
        elif int(m.group(1)) < FIRST_KEY:
            report.error(where, f"key {key!r} must be S{FIRST_KEY} or above (S01-S80 are the owner's sheet)")
        elif key in keys:
            report.error(where, f"key {key!r} is used twice")
        else:
            keys.add(key)
        if not _text(s.get("title")):
            report.error(where, "title is required")
        url = s.get("url")
        if url is not None and not (isinstance(url, str) and url.startswith(("https://", "http://"))):
            report.error(where, f"url {url!r} must start with http:// or https://")
        if not url and not _text(s.get("notes")):
            report.error(where, "needs a url, or notes saying where it can be found (e.g. a book's page)")
        if url:
            if url in urls_seen:
                report.error(where, f"url {url} is listed twice; use one key for it")
            urls_seen.add(url)
            if url in snap.source_urls:
                report.warn(where, f"url {url} is already in the sources table; the existing row will be linked")
        elif _text(s.get("title")):
            if s["title"] in untitled_seen:
                report.error(where, f"two url-less sources share the title {s['title']!r}")
            untitled_seen.add(s["title"])
        if s.get("source_type") not in SOURCE_TYPES:
            report.error(where, f"source_type {s.get('source_type')!r} is not one of {sorted(SOURCE_TYPES)}")
        try:
            date.fromisoformat(s.get("accessed_on") or "")
        except ValueError:
            report.error(where, "accessed_on must be the YYYY-MM-DD date the page was opened and checked")

    # New ingredients --------------------------------------------------------
    new_ingredients: Dict[str, dict] = {}
    for i, ing in enumerate(data.get("ingredients", [])):
        where = f"ingredients[{i}]"
        name = ing.get("name")
        if not _text(name):
            report.error(where, "name is required")
            continue
        where = f"{where} ({name})"
        if name in snap.ingredients:
            report.error(where, "already exists; this file only adds new ingredients (list corrections separately)")
        if name in new_ingredients:
            report.error(where, "is defined twice in this file")
        new_ingredients[name] = ing
        if ing.get("functional_group") not in snap.functional_groups:
            report.error(where, f"functional_group {ing.get('functional_group')!r} is not an existing group")
        if ing.get("awareness_tier") not in AWARENESS_TIERS:
            report.error(where, f"awareness_tier {ing.get('awareness_tier')!r} is not one of {sorted(AWARENESS_TIERS)}")
        if not _text(ing.get("benefits")):
            report.error(where, "benefits is required")
        for phrase in [p.strip() for p in (ing.get("good_for") or "").split(",") if p.strip()]:
            if phrase.lower() not in GOOD_FOR_TRAITS and phrase.lower() not in NEUTRAL_GOOD_FOR:
                report.error(where, f"good_for phrase {phrase!r} is not in the mapped or neutral list")
        if not _text(ing.get("bad_for")):
            report.error(where, "bad_for is required; use \"None\" when nothing applies")
        _check_claim_sources(report, f"{where}.sources", ing.get("sources"), INGREDIENT_CLAIMS, keys, used)

    def ingredient_ref(where: str, name: Any) -> bool:
        """An ingredient a row may point at: defined here, or exactly one live row."""
        if not _text(name):
            report.error(where, "ingredient name is required")
            return False
        if name in new_ingredients:
            return True
        count = snap.ingredients.get(name, 0)
        if count == 0:
            report.error(where, f"ingredient {name!r} does not exist and is not defined in this file")
            return False
        if count > 1:
            report.error(where, f"ingredient {name!r} exists as {count} separate rows; the owner must merge them first")
            return False
        return True

    # Products -----------------------------------------------------------------
    products_seen: Set[Tuple[str, str]] = set()
    in_products: Set[str] = set()       # listed by a product: the only way users ever see an ingredient
    in_concerns: Set[str] = set()
    in_rules: Set[str] = set()
    for i, p in enumerate(data.get("products", [])):
        where = f"products[{i}]"
        brand, name = p.get("brand"), p.get("name")
        if not (_text(brand) and _text(name)):
            report.error(where, "brand and name are required")
            continue
        where = f"{where} ({brand} | {name})"
        if (brand, name) in snap.products:
            report.error(where, "already exists; this file only adds new products")
        if (brand, name) in products_seen:
            report.error(where, "is listed twice")
        products_seen.add((brand, name))
        if create_slug(brand, name) in snap.slugs:
            report.error(where, f"its slug {create_slug(brand, name)!r} is already taken by another product")
        if p.get("category") not in CATEGORIES:
            report.error(where, f"category {p.get('category')!r} is not one of {sorted(CATEGORIES)}")
        if not _text(p.get("description")):
            report.error(where, "description is required")
        for price in ("price_thb", "price_usd"):
            value = p.get(price)
            if value is not None and (not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0):
                report.error(where, f"{price} must be a positive number")
        if p.get("source_url") is not None and not str(p["source_url"]).startswith(("https://", "http://")):
            report.error(where, "source_url must start with http:// or https://")
        image = p.get("image_source_url")
        if image is not None and not str(image).startswith(OBF_IMAGE_HOST):
            report.error(where, f"image_source_url must be an Open Beauty Facts image ({OBF_IMAGE_HOST}...)")
        ingredients = p.get("ingredients")
        if not isinstance(ingredients, list) or not ingredients:
            report.error(where, "ingredients must be a non-empty list in label order")
        else:
            dupes = {n for n in ingredients if ingredients.count(n) > 1}
            if dupes:
                report.error(where, f"lists {sorted(dupes)} more than once")
            for n in ingredients:
                if ingredient_ref(f"{where}.ingredients", n):
                    in_products.add(n)
        _check_claim_sources(report, f"{where}.sources", p.get("sources"), PRODUCT_CLAIMS, keys, used)
        sources = p.get("sources") or {}
        if (p.get("price_thb") is not None or p.get("price_usd") is not None) and not sources.get("price"):
            report.warn(where, "has a price but no price source")
        if not sources.get("listing") and not p.get("source_url"):
            report.warn(where, "says nowhere where its ingredient list was seen")
        if sources and not snap.has_product_sources:
            report.warn(where, "product sources need migration 0010; until it runs they are kept as SQL comments")

    # Concerns -----------------------------------------------------------------
    concerns_seen: Set[Tuple[str, str]] = set()
    for i, c in enumerate(data.get("concerns", [])):
        where = f"concerns[{i}]"
        name = c.get("ingredient")
        if not ingredient_ref(where, name):
            continue
        in_concerns.add(name)
        where = f"{where} ({name})"
        profile = c.get("target_profile")
        if not _text(profile):
            report.error(where, "target_profile is required")
            continue
        if not (_text(c.get("concern_title")) and _text(c.get("concern_description"))):
            report.error(where, "concern_title and concern_description are required")
        if c.get("severity") not in CONCERN_SEVERITIES:
            report.error(where, f"severity {c.get('severity')!r} is not one of {sorted(CONCERN_SEVERITIES)} (concerns use Moderate)")
        if (name, profile) in snap.concerns or (name, profile) in concerns_seen:
            report.error(where, f"a concern for {profile!r} already exists for this ingredient")
        concerns_seen.add((name, profile))
        bad_for = new_ingredients[name].get("bad_for") if name in new_ingredients else snap.ingredient_bad_for.get(name)
        stray = _markers(profile) - _markers(bad_for)
        if stray:
            report.error(where, f"target_profile marks ({', '.join(sorted(stray))}) but the ingredient's bad_for "
                                f"{bad_for!r} does not, so it could never explain a warning")
        _check_source_refs(report, f"{where}.sources", c.get("sources", []), keys, used)

    # Conflict rules -------------------------------------------------------------
    pairs_seen: Set[FrozenSet[str]] = set()
    for i, r in enumerate(data.get("conflict_rules", [])):
        where = f"conflict_rules[{i}]"
        a, b = r.get("ingredient_a"), r.get("ingredient_b")
        ok = ingredient_ref(where, a) & ingredient_ref(where, b)
        if not ok:
            continue
        in_rules.update((a, b))
        where = f"{where} ({a} + {b})"
        if a == b:
            report.error(where, "a rule needs two different ingredients")
        pair = frozenset((a, b))
        if pair in snap.rule_pairs or pair in pairs_seen:
            report.error(where, "a rule for this pair already exists (in either order)")
        pairs_seen.add(pair)
        if r.get("severity") not in RULE_SEVERITIES:
            report.error(where, f"severity {r.get('severity')!r} is not one of {sorted(RULE_SEVERITIES)} (rules use lower case)")
        if not _text(r.get("warning_message")):
            report.error(where, "warning_message is required")
        _check_source_refs(report, f"{where}.sources", r.get("sources", []), keys, used)

    groups_seen: Set[FrozenSet[str]] = set()
    for i, r in enumerate(data.get("category_rules", [])):
        where = f"category_rules[{i}]"
        a, b = r.get("group_a"), r.get("group_b")
        bad = [g for g in (a, b) if g not in snap.functional_groups]
        if bad:
            report.error(where, f"group(s) {bad} are not existing functional groups")
            continue
        where = f"{where} ({a} + {b})"
        pair = frozenset((a, b))
        if a == b:
            report.error(where, "a rule needs two different groups")
        if pair in snap.category_pairs or pair in groups_seen:
            report.error(where, "a rule for this group pair already exists (in either order)")
        groups_seen.add(pair)
        if r.get("severity") not in RULE_SEVERITIES:
            report.error(where, f"severity {r.get('severity')!r} is not one of {sorted(RULE_SEVERITIES)} (rules use lower case)")
        if not _text(r.get("warning_message")):
            report.error(where, "warning_message is required")
        _check_source_refs(report, f"{where}.sources", r.get("sources", []), keys, used)

    for key in sorted(keys - used):
        report.warn("sources", f"{key} is not referenced by anything")
    # A new ingredient reaches users only through a product's list; a concern or
    # rule on it alone is never shown. Live ingredients are left alone: adding a
    # concern or rule to one is normal.
    for name in sorted(set(new_ingredients) - in_products):
        users = [label for label, names in (("concern(s)", in_concerns), ("rule(s)", in_rules)) if name in names]
        if users:
            report.warn("ingredients", f"{name!r} is in no product; only its {' and '.join(users)} "
                                       "use it, so users will never see it")
        else:
            report.warn("ingredients", f"{name!r} is defined but no product, concern or rule uses it")
    return report


# --- SQL generation --------------------------------------------------------------

def q(value: Any) -> str:
    """A SQL literal. Strings are single-quoted with quotes doubled."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def _source_match(s: dict, alias: str = "s") -> str:
    """How a generated statement finds a source row: by url, or by title for a url-less one."""
    if s.get("url"):
        return f"{alias}.url = {q(s['url'])}"
    return f"{alias}.url is null and {alias}.title = {q(s['title'])}"


def generate_sql(data: dict, snap: Snapshot, source_file: str = "catalog_expansion.json") -> str:
    """Additive SQL for a validated file. Every insert is guarded by the row's
    natural key, so running the file twice adds nothing."""
    by_key = {s["key"]: s for s in data.get("sources", [])}
    out: List[str] = [
        f"-- Generated by app/db/catalog_expansion.py from {source_file}.",
        "-- REVIEW, then run in the Supabase SQL editor. Adds rows only; never updates or deletes.",
        "-- Safe to run twice. Requires migrations 0009" + (" and 0010." if snap.has_product_sources else "."),
        "",
        "begin;",
        "",
        "-- Sources",
    ]
    for s in data.get("sources", []):
        out.append(
            "insert into public.sources (title, publisher, url, source_type, accessed_on, notes)\n"
            f"select {q(s['title'])}, {q(s.get('publisher'))}, {q(s.get('url'))}, {q(s['source_type'])}, "
            f"{q(s['accessed_on'])}::date, {q(s.get('notes'))}\n"
            f"where not exists (select 1 from public.sources s where {_source_match(s)});")

    out += ["", "-- New ingredients"]
    for ing in data.get("ingredients", []):
        out.append(
            "insert into public.ingredients (name, functional_group, awareness_tier, benefits, good_for, bad_for, source)\n"
            f"select {q(ing['name'])}, {q(ing['functional_group'])}, {q(ing['awareness_tier'])}, {q(ing['benefits'])}, "
            f"{q(ing.get('good_for') or 'None')}, {q(ing['bad_for'])}, {q(INGREDIENT_SOURCE_NOTE)}\n"
            f"where not exists (select 1 from public.ingredients i where i.name = {q(ing['name'])});")

    out += ["", "-- New products, and their ingredients (product_ingredients keeps no order; label order is not stored)"]
    deferred_product_sources: List[str] = []
    for p in data.get("products", []):
        brand, name = p["brand"], p["name"]
        find_p = f"p.brand = {q(brand)} and p.name = {q(name)}"
        out.append(
            "insert into public.products (brand, name, category, description, price_thb, price_usd, slug, source_url)\n"
            f"select {q(brand)}, {q(name)}, {q(p['category'])}, {q(p['description'])}, {q(p.get('price_thb'))}, "
            f"{q(p.get('price_usd'))}, {q(create_slug(brand, name))}, {q(p.get('source_url'))}\n"
            f"where not exists (select 1 from public.products p where {find_p});")
        for ing_name in p["ingredients"]:
            out.append(
                "insert into public.product_ingredients (product_id, ingredient_id)\n"
                f"select p.id, i.id from public.products p, public.ingredients i\n"
                f"where {find_p} and i.name = {q(ing_name)}\n"
                "  and not exists (select 1 from public.product_ingredients pi where pi.product_id = p.id and pi.ingredient_id = i.id);")
        for claim, refs in (p.get("sources") or {}).items():
            for ref in refs:
                stmt = ("insert into public.product_sources (product_id, source_id, claim)\n"
                        f"select p.id, s.id, {q(claim)} from public.products p, public.sources s\n"
                        f"where {find_p} and {_source_match(by_key[ref])}\n"
                        "on conflict do nothing;")
                if snap.has_product_sources:
                    out.append(stmt)
                else:
                    deferred_product_sources.append(stmt)

    out += ["", "-- Sources for ingredient claims"]
    for ing in data.get("ingredients", []):
        for claim, refs in (ing.get("sources") or {}).items():
            for ref in refs:
                out.append(
                    "insert into public.ingredient_sources (ingredient_id, source_id, claim)\n"
                    f"select i.id, s.id, {q(claim)} from public.ingredients i, public.sources s\n"
                    f"where i.name = {q(ing['name'])} and {_source_match(by_key[ref])}\n"
                    "on conflict do nothing;")

    out += ["", "-- Concerns (created_at has no default on this table, so it is set here)"]
    for c in data.get("concerns", []):
        find_c = f"i.name = {q(c['ingredient'])} and c.target_profile = {q(c['target_profile'])}"
        out.append(
            "insert into public.ingredient_concerns (ingredient_id, concern_title, concern_description, target_profile, severity, created_at)\n"
            f"select i.id, {q(c['concern_title'])}, {q(c['concern_description'])}, {q(c['target_profile'])}, {q(c['severity'])}, now()\n"
            f"from public.ingredients i where i.name = {q(c['ingredient'])}\n"
            f"  and not exists (select 1 from public.ingredient_concerns c where c.ingredient_id = i.id and c.target_profile = {q(c['target_profile'])});")
        for ref in c.get("sources", []):
            out.append(
                "insert into public.concern_sources (concern_id, source_id)\n"
                "select c.id, s.id from public.ingredient_concerns c join public.ingredients i on i.id = c.ingredient_id, public.sources s\n"
                f"where {find_c} and {_source_match(by_key[ref])}\n"
                "on conflict do nothing;")

    out += ["", "-- Ingredient-pair conflict rules"]
    for r in data.get("conflict_rules", []):
        a, b = q(r["ingredient_a"]), q(r["ingredient_b"])
        out.append(
            "insert into public.conflict_rules (ingredient_a_id, ingredient_b_id, severity, warning_message)\n"
            f"select a.id, b.id, {q(r['severity'])}, {q(r['warning_message'])} from public.ingredients a, public.ingredients b\n"
            f"where a.name = {a} and b.name = {b}\n"
            "  and not exists (select 1 from public.conflict_rules r where (r.ingredient_a_id = a.id and r.ingredient_b_id = b.id)"
            " or (r.ingredient_a_id = b.id and r.ingredient_b_id = a.id));")
        for ref in r.get("sources", []):
            out.append(
                "insert into public.conflict_rule_sources (rule_id, source_id)\n"
                "select r.id, s.id from public.conflict_rules r\n"
                "  join public.ingredients a on a.id = r.ingredient_a_id join public.ingredients b on b.id = r.ingredient_b_id, public.sources s\n"
                f"where a.name = {a} and b.name = {b} and {_source_match(by_key[ref])}\n"
                "on conflict do nothing;")

    out += ["", "-- Functional-group conflict rules"]
    for r in data.get("category_rules", []):
        a, b = q(r["group_a"]), q(r["group_b"])
        out.append(
            "insert into public.category_conflict_rules (group_a, group_b, severity, warning_message)\n"
            f"select {a}, {b}, {q(r['severity'])}, {q(r['warning_message'])}\n"
            "where not exists (select 1 from public.category_conflict_rules r where (r.group_a = "
            f"{a} and r.group_b = {b}) or (r.group_a = {b} and r.group_b = {a}));")
        for ref in r.get("sources", []):
            out.append(
                "insert into public.category_rule_sources (rule_id, source_id)\n"
                "select r.id, s.id from public.category_conflict_rules r, public.sources s\n"
                f"where r.group_a = {a} and r.group_b = {b} and {_source_match(by_key[ref])}\n"
                "on conflict do nothing;")

    out += ["", "commit;"]
    if deferred_product_sources:
        out += ["", "-- Product sources, NOT run: migration 0010 (product_sources) had not been applied when",
                "-- this file was generated. Run 0010, then regenerate this file to include them.",
                *["-- " + line for stmt in deferred_product_sources for line in stmt.splitlines()]]
    images = [p for p in data.get("products", []) if p.get("image_source_url")]
    if images:
        out += ["", "-- Images: SQL cannot fetch them. After this file has run, copy them into storage with",
                "--   python -m app.db.catalog_expansion images <file.json>",
                *[f"--   {p['brand']} | {p['name']}: {p['image_source_url']}" for p in images]]
    return "\n".join(out) + "\n"


# --- Live database ---------------------------------------------------------------

def read_snapshot() -> Snapshot:
    """Read what already exists. Read-only."""
    from app.db.connection import supabase
    snap = Snapshot()
    for p in supabase.table("products").select("brand, name, slug").execute().data:
        snap.products.add((p["brand"], p["name"]))
        if p.get("slug"):
            snap.slugs.add(p["slug"])
    ing_rows = supabase.table("ingredients").select("id, name, bad_for, functional_group").limit(10000).execute().data
    names_by_id = {}
    for r in ing_rows:
        snap.ingredients[r["name"]] = snap.ingredients.get(r["name"], 0) + 1
        snap.ingredient_bad_for.setdefault(r["name"], r.get("bad_for"))
        if r.get("functional_group"):
            snap.functional_groups.add(r["functional_group"])
        names_by_id[r["id"]] = r["name"]
    snap.source_urls = {s["url"] for s in supabase.table("sources").select("url").execute().data if s.get("url")}
    for c in supabase.table("ingredient_concerns").select("ingredient_id, target_profile").execute().data:
        snap.concerns.add((names_by_id.get(c["ingredient_id"]), c["target_profile"]))
    for r in supabase.table("conflict_rules").select("ingredient_a_id, ingredient_b_id").execute().data:
        snap.rule_pairs.add(frozenset((names_by_id.get(r["ingredient_a_id"]), names_by_id.get(r["ingredient_b_id"]))))
    for r in supabase.table("category_conflict_rules").select("group_a, group_b").execute().data:
        snap.category_pairs.add(frozenset((r["group_a"], r["group_b"])))
    try:
        supabase.table("product_sources").select("claim").limit(1).execute()
        snap.has_product_sources = True
    except Exception:
        snap.has_product_sources = False
    return snap


def store_images(data: dict) -> None:
    """Copy each new product's Open Beauty Facts image into storage and set
    image_url where it is still empty. Writes; run only after the SQL."""
    import asyncio
    import httpx
    from app.db.connection import supabase
    from app.core.services.image_service import fetch_and_store_product_image

    async def run():
        async with httpx.AsyncClient() as client:
            for p in data.get("products", []):
                if not p.get("image_source_url"):
                    continue
                row = supabase.table("products").select("id, image_url").eq("brand", p["brand"]).eq("name", p["name"]).limit(1).execute().data
                if not row:
                    print(f"  skipped {p['brand']} | {p['name']}: not in the database yet (run the SQL first)")
                    continue
                if row[0].get("image_url"):
                    print(f"  kept    {p['brand']} | {p['name']}: already has an image")
                    continue
                url = await fetch_and_store_product_image(client, p["image_source_url"], create_slug(p["brand"], p["name"]))
                if url:
                    supabase.table("products").update({"image_url": url}).eq("id", row[0]["id"]).execute()
                    print(f"  stored  {p['brand']} | {p['name']}")
                else:
                    print(f"  failed  {p['brand']} | {p['name']}: image unavailable")
    asyncio.run(run())


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=["check", "sql", "images"])
    parser.add_argument("file")
    parser.add_argument("--out", default="app/db/migrations/0011_catalog_expansion.sql")
    args = parser.parse_args(argv)

    with open(args.file, encoding="utf-8") as f:
        data = json.load(f)
    if args.command == "images":
        store_images(data)
        return 0

    report = validate(data, read_snapshot())
    for w in report.warnings:
        print("WARNING  " + w)
    for e in report.errors:
        print("ERROR    " + e)
    if report.errors:
        print(f"\n{len(report.errors)} error(s): nothing generated. Fix them and run again.")
        return 1
    print(f"\nValid: {len(data.get('products', []))} products, {len(data.get('ingredients', []))} new ingredients, "
          f"{len(data.get('concerns', []))} concerns, {len(data.get('conflict_rules', [])) + len(data.get('category_rules', []))} rules, "
          f"{len(data.get('sources', []))} sources. {len(report.warnings)} warning(s).")
    if args.command == "sql":
        sql = generate_sql(data, read_snapshot(), source_file=args.file)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(sql)
        print(f"SQL written to {args.out}. Review it, then run it in the Supabase SQL editor.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
