"""Unit tests for the `--record-json` test recorder (root conftest.py).

The root conftest is loaded from its file under a separate module name, so
calling its hooks here records into that copy's lists and never into the
record of the run that is executing these tests.
"""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT_CONFTEST = Path(__file__).resolve().parents[2] / "conftest.py"
_spec = importlib.util.spec_from_file_location("record_json_recorder", _ROOT_CONFTEST)
recorder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(recorder)

IPV6_NODEID = ("tests/unit/test_input_safety.py::"
               "test_public_url_rules_refuse_internal_and_disguised_links['http://[::1]/']")


@pytest.mark.parametrize("nodeid, expected", [
    ("tests/unit/test_a.py::test_plain", "test_plain"),
    ("tests/unit/test_a.py::test_cases[a-b]", "test_cases[a-b]"),
    ("tests/unit/test_a.py::TestGroup::test_method[x]", "test_method[x]"),
    (IPV6_NODEID, "test_public_url_rules_refuse_internal_and_disguised_links['http://[::1]/']"),
    ("tests/unit/test_a.py::TestGroup::test_method[http://[fe80::1]/]", "test_method[http://[fe80::1]/]"),
], ids=["plain", "parametrised", "class-method", "ipv6-parameter", "class-method-ipv6-parameter"])
def test_recorded_name_is_function_name_with_full_parameter_id(nodeid, expected):
    """The recorded test name is the function name followed by its full parameter id, even when the parameter id contains "::"."""
    name = recorder._test_name(nodeid)
    assert name == expected


def test_record_keeps_function_name_and_full_node_id_for_ipv6_parameter():
    """A recorded case whose parameter id contains "::" is stored under its own function name, with the full node id and file path kept unchanged."""
    report = SimpleNamespace(nodeid=IPV6_NODEID, when="call", outcome="passed", duration=0.01, longrepr=None)
    recorder._RESULTS.clear()
    recorder.pytest_runtest_logreport(report)
    record = recorder._RESULTS.pop()
    assert record["test"] == "test_public_url_rules_refuse_internal_and_disguised_links['http://[::1]/']"
    assert record["nodeid"] == IPV6_NODEID
    assert record["file"] == "tests/unit/test_input_safety.py"
