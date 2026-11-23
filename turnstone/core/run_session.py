"""Run session headers: one id for every model call of a run.

A definition with ``session_headers`` on sends, on every call a workstream
makes with it (judge calls included):

- ``x-switchyard-session-id`` and ``agent-session-id``: the run id;
- on a child workstream only, ``x-switchyard-agent-id`` (the child's own
  workstream id) and ``x-switchyard-is-subagent: true``.

The run id is the root (coordinator) workstream's id, unless the create body
set ``session_id``. Children inherit the run id from their parent through
``parent_ws_id``; the session persists it in the workstream config under
:data:`RUN_SESSION_ID_CONFIG_KEY` so a child and a rehydrated workstream find
the same value.

The headers are applied by :func:`~turnstone.core.model_turn.lane_call_client`
on the per-call client, so every request of a lane, including the re-sent
request after a backend 401, carries the same values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AGENT_SESSION_ID_HEADER",
    "IS_SUBAGENT_HEADER",
    "RUN_SESSION_HEADER_NAMES",
    "RUN_SESSION_ID_CONFIG_KEY",
    "RUN_SESSION_ID_MAX_LEN",
    "SESSION_ID_HEADER",
    "SUBAGENT_ID_HEADER",
    "RunIdentity",
    "RunSessionIdError",
    "resolve_run_identity",
    "session_request_headers",
    "validate_run_session_id",
]

SESSION_ID_HEADER = "x-switchyard-session-id"
AGENT_SESSION_ID_HEADER = "agent-session-id"
SUBAGENT_ID_HEADER = "x-switchyard-agent-id"
IS_SUBAGENT_HEADER = "x-switchyard-is-subagent"

RUN_SESSION_HEADER_NAMES: frozenset[str] = frozenset(
    {SESSION_ID_HEADER, AGENT_SESSION_ID_HEADER, SUBAGENT_ID_HEADER, IS_SUBAGENT_HEADER}
)

# Workstream config key holding a run id that is not the workstream's own id.
RUN_SESSION_ID_CONFIG_KEY = "run_session_id"

RUN_SESSION_ID_MAX_LEN = 128
# A UUID, a hex workstream id, or a similar opaque token: safe in a header
# value and in a log line without quoting.
_RUN_SESSION_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]*")


class RunSessionIdError(ValueError):
    """A create body's ``session_id`` is not a usable run id."""


def validate_run_session_id(value: object) -> str:
    """Return the run id a create body asks for, ``""`` when it asks for none.

    ``None`` and ``""`` mean no override. Anything else must be a string of
    1 to :data:`RUN_SESSION_ID_MAX_LEN` characters from ``A-Z a-z 0-9 . _ : -``
    starting with a letter or digit, or :class:`RunSessionIdError` is raised.
    """
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise RunSessionIdError("session_id must be a string")
    if len(value) > RUN_SESSION_ID_MAX_LEN:
        raise RunSessionIdError(f"session_id holds at most {RUN_SESSION_ID_MAX_LEN} characters")
    if _RUN_SESSION_ID_RE.fullmatch(value) is None:
        raise RunSessionIdError(
            "session_id may hold only letters, digits, '.', '_', ':' and '-', "
            "and must start with a letter or digit"
        )
    return value


@dataclass(frozen=True)
class RunIdentity:
    """Who a workstream's model calls belong to.

    *session_id* is the run id. *agent_id* is the workstream's own id on a
    child workstream, ``""`` on a root one.
    """

    session_id: str
    agent_id: str = ""

    @property
    def is_subagent(self) -> bool:
        return bool(self.agent_id)

    def headers(self) -> dict[str, str]:
        """The run session headers, without checking any definition's switch."""
        if not self.session_id:
            return {}
        out = {SESSION_ID_HEADER: self.session_id, AGENT_SESSION_ID_HEADER: self.session_id}
        if self.agent_id:
            out[SUBAGENT_ID_HEADER] = self.agent_id
            out[IS_SUBAGENT_HEADER] = "true"
        return out


def resolve_run_identity(
    *,
    ws_id: str,
    parent_ws_id: str | None,
    override: str = "",
    saved: str = "",
    parent_saved: str = "",
) -> RunIdentity:
    """Work out a workstream's run identity.

    The run id is, first match wins: the create body's *override*; the value
    the workstream saved earlier (*saved*, on rehydrate); on a child, the
    parent's saved run id (*parent_saved*), else the parent's id; on a root,
    its own id. A child always carries its own *ws_id* as its agent id.
    """
    parent = parent_ws_id or ""
    if override:
        session_id = override
    elif saved:
        session_id = saved
    elif parent:
        session_id = parent_saved or parent
    else:
        session_id = ws_id
    return RunIdentity(session_id=session_id, agent_id=ws_id if parent else "")


def session_request_headers(cfg: Any, identity: RunIdentity | None) -> dict[str, str]:
    """The headers a call on a lane bound to *cfg* sends for *identity*.

    Empty unless *cfg* (the lane's model definition) has ``session_headers``
    set to ``True`` and the lane carries an identity.
    """
    if identity is None or cfg is None or getattr(cfg, "session_headers", False) is not True:
        return {}
    return identity.headers()
