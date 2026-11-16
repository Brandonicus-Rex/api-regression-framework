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


def test_bad_credentials_rejected(base_url):
    """The API must refuse requests with the wrong username and password."""
    with ApiClient(ClientConfig(base_url=base_url, username="wrong", password="wrong", retries=0)) as c:
        assert c.request("GET", "/api/v1/subscribers/anything").status_code == 401
