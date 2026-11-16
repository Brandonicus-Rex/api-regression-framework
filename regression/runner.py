"""Data-driven scenario runner.

A scenario is a YAML file with an ordered list of steps. Each step sends one
HTTP request and asserts on the response. Values from earlier responses can
be saved into variables and reused in later steps with ``${name}`` syntax.

Example step::

    - name: Create subscriber
      method: POST
      path: /api/v1/subscribers
      body: {first_name: Test, ...}
      expect:
        status: 201
        json:
          status: active
      save:
        subscriber_id: id

Run from the command line::

    python -m regression.runner scenarios/ -v
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from regression.client import ApiClient, ClientConfig

log = logging.getLogger(__name__)

# Matches ${variable_name}. Names follow Python identifier rules.
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ScenarioError(Exception):
    """Raised when a scenario file is missing required structure."""


# ---- Result types --------------------------------------------------------

@dataclass
class StepResult:
    """Outcome of one step.

    Attributes:
        name: The step's name from the YAML file.
        passed: True if every expectation was met.
        status_code: HTTP status received, or None if the request never completed.
        duration_ms: Round-trip time in milliseconds.
        failures: Human-readable reasons the step failed (empty when passed).
    """

    name: str
    passed: bool
    status_code: int | None
    duration_ms: float
    failures: list[str] = field(default_factory=list)


@dataclass
class ScenarioResult:
    """Outcome of a whole scenario: its name plus each step's result."""

    name: str
    steps: list[StepResult]

    @property
    def passed(self) -> bool:
        """True only if every executed step passed."""
        return all(s.passed for s in self.steps)


# ---- Variables and JSON helpers ------------------------------------------

def substitute(value: Any, variables: dict[str, Any]) -> Any:
    """Recursively replace ``${var}`` placeholders in strings, lists and dicts.

    If a string is *only* a placeholder (``"${count}"``), the variable's
    original type is kept, so numbers stay numbers and lists stay lists.
    Placeholders inside longer strings are converted to text.

    Raises:
        KeyError: If a placeholder names a variable that hasn't been set.
    """
    if isinstance(value, str):
        full = _VAR.fullmatch(value)
        if full:
            return variables[full.group(1)]
        return _VAR.sub(lambda m: str(variables[m.group(1)]), value)
    if isinstance(value, list):
        return [substitute(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, variables) for k, v in value.items()}
    # Numbers, booleans and None pass through unchanged.
    return value


def get_path(data: Any, dotted: str) -> Any:
    """Read a nested value from parsed JSON using a dotted path.

    Examples: ``"status"``, ``"account.plan"``, ``"features.0"`` (list index).

    Raises:
        KeyError, IndexError, ValueError: If the path doesn't exist.
    """
    current = data
    for part in dotted.split("."):
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            # Tried to step into a string/number/None.
            raise KeyError(dotted)
    return current


# ---- Assertions ----------------------------------------------------------

def check_expectations(expect: dict, status_code: int, body: Any) -> list[str]:
    """Compare a response against a step's ``expect`` block.

    Supported checks:
        status:   exact HTTP status code
        json:     {dotted.path: expected_value} for exact equality
        contains: {dotted.path: needle} for membership in a list or string

    Returns:
        A list of failure messages. An empty list means the step passed.
        All checks run, so one call reports every problem at once.
    """
    failures: list[str] = []

    expected_status = expect.get("status")
    if expected_status is not None and status_code != expected_status:
        failures.append(f"expected status {expected_status}, got {status_code}")

    for key, expected in (expect.get("json") or {}).items():
        try:
            actual = get_path(body, key)
        except (KeyError, IndexError, ValueError, TypeError):
            failures.append(f"missing JSON field '{key}'")
            continue
        if actual != expected:
            failures.append(f"field '{key}': expected {expected!r}, got {actual!r}")

    for key in expect.get("contains", {}) or {}:
        needle = expect["contains"][key]
        try:
            haystack = get_path(body, key)
        except (KeyError, IndexError, ValueError, TypeError):
            failures.append(f"missing JSON field '{key}'")
            continue
        if needle not in haystack:
            failures.append(f"field '{key}' does not contain {needle!r}")

    return failures


# ---- Loading and running -------------------------------------------------

