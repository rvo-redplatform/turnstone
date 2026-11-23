"""Run session headers (AI-DP-3012): one run id on every model call of a run.

Covers the identity rules (coordinator, child, create-body override, saved
value), the per-call header application in ``lane_call_client`` (switch on and
off, the 401 re-mint retry), judge calls, and the create handler's
``session_id`` body field.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tests._session_helpers import as_stream, make_session
from turnstone.core.judge import IntentJudge, JudgeConfig, resolve_judge_binding
from turnstone.core.model_registry import ModelConfig, ModelRegistry
from turnstone.core.model_turn import (
    ModelLane,
    ResolvedModelBinding,
    lane_call_client,
    model_turn,
    resolve_model_binding,
)
from turnstone.core.output_guard_judge import OutputGuardJudge
from turnstone.core.providers.headers import RESERVED_DEFAULT_HEADER_NAMES
from turnstone.core.run_session import (
    RUN_SESSION_HEADER_NAMES,
    RUN_SESSION_ID_CONFIG_KEY,
    RunIdentity,
    RunSessionIdError,
    resolve_run_identity,
    session_request_headers,
    validate_run_session_id,
)
from turnstone.core.trajectory import Turn
from turnstone.core.workstream import WorkstreamKind

ALIAS = "aidp-agent"
RUN_UUID = "6f1c2a9e-1b7d-4c55-9a51-0d3e7f8b2c10"


def _cfg(*, session_headers: bool = True, **extra: Any) -> ModelConfig:
    return ModelConfig(
        alias=ALIAS,
        base_url="https://aidp.example.com/v1",
        api_key="static-key",
        model="aiq-agent",
        provider="openai",
        session_headers=session_headers,
        **extra,
    )


def _registry(cfg: ModelConfig) -> ModelRegistry:
    return ModelRegistry(models={cfg.alias: cfg}, default=cfg.alias)


def _recording_client() -> tuple[MagicMock, list[dict[str, Any]]]:
    """A registry client whose ``with_options`` copies record their options."""
    calls: list[dict[str, Any]] = []
    client = MagicMock(name="registry-client")

    def _with_options(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            api_key=kwargs.get("api_key"), headers=dict(kwargs.get("default_headers") or {})
        )

    client.with_options.side_effect = _with_options
    return client, calls


def _session_kwargs(cfg: ModelConfig, **kwargs: Any) -> dict[str, Any]:
    registry = _registry(cfg)
    binding = resolve_model_binding(registry, cfg.alias)
    return {
        "registry": registry,
        "model_alias": cfg.alias,
        "model_binding": binding,
        "client": binding.lane.client,
        "model": binding.lane.model,
        **kwargs,
    }


# ---------------------------------------------------------------------------
# Identity rules
# ---------------------------------------------------------------------------


class TestRunIdentity:
    def test_root_uses_its_own_id_and_sends_no_agent_headers(self) -> None:
        identity = resolve_run_identity(ws_id="coord-1", parent_ws_id=None)
        assert identity == RunIdentity("coord-1")
        assert identity.headers() == {
            "x-switchyard-session-id": "coord-1",
            "agent-session-id": "coord-1",
        }

    def test_child_shares_the_parent_run_and_names_itself(self) -> None:
        identity = resolve_run_identity(ws_id="child-1", parent_ws_id="coord-1")
        assert identity.headers() == {
            "x-switchyard-session-id": "coord-1",
            "agent-session-id": "coord-1",
            "x-switchyard-agent-id": "child-1",
            "x-switchyard-is-subagent": "true",
        }

    def test_child_takes_the_parents_saved_run_id(self) -> None:
        identity = resolve_run_identity(
            ws_id="child-1", parent_ws_id="coord-1", parent_saved=RUN_UUID
        )
        assert identity == RunIdentity(RUN_UUID, "child-1")

    def test_override_beats_saved_and_saved_beats_parent(self) -> None:
        assert resolve_run_identity(
            ws_id="c", parent_ws_id=None, override=RUN_UUID, saved="old"
        ) == RunIdentity(RUN_UUID)
        assert resolve_run_identity(
            ws_id="k", parent_ws_id="p", saved="kept", parent_saved="other"
        ) == RunIdentity("kept", "k")

    def test_run_header_names_are_reserved_from_default_headers(self) -> None:
        assert RUN_SESSION_HEADER_NAMES <= RESERVED_DEFAULT_HEADER_NAMES

    @pytest.mark.parametrize("value", [None, ""])
    def test_no_override(self, value: object) -> None:
        assert validate_run_session_id(value) == ""

    @pytest.mark.parametrize("value", [RUN_UUID, "run_42", "a", "x" * 128, "ns:run.1"])
    def test_valid_override(self, value: str) -> None:
        assert validate_run_session_id(value) == value

    @pytest.mark.parametrize("value", [42, ["x"], "x" * 129, "-lead", "has space", "a\r\nb", "é"])
    def test_invalid_override(self, value: object) -> None:
        with pytest.raises(RunSessionIdError):
            validate_run_session_id(value)


# ---------------------------------------------------------------------------
# Per-call application
# ---------------------------------------------------------------------------


class TestLaneCallClient:
    def _lane(self, cfg: ModelConfig | None, identity: RunIdentity | None) -> ModelLane:
        client, _calls = _recording_client()
        return ModelLane(
            provider=MagicMock(),
            client=client,
            model="aiq-agent",
            alias=ALIAS,
            backend_auth_config=cfg,
            run_identity=identity,
        )

    def test_switch_on_sends_the_headers(self) -> None:
        lane = self._lane(_cfg(), RunIdentity("coord-1", "child-1"))
        assert lane_call_client(lane).headers == RunIdentity("coord-1", "child-1").headers()

    def test_switch_off_sends_none(self) -> None:
        lane = self._lane(_cfg(session_headers=False), RunIdentity("coord-1"))
        assert lane_call_client(lane) is lane.client
        lane.client.with_options.assert_not_called()

    def test_no_definition_or_no_identity_sends_none(self) -> None:
        assert session_request_headers(None, RunIdentity("x")) == {}
        assert session_request_headers(_cfg(), None) == {}
        lane = self._lane(_cfg(), None)
        assert lane_call_client(lane) is lane.client

    def test_headers_merge_over_the_definitions_default_headers(self) -> None:
        """Real SDK clients: the per-call copy keeps the client's own headers."""
        from turnstone.core.providers import create_client

        for provider in ("openai", "anthropic"):
            client = create_client(
                provider,
                base_url="https://aidp.example.com/v1",
                api_key="k",
                default_headers={"x-team": "aiq"},
            )
            try:
                lane = ModelLane(
                    provider=MagicMock(),
                    client=client,
                    model="m",
                    alias=ALIAS,
                    backend_auth_config=_cfg(),
                    run_identity=RunIdentity("coord-1", "child-1"),
                )
                sent = lane_call_client(lane, backend_auth_token="minted").default_headers
                assert sent["x-team"] == "aiq"
                assert sent["x-switchyard-session-id"] == "coord-1"
                assert sent["agent-session-id"] == "coord-1"
                assert sent["x-switchyard-agent-id"] == "child-1"
                assert sent["x-switchyard-is-subagent"] == "true"
                # The shared registry client is untouched.
                assert "x-switchyard-session-id" not in client.default_headers
            finally:
                client.close()


