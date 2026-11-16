"""A small in-memory mock of a subscriber/account lifecycle REST API.

It exists so the regression framework can be run end-to-end by anyone,
without access to a real backend. All data is fictional and lives in memory,
so it resets whenever the server restarts.

Run it with::

    python -m uvicorn mock_server.app:app --port 8000

Business rules it enforces (so tests have something meaningful to check):
- phone numbers must be 10-15 digits
- two active subscribers can't share a phone number
- a closed subscriber can't be changed
- deactivating by phone number is idempotent: it closes whatever is open on
  that number and succeeds even when nothing is, so test runs can use it to
  put their data into a known inactive state before and after each run
"""

from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

app = FastAPI(title="Mock Subscriber API", version="1.0.0")
security = HTTPBasic()

# Demo credentials. Override them with environment variables if you like.
API_USER = os.getenv("MOCK_API_USER", "demo")
API_PASSWORD = os.getenv("MOCK_API_PASSWORD", "demo-password")

Status = Literal["active", "suspended", "closed"]


# ---- Request and response models -----------------------------------------
# Pydantic validates incoming JSON against these models and returns
# HTTP 422 automatically when a request doesn't match.

class SubscriberCreate(BaseModel):
    """Body for creating a subscriber."""

    first_name: str
    last_name: str
    phone_number: str = Field(pattern=r"^\+?\d{10,15}$")
    plan: str
    features: list[str] = []


class StatusChange(BaseModel):
    """Body for suspending or restoring service. Closing has its own endpoint."""

    status: Literal["active", "suspended"]
    reason: str = "unspecified"


class FeatureUpdate(BaseModel):
    """Body for adding and/or removing features in one call."""

    add: list[str] = []
    remove: list[str] = []


class NumberChange(BaseModel):
    """Body for changing a subscriber's phone number."""

    phone_number: str = Field(pattern=r"^\+?\d{10,15}$")


class Deactivate(BaseModel):
    """Body for deactivating whatever account is open on a phone number."""

    phone_number: str = Field(pattern=r"^\+?\d{10,15}$")


class Subscriber(BaseModel):
    """A stored subscriber record, as returned by every endpoint."""

    id: str
    first_name: str
    last_name: str
    phone_number: str
    plan: str
    features: list[str]
    status: Status
    created_at: str
    updated_at: str


# In-memory "database": subscriber ID -> record.
_db: dict[str, Subscriber] = {}


# ---- Helpers -------------------------------------------------------------

def _now() -> str:
    """Current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    """FastAPI dependency that rejects requests without valid basic auth.

    ``secrets.compare_digest`` compares in constant time, so response timing
    can't leak how much of a guessed password was correct.
    """
    user_ok = secrets.compare_digest(credentials.username, API_USER)
    pass_ok = secrets.compare_digest(credentials.password, API_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")


def _get(subscriber_id: str) -> Subscriber:
    """Return a subscriber or raise 404."""
    sub = _db.get(subscriber_id)
    if sub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Subscriber not found")
    return sub


def _require_not_closed(sub: Subscriber) -> None:
    """Raise 409 Conflict if the subscriber is closed."""
    if sub.status == "closed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Subscriber is closed")


# ---- Endpoints -----------------------------------------------------------

@app.get("/health")
def health() -> dict:
    """Unauthenticated liveness check, used by tests to know the server is up."""
    return {"status": "ok"}


@app.post("/api/v1/subscribers", status_code=201, dependencies=[Depends(require_auth)])
def create_subscriber(body: SubscriberCreate) -> Subscriber:
    """Create an active subscriber. Returns 409 if the number is already in use."""
    # Closed accounts release their number, so only check non-closed records.
    if any(s.phone_number == body.phone_number and s.status != "closed" for s in _db.values()):
        raise HTTPException(status.HTTP_409_CONFLICT, "Phone number already in use")
    now = _now()
    sub = Subscriber(
        id=str(uuid.uuid4()),
        status="active",
        created_at=now,
        updated_at=now,
        **body.model_dump(),
    )
    _db[sub.id] = sub
    return sub


@app.post("/api/v1/subscribers/deactivate", dependencies=[Depends(require_auth)])
def deactivate_by_number(body: Deactivate) -> dict:
    """Close every open (active or suspended) account on a phone number.

    Idempotent: returns 200 whether or not anything was open, so regression
    runs can call it first and last to leave the number in a known inactive
    state. ``deactivated`` reports how many accounts this call closed.
    """
    closed = 0
    for sub in _db.values():
        if sub.phone_number == body.phone_number and sub.status != "closed":
            sub.status = "closed"
            sub.updated_at = _now()
            closed += 1
    return {"phone_number": body.phone_number, "status": "inactive", "deactivated": closed}


@app.get("/api/v1/subscribers/{subscriber_id}", dependencies=[Depends(require_auth)])
def get_subscriber(subscriber_id: str) -> Subscriber:
    """Fetch one subscriber by ID."""
    return _get(subscriber_id)


@app.patch("/api/v1/subscribers/{subscriber_id}/status", dependencies=[Depends(require_auth)])
def change_status(subscriber_id: str, body: StatusChange) -> Subscriber:
    """Suspend or restore service."""
    sub = _get(subscriber_id)
    _require_not_closed(sub)
    sub.status = body.status
    sub.updated_at = _now()
    return sub


@app.put("/api/v1/subscribers/{subscriber_id}/features", dependencies=[Depends(require_auth)])
def update_features(subscriber_id: str, body: FeatureUpdate) -> Subscriber:
    """Remove then add features. Adding a feature that already exists is a no-op."""
    sub = _get(subscriber_id)
    _require_not_closed(sub)
    features = [f for f in sub.features if f not in body.remove]
    features += [f for f in body.add if f not in features]
    sub.features = features
    sub.updated_at = _now()
    return sub


@app.put("/api/v1/subscribers/{subscriber_id}/phone-number", dependencies=[Depends(require_auth)])
def change_number(subscriber_id: str, body: NumberChange) -> Subscriber:
    """Assign a new phone number to the subscriber."""
    sub = _get(subscriber_id)
    _require_not_closed(sub)
    sub.phone_number = body.phone_number
    sub.updated_at = _now()
    return sub


@app.post("/api/v1/subscribers/{subscriber_id}/close", dependencies=[Depends(require_auth)])
def close_subscriber(subscriber_id: str) -> Subscriber:
    """Close the account permanently. Closed accounts reject all further changes."""
    sub = _get(subscriber_id)
    _require_not_closed(sub)
    sub.status = "closed"
    sub.updated_at = _now()
    return sub


@app.post("/api/v1/_reset", include_in_schema=False, dependencies=[Depends(require_auth)])
def reset() -> dict:
    """Test helper: delete all data so each test starts clean. Hidden from the API docs."""
    _db.clear()
    return {"reset": True}
