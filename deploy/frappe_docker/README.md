# pacioli-guard on frappe_docker

> **Status: DRAFTED, NOT YET LAB-PROVEN.** The mechanism below is source-verified against
> frappe_docker, bench, and frappe internals, and it is the direct container translation of the
> classic-bench recipe this repo has already proven live (`deploy/govern.sh`, run records in
> `broker/docs/plans/`). But no image has been built and booted from these files yet. The same bar
> DEPLOY.md holds applies here: this section says "proven" only after a from-blank lab build passes
> `doctor: ready.` Until that line changes, treat this as a recipe, not a promise.

Most of the ERPNext world runs on [frappe_docker](https://github.com/frappe/frappe_docker). This
overlay bakes `pacioli-guard` into a custom frappe_docker image so a containerized site is guarded
the same way a classic-bench site is.

## Why this is not just an apps.json entry

`apps.json` entries are git URLs consumed by `bench get-app`, which is git-only (verified in
`bench/app.py`; there is no pip path and no subdirectory support). `pacioli-guard` ships as a PyPI
wheel. Two facts make the pip route clean anyway:

- Frappe finds an installed app's code by **Python import**, not by looking inside
  `apps/<name>` (`frappe/modules/utils.py::get_pymodule_path`). A wheel in the bench venv is fully
  discoverable, hooks included.
- The only gate on `bench --site X install-app pacioli_guard` is the app's name appearing in
  `sites/apps.txt` (`frappe/installer.py::install_app`). In frappe_docker, that file is regenerated
  on every stack start by the configurator service (`ls -1 apps > sites/apps.txt`), so the durable
  fix is a **stub directory** `apps/pacioli_guard` baked into the image.

So the image layer does exactly two things beyond upstream: `pip install pacioli-guard==<pin>` into
the bench venv, and `mkdir -p apps/pacioli_guard`. Everything else in the Containerfile is
upstream's `images/layered/Containerfile` verbatim.

## Build

Needs Docker Engine v23+ (BuildKit secrets) and a local checkout of frappe_docker (the
Containerfile COPYs upstream's `resources/core/*` entrypoints from the build context).

```bash
git clone --depth 1 https://github.com/frappe/frappe_docker
cp deploy/frappe_docker/apps.json.example apps.json   # erpnext (and any other GIT apps) go here
docker build \
  --build-arg=FRAPPE_BRANCH=version-16 \
  --build-arg=PACIOLI_GUARD_VERSION=<pin, e.g. 0.6.3> \
  --secret=id=apps_json,src=apps.json \
  --tag=<your-registry>/pacioli-erpnext:16 \
  --file=<pacioli-checkout>/deploy/frappe_docker/Containerfile \
  frappe_docker
```

`PACIOLI_GUARD_VERSION` has no default and the build refuses without it. An unpinned governance
layer is an unaccountable one; pin it, and record the pin next to your site.

Note apps.json rides a BuildKit `--secret`, not a build-arg. frappe_docker moved to this in
2026-04 because the old `APPS_JSON_BASE64` build-arg leaked into `docker image history`. If a
tutorial shows the base64 form, it is describing a removed, insecure pattern.

## Run + install

Standard frappe_docker flow with your custom image:

```bash
cd frappe_docker
# custom.env: CUSTOM_IMAGE=<your-registry>/pacioli-erpnext, CUSTOM_TAG=16, PULL_POLICY=missing
docker compose --env-file custom.env \
  -f compose.yaml -f overrides/compose.mariadb.yaml -f overrides/compose.redis.yaml \
  -f overrides/compose.https.yaml config > compose.pacioli.yaml
docker compose -f compose.pacioli.yaml up -d

# New site, guarded from its first credential (guard installs BEFORE any API key exists):
docker compose -f compose.pacioli.yaml exec backend \
  bench new-site --mariadb-user-host-login-scope=% --db-root-password <pw> \
  --install-app erpnext --install-app pacioli_guard --admin-password <pw> <site>

# Existing site:
docker compose -f compose.pacioli.yaml exec backend bench --site <site> install-app pacioli_guard
docker compose -f compose.pacioli.yaml exec backend bench --site <site> migrate
```

After install, scope the seat exactly as `deploy/govern.sh` does on classic bench: dedicated
read-role, api keys, no manager roles, deny-by-default scope from the data lists. The governance
model does not change because the substrate did.

## The broker stays OUTSIDE this stack

`pacioli` (the broker) is deliberately not a compose service here. The deploy road's whole shape is
two hosts: the box that keeps the books is not the box that consents to writes
(`deploy/DEPLOY.md`). Run the broker on its own host or container exactly as `instruments.sh`
documents, pointed at this stack's published HTTP door. Co-locating the consent hand with the books
would quietly delete the separation that makes the consent mean something.

## Known unknowns (close these in the lab run)

- Whether an empty `apps/pacioli_guard` stub is sufficient at `bench build` / asset-collection
  time. Expected yes (guard is a backend-hooks app with no assets), but expected is not proven.
- The upgrade story: rebuilding the image with a newer `PACIOLI_GUARD_VERSION` and re-running
  `bench --site <site> migrate` should be the whole drill; not yet exercised.