class _Status401Error(Exception):
    status_code = 401


class TestBackendAuthRetry:
    def test_the_401_retry_resends_identical_session_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from turnstone.core import model_backend_auth
        from turnstone.core.providers._protocol import CompletionResult

        monkeypatch.setattr(model_backend_auth, "note_backend_auth_rejected", lambda *a: True)
        seen: list[Any] = []

        class _Provider:
            provider_name = "openai-compatible"

            def create_streaming(self, **kwargs: Any) -> Any:
                seen.append(kwargs["client"])
                if len(seen) == 1:
                    raise _Status401Error("unauthorized")
                return as_stream(CompletionResult(content="ok"))

        client, _calls = _recording_client()
        lane = ModelLane(
            provider=_Provider(),  # type: ignore[arg-type]
            client=client,
            model="aiq-agent",
            alias=ALIAS,
            backend_auth_resolver=MagicMock(side_effect=["stale", "fresh"]),
            backend_auth_config=_cfg(auth_mode="oauth_client_credentials"),
            run_identity=RunIdentity("coord-1", "child-1"),
        )
        model_turn(lane, [Turn.user("hi")])
        assert [c.api_key for c in seen] == ["stale", "fresh"]
        assert seen[0].headers == seen[1].headers == RunIdentity("coord-1", "child-1").headers()


# ---------------------------------------------------------------------------
# ChatSession: coordinator, children, override, persistence
# ---------------------------------------------------------------------------


