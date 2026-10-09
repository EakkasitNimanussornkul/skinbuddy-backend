"""Request handlers that call the (synchronous) Supabase client run in FastAPI's
threadpool, not on the event loop (audit 2026-10-09, findings 1 and 9).

An `async def` handler that calls the synchronous client freezes the whole server for
each database round trip: one client looping /products/compare made a route with no
database call take 1,286 ms instead of 3 ms. A plain `def` handler is run in a worker
thread by FastAPI. Where a handler must `await` (the LINE calls, reading an upload's
body) it stays `async def` and sends only its blocking calls to the threadpool.

The fake client's execute() sleeps here, so a handler that blocks the loop is caught by
a ticker task running on the same loop: it ticks every 10 ms and records its longest gap.
Docstrings state the expected output and are lifted verbatim into the Test Record.
"""

import asyncio
import inspect
import io
import time

import httpx
import pytest
from PIL import Image

from app.config.setting import settings
from app.core import consent
from app.core.services import account_deletion_service as service
from app.core.services import image_upload
from app.core.services.token import create_supabase_compatible_token
from tests.conftest import FakeQuery, FakeSupabase, FakeSupabaseWithRpc, RecordingQuery

DELAY = 0.6                 # seconds one fake database call takes; a handler that blocks the loop stalls it for about this long
MAX_GAP = 0.3               # half of DELAY: a healthy loop ticks every ~10 ms, a blocked one stalls for DELAY, so a loaded machine has ~0.3 s of slack either way
A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

ALL_MODULES = ("app.api.auth", "app.api.shelf", "app.api.quiz", "app.api.consent", "app.api.products",
               "app.api.submissions", "app.api.account_deletion",
               "app.core.services.compatibility_service", "app.core.services.ingredient_lookup",
               "app.core.services.token", "app.core.services.image_upload",
               "app.core.services.submission_service", "app.core.services.consent_gate")


class SlowQuery(RecordingQuery):
    delay = DELAY

    def execute(self):
        time.sleep(self.delay)
        return super().execute()


class SlowSupabase(FakeSupabaseWithRpc):
    """Every table read or write takes DELAY seconds, like a slow network."""
    delay = DELAY

    def table(self, name):
        query = SlowQuery(name, self.store, self.orders)
        query.delay = self.delay
        return query


@pytest.fixture
def slow_db(monkeypatch):
    import importlib

    def _make(store):
        fake = SlowSupabase(store)
        for path in ALL_MODULES:
            monkeypatch.setattr(importlib.import_module(path), "supabase", fake)
        return fake
    return _make


def _app():
    from app.main import app
    return app


async def _drive(calls, ticker_seconds=None):
    """Run each (method, path, kwargs) against the app at once on this loop, with a ticker
    beside them. Returns (responses, seconds each took, the ticker's longest gap, wall seconds)."""
    gaps, stop = [], asyncio.Event()

    async def tick():
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        async def one(method, path, kwargs):
            started = time.perf_counter()
            resp = await http.request(method, path, **kwargs)
            return resp, time.perf_counter() - started

        ticker = asyncio.create_task(tick())
        await asyncio.sleep(0.05)
        wall = time.perf_counter()
        done = await asyncio.gather(*(one(*c) for c in calls))
        wall = time.perf_counter() - wall
        stop.set()
        await ticker
    return [d[0] for d in done], [d[1] for d in done], max(gaps), wall


def drive(calls):
    return asyncio.run(_drive(calls))


USER = {"id": "user-1", "skin_type": "DSPT", "role": "user", "line_id": "line-user-1"}
ADMIN = {"id": "admin-1", "skin_type": "DSPT", "role": "admin", "line_id": "line-admin"}
PRODUCT = {"id": A_ID, "brand": "Brand", "name": "Serum", "category": "Serum", "slug": None,
           "product_sources": [], "product_ingredients": []}


def _png():
    out = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 120, 40)).save(out, "PNG")
    return out.getvalue()


# --- the table of routes ------------------------------------------------------------
#
# (label, method, path, request kwargs, expected status). Each is a route that reads or
# writes the database and is now a plain `def`.

