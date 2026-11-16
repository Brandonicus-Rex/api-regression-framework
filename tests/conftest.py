"""Shared pytest fixtures.

Starts the mock API in a background thread once per test session, so tests
make real HTTP calls without needing a separate terminal.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
import requests
import uvicorn

from mock_server.app import API_PASSWORD, API_USER, app
from regression.client import ApiClient, ClientConfig


def _free_port() -> int:
    """Ask the OS for an unused port.

    Binding to port 0 makes the OS choose a free one. This avoids clashes
    with anything else running locally or in CI.
    """
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def base_url():
    """Run the mock API for the whole test session and yield its URL."""
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    # daemon=True means the thread can't keep pytest alive if shutdown fails.
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Poll the health endpoint for up to ~5 seconds until the server answers.
    url = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            if requests.get(f"{url}/health", timeout=0.5).ok:
                break
        except requests.ConnectionError:
            time.sleep(0.1)
    else:
        # The loop finished without a successful health check.
        raise RuntimeError("Mock server did not start")

    yield url

    # Teardown: ask uvicorn to stop and wait for the thread to finish.
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def client(base_url):
    """An authenticated ApiClient, with the mock database reset before each test."""
    # retries=0 so a failing test fails fast instead of waiting on backoff.
    config = ClientConfig(base_url=base_url, username=API_USER, password=API_PASSWORD, retries=0)
    with ApiClient(config) as c:
        c.request("POST", "/api/v1/_reset")
        yield c
