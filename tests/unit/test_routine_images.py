"""Tests that a proposed routine carries each product's photo (UC-15).

The chatbot's routine card shows a thumbnail per step, and it has only the
proposal to go on: nothing is saved until the user applies it, so there is no
routine_steps -> products join to read image_url from. generate_routine must
therefore copy image_url from the catalog onto every step, through both the
first LLM pass and the validation pass that rebuilds the steps.

Supabase is replaced with the in-memory fake (tests/conftest.py). The LLM is a
stub that returns fixed JSON, and the embedding, fact lookup and conflict engine
are stubbed out, so these tests exercise only how steps are assembled.

Docstrings state the expected output, and are lifted verbatim into the
"Expected Unit Output" field of the generated Test Record.
"""

import json

import pytest

from app.core.services import routine_service

USER_ID = "user-1"
CLEANSER_IMG = "https://cdn.example.com/cleanser.jpg"

PRODUCTS = [
    {"id": "p-cleanser", "brand": "CeraVe", "name": "Foaming Cleanser",
     "category": "cleanser", "image_url": CLEANSER_IMG},
    {"id": "p-spf", "brand": "La Roche-Posay", "name": "Anthelios SPF 50",
     "category": "sunscreen", "image_url": None},
]

LLM_REPLY = json.dumps({"steps": [
    {"product_id": "p-cleanser", "step_order": 1, "time_of_day": "both",
     "frequency": "daily", "reason": "Gentle daily cleanse", "caution": ""},
    {"product_id": "p-spf", "step_order": 2, "time_of_day": "AM",
     "frequency": "daily", "reason": "Daily UV protection", "caution": ""},
]})


class StubLLM:
    """Answers every invoke() with the same reply, like a model that agrees
    with itself on the validation pass."""

    def __init__(self, reply):
        self.reply = reply
        self.calls = 0

    def invoke(self, _messages):
        self.calls += 1
        return type("Reply", (), {"content": self.reply})()


class StubEmbeddings:
    def embed_query(self, _text):
        return [0.0] * 768


@pytest.fixture
def routine_env(monkeypatch, patch_supabase):
    patch_supabase({"products": [dict(p) for p in PRODUCTS], "shelf_items": []},
                   "app.core.services.routine_service")
    monkeypatch.setattr(routine_service.chat_repo, "get_user_skin_context", lambda _uid: "OSPT")
    monkeypatch.setattr(routine_service.chat_repo, "get_matching_facts", lambda _vec: "No specific facts found.")
    monkeypatch.setattr(routine_service, "embeddings", StubEmbeddings())
    monkeypatch.setattr(routine_service, "_run_compatibility_checks", lambda _uid, _steps: [])
    llm = StubLLM(LLM_REPLY)
    monkeypatch.setattr(routine_service, "llm", llm)
    return llm


def test_catalog_entries_carry_the_product_image_url(routine_env):
    """Returns each catalog entry with the product's image_url, or None when the
    product has no photo."""
    catalog = routine_service._get_product_catalog(USER_ID)

    by_id = {p["product_id"]: p for p in catalog}
    assert by_id["p-cleanser"]["image_url"] == CLEANSER_IMG
    assert by_id["p-spf"]["image_url"] is None


def test_validated_routine_steps_keep_their_product_image_url(routine_env):
    """Returns a validated routine whose steps each carry their product's
    image_url from the catalog, not from the LLM's reply."""
    result = routine_service.generate_routine(USER_ID, "")

    by_id = {s["product_id"]: s for s in result["steps"]}
    assert routine_env.calls == 2
    assert result["validation"]["status"] == "validated"
    assert by_id["p-cleanser"]["image_url"] == CLEANSER_IMG
    assert by_id["p-spf"]["image_url"] is None


def test_unvalidated_routine_steps_keep_their_product_image_url(routine_env, monkeypatch):
    """Returns the first-pass routine with each step's image_url when the
    validation pass fails and the unvalidated steps are kept."""
    monkeypatch.setattr(routine_service, "_validate_routine", lambda *_args: None)

    result = routine_service.generate_routine(USER_ID, "")

    by_id = {s["product_id"]: s for s in result["steps"]}
    assert result["validation"]["status"] == "unvalidated"
    assert by_id["p-cleanser"]["image_url"] == CLEANSER_IMG
