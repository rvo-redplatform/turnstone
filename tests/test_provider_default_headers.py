"""A definition's ``default_headers``: the shared rule, the client, and registry load."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from turnstone.core import model_registry as mr_module
from turnstone.core.model_registry import ModelConfig, ModelRegistry, load_model_registry
from turnstone.core.providers import create_client
from turnstone.core.providers.headers import (
    DEFAULT_HEADER_VALUE_MAX_LEN,
    DEFAULT_HEADERS_MAX_COUNT,
    RESERVED_DEFAULT_HEADER_NAMES,
    DefaultHeadersError,
    validate_default_headers,
)

# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestValidateDefaultHeaders:
    def test_plain_headers_pass_unchanged(self) -> None:
        headers = {"X-Team": "aiq", "x-trace-tag": "run 1"}
        assert validate_default_headers(headers) == headers

    def test_none_and_empty_are_the_empty_map(self) -> None:
        assert validate_default_headers(None) == {}
        assert validate_default_headers({}) == {}

    @pytest.mark.parametrize(
        "name",
        [
            "Authorization",
            "authorization",
            "X-API-KEY",
            "Proxy-Authorization",
            "Cookie",
            "api-key",
            "x-goog-api-key",
        ],
    )
    def test_credential_names_refused_in_any_case(self, name: str) -> None:
        with pytest.raises(DefaultHeadersError, match="carries a credential"):
            validate_default_headers({name: "v"})

    @pytest.mark.parametrize(
        "name",
        [
            "anthropic-workspace-id",
            "anthropic-beta",
            "x-switchyard-session-id",
            "x-switchyard-agent-id",
            "x-switchyard-is-subagent",
            "agent-session-id",
            "Host",
            "Content-Length",
            "Transfer-Encoding",
        ],
    )
    def test_turnstone_and_transport_names_refused(self, name: str) -> None:
        with pytest.raises(DefaultHeadersError, match="set by Turnstone"):
            validate_default_headers({name: "v"})

    @pytest.mark.parametrize(
        ("headers", "needle"),
        [
            (["x-team"], "must be an object"),
            ({"bad header": "v"}, "not a valid header name"),
            ({"": "v"}, "not a valid header name"),
            ({1: "v"}, "not a valid header name"),
            ({"x-" + "a" * 200: "v"}, "not a valid header name"),
            ({"X-Team": "a", "x-team": "b"}, "more than once"),
            ({"x-ok": 1}, "string value"),
            ({"x-ok": "line\r\nx-injected: 1"}, "control character"),
            ({"x-ok": " padded"}, "space at either end"),
            ({"x-ok": "café"}, "outside ASCII"),
            ({"x-ok": "secret://file/a"}, "secret://"),
            ({"x-ok": "v" * (DEFAULT_HEADER_VALUE_MAX_LEN + 1)}, "exceeds"),
            ({f"x-{i}": "v" for i in range(DEFAULT_HEADERS_MAX_COUNT + 1)}, "at most"),
        ],
    )
    def test_malformed_maps_refused(self, headers: Any, needle: str) -> None:
        with pytest.raises(DefaultHeadersError, match=needle):
            validate_default_headers(headers)

    def test_error_never_quotes_a_value(self) -> None:
        with pytest.raises(DefaultHeadersError) as exc:
            validate_default_headers({"x-ok": "sek\x01ret"})
        assert "sek" not in str(exc.value)

    def test_is_a_value_error(self) -> None:
        assert issubclass(DefaultHeadersError, ValueError)

    def test_console_has_no_reserved_list_of_its_own(self) -> None:
        """AI-DP-3073: the console's save check is the core rule, so it can
        never save a definition the registry then refuses at load."""
        from turnstone.console import server

        assert not hasattr(server, "_RESERVED_DEFAULT_HEADER_NAMES")

    @pytest.mark.parametrize("name", sorted(RESERVED_DEFAULT_HEADER_NAMES))
    def test_console_refuses_every_core_reserved_name(self, name: str) -> None:
        from turnstone.console.server import _parse_default_headers_field

        parsed, err = _parse_default_headers_field({name.title(): "v"})
        assert parsed is None
        assert err is not None and err.status_code == 400

    @pytest.mark.parametrize(
        "headers",
        [{"x-ok": "caf\u00e9"}, {"x-ok": " v"}, {"x-ok": "v "}, {"x-ok": "secret://file/a"}],
        ids=["non-ascii", "leading-space", "trailing-space", "reference"],
    )
    def test_console_refuses_what_core_refuses(self, headers: dict[str, str]) -> None:
        from turnstone.console.server import _parse_default_headers_field

        with pytest.raises(DefaultHeadersError) as core:
            validate_default_headers(headers)
        parsed, err = _parse_default_headers_field(headers)
        assert parsed is None and err is not None
        assert err.status_code == 400
        assert json.loads(bytes(err.body)) == {"error": str(core.value)}

    def test_console_accepts_what_core_accepts(self) -> None:
        from turnstone.console.server import _parse_default_headers_field

        headers = {"X-Team": "aiq", "x-switchyard-route": "claude"}
        assert _parse_default_headers_field(headers) == (validate_default_headers(headers), None)

    def test_reserved_names_are_lower_case(self) -> None:
        assert all(name == name.lower() for name in RESERVED_DEFAULT_HEADER_NAMES)


# ---------------------------------------------------------------------------
# create_client
# ---------------------------------------------------------------------------


def _wire_headers(client: Any, *, anthropic: bool) -> dict[str, dict[str, str]]:
    """Send one generation call and one model listing through a mock transport;
    return each request's headers by path."""
    import httpx2

    seen: dict[str, dict[str, str]] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen[request.url.path] = dict(request.headers)
        if request.url.path.endswith("/models"):
            body: dict[str, Any] = {"object": "list", "data": []}
            if anthropic:
                body = {"data": [], "has_more": False, "first_id": None, "last_id": None}
            return httpx2.Response(200, json=body, request=request)
        if anthropic:
            body = {
                "id": "m",
                "type": "message",
                "role": "assistant",
                "model": "m",
                "content": [],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 0},
            }
        else:
            body = {
                "id": "c",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
            }
        return httpx2.Response(200, json=body, request=request)

    wired = client.with_options(
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0
    )
    messages = [{"role": "user", "content": "hi"}]
    if anthropic:
        wired.messages.create(model="m", max_tokens=8, messages=messages)
    else:
        wired.chat.completions.create(model="m", messages=messages)
    wired.models.list()
    return seen


