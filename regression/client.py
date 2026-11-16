"""HTTP client used by the scenario runner.

Wraps ``requests.Session`` with the defaults a test tool should always have:
- every request has a timeout, so a hung server can't freeze a test run
- transient server errors are retried with backoff
- connection details and credentials come from configuration, never from code
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClientConfig:
    """Connection settings for the API under test.

    Attributes:
        base_url: Root URL of the API, e.g. ``http://127.0.0.1:8000``.
        username: Basic-auth username, or None for no auth.
        password: Basic-auth password, or None for no auth.
        timeout: Seconds to wait for each response before giving up.
        retries: How many times to retry transient failures.
        verify_tls: Whether to verify HTTPS certificates. Only disable this
            for test environments with self-signed certificates.
    """

    base_url: str
    username: str | None = None
    password: str | None = None
    timeout: float = 10.0
    retries: int = 3
    verify_tls: bool = True

    @classmethod
    def from_env(cls) -> "ClientConfig":
        """Build a config from environment variables (see ``.env.example``).

        Raises:
            RuntimeError: If ``API_BASE_URL`` is not set.
        """
        base_url = os.getenv("API_BASE_URL")
        if not base_url:
            raise RuntimeError("API_BASE_URL is not set. Copy .env.example to .env and fill it in.")
        return cls(
            # Strip a trailing slash so URL joining in ApiClient.request is predictable.
            base_url=base_url.rstrip("/"),
            username=os.getenv("API_USERNAME"),
            password=os.getenv("API_PASSWORD"),
            timeout=float(os.getenv("API_TIMEOUT", "10")),
            retries=int(os.getenv("API_RETRIES", "3")),
            # Anything other than the literal "false" keeps verification on (fail safe).
            verify_tls=os.getenv("API_VERIFY_TLS", "true").lower() != "false",
        )


class ApiClient:
    """A reusable HTTP session configured from a ``ClientConfig``.

    Use it as a context manager so the underlying connections are closed::

        with ApiClient(config) as client:
            response = client.request("GET", "/health")
    """

    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.session = requests.Session()

        # Only send credentials when both parts are present.
        if config.username and config.password:
            self.session.auth = (config.username, config.password)

        self.session.headers.update({"Content-Type": "application/json", "Accept": "application/json"})
        self.session.verify = config.verify_tls

        # Retry policy:
        # - only on 502/503/504, which usually mean "try again shortly"
        # - only for idempotent methods; POST is excluded because retrying a
        #   create request could create the same record twice
        # - backoff_factor=0.5 waits 0.5s, 1s, 2s... between attempts
        retry = Retry(
            total=config.retries,
            backoff_factor=0.5,
            status_forcelist=(502, 503, 504),
            allowed_methods=("GET", "PUT", "DELETE", "HEAD", "OPTIONS"),
        )
        adapter = HTTPAdapter(max_retries=retry)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def request(self, method: str, path: str, json: dict | None = None) -> requests.Response:
        """Send one request and return the response.

        Args:
            method: HTTP method, e.g. ``"GET"`` or ``"POST"``.
            path: Path relative to the base URL, e.g. ``"/api/v1/subscribers"``.
            json: Optional request body, serialized as JSON.

        Returns:
            The ``requests.Response``. HTTP error statuses (4xx/5xx) are returned,
            not raised, because tests often expect them.

        Raises:
            requests.RequestException: On network errors or timeouts.
        """
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        log.debug("%s %s", method, url)
        response = self.session.request(method, url, json=json, timeout=self.config.timeout)
        log.info("%s %s -> %s", method, path, response.status_code)
        return response

    def close(self) -> None:
        """Close all pooled connections."""
        self.session.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
