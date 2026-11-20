"""Tests for the generic client-credentials grant (raw scope, HTTP Basic)."""

from __future__ import annotations

import asyncio
import base64
import logging
import traceback
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import httpx2
import pytest
import structlog

from turnstone.core.oauth import grants as oauth_grants
from turnstone.core.oauth import http as oauth_http
from turnstone.core.oauth import tokens as oauth_tokens

if TYPE_CHECKING:
    from collections.abc import Callable

TOKEN_URL = "https://auth.example.test/oauth2/token"
CLIENT_ID = "4k2exampleclientid"
# Not token-shaped on purpose: the output guard's credential redaction would
# not catch it, so only the grant's own scrubbing keeps it out of errors.
CLIENT_SECRET = "Sup3rS3cretValueXyz"
SCOPE = "ai-gateway/invoke"
BASIC = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode("ascii")


def _mint(handler: Callable[[httpx2.Request], httpx2.Response]) -> dict[str, Any]:
    async def run() -> dict[str, Any]:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            return await oauth_grants.mint_client_credentials_basic(
                token_endpoint=TOKEN_URL,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                scope=SCOPE,
                http_client=client,
            )

    return asyncio.run(run())


def _json(status: int, body: Any) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json=body)

    return handler


def _assert_no_secret(text: str) -> None:
    assert CLIENT_SECRET not in text
    assert BASIC not in text


def test_success_sends_raw_scope_and_basic_auth() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            json={"access_token": "at-123", "expires_in": 3600, "token_type": "Bearer"},
        )

    result = _mint(handler)

    assert result == {"access_token": "at-123", "expires_in": 3600}
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == TOKEN_URL
    assert request.headers["authorization"] == f"Basic {BASIC}"
    form = parse_qs(request.content.decode())
    # Raw scope, not Entra's <aud>/.default; credentials only in the header.
    assert form == {"grant_type": ["client_credentials"], "scope": [SCOPE]}
    _assert_no_secret(request.content.decode())


def test_empty_scope_is_omitted() -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json={"access_token": "at", "expires_in": 60})

    async def run() -> None:
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            await oauth_grants.mint_client_credentials_basic(
                token_endpoint=TOKEN_URL,
                client_id=CLIENT_ID,
                client_secret=CLIENT_SECRET,
                scope="",
                http_client=client,
            )

    asyncio.run(run())
    assert parse_qs(seen[0].content.decode()) == {"grant_type": ["client_credentials"]}


@pytest.mark.parametrize(
    ("status", "body", "expected_class"),
    [
        (400, {"error": "invalid_scope"}, oauth_http.RefreshFailureClass.PERMANENT),
        (400, {}, oauth_http.RefreshFailureClass.AMBIGUOUS),
        (400, {"error": "invalid_client"}, oauth_http.RefreshFailureClass.TRANSIENT),
        (401, {"error": "invalid_client"}, oauth_http.RefreshFailureClass.TRANSIENT),
        (401, {}, oauth_http.RefreshFailureClass.AMBIGUOUS),
        (503, {"error": "unavailable"}, oauth_http.RefreshFailureClass.TRANSIENT),
    ],
)
def test_error_status_maps_to_refresh_error(
    status: int, body: dict[str, Any], expected_class: oauth_http.RefreshFailureClass
) -> None:
    with pytest.raises(oauth_http.OAuthRefreshError) as excinfo:
        _mint(_json(status, body))

    assert f"HTTP {status}" in str(excinfo.value)
    assert excinfo.value.failure_class is expected_class
    _assert_no_secret(str(excinfo.value))


def test_error_body_echoing_secret_is_scrubbed() -> None:
    # Some ASes mirror request material in error_description.
    body = {
        "error": "invalid_client",
        "error_description": f"bad client {CLIENT_ID}:{CLIENT_SECRET} / Basic {BASIC}",
    }
    with pytest.raises(oauth_http.OAuthRefreshError) as excinfo:
        _mint(_json(401, body))

    message = str(excinfo.value)
    assert "HTTP 401" in message
    assert "***" in message
    _assert_no_secret(message)
    _assert_no_secret("".join(traceback.format_exception(excinfo.value)))


def test_transport_failure_is_refresh_error_without_secret() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError(f"boom {request.headers['authorization']}", request=request)

    with pytest.raises(oauth_http.OAuthRefreshError) as excinfo:
        _mint(handler)

    assert "request failed" in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    _assert_no_secret(str(excinfo.value))
    _assert_no_secret("".join(traceback.format_exception(excinfo.value)))


def test_missing_expires_in_is_left_out_and_caller_defaults() -> None:
    result = _mint(_json(200, {"access_token": "at-xyz", "token_type": "Bearer"}))

    assert result == {"access_token": "at-xyz"}
    # The cache layer's helper then falls back to its default lifetime
    # instead of a NULL (never-expiring) expiry.
    assert oauth_tokens._expires_at_from_response(result) is None
    assert oauth_tokens._expires_at_from_response(result, default_ttl_seconds=300) is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3600", 3600),
        (3600.0, 3600),
        (0, None),
        (-5, None),
        (True, None),
        ("soon", None),
        ("inf", None),
        (None, None),
    ],
)
def test_expires_in_normalisation(raw: Any, expected: int | None) -> None:
    result = _mint(_json(200, {"access_token": "at", "expires_in": raw}))

    assert result.get("expires_in") == expected


@pytest.mark.parametrize("body", [{}, {"access_token": ""}, {"access_token": 7}])
def test_missing_access_token_fails_closed(body: dict[str, Any]) -> None:
    with pytest.raises(oauth_http.OAuthRefreshError, match="missing access_token"):
        _mint(_json(200, body))


def test_non_object_body_is_refresh_error() -> None:
    with pytest.raises(oauth_http.OAuthRefreshError, match="not a JSON object"):
        _mint(_json(200, ["access_token"]))


def test_secret_never_reaches_logs(caplog: pytest.LogCaptureFixture) -> None:
    """Callers log mint failures with exc_info; nothing logged may hold the secret."""
    caplog.set_level(logging.DEBUG)
    log = structlog.get_logger("test_oauth_grants")
    body = {"error": "invalid_client", "error_description": f"secret={CLIENT_SECRET}"}

    with structlog.testing.capture_logs() as captured:
        for handler in (_json(200, {"access_token": "at", "expires_in": 60}), _json(401, body)):
            try:
                _mint(handler)
            except oauth_http.OAuthRefreshError as exc:
                log.warning("model_app.mint_failed", error=str(exc), exc_info=True)
                logging.getLogger("test_oauth_grants").warning("mint failed", exc_info=True)

    assert any(entry["event"] == "model_app.mint_failed" for entry in captured)
    _assert_no_secret(repr(captured))
    _assert_no_secret(caplog.text)
