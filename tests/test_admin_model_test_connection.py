"""Console Test connection for ``oauth_client_credentials`` model definitions (AI-DP-3010).

``POST /v1/api/admin/model-definitions/test-connection`` mints one fresh token with the
B3 grant and reports ``ok`` + ``expires_in``, or a redacted error. It never returns the
token or the secret, needs ``admin.models``, and needs ``admin.mcp`` as well for any
value that is not the stored row's.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.routing import Route
from starlette.testclient import TestClient

from tests._coord_test_helpers import _AuthMiddleware, _FakeConfigStore
from tests._secret_refs_helpers import install_fake_resolver
from turnstone.console.server import admin_test_model_connection
from turnstone.core.oauth import http as oauth_http

if TYPE_CHECKING:
    from turnstone.core.storage._sqlite import SQLiteBackend

_PATH = "/v1/api/admin/model-definitions/test-connection"
_TOKEN_URL = "https://auth.example/oauth2/token"
_OTHER_TOKEN_URL = "https://auth2.example/oauth2/token"
_SECRET = "stored-secret-1"
_TOKEN = "eyJ.minted-access-token.sig"
_BOTH = "admin.models,admin.mcp"


@pytest.fixture
def storage(tmp_path: Any, sqlite_backend_factory: Any) -> SQLiteBackend:
    backend: SQLiteBackend = sqlite_backend_factory(str(tmp_path / "models.db"))
    return backend


class _TokenEndpoint:
    """A fake authorization server recording each token POST."""

    def __init__(self, status: int = 200, payload: dict[str, Any] | None = None) -> None:
        self.status = status
        self.payload = (
            payload
            if payload is not None
            else {"access_token": _TOKEN, "token_type": "Bearer", "expires_in": 3600}
        )
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.payload)

    def basic_pair(self, index: int = -1) -> str:
        header = self.requests[index].headers["authorization"]
        assert header.startswith("Basic ")
        return base64.b64decode(header.removeprefix("Basic ")).decode()


@pytest.fixture
def endpoint(monkeypatch: pytest.MonkeyPatch) -> _TokenEndpoint:
    fake = _TokenEndpoint()
    monkeypatch.setattr(
        oauth_http,
        "json_http_client",
        lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(fake)),
    )
    return fake


def _client(
    storage: SQLiteBackend, perms: str = "admin.models", allowlist: str = _TOKEN_URL
) -> TestClient:
    app = Starlette(
        routes=[Route(_PATH, admin_test_model_connection, methods=["POST"])],
        middleware=[Middleware(_AuthMiddleware)],
    )
    app.state.auth_storage = storage
    app.state.config_store = _FakeConfigStore({"model.oauth_token_url_allowlist": allowlist})
    client = TestClient(app)
    client.headers.update({"X-Test-User": "admin", "X-Test-Perms": perms})
    return client


def _seed(storage: SQLiteBackend, *, secret: str = _SECRET, auth_mode: str = "") -> str:
    storage.create_model_definition(
        definition_id="cc-1",
        alias="aidp",
        model="aiq-agent",
        provider="openai-compatible",
        base_url="https://aidp.example/v1",
        api_key="",
        context_window=8192,
        capabilities="{}",
        enabled=True,
        created_by="admin",
        auth_mode=auth_mode or "oauth_client_credentials",
        obo_audience="",
        obo_scopes="ai-gateway/invoke",
        max_concurrency=0,
        oauth_token_url=_TOKEN_URL,
        oauth_client_id="client-1",
        oauth_client_secret=secret,
    )
    return "cc-1"


def _assert_no_credentials(resp: Any, *values: str) -> None:
    for value in (_TOKEN, *values):
        assert value not in resp.text


def test_stored_definition_success_returns_ok_and_expires_in_without_token(
    storage: SQLiteBackend, endpoint: _TokenEndpoint
) -> None:
    definition_id = _seed(storage)
    resp = _client(storage).post(_PATH, json={"definition_id": definition_id})

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True, "expires_in": 3600}
    _assert_no_credentials(resp, _SECRET)
    # One fresh mint with the stored credentials and scope, sent as HTTP Basic.
    assert len(endpoint.requests) == 1
    assert str(endpoint.requests[0].url) == _TOKEN_URL
    assert endpoint.basic_pair() == f"client-1:{_SECRET}"
    assert b"scope=ai-gateway%2Finvoke" in endpoint.requests[0].content
    assert _SECRET.encode() not in endpoint.requests[0].content


def test_each_test_mints_fresh(storage: SQLiteBackend, endpoint: _TokenEndpoint) -> None:
    definition_id = _seed(storage)
    client = _client(storage)
    for _ in range(2):
        assert client.post(_PATH, json={"definition_id": definition_id}).json()["ok"] is True
    assert len(endpoint.requests) == 2


def test_masked_secret_echo_keeps_the_stored_secret_under_admin_models(
    storage: SQLiteBackend, endpoint: _TokenEndpoint
) -> None:
    """The edit form sends its fields back, the secret as ``***``: that is the stored pair."""
    definition_id = _seed(storage)
    resp = _client(storage).post(
        _PATH,
        json={
            "definition_id": definition_id,
            "auth_mode": "oauth_client_credentials",
            "oauth_token_url": _TOKEN_URL,
            "oauth_client_id": "client-1",
            "oauth_client_secret": "***",
            "obo_scopes": "ai-gateway/invoke",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert endpoint.basic_pair() == f"client-1:{_SECRET}"


def test_missing_expires_in_reports_null(storage: SQLiteBackend, endpoint: _TokenEndpoint) -> None:
    endpoint.payload = {"access_token": _TOKEN}
    resp = _client(storage).post(_PATH, json={"definition_id": _seed(storage)})
    assert resp.json() == {"ok": True, "expires_in": None}


def test_bad_secret_returns_sanitized_error(
    storage: SQLiteBackend, endpoint: _TokenEndpoint
) -> None:
    """A rejected secret is ``ok: false`` with a redacted reason, even when the AS echoes it."""
    bad = "wrong-secret-xyz"
    endpoint.status = 401
    endpoint.payload = {"error": "invalid_client", "error_description": f"bad secret {bad}"}
    resp = _client(storage, perms=_BOTH).post(
        _PATH, json={"definition_id": _seed(storage), "oauth_client_secret": bad}
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "expires_in" not in body or body["expires_in"] is None
    assert "401" in body["error"]
    assert "invalid_client" in body["error"]
    assert body["failure_class"] == "transient"
    _assert_no_credentials(
        resp, bad, _SECRET, base64.b64encode(f"client-1:{bad}".encode()).decode()
    )
    assert endpoint.basic_pair() == f"client-1:{bad}"


def test_submitted_definition_without_stored_row(
    storage: SQLiteBackend, endpoint: _TokenEndpoint
) -> None:
    resp = _client(storage, perms=_BOTH).post(
        _PATH,
        json={
            "auth_mode": "oauth_client_credentials",
            "oauth_token_url": _TOKEN_URL,
            "oauth_client_id": "client-new",
            "oauth_client_secret": "new-secret",
            "obo_scopes": "ai-gateway/invoke",
        },
    )
    assert resp.json() == {"ok": True, "expires_in": 3600}
    assert endpoint.basic_pair() == "client-new:new-secret"


def test_requires_admin_models(storage: SQLiteBackend, endpoint: _TokenEndpoint) -> None:
    definition_id = _seed(storage)
    for perms in ("admin.mcp", "admin.settings"):
        resp = _client(storage, perms=perms).post(_PATH, json={"definition_id": definition_id})
        assert resp.status_code == 403, resp.text
    assert endpoint.requests == []


@pytest.mark.parametrize(
    "override",
    [
        {"oauth_client_secret": "other-secret"},
        {"oauth_client_id": "client-2"},
        {"oauth_token_url": _OTHER_TOKEN_URL},
        {"obo_scopes": "ai-gateway/admin"},
    ],
)
def test_unsaved_values_also_need_admin_mcp(
    storage: SQLiteBackend, endpoint: _TokenEndpoint, override: dict[str, str]
) -> None:
    """Re-aiming or replacing the stored pair is what saving it would gate."""
    definition_id = _seed(storage)
    body = {"definition_id": definition_id, **override}
    resp = _client(storage, allowlist=f"{_TOKEN_URL}\n{_OTHER_TOKEN_URL}").post(_PATH, json=body)
    assert resp.status_code == 403, resp.text
    assert endpoint.requests == []
    resp = _client(storage, perms=_BOTH, allowlist=f"{_TOKEN_URL}\n{_OTHER_TOKEN_URL}").post(
        _PATH, json=body
    )
    assert resp.status_code == 200, resp.text
    assert len(endpoint.requests) == 1


def test_submitted_only_needs_admin_mcp(storage: SQLiteBackend, endpoint: _TokenEndpoint) -> None:
    resp = _client(storage).post(
        _PATH,
        json={
            "auth_mode": "oauth_client_credentials",
            "oauth_token_url": _TOKEN_URL,
            "oauth_client_id": "c",
            "oauth_client_secret": "s",
            "obo_scopes": "x",
        },
    )
    assert resp.status_code == 403
    assert endpoint.requests == []


def test_token_url_outside_allowlist_is_refused(
    storage: SQLiteBackend, endpoint: _TokenEndpoint
) -> None:
    definition_id = _seed(storage)
    # The stored URL is checked too: de-listing an endpoint stops its tests.
    resp = _client(storage, allowlist=_OTHER_TOKEN_URL).post(
        _PATH, json={"definition_id": definition_id}
    )
    assert resp.status_code == 400
    assert "model.oauth_token_url_allowlist" in resp.json()["error"]
    assert endpoint.requests == []


def test_non_client_credentials_mode_is_refused(
    storage: SQLiteBackend, endpoint: _TokenEndpoint
) -> None:
    resp = _client(storage).post(_PATH, json={"definition_id": _seed(storage, auth_mode="static")})
    assert resp.status_code == 400
    assert "oauth_client_credentials" in resp.json()["error"]
    assert endpoint.requests == []


def test_unknown_definition_is_404(storage: SQLiteBackend, endpoint: _TokenEndpoint) -> None:
    resp = _client(storage).post(_PATH, json={"definition_id": "nope"})
    assert resp.status_code == 404


def test_missing_scope_is_refused(storage: SQLiteBackend, endpoint: _TokenEndpoint) -> None:
    resp = _client(storage, perms=_BOTH).post(
        _PATH, json={"definition_id": _seed(storage), "obo_scopes": ""}
    )
    assert resp.status_code == 400
    assert "obo_scopes" in resp.json()["error"]
    assert endpoint.requests == []


def test_env_placeholder_secret_is_refused(
    storage: SQLiteBackend, endpoint: _TokenEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A submitted ``${VAR}`` is refused, as on save: it must not expand the console's env."""
    monkeypatch.setenv("B7_TEST_SECRET", "from-env")
    resp = _client(storage, perms=_BOTH).post(
        _PATH, json={"definition_id": _seed(storage), "oauth_client_secret": "${B7_TEST_SECRET}"}
    )
    assert resp.status_code == 400
    assert "from-env" not in resp.text
    assert endpoint.requests == []


def test_stored_secret_reference_resolves_for_the_mint(
    storage: SQLiteBackend, endpoint: _TokenEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_resolver(monkeypatch, {"/cognito": "resolved-secret"})
    definition_id = _seed(storage, secret="secret://fake/cognito")
    resp = _client(storage).post(_PATH, json={"definition_id": definition_id})
    assert resp.json() == {"ok": True, "expires_in": 3600}
    assert endpoint.basic_pair() == "client-1:resolved-secret"
    assert "resolved-secret" not in resp.text


def test_unresolvable_secret_reference_is_refused(
    storage: SQLiteBackend, endpoint: _TokenEndpoint, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fake_resolver(monkeypatch, {})
    resp = _client(storage, perms=_BOTH).post(
        _PATH, json={"definition_id": _seed(storage), "oauth_client_secret": "secret://fake/nope"}
    )
    assert resp.status_code in (400, 503)
    assert endpoint.requests == []


def test_transport_failure_is_ok_false(
    storage: SQLiteBackend, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        oauth_http,
        "json_http_client",
        lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(fail)),
    )
    resp = _client(storage).post(_PATH, json={"definition_id": _seed(storage)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]
    _assert_no_credentials(resp, _SECRET)