class TestSessionIdentity:
    def test_coordinator_and_child_calls_carry_the_same_session_id(self, tmp_db: Any) -> None:
        cfg = _cfg()
        coord = make_session(
            **_session_kwargs(cfg, ws_id="coord-1", user_id="u1", kind=WorkstreamKind.COORDINATOR)
        )
        child = make_session(**_session_kwargs(cfg, ws_id="child-1", parent_ws_id="coord-1"))
        coord_headers = session_request_headers(cfg, coord._primary_lane().run_identity)
        child_headers = session_request_headers(cfg, child._primary_lane().run_identity)
        assert coord_headers == {
            "x-switchyard-session-id": "coord-1",
            "agent-session-id": "coord-1",
        }
        assert child_headers["x-switchyard-session-id"] == "coord-1"
        assert child_headers["agent-session-id"] == "coord-1"
        # Each child carries its own agent id.
        assert child_headers["x-switchyard-agent-id"] == "child-1"
        assert child_headers["x-switchyard-is-subagent"] == "true"
        other = make_session(**_session_kwargs(cfg, ws_id="child-2", parent_ws_id="coord-1"))
        assert other._primary_lane().run_identity == RunIdentity("coord-1", "child-2")

    def test_create_body_override_reaches_the_children(self, tmp_db: Any) -> None:
        from turnstone.core.memory import save_workstream_config

        cfg = _cfg()
        coord = make_session(
            **_session_kwargs(
                cfg,
                ws_id="coord-1",
                user_id="u1",
                kind=WorkstreamKind.COORDINATOR,
                run_session_id=RUN_UUID,
            )
        )
        assert coord._primary_lane().run_identity == RunIdentity(RUN_UUID)
        saved = coord._config_for_save()
        assert saved[RUN_SESSION_ID_CONFIG_KEY] == RUN_UUID
        # The coordinator's construction save, as a manager-built session makes it.
        save_workstream_config("coord-1", saved)
        child = make_session(**_session_kwargs(cfg, ws_id="child-1", parent_ws_id="coord-1"))
        assert child._primary_lane().run_identity == RunIdentity(RUN_UUID, "child-1")
        assert child._config_for_save()[RUN_SESSION_ID_CONFIG_KEY] == RUN_UUID

    def test_root_default_is_not_saved(self, tmp_db: Any) -> None:
        session = make_session(**_session_kwargs(_cfg(), ws_id="ws-1"))
        assert RUN_SESSION_ID_CONFIG_KEY not in session._config_for_save()

    def test_rehydrate_keeps_the_saved_run_id(self, tmp_db: Any) -> None:
        from turnstone.core.memory import save_workstream_config

        save_workstream_config("ws-1", {"model_alias": ALIAS, RUN_SESSION_ID_CONFIG_KEY: RUN_UUID})
        session = make_session(**_session_kwargs(_cfg(), ws_id="ws-1"))
        assert session._primary_lane().run_identity == RunIdentity(RUN_UUID)

    def test_a_rebind_keeps_the_identity(self, tmp_db: Any) -> None:
        cfg = _cfg()
        session = make_session(**_session_kwargs(cfg, ws_id="child-1", parent_ws_id="coord-1"))
        with session._model_binding_lock:
            session._publish_binding_locked(
                resolve_model_binding(session._registry, ALIAS), refill_limiter=False
            )
        assert session._primary_lane().run_identity == RunIdentity("coord-1", "child-1")

    def test_session_model_call_sends_the_headers(self, tmp_db: Any) -> None:
        """A real primary-lane call: the client the provider is handed carries them."""
        from turnstone.core.providers._protocol import CompletionResult

        cfg = _cfg()
        session = make_session(**_session_kwargs(cfg, ws_id="child-1", parent_ws_id="coord-1"))
        client, _calls = _recording_client()
        provider = MagicMock()
        provider.create_streaming.return_value = as_stream(CompletionResult(content="ok"))
        lane = dataclasses.replace(session._primary_lane(), client=client, provider=provider)
        model_turn(lane, [Turn.user("hi")])
        sent = provider.create_streaming.call_args.kwargs["client"]
        assert sent.headers == RunIdentity("coord-1", "child-1").headers()


# ---------------------------------------------------------------------------
# Judges
# ---------------------------------------------------------------------------


def _stamped_binding(cfg: ModelConfig, identity: RunIdentity | None) -> ResolvedModelBinding:
    binding = resolve_model_binding(_registry(cfg), cfg.alias)
    return dataclasses.replace(
        binding, lane=dataclasses.replace(binding.lane, run_identity=identity)
    )


