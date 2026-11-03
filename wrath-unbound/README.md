# Wrath Unbound — tracked provisioning snapshot

Frozen copy of the Wrath Unbound payload staged into this tree for
[PRO-2](/PRO/issues/PRO-2), plus the compose override, and (since PRO-9) the
company-authored `mod-custom-classes` module and its conf. This fork's
`.gitignore` excludes `/modules/*`, `/env/dist/*`, and `/*.override.yml`, so
the live provisioned files are untracked; this directory is the tracked
source of truth for reprovisioning (e.g. on the new Docker host).

## Provenance

Extracted verbatim (heredoc payloads `WU_PAYLOAD_EOF_1`…`_19`) from:

- repo: `https://github.com/DadsMmoLab/dads-mmo-lab.git`
- commit: `a093beac29bacd96dea573d1e1517aa8bc1e928e`
- file: `guides/unbound-wrath/install-wrath-unbound-addon.sh` (blob `c0458459e71146be46844f2b61c4887d9704fe21`)
- installer version: Wrath Unbound installer **v1.2.2 (2026-06-14)**

Companion guide: `guides/unbound-wrath/Wrath-Unbound-Addon-HOWTO.md` (blob
`92064ac64e71`). Base-server installer this assumes:
`guides/wow-wotlk/install-wow-wotlk.sh` (blob `b86044345077`).

## What is here

| Snapshot path | Live tree target |
|---|---|
| `modules/mod-unbound/` | `modules/mod-unbound/` (C++ module + SQL + core patch) |
| `modules/mod-custom-classes/` | `modules/mod-custom-classes/` (C++ module + table DDL + core hook patch; company-authored for [PRO-9](/PRO/issues/PRO-9), not part of the installer payload) |
| `env/dist/etc/modules/mod_ale.conf` | `env/dist/etc/modules/mod_ale.conf` |
| `env/dist/etc/modules/mod_custom_classes.conf` | `env/dist/etc/modules/mod_custom_classes.conf` |
| `env/dist/etc/modules/lua_scripts/unbound_mentor.lua` | `env/dist/etc/modules/lua_scripts/unbound_mentor.lua` |
| `docker-compose.override.yml` | `docker-compose.override.yml` (repo root) |
| `docker-compose.loadtest.override.yml` | `docker-compose.loadtest.override.yml` (repo root; PRO-33 load-test profile, not loaded by a plain `docker compose up`) |
| `../scripts/compose-sync.sh` | repo `scripts/compose-sync.sh` — installs both root overrides above from these snapshots; `--check` is the pre-boot drift assertion |

Notes:

- `docker-compose.override.yml` here is the **source of truth** (PRO-178):
  the root `docker-compose.override.yml` is a gitignored, generated copy.
  Edit this file and run `sudo -u pira scripts/compose-sync.sh` (PRO-180) to
  install it (`--check` is read-only and runs as any user);
  never hand-edit the root copy (hand-editing it is what caused the PRO-178
  drift). `docker-compose.loadtest.override.yml` works the same way and is
  tracked here for the first time in PRO-180 — before PRO-180 it existed only
  as an untracked root file, so a clean-clone reprovision silently lost the
  1600–2000 randombot load-test profile. `scripts/compose-sync.sh --check`
  fails if either root copy drifts from its snapshot; there is no allowlist.
  The override also mounts the
  PRO-30 dev DB tuning `conf/mysql-dev.cnf`, which is tracked in the fork
  itself (gitignore exception `!/conf/mysql-dev.cnf`) and needs no staging.
- `modules/mod-unbound` needs **no CMakeLists.txt**: with the docker build's
  `MODULES=static`, AzerothCore's `modules/CMakeLists.txt`
  (`GetModuleSourceList` → `CollectSourceFiles`) auto-collects
  `modules/mod-unbound/src/**` into the worldserver static module build. The
   loader entry point is `Addmod_unboundScripts()` (defined in
   `src/UnboundSystem_loader.cpp`), which the generated `ModulesLoader.cpp`
   invokes. `modules/mod-custom-classes` works the same way (loader entry
   point `Addmod_custom_classesScripts()`, defined in
   `src/CustomClassSystem_loader.cpp`).
