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

## Fork branches and images (rvo-redplatform)

The cluster runs a patched build of an upstream release, not upstream's
`ghcr.io/turnstonelabs/turnstone`. `dev` is kept a pure mirror of upstream so
syncing is always a fast-forward; everything we carry is a patch series on top.

| Branch | Base | Purpose |
|---|---|---|
| `dev` | upstream `dev`, exactly | Mirror. Fast-forward only; never commit, merge or open PRs into it. |
| `rvoh/dev` | `dev` + our patch series | Where fork work lands (GitHub default branch, PR target). Rebased onto `dev` at each sync. Upstream PRs are cut from here. |
| `rvoh/<version>` | upstream tag `v<version>` + the patch series | Release branch: what runs. Images are built from it and the Argo Application renders this chart from it. |
| `fix/*`, `feat/*`, `ci/*` | `rvoh/dev` | Short-lived PR branches into `rvoh/dev`. |

Keep the patch series small and topical (one commit per concern) so each sync
rebase stays cheap. Before a rebase, tag the old tip
(`git tag archive/rvoh-dev-<date> rvoh/dev`) so the previous series is
recoverable after the force-push.

Images: `543248655649.dkr.ecr.us-east-1.amazonaws.com/turnstone/turnstone:<version>-rvoh.<n>`,
built by `.github/workflows/publish-ecr-image.yml` (GitHub OIDC into the
nonprod `gha` role, same pattern as the Switchyard fork) when a
`<version>-rvoh.<n>` tag is pushed. The ECR repo lives in
`tfe_redplatform-tools` `application/nonprod/ecr.tf` and is IMMUTABLE, so every
build needs a new `<n>`. Tags carry no leading `v`: upstream's own CI/publish
workflows key on `v*` and must not fire here.

### Syncing from upstream

```sh
git fetch https://github.com/turnstonelabs/turnstone.git dev
git push origin FETCH_HEAD:refs/heads/dev          # fast-forward only; never --force
git tag archive/rvoh-dev-$(date +%F) origin/rvoh/dev
git checkout rvoh/dev && git rebase origin/dev     # resolve conflicts patch by patch
git push --force-with-lease origin rvoh/dev
```

GitHub's "Sync fork" button does the first two lines. Syncing never changes
what runs: the cluster follows the release branch, not `dev`.

### Shipping a fix (same upstream version)

1. Land the fix on `rvoh/dev` via PR.
2. Cherry-pick it onto the release branch and tag the next build number:
   ```sh
   git checkout rvoh/1.8.5
   git cherry-pick <fix sha>
   git tag -a 1.8.5-rvoh.<n> -m "Turnstone 1.8.5 + <what changed>"
   git push origin rvoh/1.8.5 1.8.5-rvoh.<n>
   ```
   A chart-only change needs no tag or image: push the branch and Sync.
3. Watch Actions -> "Publish turnstone image to ECR". Confirm:
   ```sh
   aws ecr describe-images --repository-name turnstone/turnstone --region us-east-1 \
     --query 'imageDetails[].imageTags' --output text
   ```
4. In `eks-rp-tools-nonprod-crds`, set `image.tag: "1.8.5-rvoh.<n>"` in
   `nonprod/argocd-apps/turnstone.yaml`, PR, merge, then apply the manifest to
   the Argo hub (`kubectl --context rp-tools-ue1 apply -f ...`; Argo does not
   watch that file) and Sync when the console shows the cluster idle. Rollouts
   interrupt in-flight agent work.

Rollback is the same loop with the previous tag: images are immutable, so
pointing `image.tag` back is always safe within one upstream version.

### Moving to a new upstream release

1. Sync (above).
2. Replay the patch series onto the upstream tag as a new release branch:
   ```sh
   git fetch https://github.com/turnstonelabs/turnstone.git 'refs/tags/v1.9.0:refs/tags/v1.9.0'
   git checkout -b rvoh/1.9.0 rvoh/dev
   git rebase --onto v1.9.0 origin/dev               # patches upstream already shipped drop out
   git tag -a 1.9.0-rvoh.1 -m "Turnstone 1.9.0 + ..." && git push origin rvoh/1.9.0 1.9.0-rvoh.1
   ```
   Drop by hand anything that only makes sense on upstream's pre-release.
   Read the release's "Database migrations" changelog section first:
   migrations are forward-only, so rolling back across upstream versions is
   not a tag flip.
3. In the Argo Application set `targetRevision: rvoh/1.9.0` and
   `image.tag: "1.9.0-rvoh.1"` together (chart and image move as one), then
   follow step 4 above. Delete the old `rvoh/<version>` branch once nothing
   runs it.