class TestJudgeCalls:
    @pytest.mark.parametrize("alias", ["", ALIAS])
    def test_judge_binding_carries_the_run_identity(self, alias: str) -> None:
        identity = RunIdentity("coord-1", "child-1")
        binding = resolve_judge_binding(alias, _stamped_binding(_cfg(), identity), None)
        assert binding.lane.run_identity == identity

    def test_intent_judge_call_carries_session_and_default_headers(self) -> None:
        cfg = _cfg(default_headers={"x-team": "aiq"})
        identity = RunIdentity("coord-1", "child-1")
        judge = IntentJudge(
            config=JudgeConfig(enabled=True, read_only_tools=False),
            session_binding=_stamped_binding(cfg, identity),
        )
        assert judge._client_factory_args["default_headers"] == {"x-team": "aiq"}
        rebuilt = judge._create_client()
        try:
            # The judge's per-evaluation lane is ``replace(self._lane, client=...)``.
            lane = dataclasses.replace(judge._lane, client=rebuilt)
            sent = lane_call_client(lane).default_headers
            assert sent["x-team"] == "aiq"
            assert sent["x-switchyard-session-id"] == "coord-1"
            assert sent["agent-session-id"] == "coord-1"
            assert sent["x-switchyard-agent-id"] == "child-1"
            assert sent["x-switchyard-is-subagent"] == "true"
        finally:
            rebuilt.close()

    def test_output_guard_judge_call_carries_session_and_default_headers(self) -> None:
        cfg = _cfg(default_headers={"x-team": "aiq"})
        guard = OutputGuardJudge(
            config=JudgeConfig(enabled=True, read_only_tools=False),
            session_binding=_stamped_binding(cfg, RunIdentity("coord-1")),
        )
        try:
            assert guard._client_factory_args["default_headers"] == {"x-team": "aiq"}
            rebuilt = guard._create_client()
            lane = dataclasses.replace(guard._lane, client=rebuilt)
            sent = lane_call_client(lane).default_headers
            assert sent["x-team"] == "aiq"
            assert sent["x-switchyard-session-id"] == "coord-1"
            assert "x-switchyard-agent-id" not in sent
        finally:
            guard.retire()

    def test_judge_with_switch_off_sends_none(self) -> None:
        judge = IntentJudge(
            config=JudgeConfig(enabled=True, read_only_tools=False),
            session_binding=_stamped_binding(_cfg(session_headers=False), RunIdentity("c")),
        )
        rebuilt = judge._create_client()
        try:
            lane = dataclasses.replace(judge._lane, client=rebuilt)
            assert lane_call_client(lane) is rebuilt
            assert "x-switchyard-session-id" not in rebuilt.default_headers
        finally:
            rebuilt.close()


# ---------------------------------------------------------------------------
# Create handler: the ``session_id`` body field
# ---------------------------------------------------------------------------


def _create(monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]) -> tuple[dict[str, Any], Any]:
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from turnstone.core.session_routes import SessionEndpointConfig, make_create_handler

    mgr = MagicMock()
    seen: dict[str, Any] = {}

    def _mgr_create(**kwargs: Any) -> Any:
        seen.update(kwargs)
        raise RuntimeError("stop")

    mgr.create.side_effect = _mgr_create
    cfg = SessionEndpointConfig(
        permission_gate=None,
        manager_lookup=lambda _request: (mgr, None),
        tenant_check=None,
        not_found_label="Workstream not found",
        audit_action_prefix="workstream",
        create_build_kwargs=lambda _r, _body, uid, *_a: {"user_id": uid},
    )
    app = Starlette(routes=[Route("/new", make_create_handler(cfg), methods=["POST"])])
    with TestClient(app) as client:
        resp = client.post("/new", json=body)
    return seen, resp


class TestCreateBody:
    def test_session_id_reaches_the_session_factory_kwargs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen, _resp = _create(monkeypatch, {"session_id": RUN_UUID})
        assert seen["run_session_id"] == RUN_UUID

    def test_absent_session_id_passes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen, _resp = _create(monkeypatch, {})
        assert "run_session_id" not in seen

    def test_invalid_session_id_is_refused_before_create(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen, resp = _create(monkeypatch, {"session_id": "bad value\r\n"})
        assert resp.status_code == 400
        assert "session_id" in resp.json()["error"]
        assert seen == {}
