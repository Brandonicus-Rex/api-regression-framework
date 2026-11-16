"""Unit tests for the runner's helper functions. No server needed."""

import pytest

from regression.runner import ScenarioError, check_expectations, get_path, load_scenario, substitute


# ---- substitute() --------------------------------------------------------

def test_substitute_keeps_type_for_whole_value():
    # "${n}" on its own should return the int 5, not the string "5".
    assert substitute("${n}", {"n": 5}) == 5


def test_substitute_inside_string_and_nested():
    data = {"path": "/items/${id}", "list": ["${a}", "x"]}
    assert substitute(data, {"id": "abc", "a": 1}) == {"path": "/items/abc", "list": [1, "x"]}


# ---- get_path() ----------------------------------------------------------

def test_get_path_dicts_and_lists():
    body = {"a": {"b": [10, 20]}}
    assert get_path(body, "a.b.1") == 20


# ---- check_expectations() ------------------------------------------------

def test_check_expectations_reports_mismatches():
    # Both the status and the field are wrong, so both should be reported.
    failures = check_expectations({"status": 200, "json": {"status": "active"}}, 404, {"status": "closed"})
    assert len(failures) == 2


def test_check_expectations_missing_field():
    assert check_expectations({"json": {"missing": 1}}, 200, {}) == ["missing JSON field 'missing'"]


def test_check_expectations_contains():
    assert check_expectations({"contains": {"features": "mms"}}, 200, {"features": ["mms"]}) == []


# ---- load_scenario() -----------------------------------------------------

def test_load_scenario_validates(tmp_path):
    # A step without "method" should be rejected when the file loads,
    # not halfway through a test run.
    bad = tmp_path / "bad.yaml"
    bad.write_text("steps:\n  - name: no method\n    path: /x\n")
    with pytest.raises(ScenarioError):
        load_scenario(bad)


def test_load_scenario_validates_cleanup_steps(tmp_path):
    # Cleanup steps are held to the same rules as test steps.
    bad = tmp_path / "bad_cleanup.yaml"
    bad.write_text(
        "cleanup:\n  - name: no method\n    path: /x\n"
        "steps:\n  - name: ok\n    method: GET\n    path: /x\n"
    )
    with pytest.raises(ScenarioError):
        load_scenario(bad)
