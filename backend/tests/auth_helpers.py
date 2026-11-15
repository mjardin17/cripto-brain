"""Shared Stage 4A test authentication helper for FastAPI TestClient suites."""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

DEFAULT_OWNER_USERNAME = "testowner"
DEFAULT_OWNER_PASSWORD = "Owner-Strong-Pass!2026"


def sync_csrf_header(client: TestClient) -> str | None:
    """Copy certainty_csrf cookie into TestClient default X-CSRF-Token header."""
    csrf = client.cookies.get("certainty_csrf")
    if csrf:
        client.headers["X-CSRF-Token"] = csrf
    else:
        client.headers.pop("X-CSRF-Token", None)
    return csrf


def authenticate_test_client(
    client: TestClient,
    *,
    username: str = DEFAULT_OWNER_USERNAME,
    password: str = DEFAULT_OWNER_PASSWORD,
    email: str = "owner@certainty.local",
) -> dict[str, Any]:
    """Enroll initial OWNER (if 0 users) or sign in, and attach X-CSRF-Token to client."""
    status = client.get("/api/auth/status").json()
    if status.get("needs_owner_bootstrap"):
        resp = client.post(
            "/api/auth/bootstrap-owner",
            json={"username": username, "password": password, "email": email},
        )
        assert resp.status_code == 200, f"Bootstrap failed: {resp.text}"
        sync_csrf_header(client)
        return resp.json()["user"]

    resp = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"Login failed: {resp.text}"
    sync_csrf_header(client)
    return resp.json()["user"]