- `mod-ale` and `mod-playerbots` are **not** part of this snapshot — they
  are separate upstream clones, pinned in [`docs/dev-environment.md`](../docs/dev-environment.md).
- The core hook patches are **committed in-tree on the company `Playerbot`
  branch** (mod-unbound: `f5b197b8d`; mod-custom-classes: `b732dfd71`), so a
  current fork clone verifies them with the `grep` checks in the Staging
  command below instead of re-applying. On a tree pinned to the upstream
  base `7f12e89ee` (e.g. an upstream-merge test — see
  [Reprovisioning](../docs/dev-environment.md#reprovisioning-from-a-clean-clone-e-g-on-the-new-docker-host)),
  apply them with `git apply` from the repo root after copying the module
  into place: `unbound-core-access.patch` first, then
  `custom-class-core-access.patch` (both verified 2026-10-05).

## Staging command

Run these **as the tree owner**. On the build host the checkout is
`pira:pira` (`0775` dirs, `0664` files) and agent logins such as `scuff-dev`
are not in the `pira` group, so a bare write exits `1` on the first `cp`;
use a `pira` shell or prefix with `sudo -u pira` (shown on the installer
below). `scripts/compose-sync.sh --check` is read-only and safe as any user.

```bash
cp -r wrath-unbound/modules/mod-unbound modules/
cp -r wrath-unbound/modules/mod-custom-classes modules/
mkdir -p env/dist/etc/modules/lua_scripts
cp wrath-unbound/env/dist/etc/modules/mod_ale.conf env/dist/etc/modules/
cp wrath-unbound/env/dist/etc/modules/mod_custom_classes.conf env/dist/etc/modules/
cp wrath-unbound/env/dist/etc/modules/lua_scripts/unbound_mentor.lua env/dist/etc/modules/lua_scripts/
# root overrides are generated copies of the snapshots above (PRO-180):
# the installer WRITES, so it runs as the tree owner; `--check` is the
# read-only pre-boot assertion and needs no sudo
sudo -u pira scripts/compose-sync.sh
# core hook patches are in-tree on Playerbot (f5b197b8d, b732dfd71):
# verify, do not re-apply (on a base-pinned tree, git-apply both patches
# instead, unbound first):
grep -n GetUnboundClassMask src/server/game/Entities/Player/Player.h
grep -n OnPlayerAfterUpdateAllStats src/server/game/Entities/Unit/StatSystem.cpp
grep -n OnPlayerLfgSetRoles src/server/game/Handlers/LFGHandler.cpp
```

SQL migrations and apply order: see
[docs/dev-environment.md](../docs/dev-environment.md#first-boot-sql-migrations-one-time-idempotent).

## Load testing (randombot volume, PRO-33)

The default override boots **50** randombots
(`AC_AI_PLAYERBOT_MIN_RANDOM_BOTS`/`MAX_RANDOM_BOTS` = `50`). A plain
`docker compose up` auto-merges only `docker-compose.yml` +
`docker-compose.override.yml`. The 1600–2000 load-test volume lives in
`docker-compose.loadtest.override.yml` and is enabled explicitly, from the
repo root:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.loadtest.override.yml \
  docker compose up -d --force-recreate ac-worldserver
```

Return to the 50-bot default afterwards:

```bash
docker compose up -d --force-recreate ac-worldserver
```

`AC_AI_PLAYERBOT_RANDOM_BOT_AUTOLOGIN: "1"` stays on in both, so the world
still populates in normal operation. Background: the 1600–2000 boot-time
login storm was the primary stressor behind the auth-chain wedge
([PRO-21](/PRO/issues/PRO-21)); DB-async SPOF hardening is tracked
separately in [PRO-32](/PRO/issues/PRO-32).