ROUTES = [
    ("GET /auth/me", "GET", "/auth/me", {}, 200),
    ("PATCH /auth/me", "PATCH", "/auth/me", {"json": {"skin_type": "OSPT"}}, 200),
    ("GET /shelf/", "GET", "/shelf/", {}, 200),
    ("POST /shelf/add", "POST", "/shelf/add", {"json": {"product_id": A_ID, "usage_state": "active"}}, 200),
    ("DELETE /shelf/{id}", "DELETE", "/shelf/s-1", {}, 200),
    ("PATCH /shelf/{id}/status", "PATCH", "/shelf/s-1/status", {"json": {"usage_state": "active"}}, 200),
    ("POST /quiz/save", "POST", "/quiz/save", {"json": {"skinType": "DSPT", "scores": {"a": 1.0}}}, 200),
    ("POST /consent/health", "POST", "/consent/health",
     {"json": {"health_version": consent.HEALTH_CONSENT_VERSION}}, 200),
    ("POST /consent/terms", "POST", "/consent/terms",
     {"json": {"terms_version": consent.TERMS_VERSION, "age_confirmed": True}}, 200),
    ("DELETE /consent/health", "DELETE", "/consent/health", {}, 200),
    ("POST /ingredients/match", "POST", "/ingredients/match", {"json": {"names": ["Water"]}}, 200),
    ("GET /products/search", "GET", "/products/search", {}, 200),
    ("GET /products/{id}", "GET", f"/products/{A_ID}", {}, 200),
    ("GET /products/slug/{slug}", "GET", f"/products/slug/{A_ID}", {}, 200),
    ("GET /meta/functional-groups", "GET", "/meta/functional-groups", {}, 200),
    ("GET /ingredients/search", "GET", "/ingredients/search", {"params": {"q": "water"}}, 200),
    ("GET /submissions/mine", "GET", "/submissions/mine", {}, 200),
    ("GET /submissions/admin", "GET", "/submissions/admin", {}, 200),
]


@pytest.mark.parametrize("label, method, path, kwargs, status", ROUTES, ids=[r[0] for r in ROUTES])
def test_a_slow_database_call_in_a_converted_route_does_not_freeze_the_event_loop(
        slow_db, as_user, label, method, path, kwargs, status):
    """Returns the route's normal answer while the event loop keeps ticking: the longest gap
    between ticks of a task on the same loop stays under 0.3 s although the route's database
    calls take 0.6 s each, so one slow route no longer stops every other request."""
    as_user("admin-1")        # an admin, so the admin routes in the table are allowed too
    slow_db({"users": [dict(USER), dict(ADMIN)], "products": [dict(PRODUCT)], "shelf_items": [{"id": "s-1", "user_id": "admin-1", "usage_state": "active"}],
             "quiz_results": [], "ingredients": [{"id": "i-1", "name": "Water", "functional_group": "Solvent"}],
             "product_submissions": [], "conflict_rules": [], "category_conflict_rules": []})
    [resp], [took], gap, _ = drive([(method, path, kwargs)])
    assert resp.status_code == status, resp.text
    assert took >= DELAY, "the route never reached the database, so this proved nothing"
    assert gap < MAX_GAP, f"{label} held the event loop for {gap:.2f} s"


def test_eight_slow_database_requests_overlap_instead_of_queueing(slow_db, as_user):
    """Returns HTTP 200 for eight simultaneous GET /auth/me whose database call takes 0.6 s
    each, all finished in under 2.4 s together (one after another would take 4.8 s): the
    requests wait on the database at the same time, in separate threads."""
    as_user("user-1")
    slow_db({"users": [dict(USER)]})
    responses, _, _, wall = drive([("GET", "/auth/me", {})] * 8)
    assert [r.status_code for r in responses] == [200] * 8
    assert wall < 4 * DELAY, f"8 requests took {wall:.2f} s; they ran one at a time"


def test_a_route_with_no_database_call_answers_at_once_while_a_slow_one_waits(slow_db, as_user):
    """Returns GET /meta/categories in under 0.3 s while a GET /auth/me is waiting 0.6 s on
    the database: the situation the audit measured at 1,286 ms instead of 3 ms."""
    as_user("user-1")
    slow_db({"users": [dict(USER)]})
    responses, took, _, _ = drive([("GET", "/auth/me", {}), ("GET", "/meta/categories", {})])
    assert [r.status_code for r in responses] == [200, 200]
    assert took[1] < DELAY / 2, f"/meta/categories took {took[1]:.3f} s behind a slow database call"


# --- the routes that stay async def ---------------------------------------------------

