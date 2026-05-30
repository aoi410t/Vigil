"""Tests for the FFLogs user-OAuth flow (authorization code + token persistence)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from db.models import FFLogsUserAuth
from ingest.fflogs import (
    FFLogsAPIError,
    FFLogsArchivedError,
    FFLogsAuthError,
    FFLogsClient,
    FFLogsUserAuthNotConfigured,
)


def _client_with_transport(transport: httpx.MockTransport) -> FFLogsClient:
    return FFLogsClient(
        client_id="cid",
        client_secret="csec",
        http_client=httpx.Client(transport=transport),
    )


@pytest.fixture(autouse=True)
def _clear_fflogs_user_auth(db_session):
    """Wipe any real connected-user row from the dev DB so each test starts
    from a clean state. The db_session's savepoint rollback restores it on
    test exit, so production data isn't lost.
    """
    db_session.query(FFLogsUserAuth).delete()
    db_session.flush()
    yield


def test_build_authorize_url_has_required_params():
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    with _client_with_transport(transport) as client:
        url = client.build_authorize_url(
            redirect_uri="http://localhost:8800/auth/fflogs/callback",
            state="randomstate",
            scope="view-private-reports",
        )
    assert url.startswith("https://www.fflogs.com/oauth/authorize?")
    assert "client_id=cid" in url
    assert "response_type=code" in url
    assert "redirect_uri=http%3A%2F%2Flocalhost%3A8800%2Fauth%2Ffflogs%2Fcallback" in url
    assert "state=randomstate" in url
    assert "scope=view-private-reports" in url


def test_new_state_is_random_and_urlsafe():
    s1 = FFLogsClient.new_state()
    s2 = FFLogsClient.new_state()
    assert s1 != s2
    assert len(s1) >= 32
    # urlsafe set: A-Z a-z 0-9 - _
    assert all(c.isalnum() or c in "-_" for c in s1)


def test_exchange_code_persists_refresh_token(db_session):
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/oauth/token"
        body = dict(p.split("=", 1) for p in req.content.decode().split("&"))
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "AUTHCODE"
        return httpx.Response(200, json={
            "access_token": "ACCESS123",
            "refresh_token": "REFRESH456",
            "expires_in": 3600,
            "token_type": "Bearer",
        })
    transport = httpx.MockTransport(handler)
    with _client_with_transport(transport) as client:
        row = client.exchange_authorization_code(
            session=db_session, code="AUTHCODE",
            redirect_uri="http://localhost:8800/cb",
        )
        db_session.flush()
    assert row.refresh_token == "REFRESH456"
    assert row.access_token == "ACCESS123"
    assert row.access_expires_at is not None
    stored = db_session.get(FFLogsUserAuth, 1)
    assert stored is not None
    assert stored.refresh_token == "REFRESH456"


def test_exchange_code_malformed_response_raises(db_session):
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"access_token": "x"}))
    with _client_with_transport(transport) as client:
        with pytest.raises(FFLogsAuthError):
            client.exchange_authorization_code(db_session, "code", "redirect")


def test_refresh_user_token_uses_stored_refresh(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(FFLogsUserAuth(
        id=1, refresh_token="OLD_REFRESH", access_token="OLD_ACCESS",
        access_expires_at=now - timedelta(seconds=10),
        connected_at=now, updated_at=now,
    ))
    db_session.flush()

    def handler(req: httpx.Request) -> httpx.Response:
        body = dict(p.split("=", 1) for p in req.content.decode().split("&"))
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "OLD_REFRESH"
        return httpx.Response(200, json={
            "access_token": "NEW_ACCESS",
            "refresh_token": "NEW_REFRESH",
            "expires_in": 3600,
        })
    transport = httpx.MockTransport(handler)
    with _client_with_transport(transport) as client:
        row = client.refresh_user_token(db_session)
    assert row.access_token == "NEW_ACCESS"
    assert row.refresh_token == "NEW_REFRESH"


def test_refresh_user_token_no_row_raises(db_session):
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    with _client_with_transport(transport) as client:
        with pytest.raises(FFLogsUserAuthNotConfigured):
            client.refresh_user_token(db_session)


def test_graphql_user_includes_bearer_to_user_endpoint(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(FFLogsUserAuth(
        id=1, refresh_token="R", access_token="VALID_TOKEN",
        access_expires_at=now + timedelta(hours=1),
        connected_at=now, updated_at=now,
    ))
    db_session.flush()

    def handler(req: httpx.Request) -> httpx.Response:
        assert str(req.url) == "https://www.fflogs.com/api/v2/user"
        assert req.headers["Authorization"] == "Bearer VALID_TOKEN"
        return httpx.Response(200, json={"data": {"hello": "world"}})
    transport = httpx.MockTransport(handler)
    with _client_with_transport(transport) as client:
        data = client.graphql_user(db_session, "query { hello }")
    assert data == {"hello": "world"}


def test_graphql_user_refreshes_on_expired_token(db_session):
    now = datetime.now(timezone.utc)
    db_session.add(FFLogsUserAuth(
        id=1, refresh_token="R", access_token="STALE",
        access_expires_at=now - timedelta(seconds=10),
        connected_at=now, updated_at=now,
    ))
    db_session.flush()
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={
                "access_token": "FRESH", "refresh_token": "R", "expires_in": 3600,
            })
        calls.append(req.headers["Authorization"])
        return httpx.Response(200, json={"data": {"ok": True}})
    transport = httpx.MockTransport(handler)
    with _client_with_transport(transport) as client:
        client.graphql_user(db_session, "query { ok }")
    assert calls == ["Bearer FRESH"]


def test_archive_error_subclass_recognized():
    """The /client GraphQL helper raises FFLogsArchivedError on archive paywall."""
    archived_payload = {"errors": [{
        "message": "This report has been archived. Subscribing users can access the report content via the /user API endpoint.",
        "path": ["reportData", "report", "events"],
    }]}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(200, json=archived_payload)
    transport = httpx.MockTransport(handler)
    with _client_with_transport(transport) as client:
        with pytest.raises(FFLogsArchivedError):
            client.graphql("query { x }")


def test_graphql_with_archive_retry_falls_back_to_user(db_session):
    """When /client says archived and user auth exists, retry via /user."""
    now = datetime.now(timezone.utc)
    db_session.add(FFLogsUserAuth(
        id=1, refresh_token="R", access_token="USERTOK",
        access_expires_at=now + timedelta(hours=1),
        connected_at=now, updated_at=now,
    ))
    db_session.flush()
    archived = {"errors": [{
        "message": "This report has been archived. Subscribing users can access the report content via the /user API endpoint.",
    }]}
    endpoints_hit: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        endpoints_hit.append(req.url.path)
        if req.url.path == "/api/v2/client":
            return httpx.Response(200, json=archived)
        if req.url.path == "/api/v2/user":
            return httpx.Response(200, json={"data": {"recovered": True}})
        return httpx.Response(500)
    transport = httpx.MockTransport(handler)
    with _client_with_transport(transport) as client:
        data = client.graphql_with_archive_retry(db_session, "query { x }")
    assert data == {"recovered": True}
    assert endpoints_hit == ["/api/v2/client", "/api/v2/user"]


def test_graphql_with_archive_retry_no_user_auth_propagates(db_session):
    """No user auth + archived response -> error bubbles, no /user attempt."""
    archived = {"errors": [{
        "message": "This report has been archived. Subscribing users can access the report content via the /user API endpoint.",
    }]}
    endpoints_hit: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        endpoints_hit.append(req.url.path)
        return httpx.Response(200, json=archived)
    transport = httpx.MockTransport(handler)
    with _client_with_transport(transport) as client:
        with pytest.raises(FFLogsArchivedError):
            client.graphql_with_archive_retry(db_session, "query { x }")
    assert endpoints_hit == ["/api/v2/client"]


def test_has_user_auth_reflects_db_state(db_session):
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    with _client_with_transport(transport) as client:
        assert client.has_user_auth(db_session) is False
        now = datetime.now(timezone.utc)
        db_session.add(FFLogsUserAuth(
            id=1, refresh_token="R", connected_at=now, updated_at=now,
        ))
        db_session.flush()
        assert client.has_user_auth(db_session) is True


def test_user_auth_status_payload_shape(db_session):
    transport = httpx.MockTransport(lambda req: httpx.Response(200))
    with _client_with_transport(transport) as client:
        assert client.user_auth_status(db_session) == {"connected": False}
        now = datetime.now(timezone.utc)
        db_session.add(FFLogsUserAuth(
            id=1, refresh_token="R", access_token="A",
            access_expires_at=now + timedelta(hours=1),
            scope="view-private-reports",
            user_label="aoi",
            connected_at=now, updated_at=now,
        ))
        db_session.flush()
        status = client.user_auth_status(db_session)
    assert status["connected"] is True
    assert status["scope"] == "view-private-reports"
    assert status["user_label"] == "aoi"
    assert status["connected_at"] is not None
