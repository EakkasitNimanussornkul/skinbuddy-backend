"""Shared pytest fixtures and test-environment setup.

IMPORTANT: the env vars below are set *before* anything under `app.` is
imported. `app/config/setting.py` instantiates `Settings()` at import time and
`app/db/connection.py` builds the Supabase client at import time, so by the time
a test module runs, both are already frozen. Environment variables take priority
over the `.env` file in pydantic-settings, so forcing dummy values here
guarantees the test suite can never reach the real Supabase project even if a
test forgets to patch something.
"""

import os
import uuid

os.environ.update({
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "SUPABASE_URL": "https://test-project.supabase.co",
    "SUPABASE_ANON_KEY": "test-anon-key",
    "SUPABASE_SERVICE_ROLE_KEY": "test-service-role-key",
    "SUPABASE_JWT_SECRET": "test-jwt-secret-for-unit-tests-only",
    "LINE_CHANNEL_ID": "test-channel-id",
    "LINE_CHANNEL_SECRET": "test-channel-secret",
    "LINE_REDIRECT_URI": "http://localhost:5173/callback",
    "FRONTEND_URL": "http://localhost:5173",
    "GEMINI_API_KEY": "test-gemini-key",
})

import pytest  # noqa: E402
from postgrest.exceptions import APIError  # noqa: E402


# --- Fake Supabase client ----------------------------------------------------
#
# The real client is a fluent builder: table(...).select(...).eq(...).execute().
# The fake below accepts any chained call and returns canned rows keyed by table
# name, so a test can describe the DB as a plain dict.


class FakeResponse:
    def __init__(self, data):
        self.data = data


# Postgres supplies these on insert. Fixed rather than "now" so a test that does
# assert on them is deterministic.
FAKE_TIMESTAMP = "2026-01-01T00:00:00+00:00"


class FakeQuery:
    def __init__(self, table_name, store):
        self._table_name = table_name
        self._store = store
        self._single = False
        self._filters = []   # (column, value, negated)
        self._limit = None
        # Write intent, resolved in execute(). These MUST be initialised here:
        # __getattr__ answers unknown names with a chain function, so a missing
        # attribute would read as truthy and silently misroute the dispatch
        # below. __getattr__ now also raises for private names to make that
        # failure loud rather than mysterious.
        self._pending_insert = None
        self._pending_update = None
        self._pending_delete = False

    # Filters the tests actually depend on are implemented for real; everything
    # else (select/or_/gte/lte/order/...) is a no-op that continues the chain.
    #
    # select() in particular stays a no-op on purpose: the handlers pass
    # PostgREST join strings like "*, products(*, product_ingredients(...))".
    # The fake cannot resolve joins, so tests seed the already-joined shape.
    def eq(self, column, value):
        self._filters.append((column, value, False))
        return self

    def neq(self, column, value):
        self._filters.append((column, value, True))
        return self

    def limit(self, n):
        self._limit = n
        return self

    def single(self):
        self._single = True
        return self

    def insert(self, payload):
        # Real supabase-py takes a single row or a list of rows.
        self._pending_insert = payload if isinstance(payload, list) else [payload]
        return self

    def update(self, payload):
        self._pending_update = payload
        return self

    def delete(self):
        self._pending_delete = True
        return self

    def __getattr__(self, name):
        if name.startswith("_"):
            # Never answer a private name with the catch-all — see __init__.
            raise AttributeError(name)

        def _chain(*_args, **_kwargs):
            return self
        return _chain

    def _matching(self, rows):
        for column, value, negated in self._filters:
            rows = [r for r in rows if (r.get(column) != value) == negated]
        return rows

    def execute(self):
        if self._pending_insert is not None:
            rows = self._store.setdefault(self._table_name, [])
            inserted = []
            for payload in self._pending_insert:
                new_row = dict(payload)
                # add_to_shelf returns this row straight to the caller, which
                # needs the id to patch or delete the item later.
                new_row.setdefault("id", str(uuid.uuid4()))
                new_row.setdefault("created_at", FAKE_TIMESTAMP)
                rows.append(new_row)
                inserted.append(new_row)
            return FakeResponse(inserted)

        rows = self._store.get(self._table_name, [])
        matched = self._matching(rows)

        # Matched rows are the stored dicts themselves, so updating them in
        # place is what makes the write observable via fake.store.
        if self._pending_update is not None:
            for row in matched:
                row.update(self._pending_update)
            return FakeResponse(matched)

        if self._pending_delete:
            for row in matched:
                rows.remove(row)
            return FakeResponse(matched)

        if self._limit is not None:
            matched = matched[:self._limit]
        if self._single:
            # The real .single() RAISES APIError PGRST116 on zero rows rather
            # than returning empty data, so the fake does too.
            #
            # It used to return None here and carry a note saying tests that
            # cared about the zero-row path had to exercise the real client.
            # That is not something a unit suite can do, so in practice nothing
            # checked it, and the leniency hid live defects behind green tests:
            # BE-DEF-07 was one, and resolve_product_record was still returning
            # 400 instead of 404 for an unknown product id while a test asserted
            # the 404 and passed.
            #
            # A handler that must tolerate a missing row should not use
            # .single() at all - use .limit(1) and read res.data[0] if present,
            # which needs no exception handling and behaves the same here as in
            # production.
            if not matched:
                raise APIError({
                    "message": "Cannot coerce the result to a single JSON object",
                    "code": "PGRST116",
                    "hint": None,
                    "details": "The result contains 0 rows",
                })
            return FakeResponse(matched[0])
        return FakeResponse(matched)


class FakeSupabase:
    def __init__(self, store=None):
        self.store = store or {}

    def table(self, name):
        return FakeQuery(name, self.store)


@pytest.fixture
def fake_supabase():
    """Returns a factory: fake_supabase({"products": [...], "users": [...]})."""
    return FakeSupabase


@pytest.fixture
def patch_supabase(monkeypatch):
    """Patch the `supabase` name inside specific modules.

    Every module does `from app.db.connection import supabase`, which binds the
    client into that module's own namespace. Patching `app.db.connection.supabase`
    would therefore have no effect — the already-imported modules keep their own
    reference. Patch each consuming module by name instead.

        patch_supabase({"products": rows}, "app.api.products")
    """
    def _patch(store, *module_paths):
        import importlib

        fake = FakeSupabase(store)
        for path in module_paths:
            module = importlib.import_module(path)
            monkeypatch.setattr(module, "supabase", fake)
        return fake

    return _patch


@pytest.fixture
def as_user():
    """Authenticate every subsequent request as the given user id.

        as_user("user-1")

    Overrides the `get_current_user_id` dependency so the route runs its real
    body instead of rejecting the request at the auth boundary (which
    tests/api/test_auth_boundaries.py already covers). The override is removed
    on teardown, so it cannot leak into another test.
    """
    from app.main import app
    from app.core.services.token import get_current_user_id

    def _as(user_id):
        app.dependency_overrides[get_current_user_id] = lambda: user_id
        return user_id

    yield _as
    app.dependency_overrides.pop(get_current_user_id, None)


@pytest.fixture
def client():
    """FastAPI TestClient. Imported lazily so pure unit tests never pay the cost
    of importing the whole app (which pulls in the LangChain/Gemini chat stack)."""
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
