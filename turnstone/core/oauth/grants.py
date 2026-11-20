"""Delegated and application OAuth grants and the operative profile registry."""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING, Any

from turnstone.core.log import get_logger
from turnstone.core.oauth import http as oauth_http

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import httpx2

log = get_logger(__name__)


async def refresh_token(
    *,
    as_metadata: oauth_http.ASMetadata,
    refresh_token_value: str,
    client_id: str,
    client_secret: str | None,
    resource: str,
    scopes: str,
    http_client: httpx2.AsyncClient,
) -> dict[str, Any]:
    """Refresh an access token via the ``refresh_token`` grant.

    Raises :class:`OAuthRefreshError` with the classified token-endpoint failure.
    """
    data: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token_value,
        "client_id": client_id,
    }
    if client_secret:
        data["client_secret"] = client_secret
    if resource:
        data["resource"] = resource
    if scopes:
        data["scope"] = scopes

    return await oauth_http._hardened_token_post(
        token_endpoint=as_metadata.token_endpoint,
        data=data,
        http_client=http_client,
        request_label="refresh",
        endpoint_label="refresh endpoint",
    )


# ---------------------------------------------------------------------------
# Single-credential on-behalf-of minting (auth_type='oauth_obo', issue #551)
#
# Servers with auth_type='oauth_obo' never run the per-server browser consent
# flow.  Instead the user's single captured IdP refresh token (see
# `[oidc] capture_user_credential`) is redeemed on demand for a short-lived
# server-audience access token via the deployment's grant leg:
#
#   entra    — one refresh-token grant with scope=<audience>/.default
#              (Entra RTs are client-bound, not resource-bound — verified)
#   rfc8693  — refresh grant for a subject token, then a standard
#              token-exchange with audience=<server client id> (verified on
#              Keycloak 26.3; per-server oauth_scopes activates optional
#              audience scopes)
#
# The minted token is cached in the existing per-(user, server)
# oauth_tokens row with refresh_token_ct=NULL — cache, not custody.  A
# permanent mint failure drops ONLY that cache row (re-consent UX for that
# server); the shared credential is NEVER auto-deleted here — a missing
# tenant grant for one server (AADSTS65001, verified) must not lock the user
# out of every other OBO server.  Credential lifecycle (logout/admin revoke)
# is handled elsewhere.
# ---------------------------------------------------------------------------


async def _obo_token_post(
    *,
    token_endpoint: str,
    data: dict[str, str],
    http_client: httpx2.AsyncClient,
    leg: str,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """POST one OBO grant-leg request; classify failures like a refresh.

    Label-binding wrapper over :func:`_hardened_token_post` (the shared
    body-size-cap / JSON-object-validation / conservative
    :func:`_classify_refresh_failure` skeleton), so the OBO state machine
    reacts to AS rejections exactly like the oauth_user one — a verified
    AADSTS65001 (missing tenant grant) classifies PERMANENT via
    ``invalid_grant``.
    """
    label = f"obo {leg}"
    return await oauth_http._hardened_token_post(
        token_endpoint=token_endpoint,
        data=data,
        http_client=http_client,
        request_label=label,
        endpoint_label=label,
        # OBO: an over-sized client-error body escalates (AMBIGUOUS) rather than
        # looping "please retry" — see _hardened_token_post. (oauth_user keeps
        # the TRANSIENT default.)
        classify_oversized_by_status=True,
        headers=headers,
    )


# A leg persists a rotated CREDENTIAL refresh token the instant it obtains one,
# via a caller-supplied ``persist_rotation`` callback bound to the credential
# under the held lock. It is the ONLY channel by which a mint updates the stored
# credential — the returned access-token dict's own ``refresh_token`` (if any) is
# never written back to the credential, so an audience-scoped exchange RT (RFC
# 8693 §2.2.1) cannot poison it.


async def _maybe_persist_rotation(
    resp: dict[str, Any],
    credential_refresh_token: str,
    persist_rotation: Callable[[str], Awaitable[None]],
) -> None:
    """Persist a rotated credential RT from *resp* when it differs from the current one."""
    rotated = resp.get("refresh_token")
    if isinstance(rotated, str) and rotated and rotated != credential_refresh_token:
        await persist_rotation(rotated)


#: Audiences already warned about ignored entra scopes — once per audience per
#: process (see _obo_mint_entra). One of the module's _warn_dedup_once
#: namespaces; the mechanics live with that helper.
_ENTRA_SCOPE_IGNORED_WARNED: set[str] = set()


async def _obo_mint_entra(
    *,
    oidc_config: Any,
    credential_refresh_token: str,
    audience: str,
    scopes: str,
    http_client: httpx2.AsyncClient,
    persist_rotation: Callable[[str], Awaitable[None]],
) -> dict[str, Any]:
    """Entra leg: redeem the client-bound RT directly for the audience.

    Wire shape verified against a real tenant (docs/design/obo-spike):
    ``grant_type=refresh_token`` + ``scope=<audience>/.default`` returns an
    audience-scoped access token and (usually) a rotated refresh token.

    ``scope`` is Entra's ONLY audience carrier, so it always pins
    ``<audience>/.default`` — the pre-consented-delegated-permissions model the
    feature targets. Per-server ``oauth_scopes`` do NOT apply here (a bare scope
    list would drop the audience and yield a wrong-audience token); they are a
    ``rfc8693``-only knob.
    """
    if scopes:
        # Entra ignores oauth_scopes (it pins <audience>/.default), so a
        # configured scope restriction silently does not apply on this
        # credential-minting path. The admin write path rejects NEW
        # scopes-with-entra, but a deployment-level profile switch
        # (rfc8693→entra) leaves pre-existing scoped rows — surface that ONCE
        # per audience per process (not per mint) so it's visible at default log
        # levels without flooding.
        _warn_dedup_once(
            _ENTRA_SCOPE_IGNORED_WARNED,
            audience,
            "mcp_server.oauth.obo_entra_scopes_ignored",
            audience=audience,
            hint=(
                "oauth_scopes is not applied on the entra grant leg (it mints "
                "<audience>/.default); clear oauth_scopes or use the rfc8693 profile"
            ),
        )
    resp = await _obo_token_post(
        token_endpoint=oidc_config.token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": credential_refresh_token,
            "client_id": oidc_config.client_id,
            "client_secret": oidc_config.client_secret,
            "scope": f"{audience}/.default",
        },
        http_client=http_client,
        leg="entra-redemption",
    )
    await _maybe_persist_rotation(resp, credential_refresh_token, persist_rotation)
    return resp


