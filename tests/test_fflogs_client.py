"""Unit tests for the FFLogs OAuth + GraphQL client (T-002).

Mocks the HTTP layer via httpx.MockTransport so these run with no network.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

from ingest.fflogs import (
    FFLogsAPIError,
    FFLogsAuthError,
    FFLogsClient,
    Token,
)


def _make_client(handler) -> FFLogsClient:
    transport = httpx.MockTransport(handler)
    http = httpx.Client(transport=transport)
    return FFLogsClient(client_id="id", client_secret="secret", http_client=http)


def test_token_is_valid_window():
    t = Token(access_token="x", expires_at=time.time() + 3600)
    assert t.is_valid()
    expired = Token(access_token="x", expires_at=time.time() - 1)
    assert not expired.is_valid()
    empty = Token(access_token="", expires_at=time.time() + 3600)
    assert not empty.is_valid()


def test_missing_creds_raises():
    with pytest.raises(FFLogsAuthError):
        FFLogsClient(client_id="", client_secret="")


def test_get_token_exchanges_and_caches():
    calls = {"token": 0, "graphql": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            calls["token"] += 1
            assert req.headers.get("Authorization", "").startswith("Basic ")
            return httpx.Response(200, json={"access_token": "abc", "expires_in": 3600, "token_type": "Bearer"})
        raise AssertionError(f"unexpected URL {req.url}")

    c = _make_client(handler)
    assert c.get_token() == "abc"
    assert c.get_token() == "abc"  # cached, no second call
    assert calls["token"] == 1


def test_get_token_force_refresh():
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        return httpx.Response(200, json={"access_token": f"tok{n['i']}", "expires_in": 3600})

    c = _make_client(handler)
    assert c.get_token() == "tok1"
    assert c.get_token(force_refresh=True) == "tok2"


def test_token_refreshes_when_expired():
    n = {"i": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        n["i"] += 1
        # expires_in of 1 puts us well inside the refresh margin immediately
        return httpx.Response(200, json={"access_token": f"tok{n['i']}", "expires_in": 1})

    c = _make_client(handler)
    assert c.get_token() == "tok1"
    # token is now within TOKEN_REFRESH_MARGIN_S of expiry → next call refetches
    assert c.get_token() == "tok2"


def test_token_exchange_http_error():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad creds")

    c = _make_client(handler)
    with pytest.raises(FFLogsAuthError):
        c.get_token()


def test_token_exchange_malformed_response():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"oops": True})

    c = _make_client(handler)
    with pytest.raises(FFLogsAuthError):
        c.get_token()


def test_graphql_uses_bearer_and_returns_data():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        assert req.headers["Authorization"] == "Bearer T"
        body = json.loads(req.content)
        assert body["query"].strip().startswith("query")
        assert body["variables"] == {"code": "abc"}
        return httpx.Response(200, json={"data": {"reportData": {"report": {"code": "abc"}}}})

    c = _make_client(handler)
    data = c.graphql("query Q($code: String!) { x }", {"code": "abc"})
    assert data["reportData"]["report"]["code"] == "abc"


def test_graphql_401_triggers_refresh_and_retry():
    state = {"tokens": 0, "graphql": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            state["tokens"] += 1
            return httpx.Response(200, json={"access_token": f"T{state['tokens']}", "expires_in": 3600})
        state["graphql"] += 1
        # first GraphQL call: 401 even though we have a cached token
        # second GraphQL call (after force-refresh): success
        if state["graphql"] == 1:
            return httpx.Response(401, text="token expired")
        return httpx.Response(200, json={"data": {"ok": True}})

    c = _make_client(handler)
    out = c.graphql("query { ok }")
    assert out == {"ok": True}
    assert state["tokens"] == 2  # initial + force refresh
    assert state["graphql"] == 2


def test_graphql_errors_payload_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(200, json={"errors": [{"message": "nope"}]})

    c = _make_client(handler)
    with pytest.raises(FFLogsAPIError):
        c.graphql("query { ok }")


def test_graphql_non_200_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "T", "expires_in": 3600})
        return httpx.Response(500, text="server boom")

    c = _make_client(handler)
    with pytest.raises(FFLogsAPIError):
        c.graphql("query { ok }")
