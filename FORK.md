# Working on the rvo-redplatform fork

This repo is RVOH's fork of [turnstonelabs/turnstone](https://github.com/turnstonelabs/turnstone).
It runs on EKS `rp-tools-nonprod-us-east-1` (namespace `turnstone`) via Argo CD, using an
image we build ourselves. This file covers how to change it, release it and keep it in
sync with upstream. Upstream's own process is in `CONTRIBUTING.md` and `docs/releasing.md`.

## Branches

| Branch | What it is | Rules |
|---|---|---|
| `dev` | Exact mirror of upstream `main` | Fast-forward only. Never commit, merge or open PRs into it. |
| `rvoh/dev` | Upstream `main` + our patch series | **Default branch.** All fork work lands here via PR. Rebased onto `dev` at each sync. |
| `rvoh/<version>` (e.g. `rvoh/1.8.5`) | Upstream tag `v<version>` + our patches | **What runs.** The image is built from it and Argo renders the chart from it. |
| `feat/*`, `fix/*`, `ci/*`, `docs/*`, `chore/*` | Short-lived work branches | Branch from `rvoh/dev`, PR into `rvoh/dev`. |

Our patches are a small series of topical commits on `rvoh/dev` (`git log dev..rvoh/dev`).
Each one is reapplied on every sync, so keep them focused and few.

Upstream renamed its default branch from `dev` to `main` (first synced 2026-11-20). Our
mirror keeps the name `dev` so nothing that references it breaks; it now fast-forwards
to upstream `main`.

Fetch upstream by URL. Don't add an `upstream` remote, so nothing can be pushed there by
mistake:

```sh
git fetch https://github.com/turnstonelabs/turnstone.git main
```

## Making a change

```sh
git fetch origin
git switch -c fix/<short-name> origin/rvoh/dev
# edit, commit
```

Test locally before pushing. The fork has **no CI on PRs**: upstream's CI workflow is
disabled in this repo's Actions settings because it would run on every mirror sync.

```sh
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[test,dev]"
ruff check turnstone tests && ruff format --check turnstone tests
mypy turnstone
pytest tests/<relevant tests>      # full suite: pytest (~20 min)
```

Some tests (e.g. `tests/test_release_script.py`, which runs workflow snippets using
`${VAR,,}`) need bash 4+. They fail under macOS's stock bash 3.2; install a newer bash
(`brew install bash`) or run them on Linux before treating the failure as real.

For chart changes, render with the Argo Application's values and compare against what's
deployed. The values live in `eks-rp-tools-nonprod-crds/nonprod/argocd-apps/turnstone.yaml`,
under `spec.source.helm.values`.

```sh
helm lint deploy/helm/turnstone
helm template turnstone deploy/helm/turnstone -n turnstone -f argo-values.yaml > new.yaml
```

The chart is versioned `X.Y.Z-rvoh.N` (upstream's chart version plus our build suffix,
e.g. `0.3.13-rvoh.1`). Bump `N` in `deploy/helm/turnstone/Chart.yaml` for a fork-only chart
change. `scripts/release.sh` accepts this form as well as plain `X.Y.Z`.

Then push and open a PR **into `rvoh/dev`**. It's the default, so `gh pr create` picks it.

```sh
git push -u origin fix/<short-name>
gh pr create --base rvoh/dev
```

Prefer **squash merge** so each change stays one commit. That keeps sync rebases simple.

### Fix it upstream too

If the change isn't RVOH-specific (a bug fix, not chart or CI wiring), also send it
upstream. Branch from upstream `main`, cherry-pick, and open a PR against
`turnstonelabs/turnstone` `main`. Upstream requires signing their CLA (see `CONTRIBUTING.md`).
Once upstream ships the fix, drop our copy at the next sync. The rebase skips it
automatically if upstream merged it unchanged. If upstream reworded or reworked it, drop
it by hand when it conflicts.

## Testing an image before release (optional)