_HEADERS = {"X-Team": "aiq", "x-tenant": "rvoh"}


class TestCreateClientDefaultHeaders:
    @pytest.mark.parametrize(
        ("provider", "base_url"),
        [
            ("openai", ""),
            ("openai-compatible", "https://aidp.example/v1"),
            ("xai", ""),
            ("google", ""),
        ],
    )
    def test_headers_reach_the_openai_client(self, provider: str, base_url: str) -> None:
        client = create_client(provider, base_url=base_url, api_key="k", default_headers=_HEADERS)
        try:
            # A minted credential rides a with_options copy; the headers must too.
            seen = _wire_headers(client.with_options(api_key="minted"), anthropic=False)
        finally:
            client.close()
        assert len(seen) == 2
        for headers in seen.values():
            assert headers["x-team"] == "aiq"
            assert headers["x-tenant"] == "rvoh"
            assert headers["authorization"] == "Bearer minted"

    @pytest.mark.parametrize(
        ("provider", "base_url"),
        [("anthropic", ""), ("anthropic-compatible", "http://localhost:8000")],
    )
    def test_headers_reach_the_anthropic_client(self, provider: str, base_url: str) -> None:
        client = create_client(
            provider,
            base_url=base_url,
            api_key="k",
            workspace_id="wrkspc_01ABC",
            default_headers=_HEADERS,
        )
        try:
            seen = _wire_headers(client.with_options(api_key="minted"), anthropic=True)
        finally:
            client.close()
        assert len(seen) == 2
        for headers in seen.values():
            assert headers["x-team"] == "aiq"
            assert headers["x-tenant"] == "rvoh"
            # The workspace pin is kept alongside the definition's headers.
            assert headers["anthropic-workspace-id"] == "wrkspc_01ABC"

    @pytest.mark.parametrize("provider", ["openai", "anthropic"])
    def test_no_headers_sends_none_extra(self, provider: str) -> None:
        client = create_client(provider, base_url="", api_key="k")
        try:
            seen = _wire_headers(client, anthropic=provider == "anthropic")
        finally:
            client.close()
        for headers in seen.values():
            assert "x-team" not in headers

    @pytest.mark.parametrize(
        ("provider", "base_url"),
        [("openai-compatible", "https://aidp.example/v1"), ("anthropic", "")],
    )
    @pytest.mark.parametrize("name", ["Authorization", "x-api-key", "anthropic-workspace-id"])
    def test_reserved_header_refused_before_a_client_exists(
        self, provider: str, base_url: str, name: str
    ) -> None:
        with pytest.raises(DefaultHeadersError):
            create_client(provider, base_url=base_url, api_key="k", default_headers={name: "x"})


