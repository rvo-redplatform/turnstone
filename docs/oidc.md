# OpenID Connect (OIDC) Single Sign-On

Turnstone supports OpenID Connect for federated authentication, allowing
users to log in with their existing corporate identity provider instead of
managing a separate password. OIDC is opt-in: when configured, the login
screen shows a "Continue with SSO" button alongside the existing
username/password form. When not configured, the login experience is
unchanged.

Any OIDC-compliant provider works: Google, Okta, Azure AD, Keycloak,
Auth0, OneLogin, and others that publish a
`.well-known/openid-configuration` discovery document.

---

## Prerequisites

1. A registered **confidential** OIDC client at your identity provider
2. The client's redirect URI must include:
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
3. A local admin user must exist in Turnstone (complete the initial setup
   wizard before enabling OIDC)

---

## Configuration

Configure OIDC in the `[oidc]` section of the shared `config.toml` used by the
console and server nodes. Docker deployments can prepare and mount this file
through the installer's optional OAuth/SSO step or the
[shared config overlay](docker.md#shared-bootstrap-config). For other
deployments, select the file with `TURNSTONE_CONFIG` on each process. On
Kubernetes, the Helm chart's `oidc.*` values set the issuer, client id,
redirect base, scopes and provider name for both pods, and pass the client
secret from a Kubernetes Secret as `TURNSTONE_OIDC_CLIENT_SECRET`, as the
chart passes the JWT secret; a `secret://` reference keeps the value itself in
the store. The chart's `config.file` carries the rest of this table; see
[the chart README](../deploy/helm/turnstone/README.md#single-sign-on).

### Shared config example

Edit the existing `[oidc]` section in place, preserving other sections and
the token encryption key. Replace the provider placeholders before enabling
SSO; create a local admin first.

```toml
[oidc]
issuer = "https://identity.example.com"
client_id = "your-client-id"
client_secret = "your-client-secret"  # or a secret:// reference
redirect_base = "https://app.example.com"
provider_name = "SSO"
password_enabled = true
capture_user_credential = false
```

SSO login alone does not need refresh-credential capture. For delegation,
follow [MCP sign-in passthrough](mcp-oauth.md#auth_typeoauth_obo--single-credential-sign-in-passthrough)
or [model gateway credentials](#model-gateway-credentials). Both reuse this
file and the shared token encryption key. After changing bootstrap config,
restart every consumer. For Docker, restart after TOML edits and recreate
services after changing Compose mounts or environment variables; see the
[shared config instructions](docker.md#shared-bootstrap-config).

### Environment variable reference

Environment variables remain supported and take precedence over TOML when
both are set. Keep the client secret in the private TOML file rather than
inherited process environments. Setting `TURNSTONE_CONFIG` selects that file;
the installer does not copy OIDC variables from `.env` into the services.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TURNSTONE_OIDC_ISSUER` | Yes | — | Issuer URL (e.g. `https://accounts.google.com`). Must serve `/.well-known/openid-configuration`. |
| `TURNSTONE_OIDC_CLIENT_ID` | Yes | — | OAuth 2.0 client ID from your provider |
| `TURNSTONE_OIDC_CLIENT_SECRET` | Yes | — | OAuth 2.0 client secret (confidential client); may be a `secret://` reference, resolved at startup |
| `TURNSTONE_OIDC_SCOPES` | No | `openid email profile` | Space-separated OAuth scopes to request |
| `TURNSTONE_OIDC_PROVIDER_NAME` | No | `SSO` | Display name for the login button (e.g. "Google", "Okta") |
| `TURNSTONE_OIDC_ROLE_CLAIM` | No | — | ID token claim containing role/group values (see [Role Mapping](#role-mapping)) |
| `TURNSTONE_OIDC_ROLE_MAP` | No | — | Mapping from claim values to Turnstone role IDs (see [Role Mapping](#role-mapping)) |
| `TURNSTONE_OIDC_PASSWORD_ENABLED` | No | `true` | Set to `false` to hide the password form and block all username/password logins (including admin). API tokens continue to work. |
| `TURNSTONE_OIDC_REDIRECT_BASE` | Yes | — | Externally-reachable origin for the OIDC redirect URI (e.g. `https://app.example.com`). Without this, OIDC will refuse to start. The previous Host-header fallback was unsafe under permissive reverse proxies. |
| `TURNSTONE_OIDC_TRUSTED_ENDPOINT_HOSTS` | No | — | Comma-separated list of additional hostnames whose endpoints the IdP discovery document is allowed to reference. See [Cross-host endpoints](#cross-host-endpoints). |
| `TURNSTONE_OIDC_ALLOW_PRIVATE_NETWORK` | No | `false` | Allow the issuer (and its discovered endpoints) to resolve to private/internal addresses — needed for a self-hosted IdP on an internal network. See [Self-hosted and internal IdPs](#self-hosted-and-internal-idps). |
| `TURNSTONE_OIDC_CAPTURE_USER_CREDENTIAL` | No | `false` | Capture an encrypted refresh credential at SSO login for delegated MCP/model access; requires the shared token encryption key. |
| `TURNSTONE_OIDC_OBO_GRANT_PROFILE` | No | `entra` | Token minting profile supported by the IdP: `entra` or `rfc8693`. See [MCP delegation](mcp-oauth.md#deployment-configuration-oidc-in-configtoml). |

All four required fields — issuer, client ID, client secret, and
`TURNSTONE_OIDC_REDIRECT_BASE` — must be set. If any are missing OIDC
is disabled at startup (an error is logged when only `redirect_base`
is missing) and the login screen shows only the password form.

### Redirect base (required)

`[oidc] redirect_base` (or `TURNSTONE_OIDC_REDIRECT_BASE`) pins the redirect
URI sent to the identity provider to a known externally-visible origin. Set
it to the public origin of your Turnstone deployment:

```toml
[oidc]
redirect_base = "https://app.example.com"
```

The resulting callback URL will be
`https://app.example.com/v1/api/auth/oidc/callback` — register this as the
authorized redirect URI in your identity provider.

Per-user MCP authorization uses the same origin with a different path,
`/v1/api/mcp/oauth/callback`, registered with the MCP provider. Setting only
`redirect_base` supports that flow without enabling SSO.

OIDC will refuse to start when this setting is unset. There is no
Host-header fallback: a permissive reverse proxy or direct backend access
would otherwise let an attacker spoof `Host` and steer the IdP redirect
to a callback origin they control.

### Cross-host endpoints

By default, every endpoint in the IdP discovery document
(`token_endpoint`, `jwks_uri`, `userinfo_endpoint`) must share the
issuer's `(scheme, host, port)`. This prevents a hostile or compromised
IdP from redirecting the token-exchange POST (which carries
`client_secret`) to an arbitrary host, and prevents JWKS fetches from
being aimed at internal services.

A few public IdPs legitimately split endpoints across hostnames. Google
and Microsoft Entra ID are the canonical examples:

| IdP | Issuer host | Cross-host endpoint(s) |
|-----|-------------|------------------------|
| Google | `accounts.google.com` | `oauth2.googleapis.com`, `www.googleapis.com`, `openidconnect.googleapis.com` |
| Microsoft Entra | `login.microsoftonline.com` | `graph.microsoft.com` (userinfo) |

Both sets are built in — operators using `https://accounts.google.com` or
`https://login.microsoftonline.com/<tenant>/v2.0` need no extra
configuration. (Entra's discovery document advertises `userinfo_endpoint`
on `graph.microsoft.com`, distinct from the issuer host.)

For other IdPs whose discovery document references a non-issuer host,
extend the allow-list explicitly:

```toml
[oidc]
trusted_endpoint_hosts = ["token.example.com", "keys.example.com"]
```

The same scheme / no-userinfo / SSRF rules apply to allow-listed hosts —
this knob only relaxes the same-origin check, not the security gates.
Each entry is a hostname (no scheme, no path).

### Self-hosted and internal IdPs

By default Turnstone refuses an issuer whose hostname resolves to a
private or internal address:

```
OIDCError: endpoint URL resolves to non-public address (10.0.0.5): https://auth.example.site
```

This is SSRF hardening, not a licensing or product restriction: the OIDC
flow makes server-side HTTP requests (discovery, JWKS, token exchange),
and refusing non-public destinations keeps a mistyped or maliciously
steered issuer from aiming those fetches at internal services. For a
self-hosted IdP (Keycloak, Authentik, Dex, …) on a private network,
opt in explicitly in `config.toml`:

```toml
[oidc]
allow_private_network = true
```

or via `TURNSTONE_OIDC_ALLOW_PRIVATE_NETWORK=true` (the env var wins
when both are set).

The opt-in admits private-range (RFC 1918), unique-local, site-local,
CGNAT (100.64/10, where overlay VPNs commonly assign hosts), and
loopback addresses. Link-local, multicast, reserved ranges and known
cloud-metadata endpoints stay refused even with the opt-in — no
legitimate IdP lives there. An address is judged by what it actually
reaches, so an IPv6 transition address (NAT64, 6to4, Teredo) wrapping
an internal IPv4 is treated as an internal destination. A local-use
NAT64 wrapper in `64:ff9b:1::/48` is refused when any valid layout
decodes into a refused range, even if its actual IPv4 destination is
public; the private-network opt-in cannot restore access. The standard
`64:ff9b::/96` DNS64 prefix still admits public IPv4 destinations. The
HTTPS requirement and the same-origin endpoint checks are unaffected.

This knob only affects the login-flow IdP configured here. OAuth endpoints
advertised by remote MCP servers use the separate runtime setting
[`mcp.oauth_allow_private_network`](mcp-oauth.md#mcp-servers-on-a-private-network),
which also defaults to the strict public-address rule.

### Model gateway credentials

For Docker, use the same [shared config](docker.md#shared-bootstrap-config)
for the console and every node. Enable credential capture for users who
will use delegated models, and have them sign in again. Application identity
(`entra_app`) uses the registration's own permissions and needs no user
credential capture. Configure model auth modes and audiences in the admin
Models tab, following the runtime settings linked below.

The same OIDC registration can authenticate model gateways. A model definition
with `auth_mode = "entra_obo"` (Entra grant profile) or `auth_mode =
"rfc8693_obo"` (RFC 8693 token-exchange profile) redeems the driving user's
captured credential for its exact `obo_audience`; `auth_mode = "entra_app"`
uses the registration's client ID and secret with Entra client credentials.
All three bind the result through the provider SDK's native credential option
rather than injecting an override header. The grant mode is never inferred:
missing user context or a failed OBO mint cannot switch a delegated definition
to client credentials.

Each dynamic mode pairs with the grant profile whose dialect it names:
`entra_obo` and `entra_app` require `obo_grant_profile = "entra"`;
`rfc8693_obo` requires `obo_grant_profile = "rfc8693"`. The pairing is
enforced when a write chooses a `(auth_mode, obo_audience)` pair — a same-pair
edit of a row saved before the pairing rule keeps working — and at runtime a
mismatched legacy row refuses to mint with `cause=grant_profile_mismatch` and
no IdP traffic. RFC 8693 client-credentials is not implemented.

The delegated modes need the [shared token encryption key](oauth-storage.md),
a credential captured for the driving user, and delegated/admin-consented
permission to the audience.
`rfc8693_obo` additionally carries `obo_scopes`, the space-separated scope
list its exchange leg requests: exchange-capable IdPs that gate audiences
behind optional scopes refuse the exchange without it ("Requested audience not
available"), which is why the scope-less Entra-named mode could never mint on
that profile (issue #955). Scopes are stored shape-checked only — whether a
value satisfies the IdP stays the IdP's call at mint time. Turning
`capture_user_credential` off later stops *new* captures but does not
invalidate credentials already stored, so existing users keep minting.
`entra_app` requires a confidential-client secret. Configure the permitted
resource IDs in the runtime setting `model.auth_audience_allowlist` before
saving dynamic model definitions. De-listing an audience later blocks every
write that would arm or re-aim a definition at it, but does not stop aliases
already configured from minting — disabling the row (the `admin.models` disarm
lever) is what stops minting. See
[Settings](settings.md#model-backend-authentication) for permissions, failure
policy, and lane identity rules.

An unrecognised `obo_grant_profile` is warned about at startup and **rejected
at the write choke points**: configuring an `oauth_obo` MCP server or a dynamic
model alias returns a 400 that echoes the configured value, so the typo is the
diagnosis. At runtime an unknown profile never mints — the mint legs resolve by
exact name; the full cause detail is logged once per audience, and every
affected call still logs its per-turn fallback or refusal naming the alias,
the target audience, and the last recorded cause (`cause=` — for example
`unsupported_grant_profile` or `oidc_not_enabled`) — so a pre-existing row
degrades loudly, with the reason visible mid-incident even after the
once-per-process line has rotated out of retained logs, rather than silently
swapping per-user attribution for the shared static key.

The token keyring is shared by the console and every node that reads the
database. See [Shared OAuth storage](oauth-storage.md) for bootstrap settings,
startup requirements, key rotation, and the effect of identity unlink on
delegated credentials and app-identity caches.

### Client-credentials model gateways (`oauth_client_credentials`)

Some model gateways issue their own tokens and do not trust Turnstone's
`[oidc]` client. An example is an AWS Cognito app client in front of an AI
gateway. For these, use `auth_mode = "oauth_client_credentials"`. Turnstone
mints one shared token per alias from the definition's own token endpoint,
with the OAuth 2.0 client-credentials grant. No user is involved, so the mode
works for service, CLI and coordinator turns the same way.

This mode does not use `[oidc]`, `obo_grant_profile` or `obo_audience`, and
`model.auth_audience_allowlist` does not apply to it. The token URL
allow-list below takes their place.

#### Fields

| Field | Required | Meaning |
|-------|----------|---------|
| `oauth_token_url` | yes | The token endpoint. It must be an absolute `http(s)` URL with a host and no userinfo or fragment. It must also be listed in `model.oauth_token_url_allowlist`. |
| `oauth_client_id` | yes | The app client ID. |
| `oauth_client_secret` | yes | The app client secret. It can be a `secret://` reference (recommended) or a literal. |
| `obo_scopes` | yes | The `scope` value, sent exactly as written. Turnstone does not add `/.default` to it. |

If any of these is missing, the registry load fails with one error that names
every missing field. For example: `Model 'aidp' requires oauth_token_url,
oauth_client_id, oauth_client_secret when auth_mode is
'oauth_client_credentials'`. A `secret://` reference is parsed when the
definition loads, so a malformed one fails early. The secret itself is only
resolved at mint time, so you can rotate it without a reload. A stale
`obo_audience` on a definition in this mode is dropped with a warning.

The request is a `POST` with `grant_type=client_credentials` and the raw
`scope`. The client authenticates with `Authorization: Basic
base64(client_id:client_secret)`, and neither credential is put in the form
body. Errors from the token endpoint are logged with the secret and the Basic
credential replaced by `***`.

#### Configuration example

Settings, in the admin **Settings** tab or with the console settings API (see
[Settings](settings.md#client-credentials-settings)):

```text
model.oauth_token_url_allowlist  = https://rp-aidp-tools-nonprod.auth.us-east-1.amazoncognito.com/oauth2/token
model.oauth_refresh_margin_seconds = 660
```

A model definition in `config.toml`:

```toml
[models.aidp-agent]
provider = "openai"
base_url = "https://aidp.rp-tools-nonprod.rvohealth.com/v1"
model = "aiq-agent"                       # the gateway route id
auth_mode = "oauth_client_credentials"
oauth_token_url = "https://rp-aidp-tools-nonprod.auth.us-east-1.amazoncognito.com/oauth2/token"
oauth_client_id = "<cognito-app-client-id>"
oauth_client_secret = "secret://file/run/turnstone/secrets/aidp-client-secret"
obo_scopes = "ai-gateway/invoke"
```

The same definition, created with the console API
(`POST /v1/api/admin/model-definitions`):

```json
{
  "alias": "aidp-agent",
  "provider": "openai",
  "base_url": "https://aidp.rp-tools-nonprod.rvohealth.com/v1",
  "model": "aiq-agent",
  "auth_mode": "oauth_client_credentials",
  "oauth_token_url": "https://rp-aidp-tools-nonprod.auth.us-east-1.amazoncognito.com/oauth2/token",
  "oauth_client_id": "<cognito-app-client-id>",
  "oauth_client_secret": "secret://file/run/turnstone/secrets/aidp-client-secret",
  "obo_scopes": "ai-gateway/invoke"
}
```

On Kubernetes, mount the client secret from a Kubernetes Secret with the Helm
chart's `config.secretMounts`, and set `[secrets.file] root` to the mount
directory. See [Secret references](security.md#secret-references). A
definition saved in the console never expands `${VAR}`, and the console
refuses a `${VAR}` in the token URL or the secret.

#### Token URL allow-list

`model.oauth_token_url_allowlist` lists the exact token endpoint URLs that
definitions may mint from, separated by commas or newlines. There is no
wildcard or host matching. The default is empty, which denies every URL, so
an operator must list an endpoint before an admin can save a definition in
this mode.

The console checks the allow-list on every write that would save or change
such a definition, and refuses an unlisted URL with a 400. The mint checks it
again on every call, so removing a URL from the list stops minting for every
definition that uses it. The console's
`GET /v1/api/admin/model-definitions/auth-constraints` returns the current
list as `oauth_token_url_allowlist` and the mode names as
`client_credentials_auth_modes`, for the admin UI.

#### Refresh margin and caching

The token is cached per alias in the encrypted `oauth_tokens` table, in the
same `__model_app__:<alias>` row that `entra_app` uses. Every node shares
the row, and each host also keeps an in-memory copy. Concurrent calls at
expiry are single-flight: a burst of calls mints once.

A cached token with fewer than `model.oauth_refresh_margin_seconds` left is
minted again before the next call. The default is 660 seconds, the 10-minute
call limit plus one minute, and the minimum is 60. So a 3600-second token is
used for 49 minutes. The margin is capped at half of the token's lifetime, so
an IdP that issues short tokens (for example 300 seconds) does not cause a
mint on every call.

The cache entry is keyed by the token URL, the client ID, the resolved
secret and the scope. If any of them changes, the old token is never served,
even before anything purges it and even from a host's in-memory copy. When an
admin changes the token URL, client ID or secret in the console, the console
also deletes the cached row.

#### Fail closed

This mode always fails closed. When Turnstone cannot get a token, the call
raises `BackendAuthUnavailableError` and nothing is sent to the model
gateway. It never falls back to a static `api_key`, even when
`model.auth_fail_closed` is `false`. The refusal is logged as
`model_cc.refused` with the alias, the token URL and one of these causes:

| Cause | Meaning |
|-------|---------|
| `credentials_unavailable` | The definition is missing the URL, the client ID, the secret or the scope. |
| `token_url_not_allowlisted` | The token URL is not in `model.oauth_token_url_allowlist`. |
| `secret_unavailable` | The `secret://` reference could not be resolved. |
| `mint_failed` | The token endpoint refused the grant or could not be reached. |
| `mint_missing_access_token` | The token endpoint answered without an `access_token`. |
| `token_store_unavailable` | The process has no protected token store. |

After a failed mint, calls for that alias are refused for 30 seconds without
contacting the token endpoint, so a wrong secret (Cognito `invalid_client`)
does not cause a tight retry loop. Correcting the configuration clears this
cooldown.

#### 401 from the model gateway

A gateway can revoke a token before it expires. When the gateway answers a
request with `401` and Turnstone minted the token for that call, Turnstone
marks the token as rejected, mints a new one and sends the request once more.
A second `401` is returned to the caller. The `401` arrives before any
streamed output, so the retry cannot duplicate output. Concurrent callers
mint only once. Only this mode retries; the other modes return the `401`
as before. Audio calls are not retried.

#### Secret masking in the console

The console never returns a literal client secret. On read, a literal
`oauth_client_secret` comes back as `***`, as `api_key` does. A `secret://`
reference is returned as written, so you can see which secret entry the
definition uses. On update, an empty value, `***` or the unchanged reference
keeps the stored secret, so the secret cannot be cleared through the API. The
audit log records `(set)` on create and `(updated)` on update, never the
value.

As for every dynamic mode, changing a definition in this mode also needs
`admin.mcp`, apart from the tuning fields (see
[Settings](settings.md#model-backend-authentication)). Changing the token URL,
the client ID or the secret is also recorded as a posture event. Disabling
the definition needs only `admin.models`, even when the definition cannot
load.

Per-definition request headers (`default_headers`) and run session headers
(`session_headers`) are often used with this mode. See
[Architecture](architecture.md#per-definition-default-headers).

---

## Provider-Specific Setup

Merge these fields into your existing `[oidc]` section, retaining its
`redirect_base` and any delegation settings. Role mappings below are examples;
choose the group/role claims and Turnstone permissions appropriate to your
organization.

### Google

1. Go to [Google Cloud Console](https://console.cloud.google.com/) >
   **APIs & Services** > **Credentials**
2. Click **Create Credentials** > **OAuth 2.0 Client ID**
3. Application type: **Web application**
4. Add authorized redirect URI:
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
5. Copy the **Client ID** and **Client secret**

```toml
[oidc]
issuer = "https://accounts.google.com"
client_id = "123456789.apps.googleusercontent.com"
client_secret = "GOCSPX-..."
provider_name = "Google"
```

### Okta

1. In the Okta Admin Console, go to **Applications** > **Create App
   Integration**
2. Sign-in method: **OIDC - OpenID Connect**
3. Application type: **Web Application**
4. Add sign-in redirect URI:
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
5. Note the **Issuer** (your Okta domain, e.g.
   `https://dev-123456.okta.com`)

```toml
[oidc]
issuer = "https://dev-123456.okta.com"
client_id = "0oaXXXXXXXXXXXXX"
client_secret = "..."
provider_name = "Okta"
role_claim = "groups"
role_map = {admin = "builtin-admin", everyone = "builtin-operator"}
```

### Azure AD (Entra ID)

1. In the Azure Portal, go to **App registrations** > **New registration**
2. Redirect URI: **Web** >
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
3. Under **Certificates & secrets**, create a new **Client secret** and
   copy the value immediately
4. The issuer URL is
   `https://login.microsoftonline.com/{tenant-id}/v2.0`

```toml
[oidc]
issuer = "https://login.microsoftonline.com/YOUR_TENANT_ID/v2.0"
client_id = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
client_secret = "..."
provider_name = "Azure AD"
role_claim = "roles"
role_map = {Admin = "builtin-admin", User = "builtin-operator"}
```

### Keycloak

1. In the Keycloak Admin Console, select your **Realm**
2. Go to **Clients** > **Create client**
3. Client type: **OpenID Connect**
4. Set **Client authentication** to **On** (confidential)
5. Add valid redirect URI:
   `https://your-turnstone-host/v1/api/auth/oidc/callback`
6. The issuer URL is
   `https://keycloak.example.com/realms/your-realm`

```toml
[oidc]
issuer = "https://keycloak.example.com/realms/your-realm"
client_id = "turnstone"
client_secret = "..."
provider_name = "Keycloak"
role_claim = "realm_access.roles"
role_map = {admin = "builtin-admin", operator = "builtin-operator"}
```

---

## Role Mapping

OIDC role mapping assigns Turnstone roles to users based on claims in the
ID token. This is optional — without it, OIDC users are provisioned with
the `builtin-viewer` role (read-only access) by default.

### Configuration

Set `[oidc] role_claim` to the name of the claim in the ID token that
contains the user's group or role memberships. Then map claim values to
Turnstone role IDs in `role_map`:

```toml
[oidc]
role_claim = "groups"

[oidc.role_map]
admin = "builtin-admin"
engineering = "builtin-operator"
viewer = "builtin-viewer"
```

The environment equivalent uses `TURNSTONE_OIDC_ROLE_CLAIM=groups` and a
comma-separated `TURNSTONE_OIDC_ROLE_MAP` such as
`admin:builtin-admin,engineering:builtin-operator,viewer:builtin-viewer`.

### Behavior

- **Synced on every login**: roles are added when new claim values appear,
  and OIDC-assigned roles are revoked when the corresponding claim value
  is no longer present. Roles assigned manually (or by other sources) are
  never touched — only roles with `assigned_by="oidc"` are subject to
  revocation.
- **List or string**: the claim value can be a JSON array
  (`["admin", "engineering"]`) or a single string (`"admin"`). Both are
  handled correctly.
- **Unknown values**: claim values not present in the role map are silently
  ignored.
- **Missing roles**: if the role map references a Turnstone role ID that
  does not exist in the database, the assignment is skipped (no error).
- **Evaluated on every login**: roles are checked and applied each time
  the user authenticates via OIDC, so new group memberships are picked
  up on the next login.

### `assigned_by` markers

Role assignments record an `assigned_by` value that controls how the
sync logic treats them. OIDC-driven flows use two distinct markers:

- `oidc` — set by claim-driven role mapping; revoked automatically on
  the next login when the corresponding claim value is no longer
  present.
- `oidc-default` — applied to brand-new OIDC users who have no
  claim-mapped roles, as a safety net so they still get
  `builtin-viewer` access on first login. Survives subsequent logins
  regardless of claim contents and is never revoked by `apply_role_mapping`.

### Built-in Roles

| Role ID | Permissions |
|---------|-------------|
| `builtin-admin` | All permissions |
| `builtin-operator` | read, write, workstreams.create, workstreams.close |
| `builtin-viewer` | read |

---

## User Provisioning

When a user logs in via OIDC for the first time, Turnstone automatically
creates a local user account:

1. The OIDC identity (`issuer` + `sub` claim) is stored in the
   `oidc_identities` table and linked to the new user
2. The **username** is derived from the `preferred_username` claim,
   falling back to the email local part, with deduplication if needed
3. The **display name** comes from the `name` claim, falling back to
   `preferred_username` or email
4. The user's password hash is set to a sentinel value (`!oidc`) — OIDC
   users cannot log in with a password

On subsequent logins, the existing user is matched by `(issuer, sub)` and
the `last_login` timestamp is updated. Role mapping is re-evaluated on
every login.

---

## OIDC-Only Mode

To enforce OIDC for all logins and hide the password form, set:

```toml
[oidc]
password_enabled = false
```

In this mode the login screen shows only the "Continue with SSO" button.
The password form, token toggle, and sign-in button are all hidden.
All username/password logins are blocked at the API level, including
admin accounts.

The first admin account must be created via the setup wizard (with a
password) before OIDC is enabled. The setup wizard always works
regardless of this setting because it is only available when zero users
exist in the database.

API token login (`POST /v1/api/auth/login` with a `ts_` token)
continues to work regardless of this setting. JWTs and API tokens are
the supported authentication methods. OIDC-only mode affects
password-based authentication only.

---

## Login Flow

Both the server and console support OIDC login. The flow is identical:

1. The browser fetches `GET /v1/api/auth/status` at page load
2. If the response includes `oidc_enabled: true`, the login screen shows
   a "Continue with {provider_name}" button
3. Clicking the button navigates to `GET /v1/api/auth/oidc/authorize`
4. Turnstone generates a state token, nonce, and PKCE verifier, stores
   them in the database, and redirects the browser to the identity
   provider's authorization endpoint
5. The user authenticates at the identity provider
6. The IdP redirects back to
   `GET /v1/api/auth/oidc/callback?code=...&state=...`
7. Turnstone validates the state, exchanges the authorization code for
   tokens using the PKCE verifier, validates the ID token against the
   provider's JWKS public keys, provisions or matches the user, and
   issues a Turnstone JWT
8. The browser is redirected to `/?oidc_success=1` with the JWT set in
   an `HttpOnly` session cookie
9. The browser JavaScript detects the `oidc_success` query parameter,
   strips it from the URL, hides the login overlay, and calls
   `onLoginSuccess()` to initialize the application

---

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/v1/api/auth/oidc/authorize` | Public | Redirects to identity provider |
| GET | `/v1/api/auth/oidc/callback` | Public | Handles IdP callback, issues JWT |

Both endpoints are public (no authentication required) because they are
part of the login flow itself.

### Auth status response

When OIDC is enabled, `GET /v1/api/auth/status` includes additional
fields:

```json
{
  "auth_enabled": true,
  "has_users": true,
  "setup_required": false,
  "oidc_enabled": true,
  "oidc_provider_name": "Google",
  "password_enabled": true
}
```

---

## Database Schema

Migration 018 creates two tables:

```sql
CREATE TABLE oidc_identities (
    issuer      TEXT NOT NULL,
    subject     TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    email       TEXT NOT NULL DEFAULT '',
    created     TEXT NOT NULL,
    last_login  TEXT NOT NULL,
    PRIMARY KEY (issuer, subject)
);

CREATE INDEX idx_oidc_identities_user_id ON oidc_identities(user_id);

CREATE TABLE oidc_pending_states (
    state          TEXT PRIMARY KEY,
    nonce          TEXT NOT NULL,
    code_verifier  TEXT NOT NULL,
    audience       TEXT NOT NULL,
    created_at     TEXT NOT NULL
);
```

The `oidc_identities` table links an OIDC subject (identified by
`issuer` + `subject`) to a Turnstone `user_id`. A single user can have
multiple OIDC identities (e.g. from different providers).

The `oidc_pending_states` table stores authorization flow state for
callback validation. Entries are automatically cleaned up after 5 minutes.

---

## Security Notes

- **Authorization Code Flow with PKCE**: the recommended OAuth 2.0 flow
  for web applications. PKCE prevents authorization code interception
  attacks even without a client secret (though the client secret is still
  used for additional security).
- **ID token validation**: all tokens are validated using the provider's
  JWKS public keys (RS256 or ES256). The signature, issuer, audience,
  and expiry are all checked.
- **State parameter**: a cryptographically random state token prevents
  CSRF attacks on the callback endpoint. The state is stored server-side
  and verified on callback.
- **Nonce**: a random nonce is included in the authorization request and
  verified in the ID token to prevent replay attacks.
- **Client secret**: never leaves the server — it is only used in the
  server-to-IdP token exchange, not exposed to the browser.
- **OIDC users cannot use password login**: the sentinel password hash
  (`!oidc`) ensures `verify_password()` always rejects password attempts
  for OIDC-provisioned users.
- **Rate limiting**: the callback endpoint shares the login rate limiter
  (5 attempts per 5-minute window per IP).
- **State TTL**: pending authorization states expire after 5 minutes.
  Expired states are lazily cleaned up on each callback.
- **Setup guard**: OIDC login requires at least one local admin user to
  exist. This ensures the initial admin account is always created via the
  setup wizard with a password, not hijacked by an external identity.

---

## Troubleshooting

### "OIDC not configured"

All four required `[oidc]` fields must be set: `issuer`, `client_id`,
`client_secret`, and `redirect_base`. Check that none are empty or
whitespace-only, that `TURNSTONE_CONFIG` selects the intended file, and that
no environment overrides replace its values.

### "OIDC enabled but TURNSTONE_OIDC_REDIRECT_BASE is unset"

This error is logged when the three credential fields are set but
`redirect_base` is missing from both TOML and the environment. OIDC is
disabled at startup to prevent Host-header-derived redirect URI spoofing.
Set `[oidc] redirect_base` to your service's externally-visible origin
(e.g. `https://app.example.com`) and restart the consumers. See
[Redirect base](#redirect-base-required) for the rationale.

### Discovery silently disables OIDC with "host does not match issuer"

The IdP discovery document points `token_endpoint`, `jwks_uri`, or
`userinfo_endpoint` at a hostname that doesn't share the issuer's
origin. If the IdP is legitimate, add the additional hostname(s) to
`[oidc] trusted_endpoint_hosts`. Google and Entra endpoints are allow-listed
automatically; see [Cross-host endpoints](#cross-host-endpoints).

### "Login session expired"

The authorization flow must complete within 5 minutes. If the user takes
too long at the identity provider, the pending state expires. Try again.

### "Initial setup required"

OIDC login is blocked until at least one local admin user exists.
Complete the setup wizard first (navigate to the Turnstone URL and follow
the prompts to create an admin user with a password).

### Discovery fails at startup

Check that the issuer URL is reachable from the Turnstone server and
serves a valid `/.well-known/openid-configuration` document. The server
logs the discovery attempt at startup:

```
OIDC discovery failed for https://your-issuer.example.com: ...
```

OIDC is automatically disabled when discovery fails. Restart the server
after fixing the connectivity issue.

### Redirect URI mismatch

The redirect URI configured at the identity provider must exactly match
`https://your-host/v1/api/auth/oidc/callback`. Common issues:

- **Scheme mismatch**: the redirect uses `https://` — make sure TLS is
  configured or a reverse proxy sets the `X-Forwarded-Proto` header
- **Port mismatch**: if running on a non-standard port, include it in
  the redirect URI
- **Path mismatch**: the path must include the `/v1` API version prefix

### User not assigned expected roles

Check that:

1. `[oidc] role_claim` matches the exact claim name in the ID
   token (case-sensitive)
2. `[oidc] role_map` maps the correct claim values to valid
   Turnstone role IDs
3. The roles referenced in the map exist in the database (check the
   admin panel > Roles tab)
4. The identity provider is configured to include the claim in the ID
   token (some providers require explicit scope or claim configuration)