def test_a_slow_storage_upload_does_not_freeze_the_event_loop(slow_db, as_user, monkeypatch):
    """Returns HTTP 200 with the stored image_path for POST /submissions/images and for
    POST /products/{id}/image while the (synchronous) storage upload takes 0.6 s, and the
    event loop keeps ticking: the upload runs in the threadpool."""
    as_user("admin-1")
    slow_db({"users": [dict(ADMIN)], "products": [{"id": A_ID}]})

    def slow_store(data, ext, content_type, folder):
        time.sleep(DELAY)
        return {"image_path": f"{folder}/x.{ext}", "public_url": "u"}

    monkeypatch.setattr(image_upload, "store_image", slow_store)
    png = ("photo.png", _png(), "image/png")
    # POST /products/{id}/image also reads the product (0.6 s), so it is the slower one.
    responses, took, gap, _ = drive([("POST", "/submissions/images", {"files": {"file": png}}),
                                     ("POST", f"/products/{A_ID}/image", {"files": {"file": png}})])
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert took[0] >= DELAY and took[1] >= 2 * DELAY
    assert gap < MAX_GAP, f"an upload held the event loop for {gap:.2f} s"


def test_account_deletion_runs_its_database_calls_in_the_threadpool(monkeypatch, as_user):
    """Returns HTTP 200 {"deleted": true, "line_deauthorized": true} for POST /auth/me/delete
    while each of its three synchronous steps (reading the user, the delete_user_account call,
    the photo cleanup) takes 0.6 s, and the event loop keeps ticking."""
    from app.api import account_deletion
    as_user("user-1")
    monkeypatch.setattr(settings, "LINE_DELETE_REDIRECT_URI", "http://localhost:5173/cb")

    def slowly(result):
        def run(*_args):
            time.sleep(DELAY)
            return result
        return run

    async def ok(*_args):
        return "line-user-1"

    async def deauth(*_args):
        return True

    monkeypatch.setattr(account_deletion, "_load_user", slowly(dict(USER)))
    monkeypatch.setattr(account_deletion, "_delete_in_database", slowly({"image_paths": []}))
    monkeypatch.setattr(service, "delete_unused_photos", slowly(0))
    monkeypatch.setattr(service, "exchange_code", ok)
    monkeypatch.setattr(service, "fetch_line_user_id", ok)
    monkeypatch.setattr(service, "deauthorize", deauth)
    [resp], [took], gap, _ = drive([("POST", "/auth/me/delete", {"json": {"code": "c"}})])
    assert resp.status_code == 200 and resp.json() == {"deleted": True, "line_deauthorized": True}
    assert took >= 3 * DELAY
    assert gap < MAX_GAP, f"account deletion held the event loop for {gap:.2f} s"


def test_the_supabase_sub_clients_are_built_at_import_not_on_first_use():
    """The shared Supabase client already holds its PostgREST and storage sub-clients once
    app.db.connection is imported, so two threads can never race to create them."""
    from app.db import connection
    assert connection.supabase._postgrest is not None
    assert connection.supabase._storage is not None


# --- which handlers are coroutines ----------------------------------------------------

# Every route of ours that must stay `async def`, and why. Any other route in our modules
# has to be a plain `def`, so a new handler written as `async def` calling the synchronous
# client is caught here.
STAYS_ASYNC = {
    ("POST", "/auth/line"): "awaits LINE; deliberately untouched in this change",
    ("POST", "/auth/me/delete"): "awaits the LINE calls; its database calls use run_in_threadpool",
    ("GET", "/meta/categories"): "constant, no database call",
    ("GET", "/meta/concern-tags"): "constant, no database call",
    ("GET", "/meta/policy-versions"): "constant, no database call",
    ("POST", "/submissions/images"): "awaits the upload body; the storage upload uses run_in_threadpool",
    ("POST", "/products/{product_id}/image"): "awaits the upload body; its database and storage calls use run_in_threadpool",
    ("POST", "/submissions/admin/cleanup-images"): "already hands its work to run_in_threadpool",
}
# Teammates' routers: not ours to change here.
NOT_OURS = ("app.api.routine", "app.api.chat", "app.api.analysis", "app.api.notifications")


def _our_routes():
    from fastapi.routing import APIRoute
    for route in _app().routes:
        if isinstance(route, APIRoute) and route.endpoint.__module__ not in NOT_OURS:
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                yield method, route.path, route.endpoint


