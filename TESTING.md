# Running the tests

234 automated tests. They run in under a second and **do not touch the database**,
so you can run them on a fresh clone without any Supabase credentials.

## Setup

Python 3.11.

```bash
pip install -r requirements.txt -r requirements-dev.txt
```

If you use conda:

```bash
conda create -n skinbuddies python=3.11 && conda activate skinbuddies && pip install -r requirements.txt -r requirements-dev.txt
```

## Run them

From the repository root:

```bash
python -m pytest
```

Expected output:

```
....................................................................... [ 30%]
....................................................................... [ 61%]
....................................................................... [ 92%]
..................                                                      [100%]
234 passed in 0.2s
```

Every dot is one passing check. A failure shows as `F` with the file, line, and
the values that did not match.

### Useful variations

```bash
python -m pytest -v
```

Names every test instead of printing dots — use this to see what is covered.

```bash
python -m pytest tests/unit/test_safety_flags.py
```

Runs one file.

```bash
python -m pytest -k "baumann"
```

Runs only tests whose name matches a substring.

```bash
python -m pytest -x
```

Stops at the first failure instead of running the whole suite.

## No database required — and why that matters

`tests/conftest.py` does two things before any application code is imported:

1. Sets fake environment variables (`SUPABASE_URL`, `SUPABASE_JWT_SECRET`, …), which take priority over anything in your `.env`.
2. Replaces the Supabase client with `FakeSupabase`, an in-memory stand-in that only ever returns rows the test itself declared.

So a test asserting "returns 2 shelf items" is true because the test put 2 items
there — not because the live database happened to hold 2 rows that day. That is
what makes the results reproducible, and it means **running the suite cannot read
or modify real user data**.

If you ever see a test hit the network, that is a bug in the test — report it.

## What is covered

| Area | File | Tests |
|---|---|---|
| Ingredient dictionary and heuristics | `tests/unit/test_ingredient_dictionary.py` | 104 |
| Auth boundaries on every route | `tests/api/test_auth_boundaries.py` | 29 |
| Baumann skin-type validation | `tests/unit/test_schemas.py` | 28 |
| Slug generation and helpers | `tests/unit/test_slug_and_helpers.py` | 20 |
| Product safety flags | `tests/unit/test_safety_flags.py` | 18 |
| Skin-match scoring | `tests/unit/test_baumann_scoring.py` | 14 |
| JWT creation and verification | `tests/unit/test_token.py` | 11 |
| Products API over HTTP | `tests/api/test_products_api.py` | 10 |

Scope is the **backend** only, and mainly Features #2 (skin quiz), #3 (shelf) and
#4 (search and compare). Features #5 (routine) and #7 (weekly analysis) have no
automated tests yet. The chatbot (Feature #6) is owned by another team member and
is not covered here. There is no frontend test suite in this repository.

## Reading a test

Every test's docstring states its expected result, so the test is readable
without running it:

```python
def test_search_omits_personalisation_for_anonymous_callers(client, patch_supabase):
    """Returns skin_match_score=None, empty match reasons and has_conflict=False
    when no user is authenticated, since there is no skin type to score against."""
    patch_supabase({"products": [CLEANSER]}, "app.api.products")

    result = client.get("/products/search").json()[0]
    assert result["skin_match_score"] is None
```

Some tests are prefixed `REGRESSION:` in their docstring. Those guard a bug that
was found and fixed — for example, `Phenoxyethanol` (a mild preservative) used to
be flagged as an alcohol because `"ethanol"` is a substring of its name. If one of
those fails, an old bug has come back.

## Adding a test

Put it in `tests/unit/` for a pure function or `tests/api/` for anything going
through an HTTP route. Two conventions worth keeping:

- **Write the docstring as the expected output.** It is not decoration — a generator lifts it verbatim into the project's formal Test Record document.
- **Patch the module that *uses* the client, not `app.db.connection`.** Each module does `from app.db.connection import supabase` at import time, which binds its own reference, so patching the source has no effect. The `patch_supabase` fixture takes the consuming module's name for this reason.

## Troubleshooting

**`ModuleNotFoundError: No module named 'app'`** — run pytest from the repository
root, not from inside `tests/`. `pytest.ini` sets `pythonpath = .` relative to the
root.

**`unrecognized arguments: --record-json`** — that option is declared in the root
`conftest.py`. Make sure you are in the repository root and that file exists.

**A test fails right after `git pull`** — likely a real regression, not a broken
test. Run `python -m pytest -x -v` to see which assertion broke first.
