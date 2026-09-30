# Wrath Unbound — tracked provisioning snapshot

Frozen, byte-identical copy of the Wrath Unbound payload staged into this
tree for [PRO-2](/PRO/issues/PRO-2), plus the compose override. This fork's
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
| `env/dist/etc/modules/mod_ale.conf` | `env/dist/etc/modules/mod_ale.conf` |
| `env/dist/etc/modules/lua_scripts/unbound_mentor.lua` | `env/dist/etc/modules/lua_scripts/unbound_mentor.lua` |
| `docker-compose.override.yml` | `docker-compose.override.yml` (repo root) |

Notes:

- `modules/mod-unbound` needs **no CMakeLists.txt**: with the docker build's
  `MODULES=static`, AzerothCore's `modules/CMakeLists.txt`
  (`GetModuleSourceList` → `CollectSourceFiles`) auto-collects
  `modules/mod-unbound/src/**` into the worldserver static module build. The
  loader entry point is `Addmod_unboundScripts()` (defined in
  `src/UnboundSystem_loader.cpp`), which the generated `ModulesLoader.cpp`
  invokes.
- `mod-ale` and `mod-playerbots` are **not** part of this snapshot — they
  are separate upstream clones, pinned in [`docs/dev-environment.md`](../docs/dev-environment.md).
- `unbound-core-access.patch` is applied with `git apply` from the repo root
  after copying `modules/mod-unbound` into place.

## Staging command

```bash
cp -r wrath-unbound/modules/mod-unbound modules/
mkdir -p env/dist/etc/modules/lua_scripts
cp wrath-unbound/env/dist/etc/modules/mod_ale.conf env/dist/etc/modules/
cp wrath-unbound/env/dist/etc/modules/lua_scripts/unbound_mentor.lua env/dist/etc/modules/lua_scripts/
cp wrath-unbound/docker-compose.override.yml .
git apply modules/mod-unbound/unbound-core-access.patch
grep -n GetUnboundClassMask src/server/game/Entities/Player/Player.h   # idempotence check
```

SQL migrations and apply order: see
[docs/dev-environment.md](../docs/dev-environment.md#first-boot-sql-migrations-one-time-idempotent).