# ---------------------------------------------------------------------------
# Registry load and client build
# ---------------------------------------------------------------------------


class _Storage:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def list_model_definitions(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return self._rows


def _row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "alias": "aidp",
        "model": "aiq-agent",
        "provider": "openai-compatible",
        "base_url": "https://aidp.example/v1",
        "api_key": "sk",
        "context_window": 200000,
        "capabilities": "{}",
        "enabled": True,
        "default_headers": '{"x-team": "aiq"}',
    }
    row.update(overrides)
    return row


def _entry(**overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "model": "aiq-agent",
        "provider": "openai-compatible",
        "base_url": "https://aidp.example/v1",
        "api_key": "sk",
        "context_window": 200000,
        "default_headers": {"x-team": "aiq"},
    }
    entry.update(overrides)
    return entry


def _load_db(*rows: dict[str, Any]) -> ModelRegistry:
    with patch("turnstone.core.model_registry.load_config", return_value={}):
        return load_model_registry("http://x/v1", "x", "x", storage=_Storage(list(rows)))


def _load_toml(**models: dict[str, Any]) -> ModelRegistry:
    with patch("turnstone.core.model_registry.load_config", return_value={"models": models}):
        return load_model_registry("http://x/v1", "x", "x")


class TestRegistryDefaultHeaders:
    def test_valid_headers_load(self) -> None:
        assert _load_db(_row()).get_config("aidp").default_headers == {"x-team": "aiq"}
        assert _load_toml(aidp=_entry()).get_config("aidp").default_headers == {"x-team": "aiq"}

    @pytest.mark.parametrize("name", ["Authorization", "X-Api-Key", "cookie"])
    def test_credential_header_refused_at_db_load(self, name: str) -> None:
        with pytest.raises(mr_module.ModelAuthConfigError, match="'aidp' default_headers"):
            _load_db(_row(default_headers=f'{{"{name}": "Bearer static"}}'))

    @pytest.mark.parametrize(
        "name", ["Authorization", "proxy-authorization", "x-switchyard-agent-id"]
    )
    def test_reserved_header_refused_at_toml_load(self, name: str) -> None:
        with pytest.raises(mr_module.ModelAuthConfigError, match="'aidp' default_headers"):
            _load_toml(aidp=_entry(default_headers={name: "v"}))

    def test_refusal_never_quotes_the_value(self) -> None:
        with pytest.raises(mr_module.ModelAuthConfigError) as exc:
            _load_toml(aidp=_entry(default_headers={"Authorization": "Bearer sk-live-123"}))
        assert "sk-live-123" not in str(exc.value)

    def test_get_client_passes_the_headers(self) -> None:
        cfg = ModelConfig(
            alias="a",
            base_url="https://aidp.example/v1",
            api_key="k",
            model="m",
            provider="openai-compatible",
            default_headers={"x-team": "aiq"},
        )
        reg = ModelRegistry(models={"a": cfg}, default="a")
        client = reg.get_client("a")
        try:
            seen = _wire_headers(client, anthropic=False)
        finally:
            client.close()
        assert all(headers["x-team"] == "aiq" for headers in seen.values())

    def test_endpoint_probe_passes_the_headers(self) -> None:
        with patch("turnstone.core.providers.create_client") as factory:
            factory.return_value.with_options.return_value.models.list.side_effect = RuntimeError
            mr_module.probe_model_endpoint(
                "openai-compatible",
                "https://aidp.example/v1",
                "k",
                default_headers={"x-team": "aiq"},
            )
        assert factory.call_args.kwargs["default_headers"] == {"x-team": "aiq"}
