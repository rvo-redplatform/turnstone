"""Add oauth_client_credentials auth and request-header columns to model_definitions.

The ``oauth_client_credentials`` auth mode mints an app token from a
per-definition OAuth2 token endpoint (for example a Cognito user pool)
instead of the single global ``[oidc]`` app that ``entra_app`` uses:

- ``oauth_token_url``: the token endpoint the client-credentials grant posts to.
- ``oauth_client_id``: the confidential client.
- ``oauth_client_secret``: a ``secret://`` reference, or a literal masked as
  ``***`` on output (stored like ``api_key``). The scope reuses ``obo_scopes``.

Two request-header columns ride along:

- ``default_headers``: a JSON object of extra headers sent on every call.
- ``session_headers``: 0/1 switch; when on, every call of a run sends the
  run's session headers.

Empty defaults (``""``, ``"{}"``, ``0``) keep every existing row untouched.

Revision ID: 090
Revises: 089
Create Date: 2026-11-20
"""

import sqlalchemy as sa
from alembic import op

revision = "090"
down_revision = "089"
branch_labels = None
depends_on = None

_TEXT_COLUMNS = (
    ("oauth_token_url", ""),
    ("oauth_client_id", ""),
    ("oauth_client_secret", ""),
    ("default_headers", "{}"),
)


def upgrade() -> None:
    for name, default in _TEXT_COLUMNS:
        op.add_column(
            "model_definitions",
            sa.Column(name, sa.Text, nullable=False, server_default=default),
        )
    op.add_column(
        "model_definitions",
        sa.Column("session_headers", sa.Integer, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("model_definitions", "session_headers")
    for name, _default in reversed(_TEXT_COLUMNS):
        op.drop_column("model_definitions", name)
