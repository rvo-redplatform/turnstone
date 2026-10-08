# turnstone Helm chart

Deploys a Turnstone cluster: server nodes, the console (dashboard + routing
proxy), and optionally SearXNG (web_search backend), the channel gateway, and
a Karpenter disruption guard. Browsers reach the console only; it proxies
every server node.

This chart **ships no bundled PostgreSQL** (see `Chart.yaml` for why the
upstream Bitnami subchart was removed). Either let the chart create a
dedicated CloudNativePG `Cluster` (`cnpg.enabled`, operator required) or point
`database.external` at an existing PostgreSQL.

## Minimum values

```yaml
image:
  tag: "1.8.5"                       # pin; never a floating tag
cnpg:
  enabled: true                      # chart-managed CNPG Cluster <release>-db
  storage:
    storageClass: gp3
auth:
  existingSecret: turnstone-auth     # key TURNSTONE_JWT_SECRET
```

Without CNPG:

```yaml
database:
  external:
    host: my-postgres.example.svc
    existingSecret: my-db-secret     # key `password` (configurable)
```

## Out-of-band Secrets

| Secret | Keys | Used by |
|---|---|---|
| `auth.existingSecret` | `TURNSTONE_JWT_SECRET` | console, server, channel |
| `database.external.existingSecret` (only without `cnpg.enabled`) | `password` (key configurable) | console, server, channel, migrate Job. With `cnpg.enabled` CNPG's generated `<cluster>-app` Secret is used automatically. |
| `config.existingSecret` (optional) | `config.toml` | console, server (`TURNSTONE_CONFIG`); OIDC, `[security]` key, `[models.*]` |
| `channel.existingSecret` (optional) | `TURNSTONE_DISCORD_TOKEN`, `TURNSTONE_SLACK_TOKEN`, `TURNSTONE_SLACK_APP_TOKEN` | channel gateway |
| `llm.existingSecret` (optional) | `OPENAI_API_KEY` | fallback key for model definitions without one |

## Notable options

| Value | Why |
|---|---|
| `cnpg.enabled` | Creates a dedicated CloudNativePG Cluster in the release namespace; `database.external.host`/`existingSecret` default to it. |
| `server.workloadKind: StatefulSet` | Stable node ids (`…-server-N`) that survive restarts; required for Turnstone's "Specific node" affinity to be safe. Default `Deployment` matches upstream. |
| `server.nodeIdFromPodName` | Sets `TURNSTONE_NODE_ID` to the pod name. Without it each start mints `{hostname}_{4hex}`. |
| `httpRoute` | Gateway API route to the console (preferred over `ingress` when a shared Gateway exists). |
| `httpRoute.istioDenyPaths` | Istio `AuthorizationPolicy` on the parent Gateway denying `/metrics` and `/node/*/metrics` for this chart's hostnames (Turnstone serves `/metrics` unauthenticated). |
| `searxng.enabled` | Backs `web_search` for OpenAI-compatible / local models. The Service is named exactly `searxng` so Turnstone's default `tools.searxng_url` resolves with no configuration. |
| `disruptionGuard.enabled` | CronJob toggling `karpenter.sh/do-not-disrupt` on server pods by live workstream state. |
| `pdb.enabled`, `priorityClass.enabled` | One node drains at a time; non-preempting priority. |
| `config.existingSecret` | Mounts the shared bootstrap `config.toml` (see `docs/docker.md`, "Shared bootstrap config"). |

## Model definitions

The server reads models from the console **Models** tab (database) and from
`[models.*]` in `config.toml`; nothing is discovered from an endpoint except
the context window on vLLM-style servers. Through a gateway, set
`context_window` explicitly on each definition.

## Validation

```sh
helm lint .
helm template turnstone . -f <your-values.yaml> | kubectl apply --dry-run=server -f -
```

## Fork images (rvo-redplatform)

The cluster runs a patched build of an upstream release, not upstream's
`ghcr.io/turnstonelabs/turnstone`. Branches:

| Branch | Base | Purpose |
|---|---|---|
| `dev` | upstream `dev` + fork chart commits | Chart Argo renders; upstream sync target. **Not** what images are built from (it is upstream's pre-release). |
| `rvoh/<version>` | upstream tag `v<version>` | Release branch: the tag plus cherry-picked fixes plus `publish-ecr-image.yml`. Images are built from here. |
| `fix/*`, `ci/*` | `dev` | Short-lived PR branches into `dev`. |

Images: `543248655649.dkr.ecr.us-east-1.amazonaws.com/turnstone/turnstone:<version>-rvoh.<n>`,
built by `.github/workflows/publish-ecr-image.yml` (GitHub OIDC into the
nonprod `gha` role, same pattern as the Switchyard fork) when a
`<version>-rvoh.<n>` tag is pushed. The ECR repo lives in
`tfe_redplatform-tools` `application/nonprod/ecr.tf` and is IMMUTABLE, so every
build needs a new `<n>`. Tags carry no leading `v`: upstream's own CI/publish
workflows key on `v*` and must not fire here.

### Shipping a fix (same upstream version)

1. Land the fix on `dev` via PR as usual (this is also where it goes upstream from).
2. Cherry-pick it onto the release branch and tag the next build number:
   ```sh
   git checkout rvoh/1.8.5
   git cherry-pick <fix sha>
   git tag -a 1.8.5-rvoh.2 -m "Turnstone 1.8.5 + <what changed>"
   git push origin rvoh/1.8.5 1.8.5-rvoh.2
   ```
3. Watch Actions -> "Publish turnstone image to ECR". Confirm:
   ```sh
   aws ecr describe-images --repository-name turnstone/turnstone --region us-east-1 \
     --query 'imageDetails[].imageTags' --output text
   ```
4. In `eks-rp-tools-nonprod-crds`, set `image.tag: "1.8.5-rvoh.2"` in
   `nonprod/argocd-apps/turnstone.yaml`, PR, merge, then apply the manifest to
   the Argo hub (`kubectl --context rp-tools-ue1 apply -f ...`; Argo does not
   watch that file) and Sync when the console shows the cluster idle. Rollouts
   interrupt in-flight agent work.

Rollback is the same loop with the previous tag: images are immutable, so
pointing `image.tag` back is always safe within one upstream version.

### Moving to a new upstream release

1. Sync `dev` from upstream (GitHub "Sync fork", or `git fetch
   https://github.com/turnstonelabs/turnstone.git dev && git merge FETCH_HEAD`;
   conflicts, if any, are in `deploy/helm/turnstone`).
2. Start a new release branch from the upstream tag and bring the fork's
   runtime commits over:
   ```sh
   git fetch https://github.com/turnstonelabs/turnstone.git 'refs/tags/v1.9.0:refs/tags/v1.9.0'
   git checkout -b rvoh/1.9.0 v1.9.0
   git cherry-pick <publish-ecr-image.yml commit> <any fix not yet released upstream>
   git tag -a 1.9.0-rvoh.1 -m "Turnstone 1.9.0 + ..." && git push origin rvoh/1.9.0 1.9.0-rvoh.1
   ```
   Drop cherry-picks upstream has already shipped. Read the release's
   "Database migrations" changelog section first: migrations are forward-only,
   so rolling back across upstream versions is not a tag flip.
3. Bump `appVersion` in `Chart.yaml` on `dev`, then follow steps 3-4 above with
   the new tag. Delete the old `rvoh/<version>` branch once nothing runs it.
