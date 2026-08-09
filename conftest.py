"""Root conftest — test-record capture.

Opt in with `pytest --record-json <path>`. Captures each test's docstring (the
documented expectation), outcome, duration, and failure message so
tools/generate_test_report.py can build the Test Record tables. Off by default,
so ordinary runs are unaffected.

This lives at the repo root rather than in tests/ because pytest only honours
`pytest_addoption` from the rootdir conftest; declared in tests/conftest.py the
option is silently rejected as an unrecognised argument.

Fixtures and the test environment live in tests/conftest.py.
"""

import inspect
import json
import platform
import textwrap
from datetime import datetime

import pytest

_META = {}
_RESULTS = []


def _strip_assert_message(line):
    """`assert x == y, f"..."` -> `assert x == y`.

    The trailing message is failure diagnostics, not part of what was checked,
    and it is often longer than the check itself. Scanning for a comma at
    bracket depth zero (and outside quotes) avoids cutting inside a tuple,
    call, or string that legitimately contains one.
    """
    depth = 0
    quote = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote:
            if char == "\\":
                index += 2
                continue
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "," and depth == 0:
            return line[:index].rstrip()
        index += 1
    return line


def _split_body(func):
    """Split a test body into (setup, checks).

    `setup` builds the inputs and fills 'Input / Test Data'; `checks` are the
    assertions and fill 'Actual Unit Output', since a passing assertion is the
    only thing pytest actually observes.

    Docstrings are tracked as a state machine rather than by testing whether a
    line starts with a quote. A multi-line docstring's *closing* line does not,
    so the naive check leaked the tail of the prose (e.g. `cannot be coerced to
    float.` plus the closing delimiter) into the input field of 40 of 61
    generated cases.
    """
    try:
        source = textwrap.dedent(inspect.getsource(func))
    except (OSError, TypeError):
        return "", []

    setup, checks = [], []
    started = False
    open_delim = None  # set while inside an unterminated docstring
    checking = False

    for line in source.splitlines():
        stripped = line.strip()

        if not started:
            # Skip the decorator/def preamble. Decorator arguments may span
            # several lines, none of which begin with "def ".
            if stripped.startswith("def "):
                started = True
            continue

        if open_delim is not None:
            if open_delim in stripped:
                open_delim = None
            continue

        if not stripped or stripped.startswith("#"):
            continue

        delim = next((d for d in ('"""', "'''") if stripped.startswith(d)), None)
        if delim is not None:
            # A one-line docstring opens and closes on the same line.
            if stripped.count(delim) < 2:
                open_delim = delim
            continue

        if stripped.startswith(("assert", "with pytest.raises")):
            checking = True

        if checking:
            checks.append(_strip_assert_message(stripped)
                          if stripped.startswith("assert") else stripped)
        else:
            setup.append(stripped)

    return " ".join(setup), checks


def _format_params(callspec):
    """Render a parametrised case as `name=value, name=value`."""
    parts = []
    for name, value in callspec.params.items():
        parts.append(f"{name}={value!r}")
    return ", ".join(parts)


def pytest_addoption(parser):
    parser.addoption(
        "--record-json", action="store", default=None, metavar="PATH",
        help="Write a machine-readable test record to PATH (for the Test Record doc).",
    )


def pytest_collection_modifyitems(items):
    for item in items:
        func = getattr(item, "function", None)
        doc = getattr(func, "__doc__", "") or ""
        callspec = getattr(item, "callspec", None)
        setup, checks = _split_body(func) if func else ("", [])
        _META[item.nodeid] = {
            "docstring": " ".join(doc.split()),
            "params": _format_params(callspec) if callspec else "",
            "input_source": setup,
            "assert_source": checks,
        }


def pytest_runtest_logreport(report):
    # "call" is the test body; a setup error still counts as a failed test.
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        meta = _META.get(report.nodeid, {})
        _RESULTS.append({
            "nodeid": report.nodeid,
            "file": report.nodeid.split("::")[0],
            "test": report.nodeid.split("::")[-1],
            "docstring": meta.get("docstring", ""),
            "params": meta.get("params", ""),
            "input_source": meta.get("input_source", ""),
            "assert_source": meta.get("assert_source", []),
            "outcome": report.outcome,
            "duration_s": round(report.duration, 4),
            "failure": str(report.longrepr) if report.outcome == "failed" else "",
        })


def pytest_sessionfinish(session, exitstatus):
    path = session.config.getoption("--record-json")
    if not path:
        return
    payload = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "pytest": pytest.__version__,
        "platform": platform.platform(),
        "exit_status": int(exitstatus),
        "totals": {
            "total": len(_RESULTS),
            "passed": sum(r["outcome"] == "passed" for r in _RESULTS),
            "failed": sum(r["outcome"] == "failed" for r in _RESULTS),
            "skipped": sum(r["outcome"] == "skipped" for r in _RESULTS),
            "duration_s": round(sum(r["duration_s"] for r in _RESULTS), 3),
        },
        "results": _RESULTS,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    print(f"\nTest record written to {path}")