async def _obo_mint_rfc8693(
    *,
    oidc_config: Any,
    credential_refresh_token: str,
    audience: str,
    scopes: str,
    http_client: httpx2.AsyncClient,
    persist_rotation: Callable[[str], Awaitable[None]],
) -> dict[str, Any]:
    """RFC 8693 leg: refresh grant for a subject token, then token exchange.

    Chain verified on Keycloak 26.3 standard token exchange
    (docs/design/obo-spike). Rotation ordering is correctness-critical: the
    refresh leg may consume-and-rotate the credential RT, so its rotated value
    is persisted IMMEDIATELY (before the exchange leg) — if the exchange then
    fails, the stored credential already holds the live rotated RT rather than a
    consumed one (else the next mint for every obo server would fail and lock the
    user out). The exchange response's own ``refresh_token`` (RFC 8693 §2.2.1
    permits one, audience-scoped) is deliberately NOT persisted to the credential.
    """
    subject = await _obo_token_post(
        token_endpoint=oidc_config.token_endpoint,
        data={
            "grant_type": "refresh_token",
            "refresh_token": credential_refresh_token,
            "client_id": oidc_config.client_id,
            "client_secret": oidc_config.client_secret,
        },
        http_client=http_client,
        leg="rfc8693-refresh",
    )
    # Persist the credential rotation BEFORE the exchange call can fail.
    await _maybe_persist_rotation(subject, credential_refresh_token, persist_rotation)

    subject_at = subject.get("access_token")
    if not isinstance(subject_at, str) or not subject_at:
        raise oauth_http.OAuthRefreshError("obo rfc8693-refresh response missing access_token")

    exchange_data: dict[str, str] = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_id": oidc_config.client_id,
        "client_secret": oidc_config.client_secret,
        "subject_token": subject_at,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": audience,
    }
    if scopes:
        # e.g. Keycloak optional audience scopes must be requested explicitly
        # or the exchange fails "Requested audience not available" (verified).
        exchange_data["scope"] = scopes
    return await _obo_token_post(
        token_endpoint=oidc_config.token_endpoint,
        data=exchange_data,
        http_client=http_client,
        leg="rfc8693-exchange",
    )


_OBO_MINT_LEGS = {
    "entra": _obo_mint_entra,
    "rfc8693": _obo_mint_rfc8693,
}


#: Grant legs a deployment may select via ``[oidc] obo_grant_profile`` — derived
#: from the operative registry so the two never drift (used by oidc config
#: validation; there is no second hand-written copy).
OBO_GRANT_PROFILES: frozenset[str] = frozenset(_OBO_MINT_LEGS)


# Hard cap per dedup namespace. Keys derive from operator config or the user
# population, so growth is bounded in practice; the cap only stops a
# pathological deployment from turning a dedup set into a leak. Past it,
# later keys go unlogged rather than unbounded, and the per-turn heartbeat
# still fires on every occurrence.
_WARN_DEDUP_CAP = 512