def load_scenario(path: Path) -> dict:
    """Load and validate a scenario YAML file.

    Uses ``yaml.safe_load`` so a scenario file can never execute code.

    Raises:
        ScenarioError: If the file has no ``steps`` list, or a step is missing
            ``name``, ``method`` or ``path``.
    """
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict) or "steps" not in data:
        raise ScenarioError(f"{path}: scenario must be a mapping with a 'steps' list")
    for i, step in enumerate(data["steps"], start=1):
        for required in ("name", "method", "path"):
            if required not in step:
                raise ScenarioError(f"{path}: step {i} is missing '{required}'")
    return data


def run_scenario(client: ApiClient, scenario: dict) -> ScenarioResult:
    """Execute every step of a scenario in order.

    For each step: substitute variables, send the request, check expectations,
    then save any requested response fields for later steps. By default the
    scenario stops at the first failure (``stop_on_failure: true``), because
    later steps usually depend on earlier ones.
    """
    # Start with the scenario's declared variables; steps add more via `save`.
    variables: dict[str, Any] = dict(scenario.get("variables") or {})
    results: list[StepResult] = []

    for step in scenario["steps"]:
        name = step["name"]
        started = time.perf_counter()

        # Send the request. Any failure here (network error, timeout, or an
        # undefined ${variable}) is recorded as a failed step, not a crash.
        try:
            path = substitute(step["path"], variables)
            body = substitute(step.get("body"), variables)
            response = client.request(step["method"].upper(), path, json=body)
        except Exception as exc:
            results.append(StepResult(name, False, None, 0.0, [f"request failed: {exc}"]))
            log.error("FAIL %s: %s", name, exc)
            if scenario.get("stop_on_failure", True):
                break
            continue

        duration = (time.perf_counter() - started) * 1000

        # Some responses (e.g. 204 No Content or HTML error pages) aren't JSON.
        try:
            payload = response.json()
        except ValueError:
            payload = None

        # Expectations can reference variables too, e.g. phone_number: ${phone}.
        expect = substitute(step.get("expect") or {}, variables)
        failures = check_expectations(expect, response.status_code, payload)

        # Save response fields for later steps, e.g. save: {subscriber_id: id}.
        for var, json_path in (step.get("save") or {}).items():
            try:
                variables[var] = get_path(payload, json_path)
            except (KeyError, IndexError, ValueError, TypeError):
                failures.append(f"could not save '{var}' from '{json_path}'")

        result = StepResult(name, not failures, response.status_code, duration, failures)
        results.append(result)
        log.info("%s %s (%.0f ms)", "PASS" if result.passed else "FAIL", name, duration)
        for f in failures:
            log.info("    - %s", f)

        if failures and scenario.get("stop_on_failure", True):
            break

    return ScenarioResult(scenario.get("name", "unnamed scenario"), results)


# ---- Command-line interface ----------------------------------------------

def print_summary(results: list[ScenarioResult]) -> None:
    """Print a readable pass/fail report for all scenarios."""
    for scenario in results:
        mark = "PASS" if scenario.passed else "FAIL"
        print(f"\n[{mark}] {scenario.name}")
        for step in scenario.steps:
            print(f"   {'ok ' if step.passed else 'ERR'} {step.name} "
                  f"(status {step.status_code}, {step.duration_ms:.0f} ms)")
            for failure in step.failures:
                print(f"         - {failure}")
    total = len(results)
    passed = sum(r.passed for r in results)
    print(f"\n{passed}/{total} scenarios passed")


def main(argv: list[str] | None = None) -> int:
    """CLI entry point.

    Returns:
        Exit code 0 if every scenario passed, 1 otherwise, so the command
        can gate a CI pipeline.
    """
    parser = argparse.ArgumentParser(description="Run YAML API regression scenarios.")
    parser.add_argument("paths", nargs="+", type=Path, help="Scenario files or directories")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show request-level logs")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    # Load settings from a local .env file if python-dotenv is installed.
    # Real environment variables still take priority.
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # Expand directories into their *.yaml files, sorted for a stable run order.
    files: list[Path] = []
    for p in args.paths:
        files.extend(sorted(p.glob("*.yaml")) if p.is_dir() else [p])

    results = []
    with ApiClient(ClientConfig.from_env()) as client:
        for f in files:
            results.append(run_scenario(client, load_scenario(f)))

    print_summary(results)
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