def test_every_database_route_of_ours_is_a_plain_function_except_the_listed_ones():
    """Every route in our modules is a plain function (run in FastAPI's threadpool) except
    the eight listed as staying async because they await something, and each of those
    eight is still a coroutine function; no route is missing from either side."""
    routes = list(_our_routes())
    assert len(routes) > 30, "the route walk found too few routes"
    wrong = [f"{m} {p}" for m, p, fn in routes
             if inspect.iscoroutinefunction(fn) != ((m, p) in STAYS_ASYNC)]
    assert wrong == []
    assert set(STAYS_ASYNC) <= {(m, p) for m, p, _ in routes}


def test_the_resolve_helper_is_a_plain_function():
    """resolve_product_record is a plain function: the handlers that call it are plain
    functions too, so it cannot be awaited."""
    from app.api.products import resolve_product_record
    assert not inspect.iscoroutinefunction(resolve_product_record)


# --- no cross-talk between users running at once ---------------------------------------

def _bearer(user_id):
    return {"Authorization": f"Bearer {create_supabase_compatible_token(user_id)}"}


def _tree_item(name, ingredients):
    return {"id": f"p-{name}", "brand": "B", "name": name, "category": "Serum", "slug": None,
            "description": None, "price_thb": 500, "price_usd": 15, "image_url": None,
            "product_sources": [], "product_ingredients": [{"ingredients": i} for i in ingredients]}


GLYCERIN = {"id": "ing-gly", "name": "Glycerin", "functional_group": "Humectant", "good_for": "Dry Skin"}
ALCOHOL = {"id": "ing-alc", "name": "Alcohol Denat.", "functional_group": "Solvent", "bad_for": "Sensitive Skin (S)",
           "ingredient_concerns": [{"target_profile": "Sensitive Skin (S)", "severity": "High",
                                    "concern_title": "Strips oil", "concern_sources": []}]}
RETINOL = {"id": "ing-ret", "name": "Retinol", "functional_group": "Retinoid"}
SALICYLIC = {"id": "ing-sa", "name": "Salicylic Acid", "functional_group": "Beta Hydroxy Acid (BHA)"}
OWNERS = {  # four users with four different skin types
    "u-dry": "DSPT", "u-oily": "ORNW", "u-sens": "OSNT", "u-res": "DRPW",
}


def _world_store():
    return {
        "users": [{"id": uid, "skin_type": st} for uid, st in OWNERS.items()],
        "products": [
            {**_tree_item("Dry Serum", [GLYCERIN, ALCOHOL, RETINOL]), "id": A_ID},
            {**_tree_item("BHA Liquid", [SALICYLIC]), "id": B_ID},
        ],
        "conflict_rules": [{"ingredient_a_id": "ing-ret", "ingredient_b_id": "ing-sa", "severity": "high",
                            "warning_message": "Alternate evenings.", "conflict_rule_sources": []}],
        "category_conflict_rules": [{"group_a": "Retinoid", "group_b": "Beta Hydroxy Acid (BHA)",
                                     "severity": "high", "warning_message": "Raises irritation.",
                                     "category_rule_sources": []}],
    }


def test_many_signed_in_requests_at_once_get_the_same_bodies_as_one_at_a_time(slow_db):
    """Returns, for 4 users with different skin types each sending GET /products/search and
    GET /products/compare 6 times at once, a body identical to the one the same request got
    when sent alone; the 4 users' bodies differ from one another (skin_match_score), so a
    request that picked up another user's skin type, or a half-built shared row, shows."""
    slow_db(_world_store()).delay = 0.02      # many calls here; the point is the overlap, not the wait
    compare = {"params": {"product_a_id": A_ID, "product_b_id": B_ID}}

    def calls(uid):
        return [("GET", "/products/search", {"headers": _bearer(uid)}),
                ("GET", "/products/compare", {"headers": _bearer(uid), **compare})]

    alone = {}
    for uid in OWNERS:
        responses, *_ = drive(calls(uid))
        assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
        alone[uid] = [r.json() for r in responses]
    scores = {uid: alone[uid][0][0]["skin_match_score"] for uid in OWNERS}
    assert len(set(scores.values())) > 1, f"the users all score alike, so cross-talk could not show: {scores}"

    order = [uid for _ in range(6) for uid in OWNERS]
    responses, *_ = drive([c for uid in order for c in calls(uid)])
    assert all(r.status_code == 200 for r in responses)
    for i, uid in enumerate(order):
        assert responses[2 * i].json() == alone[uid][0], f"search for {uid} differs when run with others"
        assert responses[2 * i + 1].json() == alone[uid][1], f"compare for {uid} differs when run with others"
