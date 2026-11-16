"""Integration tests: run every YAML scenario against the mock API.

Each scenario file becomes its own pytest case, so adding a new YAML file
under scenarios/ automatically adds a new test.
"""

from pathlib import Path

import pytest

from regression.client import ApiClient, ClientConfig
from regression.runner import load_scenario, run_scenario

SCENARIO_DIR = Path(__file__).resolve().parent.parent / "scenarios"
SCENARIOS = sorted(SCENARIO_DIR.glob("*.yaml"))


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
def test_scenario(client, path):
    """Every step in the scenario must pass."""
    result = run_scenario(client, load_scenario(path))
    # Collect every failure message so a failing test explains itself.
    failures = [f"{s.name}: {f}" for s in result.steps for f in s.failures]
    assert result.passed, "\n".join(failures)


@pytest.mark.parametrize("path", SCENARIOS, ids=[p.stem for p in SCENARIOS])
def test_scenario_starts_and_ends_with_cleanup(path):
    """Every regression scenario must clean its data first and last."""
    assert load_scenario(path).get("cleanup"), f"{path.name} has no cleanup steps"


def test_scenarios_can_run_back_to_back(client):
    """Two rounds against the same server, with no reset in between, must both pass.

    This is what a real test environment looks like: nothing wipes the
    database between runs, so each scenario has to clean up after itself.
    """
    for round_number in (1, 2):
        for path in SCENARIOS:
            result = run_scenario(client, load_scenario(path))
            failures = [f"{s.name}: {f}" for s in result.steps for f in s.failures]
            assert result.passed, f"round {round_number}, {path.name}:\n" + "\n".join(failures)


def test_cleanup_is_first_and_last_and_survives_a_failure(client):
    """Cleanup runs first, and runs last even when a test step fails."""
    phone = "5550100090"
    scenario = {
        "name": "Fails halfway",
        "variables": {"phone": phone},
        "cleanup": [{
            "name": "Deactivate ${phone}",
            "method": "POST",
            "path": "/api/v1/subscribers/deactivate",
            "body": {"phone_number": "${phone}"},
            "expect": {"status": 200, "json": {"status": "inactive"}},
        }],
        "steps": [
            {"name": "Create subscriber", "method": "POST", "path": "/api/v1/subscribers",
             "body": {"first_name": "Left", "last_name": "Open", "phone_number": "${phone}",
                      "plan": "prepaid"},
             "expect": {"status": 201}},
            {"name": "Deliberate failure", "method": "GET", "path": "/health",
             "expect": {"status": 500}},
            {"name": "Never reached", "method": "GET", "path": "/health"},
        ],
    }
    result = run_scenario(client, scenario)

    names = [s.name for s in result.steps]
    assert names[0] == f"Clean up (before): Deactivate {phone}"
    assert names[-1] == f"Clean up (after): Deactivate {phone}"
    assert "Never reached" not in names
    assert not result.passed and result.steps[-1].passed

    # The number is free again, so the account left open was deactivated.
    body = {"first_name": "Next", "last_name": "Run", "phone_number": phone, "plan": "prepaid"}
    assert client.request("POST", "/api/v1/subscribers", json=body).status_code == 201


def test_failed_opening_cleanup_skips_test_steps(client):
    """If the data can't be put in a known state, the test steps don't run."""
    scenario = {
        "cleanup": [{"name": "Broken cleanup", "method": "GET", "path": "/health",
                     "expect": {"status": 500}}],
        "steps": [{"name": "Should not run", "method": "GET", "path": "/health"}],
    }
    result = run_scenario(client, scenario)
    assert [s.name for s in result.steps] == ["Clean up (before): Broken cleanup",
                                              "Clean up (after): Broken cleanup"]
    assert not result.passed


def test_deactivate_is_idempotent(client):
    """Deactivating closes an open account once, then keeps succeeding."""
    phone = "5550100091"
    body = {"first_name": "A", "last_name": "B", "phone_number": phone, "plan": "prepaid"}
    assert client.request("POST", "/api/v1/subscribers", json=body).status_code == 201

    first = client.request("POST", "/api/v1/subscribers/deactivate", json={"phone_number": phone})
    again = client.request("POST", "/api/v1/subscribers/deactivate", json={"phone_number": phone})
    assert (first.status_code, first.json()["deactivated"]) == (200, 1)
    assert (again.status_code, again.json()["deactivated"]) == (200, 0)


def test_bad_credentials_rejected(base_url):
    """The API must refuse requests with the wrong username and password."""
    with ApiClient(ClientConfig(base_url=base_url, username="wrong", password="wrong", retries=0)) as c:
        assert c.request("GET", "/api/v1/subscribers/anything").status_code == 401
