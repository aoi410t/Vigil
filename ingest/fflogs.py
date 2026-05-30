"""FFLogs OAuth client-credentials + user-OAuth + GraphQL helper (PLAN.md §7).

Two auth flows live in this module:

1. **Client credentials** (T-002, default) — anonymous, public-data only. Goes
   against `/api/v2/client`. Token is kept in-memory and refreshed on expiry.

2. **User OAuth (authorization code)** — required for `/api/v2/user` access.
   Unlocks the connected user's archived reports + private logs (Gold tier).
   Refresh token is persisted in `fflogs_user_auth` (one-row); access token is
   refreshed on demand. Initial consent happens via the API's
   `/auth/fflogs/login` → FFLogs consent → `/auth/fflogs/callback` flow.
"""
from __future__ import annotations

import secrets
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy.orm import Session

from api.config import settings
from db.models import FFLogsUserAuth

TOKEN_URL = "https://www.fflogs.com/oauth/token"
AUTHORIZE_URL = "https://www.fflogs.com/oauth/authorize"
GRAPHQL_URL = "https://www.fflogs.com/api/v2/client"
USER_GRAPHQL_URL = "https://www.fflogs.com/api/v2/user"
TOKEN_REFRESH_MARGIN_S = 60

# Default scope per FFLogs docs. `view-private-reports` is needed to see
# archived + private reports the connected user is a member of.
DEFAULT_USER_SCOPE = "view-private-reports"


class FFLogsAuthError(RuntimeError):
    """OAuth token exchange failed."""


class FFLogsAPIError(RuntimeError):
    """GraphQL request failed or returned errors."""


class FFLogsArchivedError(FFLogsAPIError):
    """Report events are paywalled behind the `/user` API for archived logs."""


class FFLogsUserAuthNotConfigured(FFLogsAuthError):
    """No user-OAuth token in the DB; call the login flow first."""


@dataclass
class Token:
    access_token: str
    expires_at: float

    def is_valid(self, now: float | None = None) -> bool:
        t = time.time() if now is None else now
        return bool(self.access_token) and t < self.expires_at - TOKEN_REFRESH_MARGIN_S


def _is_archived_error(errors: list[dict[str, Any]] | None) -> bool:
    if not errors:
        return False
    for e in errors:
        msg = (e.get("message") or "").lower()
        if "archived" in msg and "/user" in msg:
            return True
    return False


