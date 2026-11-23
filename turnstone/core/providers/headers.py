"""Which extra request headers a model definition may send on every call.

A definition's ``default_headers`` become the SDK client's default headers, so
they ride every request the client makes. This module is the one rule for what
that map may hold; the registry applies it when it loads a definition and
:func:`~turnstone.core.providers.create_client` applies it again when it builds
the client.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from turnstone.core.http_headers import HEADER_VALUE_RE
from turnstone.core.secret_refs._reference import REFERENCE_SCHEME

__all__ = [
    "DEFAULT_HEADERS_MAX_COUNT",
    "DEFAULT_HEADER_NAME_MAX_LEN",
    "DEFAULT_HEADER_VALUE_MAX_LEN",
    "RESERVED_DEFAULT_HEADER_NAMES",
    "DefaultHeadersError",
    "validate_default_headers",
]

# Header names (lower case) a definition's ``default_headers`` may not set.
#
# - Credentials: the backend credential is the auth mode's job. A static
#   header would bypass every gate on it, and both SDKs merge client headers
#   over their own, so an ``Authorization`` here would replace the minted
#   token on every call.
# - Sent by Turnstone: a client-level default would be overwritten by (or
#   collide with) the value Turnstone sets per client or per request.
# - Transport: owned by the HTTP client; a wrong value breaks framing.
RESERVED_DEFAULT_HEADER_NAMES: frozenset[str] = frozenset(
    {
        # Credentials.
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
        "x-auth-token",
        "x-amz-security-token",
        "cookie",
        # Sent by Turnstone (or the SDK on its behalf).
        "anthropic-workspace-id",
        "anthropic-beta",
        "anthropic-version",
        "x-switchyard-session-id",
        "x-switchyard-agent-id",
        "x-switchyard-is-subagent",
        "agent-session-id",
        # Transport.
        "host",
        "content-length",
        "content-type",
        "content-encoding",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "expect",
    }
)

DEFAULT_HEADERS_MAX_COUNT = 32
DEFAULT_HEADER_NAME_MAX_LEN = 128
DEFAULT_HEADER_VALUE_MAX_LEN = 2048

# RFC 9110 field-name token.
_HEADER_NAME_RE = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class DefaultHeadersError(ValueError):
    """A ``default_headers`` map breaks the rule; the message never quotes a value."""


def validate_default_headers(headers: object) -> dict[str, str]:
    """*headers* as a plain dict a client can send, or :class:`DefaultHeadersError`.

    The map must hold at most :data:`DEFAULT_HEADERS_MAX_COUNT` entries. Each
    name must be an RFC 9110 token of at most
    :data:`DEFAULT_HEADER_NAME_MAX_LEN` characters, not in
    :data:`RESERVED_DEFAULT_HEADER_NAMES` and not repeated in another case
    (names compare case-insensitively). Each value must be a string of at most
    :data:`DEFAULT_HEADER_VALUE_MAX_LEN` characters that an HTTP header can
    carry (:data:`~turnstone.core.http_headers.HEADER_VALUE_RE`), and not a
    ``secret://`` reference. ``None`` is the empty map.
    """
    if headers is None:
        return {}
    if not isinstance(headers, Mapping):
        raise DefaultHeadersError("default_headers must be an object of header names to values")
    if len(headers) > DEFAULT_HEADERS_MAX_COUNT:
        raise DefaultHeadersError(f"default_headers holds at most {DEFAULT_HEADERS_MAX_COUNT}")
    checked: dict[str, str] = {}
    seen: set[str] = set()
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or len(name) > DEFAULT_HEADER_NAME_MAX_LEN
            or not _HEADER_NAME_RE.fullmatch(name)
        ):
            raise DefaultHeadersError(
                f"default_headers: {str(name)[:64]!r} is not a valid header name"
            )
        lowered = name.lower()
        if lowered in RESERVED_DEFAULT_HEADER_NAMES:
            raise DefaultHeadersError(
                f"default_headers: {name!r} is set by Turnstone or carries a credential; "
                "use the auth mode for credentials"
            )
        if lowered in seen:
            raise DefaultHeadersError(f"default_headers: {name!r} appears more than once")
        seen.add(lowered)
        if not isinstance(value, str):
            raise DefaultHeadersError(f"default_headers: {name!r} must have a string value")
        if len(value) > DEFAULT_HEADER_VALUE_MAX_LEN:
            raise DefaultHeadersError(
                f"default_headers: {name!r} value exceeds {DEFAULT_HEADER_VALUE_MAX_LEN} characters"
            )
        if not HEADER_VALUE_RE.fullmatch(value):
            raise DefaultHeadersError(
                f"default_headers: {name!r} value has a space at either end, a control "
                "character or a character outside ASCII"
            )
        if REFERENCE_SCHEME in value:
            # Sent verbatim, never resolved: a reference would go out as text.
            raise DefaultHeadersError(
                f"default_headers: {name!r} value cannot be a secret:// reference"
            )
        checked[name] = value
    return checked