def _warn_dedup_once[DedupKey: (str, tuple[str, str])](
    warned: set[DedupKey], key: DedupKey, event: str, **fields: Any
) -> None:
    """Emit ``log.warning(event, **fields)`` once per ``key`` in ``warned``.

    The shared mechanics of the module's once-per-key warn namespaces, in
    one home so a dedup-policy change (cap size, eviction, key
    normalization) cannot land in one namespace and silently leave another
    unbounded. The namespaces themselves stay split: each caller passes its
    own set, so saturating one can never starve the others.
    """
    if key in warned:
        return
    if len(warned) >= _WARN_DEDUP_CAP:
        return
    warned.add(key)
    log.warning(event, **fields)


async def mint_client_credentials(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    audience: str,
    http_client: httpx2.AsyncClient,
) -> dict[str, Any]:
    """Redeem the application's client credentials for an Entra audience."""
    return await _obo_token_post(
        token_endpoint=token_endpoint,
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
            "scope": f"{audience}/.default",
        },
        http_client=http_client,
        leg="client-credentials",
    )


def _scrub_secrets(message: str, secrets: tuple[str, ...]) -> str:
    """Replace every non-empty *secrets* value in *message* with ``***``."""
    for value in secrets:
        if value:
            message = message.replace(value, "***")
    return message


async def mint_client_credentials_basic(
    *,
    token_endpoint: str,
    client_id: str,
    client_secret: str,
    scope: str,
    http_client: httpx2.AsyncClient,
) -> dict[str, Any]:
    """Redeem client credentials for a raw ``scope`` with HTTP Basic client auth.

    The generic RFC 6749 §4.4 client-credentials grant behind the model
    ``oauth_client_credentials`` auth mode (e.g. AWS Cognito, where the
    AI-DP gateway scope is ``ai-gateway/invoke``). Unlike
    :func:`mint_client_credentials` it sends *scope* verbatim (no Entra
    ``<audience>/.default``) and authenticates the client with an
    ``Authorization: Basic`` header (RFC 6749 §2.3.1) instead of form fields,
    so the secret never travels in the body. Cognito accepts Basic (verified
    live); its tokens last 3600 s and carry no refresh token.

    Returns ``{"access_token": str}`` plus ``"expires_in": int`` when the AS
    sent a positive lifetime (int, float or numeric string). A missing or
    malformed ``expires_in`` is left out, so the caller's
    :func:`~turnstone.core.oauth.tokens._expires_at_from_response` applies its
    default TTL rather than caching forever.

    Raises :class:`~turnstone.core.oauth.http.OAuthRefreshError` (classified as
    by :func:`_hardened_token_post`) on transport, status or body failures and
    on a response without an access token: callers fail closed. Error text
    never contains the client secret or the encoded Basic credential.
    """
    # RFC 6749 §2.3.1 form-encodes id/secret before base64; Cognito ids and
    # secrets are alphanumeric, and its docs specify the plain pair, so the
    # pair is encoded as-is (byte-identical for every Cognito credential).
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode("ascii")
    data: dict[str, str] = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope
    secrets = (client_secret, basic)
    try:
        resp = await _obo_token_post(
            token_endpoint=token_endpoint,
            data=data,
            http_client=http_client,
            leg="client-credentials-basic",
            headers={"Authorization": f"Basic {basic}"},
        )
    except oauth_http.OAuthRefreshError as exc:
        # Defence in depth: some ASes echo request material in error bodies.
        # _format_as_error already redacts token-shaped values; the raw secret
        # is not always token-shaped, so scrub it explicitly. ``from None``
        # keeps a transport exception (which may carry request detail) off
        # the chain that callers log with exc_info.
        raise oauth_http.OAuthRefreshError(
            _scrub_secrets(str(exc), secrets), failure_class=exc.failure_class
        ) from None

    access_token = resp.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise oauth_http.OAuthRefreshError(
            "obo client-credentials-basic response missing access_token"
        )
    result: dict[str, Any] = {"access_token": access_token}
    expires_in = _positive_seconds(resp.get("expires_in"))
    if expires_in is not None:
        result["expires_in"] = expires_in
    return result


def _positive_seconds(value: Any) -> int | None:
    """Parse an ``expires_in`` (int, float or numeric string) as positive seconds."""
    seconds: int | None = None
    if isinstance(value, bool):
        # bool subclasses int: True must not read as a 1-second lifetime.
        seconds = None
    elif isinstance(value, int):
        seconds = value
    elif isinstance(value, float | str):
        try:
            seconds = int(float(value))
        except (ValueError, OverflowError):  # "abc", nan, inf
            seconds = None
    if seconds is None or seconds <= 0:
        return None
    return seconds