class FFLogsClient:
    """Thin sync client. Acquires + caches a bearer token; runs GraphQL queries.

    The `graphql()` method uses the client-credentials token against `/client`.
    The `graphql_user(..., session)` method uses the user-OAuth token from the
    `fflogs_user_auth` row against `/user`; it will raise
    `FFLogsUserAuthNotConfigured` if no user has connected yet.
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        token_url: str = TOKEN_URL,
        graphql_url: str = GRAPHQL_URL,
        user_graphql_url: str = USER_GRAPHQL_URL,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.client_id = client_id if client_id is not None else settings.fflogs_client_id
        self.client_secret = (
            client_secret if client_secret is not None else settings.fflogs_client_secret
        )
        if not self.client_id or not self.client_secret:
            raise FFLogsAuthError(
                "FFLOGS_CLIENT_ID and FFLOGS_CLIENT_SECRET must be set (see .env)."
            )
        self.token_url = token_url
        self.graphql_url = graphql_url
        self.user_graphql_url = user_graphql_url
        self._http = http_client or httpx.Client(timeout=30.0)
        self._owns_http = http_client is None
        self._token: Token | None = None

    def __enter__(self) -> "FFLogsClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    # ------------------------------------------------------------------
    # Client-credentials path (anonymous, public data)
    # ------------------------------------------------------------------

    def get_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self._token is not None and self._token.is_valid():
            return self._token.access_token
        resp = self._http.post(
            self.token_url,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        if resp.status_code != 200:
            raise FFLogsAuthError(
                f"Token exchange failed: HTTP {resp.status_code} {resp.text[:200]!r}"
            )
        body = resp.json()
        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not access_token or not isinstance(expires_in, int):
            raise FFLogsAuthError(f"Malformed token response: {body!r}")
        self._token = Token(access_token=access_token, expires_at=time.time() + expires_in)
        return self._token.access_token

    def graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        token = self.get_token()
        payload = {"query": query, "variables": variables or {}}
        resp = self._http.post(
            self.graphql_url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            token = self.get_token(force_refresh=True)
            resp = self._http.post(
                self.graphql_url,
                json=payload,
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            raise FFLogsAPIError(f"HTTP {resp.status_code}: {resp.text[:500]!r}")
        body = resp.json()
        if body.get("errors"):
            errs = body["errors"]
            if _is_archived_error(errs):
                raise FFLogsArchivedError(f"GraphQL errors: {errs!r}")
            raise FFLogsAPIError(f"GraphQL errors: {errs!r}")
        return body.get("data", {})

    def rate_limit(self) -> dict[str, Any]:
        return self.graphql(
            "query { rateLimitData { limitPerHour pointsSpentThisHour pointsResetIn } }"
        )

    def fetch_report(self, code: str) -> dict[str, Any]:
        query = """
        query Report($code: String!) {
          reportData {
            report(code: $code) {
              code title startTime endTime
              fights { id encounterID kill fightPercentage lastPhase startTime endTime }
            }
          }
        }
        """
        return self.graphql(query, {"code": code})

    # ------------------------------------------------------------------
    # User OAuth (authorization code) — Gold-tier archived + private logs
    # ------------------------------------------------------------------

    def build_authorize_url(
        self,
        redirect_uri: str,
        state: str,
        scope: str = DEFAULT_USER_SCOPE,
    ) -> str:
        """Construct the URL we redirect the user to for FFLogs consent."""
        params = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
            "scope": scope,
        }
        return f"{AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"

    @staticmethod
    def new_state() -> str:
        """Generate a random state value for the OAuth flow (CSRF guard)."""
        return secrets.token_urlsafe(32)

    def exchange_authorization_code(
        self,
        session: Session,
        code: str,
        redirect_uri: str,
        scope: str = DEFAULT_USER_SCOPE,
    ) -> FFLogsUserAuth:
        """POST the auth code to FFLogs, persist refresh + access tokens, return the row."""
        resp = self._http.post(
            self.token_url,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            auth=(self.client_id, self.client_secret),
        )
        if resp.status_code != 200:
            raise FFLogsAuthError(
                f"Authorization-code exchange failed: HTTP {resp.status_code} {resp.text[:200]!r}"
            )
        body = resp.json()
        refresh_token = body.get("refresh_token")
        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        if not refresh_token or not access_token or not isinstance(expires_in, int):
            raise FFLogsAuthError(f"Malformed token response: {body!r}")
        now = datetime.now(timezone.utc)
        access_expires_at = now + timedelta(seconds=expires_in)
        row = session.get(FFLogsUserAuth, 1)
        if row is None:
            row = FFLogsUserAuth(
                id=1, refresh_token=refresh_token,
                access_token=access_token, access_expires_at=access_expires_at,
                scope=scope, connected_at=now, updated_at=now,
            )
            session.add(row)
        else:
            row.refresh_token = refresh_token
            row.access_token = access_token
            row.access_expires_at = access_expires_at
            row.scope = scope
            row.connected_at = row.connected_at or now
            row.updated_at = now
        session.flush()
        return row

    def refresh_user_token(self, session: Session) -> FFLogsUserAuth:
        """Refresh the stored access token using the refresh token."""
        row = session.get(FFLogsUserAuth, 1)
        if row is None or not row.refresh_token:
            raise FFLogsUserAuthNotConfigured(
                "No FFLogs user auth in DB; call /auth/fflogs/login first."
            )
        resp = self._http.post(
            self.token_url,
            data={"grant_type": "refresh_token", "refresh_token": row.refresh_token},
            auth=(self.client_id, self.client_secret),
        )
        if resp.status_code != 200:
            raise FFLogsAuthError(
                f"Refresh-token exchange failed: HTTP {resp.status_code} {resp.text[:200]!r}"
            )
        body = resp.json()
        access_token = body.get("access_token")
        expires_in = body.get("expires_in")
        new_refresh = body.get("refresh_token")  # FFLogs rotates these
        if not access_token or not isinstance(expires_in, int):
            raise FFLogsAuthError(f"Malformed refresh response: {body!r}")
        now = datetime.now(timezone.utc)
        row.access_token = access_token
        row.access_expires_at = now + timedelta(seconds=expires_in)
        if new_refresh:
            row.refresh_token = new_refresh
        row.updated_at = now
        session.flush()
        return row

    def _ensure_user_token(self, session: Session) -> str:
        row = session.get(FFLogsUserAuth, 1)
        if row is None or not row.refresh_token:
            raise FFLogsUserAuthNotConfigured(
                "No FFLogs user auth in DB; call /auth/fflogs/login first."
            )
        now = datetime.now(timezone.utc)
        if (
            row.access_token
            and row.access_expires_at
            and row.access_expires_at - timedelta(seconds=TOKEN_REFRESH_MARGIN_S) > now
        ):
            return row.access_token
        row = self.refresh_user_token(session)
        return row.access_token

    def graphql_user(
        self,
        session: Session,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a GraphQL query against `/api/v2/user` using the stored user token."""
        token = self._ensure_user_token(session)
        payload = {"query": query, "variables": variables or {}}
        resp = self._http.post(
            self.user_graphql_url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 401:
            # token rejected; refresh once and retry
            row = self.refresh_user_token(session)
            resp = self._http.post(
                self.user_graphql_url,
                json=payload,
                headers={"Authorization": f"Bearer {row.access_token}"},
            )
        if resp.status_code != 200:
            raise FFLogsAPIError(f"HTTP {resp.status_code}: {resp.text[:500]!r}")
        body = resp.json()
        if body.get("errors"):
            raise FFLogsAPIError(f"GraphQL errors: {body['errors']!r}")
        return body.get("data", {})

    def graphql_with_archive_retry(
        self,
        session: Session,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Try `/client`, fall back to `/user` only if the report is archived.

        Default path stays cheap (no user-OAuth call). The fallback only
        triggers on the specific archive paywall error and only when the user
        has connected their Gold-tier account.
        """
        try:
            return self.graphql(query, variables)
        except FFLogsArchivedError:
            if self.has_user_auth(session):
                return self.graphql_user(session, query, variables)
            raise

    def has_user_auth(self, session: Session) -> bool:
        row = session.get(FFLogsUserAuth, 1)
        return row is not None and bool(row.refresh_token)

    def user_auth_status(self, session: Session) -> dict[str, Any]:
        row = session.get(FFLogsUserAuth, 1)
        if row is None or not row.refresh_token:
            return {"connected": False}
        return {
            "connected": True,
            "user_label": row.user_label,
            "scope": row.scope,
            "connected_at": row.connected_at.isoformat() if row.connected_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "access_expires_at": (
                row.access_expires_at.isoformat() if row.access_expires_at else None
            ),
        }