Actions → **Publish turnstone image to ECR** → Run workflow, on your branch. This builds
`…/turnstone/turnstone:sha-<short-sha>`. To try it on nonprod, set `image.tag` to that
value in the Argo Application (see [Deploying](#deploying-an-image-to-nonprod)), and
change it back afterwards. Don't leave nonprod on a `sha-` tag.

## Releasing a fix (same upstream version)

Merging to `rvoh/dev` changes nothing that runs. To ship, cherry-pick onto the release
branch and tag the next build number:

```sh
git fetch origin --tags
git switch rvoh/1.8.5 && git pull --ff-only
git cherry-pick -x <sha from rvoh/dev>
git tag -a 1.8.5-rvoh.<n> -m "Turnstone 1.8.5 + <what changed>"
git push origin rvoh/1.8.5 1.8.5-rvoh.<n>
```

- The tag push runs **Publish turnstone image to ECR**, which builds
  `543248655649.dkr.ecr.us-east-1.amazonaws.com/turnstone/turnstone:1.8.5-rvoh.<n>`.
- Tags have **no leading `v`**. A `v*` tag would trigger upstream's publish workflows.
  Never push upstream's `v*` tags to this repo.
- The ECR repo is **immutable**, so every build needs a new `<n>`. Find the last one with
  `git tag -l '1.8.5-rvoh.*'`.
- A **chart-only** change needs no tag or image. Push the release branch, then refresh
  and Sync in Argo.

Confirm the image exists before deploying it. Deploying a tag that was never pushed puts
every pod in ImagePullBackOff.

```sh
aws ecr describe-images --repository-name turnstone/turnstone --region us-east-1 \
  --query 'imageDetails[].imageTags' --output text
```

## Deploying an image to nonprod

Argo values live in `eks-rp-tools-nonprod-crds`, in the Application manifest
`nonprod/argocd-apps/turnstone.yaml` (`targetRevision` = chart branch, `image.tag` = image).

1. PR in `eks-rp-tools-nonprod-crds` that bumps `image.tag`, then merge it.
2. **Apply the manifest to the Argo hub by hand.** Argo runs in the rp-tools **prod**
   account and does not watch this file:
   ```sh
   kubectl --context rp-tools-ue1 apply -f nonprod/argocd-apps/turnstone.yaml
   ```
3. **Sync manually**, when the console shows no busy workstreams. The app has no
   auto-sync on purpose: a rollout interrupts in-flight agent work, and Turnstone has no
   drain mode.

To roll back within the same upstream version, set the previous `image.tag` and repeat.
Images are immutable, so this is always safe.

## Syncing from upstream

Syncing never changes what runs, because the cluster follows the release branch.

```sh
git fetch origin
git fetch https://github.com/turnstonelabs/turnstone.git main
git push origin FETCH_HEAD:refs/heads/dev                  # upstream main -> our dev; fast-forward, never --force
git tag archive/rvoh-dev-$(date +%F) origin/rvoh/dev        # keep the old series
git push origin archive/rvoh-dev-$(date +%F)
git switch rvoh/dev && git reset --hard origin/rvoh/dev
git rebase origin/dev                                       # resolve conflicts patch by patch
git push --force-with-lease origin rvoh/dev
```

Update `dev` with the fetch-and-push above rather than GitHub's **Sync fork** button: the
branch names no longer match (our `dev`, upstream `main`).

For a large sync, rebase on a `sync/upstream-main-<date>` branch cut from `origin/rvoh/dev`
instead, test it locally, then point `rvoh/dev` at it with `--force-with-lease`. The last
sync (`sync/upstream-main-2026-11-20`) was the first one from upstream `main`.

After a rebase, open PR branches based on the old `rvoh/dev` need `git rebase --onto
origin/rvoh/dev <old-rvoh/dev-tip>`. Tell the team before force-pushing `rvoh/dev`.

## Moving to a new upstream release

Read the release's "Database migrations" section in `CHANGELOG.md` first. Migrations are
forward-only, so rolling back across upstream versions is **not** a tag flip.

```sh
git fetch https://github.com/turnstonelabs/turnstone.git 'refs/tags/v1.9.0:refs/tags/v1.9.0'
git switch -c rvoh/1.9.0 origin/rvoh/dev
git rebase --onto v1.9.0 origin/dev        # replays our series onto the tag
# drop by hand anything that only makes sense on upstream's pre-release
git tag -a 1.9.0-rvoh.1 -m "Turnstone 1.9.0 + rvoh patches"
git push origin rvoh/1.9.0 1.9.0-rvoh.1
```

Then in the Argo Application set `targetRevision: rvoh/1.9.0` and `image.tag: "1.9.0-rvoh.1"`
**together**, so chart and image move as one, and deploy as above. Delete the old
`rvoh/<version>` branch once nothing runs it.

### Exception: 1.9.4-rvoh.1 was cut from upstream `main`, not tag `v1.9.4`

`rvoh/1.9.4` and its first tag `1.9.4-rvoh.1` (2026-11-23) were **not** made with the
rebase above. They point straight at `rvoh/dev` commit `80234075`, which is upstream
`main` `9aae5a3a` plus our series and the AI-DP v1 auth work (oauth_client_credentials,
default headers, run session headers). Upstream `main` `9aae5a3a` is `v1.9.4` plus 21
unreleased upstream commits (console, webui and coordinator refactors) that the AI-DP
work is built on, so replaying it onto `v1.9.4` would have conflicted.

This means:

- `rvoh/1.9.4` has upstream code that isn't in any upstream release, including
  migration 089 (`audit_events` index). With our 090, the schema head is **090**.
- Fixes for `rvoh/1.9.4` are still cherry-picked from `rvoh/dev` as in
  [Releasing a fix](#releasing-a-fix-same-upstream-version), with tags `1.9.4-rvoh.<n>`.
- At the next upstream release, go back to the normal rebase onto its tag. That tag will
  already contain these upstream commits.

## Don'ts

- Don't commit to, merge into or open PRs against `dev`.
- Don't push `v*` tags or upstream's tags to this repo.
- Don't use floating image tags (`latest`, `stable`). Pin an exact `<version>-rvoh.<n>`.
- Don't put secrets in git. They're created out-of-band with `kubectl` and referenced by
  name; see the runbook in `eks-rp-tools-nonprod-crds/nonprod/argocd-apps/turnstone/README.md`.
- Don't point the Argo Application at `dev` or `rvoh/dev`. It tracks a release branch.

## Reference

- Chart and its values: `deploy/helm/turnstone/README.md`
- Cluster runbook (secrets, first deploy, gotchas): `eks-rp-tools-nonprod-crds`
  `nonprod/argocd-apps/turnstone/README.md`
- Image workflow: `.github/workflows/publish-ecr-image.yml`
- ECR repo and the GitHub OIDC role: `tfe_redplatform-tools` `application/nonprod/ecr.tf`, `iam.tf`
