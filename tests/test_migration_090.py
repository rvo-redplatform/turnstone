"""Migration 090 adds the oauth_client_credentials and request-header columns to
``model_definitions``, and both storage backends carry them through create and update."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

_NEW_COLUMNS = {
    "oauth_token_url",
    "oauth_client_id",
    "oauth_client_secret",
    "default_headers",
    "session_headers",
}


def _columns(engine: sa.Engine) -> set[str]:
    return {column["name"] for column in sa.inspect(engine).get_columns("model_definitions")}


def _config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location",
        str(Path(__file__).resolve().parents[1] / "turnstone/core/storage/migrations"),
    )
    cfg.set_main_option("sqlalchemy.url", str(url).replace("%", "%%"))
    return cfg


def _roundtrip(url: str) -> None:
    cfg = _config(url)
    command.upgrade(cfg, "089")
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO model_definitions "
                    "(definition_id, alias, model, created, updated) "
                    "VALUES ('d1', 'legacy', 'gpt-5', '2026-01-01T00:00:00', "
                    "'2026-01-01T00:00:00')"
                )
            )
        assert not (_columns(engine) & _NEW_COLUMNS)

        command.upgrade(cfg, "090")
        assert _columns(engine) >= _NEW_COLUMNS
        with engine.connect() as conn:
            row = (
                conn.execute(
                    sa.text(
                        "SELECT oauth_token_url, oauth_client_id, oauth_client_secret, "
                        "default_headers, session_headers, auth_mode "
                        "FROM model_definitions WHERE definition_id = 'd1'"
                    )
                )
                .mappings()
                .one()
            )
        # The existing row is untouched: empty defaults, still static.
        assert dict(row) == {
            "oauth_token_url": "",
            "oauth_client_id": "",
            "oauth_client_secret": "",
            "default_headers": "{}",
            "session_headers": 0,
            "auth_mode": "static",
        }

        command.downgrade(cfg, "089")
        columns = _columns(engine)
        assert not (columns & _NEW_COLUMNS)
        assert {"auth_mode", "obo_audience", "obo_scopes"} <= columns
        with engine.connect() as conn:
            assert (
                conn.execute(
                    sa.text("SELECT alias FROM model_definitions WHERE definition_id = 'd1'")
                ).scalar_one()
                == "legacy"
            )

        command.upgrade(cfg, "090")
        assert _columns(engine) >= _NEW_COLUMNS
    finally:
        engine.dispose()


def test_model_oauth_columns_migration_sqlite(tmp_path: Path) -> None:
    _roundtrip(f"sqlite:///{tmp_path / 'model-oauth.db'}")


def test_model_oauth_columns_migration_postgresql(fresh_pg_url: Any) -> None:
    _roundtrip(fresh_pg_url.render_as_string(hide_password=False))


_HEADERS = json.dumps({"x-team": "aiq"})


def test_create_defaults_the_new_columns(storage_backend: Any) -> None:
    storage_backend.create_model_definition(definition_id="d0", alias="plain", model="gpt-5")
    row = storage_backend.get_model_definition("d0")
    assert row is not None
    assert row["oauth_token_url"] == ""
    assert row["oauth_client_id"] == ""
    assert row["oauth_client_secret"] == ""
    assert row["default_headers"] == "{}"
    assert row["session_headers"] is False


def test_create_and_update_round_trip_the_new_columns(storage_backend: Any) -> None:
    storage_backend.create_model_definition(
        definition_id="d1",
        alias="aidp",
        model="aiq-agent",
        provider="openai-compatible",
        base_url="https://aidp.example.com/v1",
        auth_mode="oauth_client_credentials",
        obo_scopes="ai-gateway/invoke",
        oauth_token_url="https://pool.auth.us-east-1.amazoncognito.com/oauth2/token",
        oauth_client_id="client-1",
        oauth_client_secret="secret://file/aidp-client-secret",
        default_headers=_HEADERS,
        session_headers=True,
    )
    row = storage_backend.get_model_definition_by_alias("aidp")
    assert row is not None
    assert row["oauth_token_url"] == "https://pool.auth.us-east-1.amazoncognito.com/oauth2/token"
    assert row["oauth_client_id"] == "client-1"
    assert row["oauth_client_secret"] == "secret://file/aidp-client-secret"
    assert json.loads(row["default_headers"]) == {"x-team": "aiq"}
    assert row["session_headers"] is True

    assert storage_backend.update_model_definition(
        "d1",
        oauth_token_url="https://other.example.com/oauth2/token",
        oauth_client_id="client-2",
        oauth_client_secret="literal-secret",
        default_headers="{}",
        session_headers=False,
    )
    row = storage_backend.get_model_definition("d1")
    assert row is not None
    assert row["oauth_token_url"] == "https://other.example.com/oauth2/token"
    assert row["oauth_client_id"] == "client-2"
    assert row["oauth_client_secret"] == "literal-secret"
    assert row["default_headers"] == "{}"
    assert row["session_headers"] is False
    # Untouched columns survive a partial update.
    assert row["obo_scopes"] == "ai-gateway/invoke"
    assert row["auth_mode"] == "oauth_client_credentials"

    [listed] = storage_backend.list_model_definitions()
    assert listed["session_headers"] is False
    assert listed["oauth_client_id"] == "client-2"
