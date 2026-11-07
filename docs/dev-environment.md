# AzerothCore + Wrath Unbound Dev Environment (PRO-2)

Dev environment for the Unbound Wrath WotLK project. Provisioned per
[PRO-2](/PRO/issues/PRO-2) on 2026-09-30. This doc is the single source of
truth for repo pinning, build/run commands, `env/` config, and the expected
healthy-boot log lines. QA and all engineers: start here.

**Status:** provisioned, built, booted, and **fully accepted (2026-10-27)**.
Current stack verified 2026-10-27 00:53 UTC on `AzerothCore rev.
34d6f4b0e8c7+` (Playerbot branch, built 2026-10-25, includes the PRO-32
async-DB hardening): `ready...`, `[UNBOUND] Prereq map built.`, `custom
class registry ready: 1 template(s) active`, no errors/crash. The final
in-game acceptance item — `.npc add 900001` from a GM3 session — **PASSED**
in [PRO-43](/PRO/issues/PRO-43) (QA, 2026-10-27): login/enter-world, the
persistent Mentor visible at -8830/614/93.5, a second Mentor spawned and
persisted, no errors, ALE gossip menu answered. See
[Verification](#verification-what-healthy-looks-like) and
[Worldserver console vs in-game GM commands](#worldserver-console-vs-in-game-gm-commands).

---

## Pinned references (the team depends on these)

| Item | Source | Pinned revision |
|---|---|---|
| AzerothCore base | `https://github.com/mod-playerbots/azerothcore-wotlk.git` (Dad's MMO Lab Playerbots fork) | branch `Playerbot`, commit **`7f12e89ee5f467a50e62eba1d525eac7dc953d03`** (2026-09-20, "Merge pull request #254 from mod-playerbots/test-staging") |
| mod-ale (Eluna/ALE Lua engine) | `https://github.com/azerothcore/mod-ale.git` | commit **`1cb86c9600260c3731c96dc3c98d25b4fc3f2153`** ("fix(PlayerMethods): SendListInventory not respecting vendorId override (#384)") — pinned by Wrath Unbound installer v1.2.2; do not move without QA re-verification |
| mod-playerbots | `https://github.com/mod-playerbots/mod-playerbots.git` | branch `master`, commit **`7bae1b5c58c76a0aa20381155edc08096d1485b2`** at provisioning time (shallow clone, per base installer) |
| Wrath Unbound payload | `https://github.com/DadsMmoLab/dads-mmo-lab.git` | commit `a093beac29bacd96dea573d1e1517aa8bc1e928e`; payload embedded in `guides/unbound-wrath/install-wrath-unbound-addon.sh` (blob `c0458459e711`, installer v1.2.2, 2026-06-14). Tracked snapshot in [`wrath-unbound/`](../wrath-unbound/README.md) |

Compatibility note: Wrath Unbound's known-compatible baseline is core
revision `e98e7a97e3f2+` on the Playerbot branch with ACDB 335.16-dev
(2026-05-29). Verified `e98e7a97e3f2` is an ancestor of the pinned commit
`7f12e89e…` in this checkout.

## Git remotes (updated 2026-10-18)

The company forks live in the `Scuff-Project-Maple` GitHub org. `origin` in
every repo in this tree points at the fork; that is the only remote
configured. The `upstream` remotes that existed in early October were
removed on 2026-10-01 (board decision in the PRO-7 thread). The upstream
source URLs are kept below for provenance only (every core patch remains a
merge risk against them); add a temporary `upstream` remote for an upstream
merge and remove it again afterwards.

| Repo | `origin` (working) | Upstream source (provenance, no remote) |
|---|---|---|
| azerothcore-wotlk (this tree, branch `Playerbot`) | `https://github.com/Scuff-Project-Maple/azerothcore-wotlk.git` | `https://github.com/mod-playerbots/azerothcore-wotlk.git` |
| `modules/mod-ale` (nested repo) | `https://github.com/Scuff-Project-Maple/mod-ale.git` | `https://github.com/azerothcore/mod-ale.git` |
| `modules/mod-playerbots` (nested repo) | `https://github.com/Scuff-Project-Maple/mod-playerbots.git` | `https://github.com/mod-playerbots/mod-playerbots.git` |

The fork `Playerbot` branch intentionally diverges from the pinned base
(`7f12e89e…`, see table above): it carries the company's T1/Wrath-Unbound
commits on top of it. `mod-playerbots` fork `master` tracks `7bae1b5c…`;
the `mod-ale` fork's `master` is at upstream HEAD — check out the pinned
`1cb86c96…` when provisioning.

**Push status (synced, verified 2026-10-27):** the fork `Playerbot` branch is
the source of truth (head `a6c0eb14d`); it carries the
T1/Wrath-Unbound base (`f5b197b8d`–`8c16d1194`), the PRO-9/PRO-10 module +
SQL work (`b732dfd71`–`4805857c4`), and the PRO-3/PRO-16/PRO-20 docs
(`e025c7dc9`–`f76e13468`) — all four PRO-15 commits (`b732dfd71`,
`deaf1109c`, `9c0db4ef6`, `ad3393eb7`) verified as ancestors of the fork
head. Pushes go through the company's managed GitHub
identity (PRO-7); cloning that branch is the reprovisioning path — see
[Reprovisioning](#reprovisioning-from-a-clean-clone-e-g-on-the-new-docker-host).

## Provisioned tree layout

This repo's `.gitignore` intentionally keeps provisioned content out of the
git index: `/modules/*`, `/env/dist/*`, and `/*.override.yml` are all
gitignored. Provisioned files therefore live in the **working tree** of this
checkout; only the core-source changes (`worldserver.conf.dist`), this doc,
the `scripts/compose-sync.sh` and `scripts/lua-sync.sh` installers, and the
`wrath-unbound/` snapshot are
tracked in git. The two root compose overrides are **generated** copies of
their `wrath-unbound/` snapshots and must not be hand-edited — see
[Keep the compose overrides in sync](#keep-the-compose-overrides-in-sync-pre-boot-assertion)
(PRO-180). The ALE scripts under `env/dist/etc/modules/lua_scripts/` are
likewise **generated** copies of their snapshots here and must not be
hand-placed — see
[Keep the ALE Lua scripts in sync](#keep-the-ale-lua-scripts-in-sync-pre-boot-assertion)
(PRO-269).

```
<repo root>  (= this checkout of azerothcore-wotlk @ 7f12e89e…)
├── modules/
 │   ├── mod-unbound/                      # Wrath Unbound C++ module (staged, not committed by fork design)
 │   │   ├── src/UnboundSystem.cpp         #   PLAYERHOOK_ON_LOGIN builds m_unboundClassMask
 │   │   ├── src/UnboundSystem_loader.cpp  #   Addmod_unboundScripts() (auto-collected build; no CMakeLists needed)
 │   │   ├── npc_setup.sql                 #   Mentor NPC creature_template 900001 (apply before first start)
 │   │   ├── data/sql/db-world/01..14_*.sql
 │   │   ├── data/sql/db-characters/01_unbound_characters.sql
 │   │   └── unbound-core-access.patch     #   6-file core patch (in-tree on Playerbot, kept for re-apply after rebases)
 │   ├── mod-custom-classes/               # data-driven custom class engine (PRO-9; staged from snapshot)
 │   │   ├── src/CustomClassSystem*.cpp    #   registry + player hooks (auto-collected build; no CMakeLists needed)
 │   │   ├── data/sql/db-world/01_custom_class_template.sql  # table DDL (DB-updater auto-applies)
 │   │   └── custom-class-core-access.patch  # 6-file core hook patch (in-tree on Playerbot, kept for re-apply after rebases)
 │   ├── mod-ale/                          # nested git repo @ 1cb86c96…
 │   └── mod-playerbots/                   # nested git repo @ 7bae1b5c… (depth-1 master)
├── env/dist/etc/modules/
 │   ├── mod_ale.conf                      # ALE.Enabled = 1, ALE.ScriptPath = /azerothcore/env/dist/etc/modules/lua_scripts
 │   ├── mod_custom_classes.conf           # CustomClasses.Enabled = true (PRO-9)
 │   └── lua_scripts/                      # installed by scripts/lua-sync.sh from wrath-unbound/ (PRO-269) — never hand-placed
 │       ├── unbound_mentor.lua            #   The Mentor gossip Lua (loaded by ALE at worldserver start)
 │       └── ranger_vex_gossip.lua         #   Ranger Vex (900200) flavor gossip, T4 "The Murloc Tide" (PRO-269)
├── docker-compose.override.yml           # generated copy of wrath-unbound/docker-compose.override.yml (PRO-178)
├── docker-compose.loadtest.override.yml  # generated copy of wrath-unbound/docker-compose.loadtest.override.yml (PRO-180)
├── scripts/compose-sync.sh               # installs the two root copies; --check asserts no drift (PRO-180)
├── scripts/lua-sync.sh                   # installs the ALE Lua scripts; --check asserts no drift and no hand-placed orphan (PRO-269)
├── src/server/apps/worldserver/worldserver.conf.dist   # modified: ValidateSkillLearnedBySpells = 0
├── docs/dev-environment.md               # this doc
└── wrath-unbound/                        # tracked snapshot of the provisioned payload (reprovisioning source)
```

Services (from the stock `docker-compose.yml`, all started by one command):
`ac-database` (mysql:8.4), `ac-db-import`, `ac-client-data-init`,
`ac-worldserver`, `ac-authserver`. Compose project resolves from CWD —
**always `cd` into the repo root before compose commands**. Except that
`ac-worldserver` is not started by a bare `docker compose up` — its world-DB
updater must clear the [PRO-179 pre-flight gate](#build--run) first.

## Single-writer lease (one worldserver, one writer at a time)

There is exactly **one** `ac-worldserver` on the [build host](#build-host) and
no mutual exclusion, so a restart under someone else's run silently destroys
their verification — it cost [PRO-36](/PRO/issues/PRO-36) its T4.7 e2e gate on
2026-10-28. Before you restart/recreate `ac-worldserver`, run
`docker compose up`, apply a DB migration, **or run the e2e suite**, you MUST
hold the lease; `action` is one of `worldserver-restart`, `e2e-suite`,
`db-migration`, `compose-up`. If `check` below reports `LEASE_HELD_LIVE`: **do
not proceed** — wait, or coordinate in the holder's issue thread. A lease that
will not parse counts as stale-breakable, and breaking a stale or unparseable
lease is the **only** time you `rm -f` a lease that is not yours — it needs a
comment on **your** issue naming it (`agentId`, `issueId`, `acquiredAt`,
`expectedMinutes`). Release when the action finishes, including on failure;
`release` refuses unless the lease names you. Lease file:
`/mnt/data/wow-server/.stack/lease` (JSON, one line); the `.stack/` dir is
`2775 scuff-dev:pira` so both host drivers can write it (the compose root
became agent-writable too with
[PRO-198](/PRO/issues/PRO-198) — see
[Git access on the compose root](#git-access-on-the-compose-root)). No cron,
no daemon: the read is the rule.

```bash
# ACQUIRE — atomic (O_EXCL); prints LEASE_HELD + the holder and exits non-zero if someone is in there
ssh -o BatchMode=yes 192.168.1.71 'cd /mnt/data/wow-server && mkdir -p .stack && ( set -o noclobber; jq -cn --arg agent "<agent-id>" --arg issue "<PRO-xx>" --arg action "<action>" --arg at "$(date -u +%FT%TZ)" --argjson mins 90 "{agentId:\$agent,issueId:\$issue,action:\$action,acquiredAt:\$at,expectedMinutes:\$mins}" > .stack/lease ) && echo LEASE_ACQUIRED || { echo LEASE_HELD; cat .stack/lease; exit 1; }'

# CHECK — prints LEASE_FREE | LEASE_HELD_LIVE | LEASE_STALE_BREAKABLE (+ the lease JSON)
ssh -o BatchMode=yes 192.168.1.71 'cd /mnt/data/wow-server && { [ -f .stack/lease ] && { jq -er "(.acquiredAt|fromdate)+(.expectedMinutes*60) > (now|floor)" .stack/lease >/dev/null && { echo LEASE_HELD_LIVE; cat .stack/lease; } || { echo LEASE_STALE_BREAKABLE; cat .stack/lease; }; } || echo LEASE_FREE; }'

# RELEASE — always, including on failure; refuses unless the lease names you
ssh -o BatchMode=yes 192.168.1.71 'cd /mnt/data/wow-server && if jq -e --arg a "<agent-id>" --arg i "<PRO-xx>" ".agentId==\$a and .issueId==\$i" .stack/lease >/dev/null 2>&1; then rm -f .stack/lease && echo LEASE_RELEASED; else echo LEASE_NOT_YOURS; cat .stack/lease 2>/dev/null; fi'
```

## Build & run

All commands in this section run **on the [build host](#build-host)**, over
BatchMode SSH (agent sandboxes have no Docker). Take the
[single-writer lease](#single-writer-lease-one-worldserver-one-writer-at-a-time)
before any command below that builds, starts, restarts or recreates a container,
or applies SQL — read-only inspection commands do not need it.

> **Gate every worldserver start (PRO-179).** The AzerothCore world-DB updater
> runs the moment `ac-worldserver` starts. `ops/deploy/worldserver-up.sh` runs
> the [PRO-179 pre-flight gate](../ops/preflight/README.md) and only then
> `docker compose up -d ac-worldserver`; on a gate failure it prints
> `REFUSING BOOT` and exits non-zero **without touching the stack**. Never run
> `docker compose up/start/recreate ac-worldserver` around the gate. Two
> sanctioned forms:
>
> - **Any worldserver boot** (the common case):
>   `bash ops/deploy/worldserver-up.sh` — gate, then `docker compose up -d
>   --no-deps ac-worldserver`. Recognised compose flags are forwarded, so
>   `bash ops/deploy/worldserver-up.sh --force-recreate` and
>   `COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.loadtest.override.yml \
>   bash ops/deploy/worldserver-up.sh` stay gated (`-f/--file` and
>   `-p/--project-name` are forwarded to compose *and* to the gate, so the gate
>   reads the same files compose will).
>   **The boot is `--no-deps` by default (PRO-306 B1).** `ac-db-import` and
>   `ac-client-data-init` are `service_completed_successfully` dependencies of
>   `ac-worldserver`, so an unqualified `up -d ac-worldserver` re-creates both
>   one-offs on every boot and re-runs the world-DB updater against the **live**
>   `acore_world` — inside a run the runbook describes as "gate passed, read-only
>   checks" (observed on all five PRO-299/PRO-303 boots; same shape as
>   [PRO-292](/PRO/issues/PRO-292)). The wrapper now asserts what compose used to
>   do implicitly — `ac-database` running and healthy, `acore_world.updates` /
>   `updates_include` already populated, `ac-client-data` volume present and the
>   last `ac-client-data-init` exited 0 — prints those as `[dep 1..3]`, prints a
>   `BOOT MODE:` line naming the mode it ran in, and prints
>   `acore_world.updates` rows before and after so a silent import cannot hide.
>   Where you **want** the one-offs (first import on a cold stack, or after a SQL
>   change you want the updater to apply), say so: `--with-one-offs` (or
>   `BOOT_MODE=with-one-offs`), and the log then states the boot is not read-only.
>   `--no-deps` itself is refused as an argument — the mode must always be
>   something the log names.
> - **Anything the wrapper genuinely cannot express** — `up -d --build`,
>   `docker compose restart` — run the gate alone first and boot only if it
>   PASSes: `PREFLIGHT_CHECK_ONLY=1 bash ops/deploy/worldserver-up.sh`
>   (exit 0 = PASS), then run your exact `docker compose` command. Never the
>   other way round.
>
> The wrapper **verifies** the [single-writer lease](#single-writer-lease-one-worldserver-one-writer-at-a-time)
> at `/mnt/data/wow-server/.stack/lease` and refuses to boot if it is missing,
> unreadable or past its `expectedMinutes` window; it never **takes** it (the
> gate is CI-safe and read-only, and a gate that could grab the lease would let
> a CI run starve the host boot path). Take the lease yourself before the
> wrapper. `PREFLIGHT_CHECK_ONLY=1` skips the lease check because nothing in
> the stack is touched.

Fresh provisioned tree (build every image; 2–4 h on a typical host, less on the
Steam Deck baseline the guide targets). The updater runs when `ac-worldserver`
starts and the gate reads the updater ledger from the live DB, so on a cold
stack bring the database up first, gate, then start the worldserver (authserver
and client-data are independent of the world updater):

```bash
cd <repo root>                       # = /mnt/data/wow-server on the host
docker compose build                 # builds all images; starts no container
docker compose up -d ac-database     # one-shot ac-db-import loads the base schema
# wait until ac-database is healthy and ac-db-import has finished
bash ops/deploy/worldserver-up.sh    # gate, then up -d ac-worldserver
docker compose up -d ac-authserver   # independent of the world updater
```

After **any** core/module C++ change (the normal Scuff-Core loop —
incremental, ~30–90 min), the rebuild stays a plain compose build and only the
worldserver start is gated:

```bash
docker compose build ac-worldserver
bash ops/deploy/worldserver-up.sh    # gate, then up -d ac-worldserver (new image)
```

If compose does not recreate on the new image and you must force it, forward
the flag through the gate — do not step around it:

```bash
bash ops/deploy/worldserver-up.sh --force-recreate
```

Stop is un-gated (no updater runs while stopped); bring it back through the
gate, not `docker compose start`:

```bash
docker compose stop ac-worldserver               # fine — updater does not run
bash ops/deploy/worldserver-up.sh                # gate, then up -d ac-worldserver
```

### Keep the compose overrides in sync (pre-boot assertion)

`docker compose` only ever reads the two **gitignored root** overrides, so a
stale root copy is a silent config regression — that is how PRO-33, PRO-65 and
PRO-178 all drifted (the tracked snapshot and the root copy were hand-edited
independently). Since PRO-180 the `wrath-unbound/` snapshots are the single
source of truth and `scripts/compose-sync.sh` owns the root copies:

```bash
cd <repo root>
scripts/compose-sync.sh --check     # read-only pre-boot assertion; any user
sudo -u pira scripts/compose-sync.sh  # install: this WRITES — tree owner only
```

Run `--check` **before** any `docker compose up`/`build`, and as the first step
of the worldserver rebuild loop above. There is deliberately no allowlist: each
pair must be byte-identical (`cmp`). Edit `wrath-unbound/*.override.yml`, never
the root copies.

**Run the install as the tree owner.** The build-host checkout is
`pira:pira` (`0775` dirs, `0664` files) and agent logins such as `scuff-dev`
are not in the `pira` group, so a bare `scripts/compose-sync.sh` exits `1` on
the first `cp`; prefix it with `sudo -u pira` (as above) or run from a `pira`
shell. `--check` only reads and is safe as any user.

### Keep the ALE Lua scripts in sync (pre-boot assertion)

ALE loads every `*.lua` it finds in `ALE.ScriptPath`
(`env/dist/etc/modules/lua_scripts`, gitignored via `/env/dist/*`), so a script
that is only ever hand-placed there is invisible to git: it survives until
something re-syncs or recreates that directory, and then the worldserver
simply stops loading it. That is exactly how `ranger_vex_gossip.lua` (Ranger
Vex `900200`, quests `900101`/`900102`) ended up live with no tracked copy
behind it — [PRO-163](/PRO/issues/PRO-163) red flag **R6**, filed as
[PRO-269](/PRO/issues/PRO-269). A lost script reads as a content regression in
whatever change is under test, which is the misattribution the PRO-37 smoke
pass must not make.

Since PRO-269 the tracked snapshots under
`wrath-unbound/env/dist/etc/modules/lua_scripts/` are the single source of
truth and `scripts/lua-sync.sh` owns the deployed directory:

```bash
cd <repo root>
scripts/lua-sync.sh --check        # read-only pre-boot assertion; any user
sudo -u pira scripts/lua-sync.sh   # install: this WRITES — tree owner only
```

Run `--check` alongside `scripts/compose-sync.sh --check` before any
`docker compose up`/`build`. Like compose-sync there is no allowlist: every
tracked script must be byte-identical (`cmp`), and — the part compose-sync has
no analogue for — a `*.lua` in the deployed directory that the snapshot does
not have is reported as an **orphan** and also fails the check. That is the
signal that would have caught PRO-269 at boot time. The installer never
deletes an orphan: a hand-placed script is either committed into the snapshot
directory (the normal case) or removed by hand, deliberately, because silently
deleting a script a live worldserver depends on is the failure being prevented.

Confirming the install actually took effect is a log check, not a file check
(`ALE.BytecodeCache = true`): `docker logs ac-worldserver | grep UNBOUND` must
show a load line per script, e.g.
`[UNBOUND] ranger_vex_gossip.lua loaded: entry 900200, quests 900101/900102`.

### Load testing (randombot volume, PRO-33)

The stack runs **50/50** randombots in normal operation
(`AC_AI_PLAYERBOT_MIN/MAX_RANDOM_BOTS` in `docker-compose.override.yml`). The
1600–2000 volume is the PRO-33 load-test profile and lives only in
`wrath-unbound/docker-compose.loadtest.override.yml` (tracked since PRO-180;
the root copy is generated). It is **not** merged by a plain
`docker compose up`:

```bash
cd <repo root>
scripts/compose-sync.sh --check
scripts/lua-sync.sh --check
COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.loadtest.override.yml \
  bash ops/deploy/worldserver-up.sh --force-recreate
# back to the 50-bot default:
bash ops/deploy/worldserver-up.sh --force-recreate
```

Take the single-writer lease (`/mnt/data/wow-server/.stack/lease`) before any
recreate, and health-check afterwards (`RestartCount=0`, empty `Errors.log`,
the expected number of `Bot … logged in` lines).

Verify the loaded count after boot: the `bot_count` row in
`acore_playerbots.playerbots_random_bots` (owner=0, bot=0) should read 50
(default) or 1600–2000 (load test):

```bash
docker exec ac-database mysql -u root -ppassword acore_playerbots \
  -e "SELECT event, value FROM playerbots_random_bots WHERE owner = 0 AND bot = 0;"
```

and the online `rndbot%` characters should match within a few minutes. The
connection has no default database, so both tables must be fully qualified
(`acore_characters.characters`, `acore_auth.account`) — bare table names fail
with `ERROR 1046 (No database selected)`:

```bash
docker exec ac-database mysql -u root -ppassword \
  -e "SELECT COUNT(*) AS online_bots
      FROM acore_characters.characters c
      JOIN acore_auth.account a ON a.id = c.account
      WHERE a.username LIKE 'rndbot%' AND c.online = 1;"
```

Background: the boot-time login storm was the primary stressor
behind the auth-chain wedge
([PRO-21](/PRO/issues/PRO-21)); DB-async SPOF hardening is tracked
separately in [PRO-32](/PRO/issues/PRO-32).

## Build host

The live stack runs on a provisioned Docker host. All builds, boots, log
checks, and in-game tests happen there — agent sandboxes have no Docker.
Reachability from an agent run was re-verified by QA on 2026-10-18
([PRO-16](/PRO/issues/PRO-16)); re-verify after any host-side change.

| Property | Value |
|---|---|
| Address | `192.168.1.71` (hostname `pve-wow-dev`) |
| SSH | `ssh 192.168.1.71` — already configured in `~/.ssh/config` (no `-i` needed) |
| User | `scuff-dev` (member of groups `scuff-dev`, `sudo`, `docker`, `pira`, `acore-git` — see [Git access on the compose root](#git-access-on-the-compose-root)) |
| Key | `~/.ssh/id_ed25519_pve_wow_dev` (referenced by the `~/.ssh/config` entry) |
| OS | Ubuntu 24.04.2 LTS, kernel 6.8.0-146-generic |
| Docker | Engine 29.8.0, storage driver `overlayfs` |
| Compose | v5.5.1 |
| CPU / RAM | 8 cores / 23 GB total (17 GB available at check) |
| Disk | `/` = 61 GB (35 GB free); **`/mnt/data` = 251 GB (215 GB free)** |
| Outbound net | github.com `200`, registry-1.docker.io reachable |
| Compose root | `/mnt/data/wow-server` |
| Published ports | worldserver `8085` (+ client-data `7878`), authserver `3724`; ac-database also publishes `3306` |

Access model is **SSH**, not an exposed Docker API. Agents run
non-interactive, so always use BatchMode. The one-liner to check the stack:

```bash
ssh -o BatchMode=yes 192.168.1.71 'cd /mnt/data/wow-server && docker compose ps'
```

Expected: `ac-worldserver`, `ac-authserver`, `ac-database` (healthy) all
`Up`. `portainer_agent` and `rathole-client` also run on this host but in a
separate compose project, so they do not appear in that command.

The [Verification](#verification-what-healthy-looks-like) section applies
unchanged on this host; the canonical quick check:

```bash
ssh -o BatchMode=yes 192.168.1.71 'docker logs ac-worldserver | grep UNBOUND'
```

A healthy boot ends with `[UNBOUND] Prereq map built.` and the stock
`ready...` marker. `ValidateSkillLearnedBySpells = 0` was confirmed in
`env/dist/etc/worldserver.conf` on the host.

### Git access on the compose root

`/mnt/data/wow-server` is owned `pira:pira` and is the **build provenance of the
running `ac-worldserver` binary** ([PRO-69](/PRO/issues/PRO-69)). Agents work on
it as `scuff-dev` without ever taking ownership. Two groups make that work, and
neither one is optional:

| Path | Group | Mode | Group that grants access |
|---|---|---|---|
| `.git/` dirs | `acore-git` | `drwxrwsr-x` (setgid) | `acore-git` — members `scuff-dev,pira` |
| `.git/` files (`HEAD`, `config`, `FETCH_HEAD`, refs) | `acore-git` | `-rw-rw-r--` | `acore-git` — same members |
| Worktree dirs | `pira` | `drwxrwxr-x` | `pira` — member `scuff-dev` |
| Worktree tracked files | `pira` | `-rw-rw-r--` (executables such as `acore.sh` carry `x`) | `pira` — member `scuff-dev` |

Under `.git` only the *directories* are setgid; the files in them are plain
group-writable. Modes re-verified on the host 2026-11-03 with
`stat -c "%A %U:%G %n" .git .git/HEAD .git/config .git/FETCH_HEAD`.

The split is historical. [PRO-70](/PRO/issues/PRO-70) fixed the **`.git`** half
only, and deliberately avoided widening agent membership by creating the
dedicated `acore-git` group plus `git config core.sharedRepository group` in
`.git/config`. That fixed `git fetch` (the frozen-`origin/Playerbot` bug) but
left `git checkout`/`switch`/`reset`/`apply` failing, because the *worktree* is
group-owned `pira` and was already `g+w` end to end — membership was the only
zero-`chmod`, zero-`chown` path. [PRO-198](/PRO/issues/PRO-198) closed it with:

```bash
sudo usermod -aG pira scuff-dev
```

Final membership (verify with `id` / `getent group` on the host):

```
uid=1001(scuff-dev) gid=1001(scuff-dev) groups=1001(scuff-dev),27(sudo),988(docker),1000(pira),1002(acore-git)
pira:x:1000:scuff-dev
acore-git:x:1002:scuff-dev,pira
```

Live `id` output is a **single line** — the block above is intentionally
unwrapped so a diff against reality does not produce a false mismatch
(re-verified on the host 2026-11-03).

Keep `acore-git` regardless of `pira`: `.git` relies on it plus the setgid bit
and `core.sharedRepository=group`, and `pira` membership alone does not restore
fetch if that group ownership is lost.

**Rules for agents on this tree:**

- **Never `chown`** any part of `/mnt/data/wow-server`, and never `chown -R`.
  Ownership stays `pira`; shared access is a *group* problem, solved with
  groups. A `chown` breaks the build provenance and the `acore-git`/`pira`
  model at once.
- Never `chgrp -R` / `chmod -R` over the worktree without explicit CEO/board
  approval — that rewrites permissions across the running binary's provenance.
- Git writes to the host tree (`checkout` / `switch` / `reset --hard` / `apply`)
  require the [single-writer lease](#single-writer-lease-one-worldserver-one-writer-at-a-time).
  The write itself is permitted — the tree is plain fork content, see
  [Host tree layout](#host-tree-layout-resynced-to-fork-head-2026-10-18) — but a
  branch move under a running worldserver invalidates someone else's
  verification, which is what the lease exists to prevent. Never `reset --hard`
  away work you did not create, never re-clone, never delete the tree. Keep the
  tree on `Playerbot` unless a lease-holding task says otherwise.
- A group change needs a **new SSH session** to take effect — existing
  processes keep stale credentials. Verify with a fresh `ssh … 'id'`, not from
  the session in which you ran `usermod`.
- Caveat: the compose root is **not** setgid, so files an agent creates land
  `scuff-dev:scuff-dev` and are read-only to `pira`. Making the *directories*
  setgid would fix that, but it is a `chmod` over the provenance tree and
  therefore needs the same CEO/board approval as the rule above.

The read-only probe, and the two checks that prove the fix **without touching
the worktree or the running stack**. `git fetch` is not a no-op: it writes
`FETCH_HEAD`, objects and `refs/remotes/origin/*` into `.git` (and can trigger
`git gc`) — that is precisely the write `acore-git` + `core.sharedRepository=group`
exist to allow, and it needs no lease because it never touches the worktree or
the stack:

```bash
# W_OK must be True for ., src, modules, .git, acore.sh, CMakeLists.txt.
# src/AzerCore is listed for completeness and is EXPECTED to print False — see
# the note under this block.
ssh -o BatchMode=yes 192.168.1.71 'cd /mnt/data/wow-server && python3 -c "
import os
for p in [\".\",\"src\",\"modules\",\"/mnt/data/wow-server/.git\",\"acore.sh\",\"CMakeLists.txt\",\"src/AzerCore\"]:
    print(f\"{p:32s} W_OK={os.access(p, os.W_OK)}\")
"'
# Anti-frozen-ref. Each fetch_exit must be 0, ls_remote_exit must be 0, and the
# ref comparison is its own statement, so a fetch/permission failure or an
# ls-remote network failure can no longer be misread as a frozen ref.
# Expected as run 2026-11-03:
#   fetch_exit=0  fetch_exit=0  ls_remote_exit=0  REF_TRACKS_REMOTE
ssh -o BatchMode=yes 192.168.1.71 'cd /mnt/data/wow-server || exit 9
git fetch origin; echo "fetch_exit=$?"
git fetch origin; echo "fetch_exit=$?"
refs=$(git ls-remote origin refs/heads/Playerbot); echo "ls_remote_exit=$?"
remote=$(printf "%s\n" "$refs" | cut -f1)
echo "local=$(git rev-parse origin/Playerbot) remote=${remote:-none}"
if [ -n "$remote" ] && [ "$(git rev-parse origin/Playerbot)" = "$remote" ]; then
  echo REF_TRACKS_REMOTE
else
  echo REF_FROZEN_OR_UNAVAILABLE
fi'
```

`src/AzerCore` is not present in this tree (`ls src/` → `cmake CMakeLists.txt
common genrev server test tools`); a `W_OK=False` there means *missing path*,
not a permissions gap — do not chase it.

### Host tree layout (resynced to fork head, 2026-10-18)

The host tree at `/mnt/data/wow-server` was resynced to the fork on
2026-10-18 as the closing step of
[PRO-2](/PRO/issues/PRO-2). It now matches the provisioned layout in this
doc:

| Item | State (verified 2026-10-19) |
|---|---|
| Core | branch `Playerbot` @ fork head (in-tree core hook patches committed: `f5b197b8d` unbound, `b732dfd71` custom-classes) |
| Git remotes | `origin` → `https://github.com/Scuff-Project-Maple/azerothcore-wotlk.git` (the previous base-repo remote was replaced) |
| Modules | plain source trees staged from the pinned/fork-snapshot sources: `mod-ale` @ `1cb86c960026`, `mod-playerbots` @ `7bae1b5c58c7`, `mod-unbound` + `mod-custom-classes` from the tracked `wrath-unbound/` snapshot, plus host-only `mod-arac` (kept) |
| Conf | `env/dist/etc/modules/mod_ale.conf`, `mod_custom_classes.conf` in place; `lua_scripts/unbound_mentor.lua` + `lua_scripts/ranger_vex_gossip.lua` installed by `scripts/lua-sync.sh` from the tracked snapshot (PRO-269 — never hand-placed); live `worldserver.conf` untouched |
| Running binary | `AzerothCore rev. 34d6f4b0e8c7+ (Playerbot branch)`, built 2026-10-25 21:12 from this tree (the `+` marks untracked content in the tree at build time). Earlier verified run: `fd6df4549+` (2026-10-19) |

Before the resync, safety artifacts were written to
`/mnt/data/backups/pre-fork-resync-20261018/`: a gzip-verified dump of all
four databases, and the full pre-reset worktree patch (12 files — the
committed core patches **plus** an in-progress hook variant belonging to
[PRO-9](/PRO/issues/PRO-9), re-land it from there if still wanted).

Consequences for anyone working on the host:

- The `acore/ac-wotlk-worldserver:master` and
  `acore/ac-wotlk-authserver:master` images are **locally built from this
  tree**, not Docker Hub pulls (running banner in the table above).
- `mod-custom-classes` is staged and enabled (`CustomClasses.Enabled =
  true`); the build-blocking snapshot bugs were fixed on the fork
  (`f484ebf21`, `3ba856b98`, `8a7fa40d0`, `0717e5d7c`, `fd6df4549`) and the
  module now compiles, loads, and reports `custom class registry ready` at
  boot.
- The tree is now plain fork content: `git checkout`/`git reset` are safe
  (untracked: `MY_SERVER.txt`, build logs).

## First-boot SQL migrations (one-time, idempotent)

Wrath Unbound's SQL must be applied **after the first successful worldserver
boot** (the catalog is populated by `SELECT … FROM npc_trainer`, and the
Playerbots synthetic trainer rows for IDs 200002–200018 are seeded into
`acore_world` by mod-playerbots' startup database updates, enabled by
`AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES=1` in the compose override) and
`npc_setup.sql` **must** be applied before the worldserver's next start
(`RegisterCreatureGossipEvent(900001, …)` in the Lua crashes at load time if
the template is missing).

Order (all idempotent; re-running is safe):

```bash
cd <repo root>
SQL=modules/mod-unbound/data/sql
for f in 01_unbound_world 02_fix_catalog_req_level 03_creation_gift_spells \
         04_catalog_druid_forms 05_individual_purchase_prereqs \
         06_universal_skill_access 07_mentor_stone 08_catalog_additions \
         10_catalog_audit_fixes 11_catalog_gap_additions \
         12_mount_spell_fix 13_flight_form_fix 14_judgement_fix; do
  docker exec -i ac-database mysql -u root -ppassword acore_world < "$SQL/db-world/$f.sql"
done
docker exec -i ac-database mysql -u root -ppassword acore_characters < "$SQL/db-characters/01_unbound_characters.sql"
docker exec -i ac-database mysql -u root -ppassword acore_world < modules/mod-unbound/npc_setup.sql
```

Note: the custom-class tables are **not** part of this manual order.
`modules/mod-custom-classes/data/sql/db-world/01_custom_class_template.sql`
and `data/sql/updates/pending_db_world/2026_10_05_*.sql` (PRO-10 Warden
seed + validation) are auto-applied by the world DB updater at
worldserver startup (module + pending update types); both are
re-apply-safe by construction.

DB root password is `password` (stock default; override with
`DOCKER_DB_ROOT_PASSWORD` in a `.env` file at the repo root if you change it).

Pre-migration compatibility gate (mirrors the installer's check):

```bash
docker exec -i ac-database mysql -u root -ppassword acore_world \
  -e "SELECT COUNT(DISTINCT ID) AS ids, COUNT(*) AS rows FROM npc_trainer WHERE ID IN (200002,200004,200006,200008,200010,200012,200014,200016,200018) AND SpellID > 0;"
```

Expected: ≥ 9 distinct IDs and ≥ 100 rows. If not, the Playerbots DB update
pass has not finished — wait for worldserver to reach `ready...` and re-check.

## Accounts (after first boot)

```bash
docker attach $(docker ps --format '{{.Names}}' | grep worldserver | head -1)
# inside the GM console:
account create USERNAME PASSWORD
account set gmlevel USERNAME 3 -1
# exit: Ctrl+P then Ctrl+Q   (never Ctrl+C — that stops the server)
```

## Verification (what "healthy" looks like)

From the host:

```bash
docker logs ac-worldserver | grep UNBOUND
```

Healthy boot shows unbound-class log lines **including the marker**:

```
[UNBOUND] Prereq map built.
```

plus ALE loading lines for every installed script — `unbound_mentor.lua` and
`ranger_vex_gossip.lua` (PRO-269; a missing `ranger_vex_gossip.lua` line means
the deployed directory drifted from the snapshot, so run
`scripts/lua-sync.sh --check`). If **no** ALE lines appear
at all, prove mod-ale is compiled in before debugging further:

```bash
docker exec ac-worldserver strings /azerothcore/env/dist/bin/worldserver | grep -i ALE.Enabled
```

Also check `docker logs ac-worldserver` end-to-end for errors (a clean boot
has none) and the stock ready marker:

```bash
docker logs ac-worldserver | grep "ready..."
```

> **A `docker logs` count is a lifetime counter, not a boot measurement
> (PRO-306 B2).** `docker compose up -d ac-worldserver` is a **no-op** for a
> container that is already running, so `docker logs ac-worldserver | grep -c
> FATAL` after such a command reports the whole container lifetime — PRO-303
> caught exactly that: a run reported `LOG_FATAL count: 0` for a container that
> had been `Up 50 minutes`. The boot-path flags exist so this cannot be read
> wrong again:
>
> ```bash
> bash ops/deploy/worldserver-up.sh --force-recreate --require-ready
> ```
>
> The wrapper then prints its own `BOOT_T0`, the container's `StartedAt`,
> `restarts`, a **since-boot** `LOG_FATAL` count next to the lifetime one, the
> `acore_world.updates` row count before and after the boot, and a `BOOT
> VERDICT:` line. It **fails** if `--force-recreate` was asked for and
> `StartedAt` did not move, so a lifetime counter can no longer be presented as
> a fresh boot. `--require-ready` additionally waits for the `…(worldserver-daemon)
> ready...` banner and exits non-zero without it; `--strict-boot` turns a
> since-boot `FATAL` into a non-zero exit instead of only reporting it.

**Verified on the live host (2026-10-27 00:53 UTC, running `rev.
34d6f4b0e8c7+`, boot started 2026-10-26T23:52:43Z):**

```
banner: AzerothCore rev. 34d6f4b0e8c7+ 2026-10-25 21:12:06 +0000 (Playerbot branch) ... ready...
[UNBOUND] ranger_vex_gossip.lua loaded: entry 900200, quests 900101/900102
[UNBOUND] Prereq map built.
```

No `ERROR|FATAL|Assert|Segmentation|terminate called` in the boot window
and no crash/restart, ~50 random playerbots online (PRO-33 default). The
one config line that looks alarming but is **benign**: `Config: Missing
property AiPlayerbot.ErrorDelay` — the live
`env/dist/etc/modules/playerbots.conf` predates
`modules/mod-playerbots/conf/playerbots.conf.dist:384`; it only disables a
retry delay (module default applies) and is safe to ignore, or add
`AiPlayerbot.ErrorDelay = 100` to the live conf on the next planned
restart.

Earlier verified boot (2026-10-19, `rev. fd6df4549+`, 01:30–01:33 UTC)
also showed `custom class registry ready: 1 template(s) active (0 removed
after DBC validation)`, `CustomClasses.Enabled = true`, and ~1500 random
playerbots. The Mentor template (900001) loaded from the world DB without
rejection in both boots.

> **A boot record that names only the banner rev proves nothing about the tree
> (PRO-306 B5).** The block above — and every PRO-299/PRO-303 boot record —
> quotes `AzerothCore rev. 34d6f4b0e8c7+`, which is the **binary**: the deployed
> `acore/ac-wotlk-worldserver:master` image was built `2026-10-25T22:08:07Z`, and
> on the compose root `git rev-list --count 34d6f4b0e8c7..HEAD` was **80** when
> PRO-306 checked it while the tree itself sat at `8f960d0ba`. So those records
> certify an October binary against a November tree rev. Until the deploy-pin
> policy on [PRO-306](/PRO/issues/PRO-306) is decided and the manifest it
> proposes is committed, a verified boot must record **both** identities by hand:
> `git -C /mnt/data/wow-server rev-parse HEAD` and
> `docker image inspect acore/ac-wotlk-worldserver:master --format '{{.Created}}'`
> next to the banner line.

### Worldserver console vs in-game GM commands

The worldserver's interactive GM console (`docker attach ac-worldserver`)
exposes only a **subset** of GM commands. In this build
(`src/server/scripts/Commands/cs_npc.cpp`,
`npc_commandscript::GetCommands()`), every `.npc` subcommand is
`Console::No` **except `load`** — so from the console you can only run:

```
.npc load <spawn guid>
```

`<spawn guid>` is the primary key of the `acore_world.creature` table, not
the creature template id. For "The Mentor" (template 900001) the persistent
spawn row is `guid 9002020` (`creature.id = 900001`, Eastern Kingdoms,
-8830/614/93.5). The row is owned by
`data/sql/updates/pending_db_world/2026_11_03_00.sql` (committed 2026-11-03,
PRO-114). The guid previously documented here, `5300679`, was never a spawn
guid: it is only the `AUTO_INCREMENT` value in
`data/sql/base/db_world/creature.sql`, and no `creature` row with that guid
exists (measured read-only on the live `acore_world` 2026-11-03):

```
.npc load 9002020
# → "Creature spawn 9002020 is already loaded."
#   The row is created by data/sql/updates/pending_db_world/
#   2026_11_03_00.sql. The 2026-10-19 run of this check was recorded against
#   guid 5300679, which does not exist in acore_world.creature, so the check
#   is owed a re-run on PRO-108 (single-writer lease).
```

All other `.npc` subcommands — including `.npc add` — are `Console::No`
(in-game GM sessions only). `.npc add 900001` therefore **must be run from
an in-game GM3 character**; it spawns "The Mentor" at the GM's position and
persists the spawn row to the DB. That in-game check **PASSED** in
[PRO-43](/PRO/issues/PRO-43) on 2026-10-27 (protocol-harness GM3 session):
`.npc add 900001` produced live GUID
`0xF1300DBBA1005C5D` and a new `acore_world.creature` row (`guid 9002023`),
so the template+command path works from a real in-game session. The test
spawn was deleted afterwards; the only remaining row is the persistent
`9002020`.

In-game test (GM3 account, in-game only — the console does not expose
`.npc add`; see above):

```
.npc add 900001
```

spawns "The Mentor" (subname "Unbound Class Trainer", display 19097, level
80, faction 35) at your position. Interacting opens the ALE-driven gossip
menu (class unlock ladder: free at 5, 3g at 25, 80g at 50, 300g at 70,
1500g at 80+).

## Reprovisioning from a clean clone (e.g. on the new Docker host)

> The **running stack is on the [build host](#build-host)** at
> `/mnt/data/wow-server`. Reprovisioning builds a clean-clone tree from the
> company forks; it is **not** a copy of the host layout (see
> [Host tree layout (provenance gap)](#host-tree-layout-provenance-gap)).
> Do not re-clone or rebuild the host to close the gap — that is
> [PRO-2](/PRO/issues/PRO-2)'s call, and the host holds the running server.

Clone from the company forks (`Scuff-Project-Maple`); no `upstream` remotes
are configured on the provisioned tree (removed 2026-10-01, board decision —
see [Git remotes](#git-remotes-updated-2026-10-18)).

**Prerequisite — the fork's `Playerbot` must be current.** Everything this
flow stages is committed on that branch: the in-tree core hook patches
(mod-unbound + mod-custom-classes), the `wrath-unbound/` snapshot (both
modules + confs), `docs/custom-class-template.md`, and the PRO-10 Warden
seed. The clone must reach at least `ae1d9e5d0` (PRO-9 mod-unbound
patch vendor — the pinned-base variant below pulls the snapshot from
there, so it must contain the staged `unbound-core-access.patch`).
While the execution workspace is ahead of the fork, sync it first —
`git push origin Playerbot` from the execution workspace (pushes go through
the company's managed GitHub identity, [PRO-7](/PRO/issues/PRO-7)).

```bash
git clone --branch=Playerbot https://github.com/Scuff-Project-Maple/azerothcore-wotlk.git azerothcore
cd azerothcore
git log --oneline -4   # head must be at/after ae1d9e5d0 (newer heads are fine)

git clone --depth 1 https://github.com/Scuff-Project-Maple/mod-playerbots.git --branch=master modules/mod-playerbots
git clone https://github.com/Scuff-Project-Maple/mod-ale.git modules/mod-ale
git -C modules/mod-ale checkout 1cb86c9600260c3731c96dc3c98d25b4fc3f2153

# stage the gitignored build-time copies of the tracked Wrath Unbound
# snapshot (see wrath-unbound/README.md for the mapping)
cp -r wrath-unbound/modules/mod-unbound modules/
cp -r wrath-unbound/modules/mod-custom-classes modules/
cp wrath-unbound/env/dist/etc/modules/mod_ale.conf env/dist/etc/modules/
cp wrath-unbound/env/dist/etc/modules/mod_custom_classes.conf env/dist/etc/modules/
# ALE Lua is installed by the installer, never hand-placed (PRO-269): it copies
# every tracked script into env/dist/etc/modules/lua_scripts/ and --check
# fails on drift or on a hand-placed orphan
scripts/lua-sync.sh
# installs both generated root overrides. On a fresh clone the tree is owned
# by you, so no sudo is needed; on the existing build-host checkout (owned by
# pira:pira, and agent logins are not in that group) use
# `sudo -u pira scripts/compose-sync.sh` instead. `--check` is read-only.
scripts/compose-sync.sh
# both root overrides are generated copies (PRO-178/PRO-180): never hand-edit
# them; edit wrath-unbound/*.override.yml and re-run the installer

# the core hook patches are committed IN-TREE on Playerbot
# (f5b197b8d mod-unbound, 6 files; b732dfd71 mod-custom-classes, 6 files)
# — verify, do NOT re-apply:
grep -n GetUnboundClassMask src/server/game/Entities/Player/Player.h
grep -n OnPlayerAfterUpdateAllStats src/server/game/Entities/Unit/StatSystem.cpp
grep -n OnPlayerLfgSetRoles src/server/game/Handlers/LFGHandler.cpp

# ValidateSkillLearnedBySpells = 0 is baked into the tracked
# src/server/apps/worldserver/worldserver.conf.dist in this tree:
grep -n '^ValidateSkillLearnedBySpells' src/server/apps/worldserver/worldserver.conf.dist
```

### Variant — pure upstream base (e.g. upstream-merge test)

Same clone, then pin the pre-company base `7f12e89ee`. The `wrath-unbound/`
snapshot does not exist at that commit, so pull the payload files forward
from `ae1d9e5d0`, stage modules + confs exactly as in the primary flow
above, then apply both core patches in this order (verified 2026-10-05,
both `git apply --check` clean on the base):

```bash
git checkout 7f12e89ee5f467a50e62eba1d525eac7dc953d03
# the forward-pull also brings worldserver.conf.dist, which carries the
# company's ValidateSkillLearnedBySpells = 0 (see D2) — no sed needed
git checkout ae1d9e5d0 -- wrath-unbound docs/custom-class-template.md data/sql/updates/pending_db_world src/server/apps/worldserver/worldserver.conf.dist
git apply modules/mod-unbound/unbound-core-access.patch
git apply modules/mod-custom-classes/custom-class-core-access.patch
```

Then `docker compose build`, bring up `ac-database` and `ac-authserver`, apply
the SQL migrations above, and start `ac-worldserver` through the gate
(`bash ops/deploy/worldserver-up.sh`) — never with a bare
`docker compose up -d --build`, which starts the worldserver un-gated. Verify
per the sections above.

## Deviations from the guide (documented explicitly, per PRO-2)

- **D1 — Build/boot executed on the Docker host, not the agent sandbox.**
  Provisioning was done on the Paperclip agent host: an LXC container with
  no Docker/podman, no root or capabilities (`CapEff=0`), and no C/C++
  toolchain. Build, boot, and log verification were then executed on the
  [build host](#build-host) (host provisioned via
  [PRO-6](/PRO/issues/PRO-6)). Clean boot + `[UNBOUND] Prereq map built.`
  verified 2026-10-19 and re-verified 2026-10-27. The in-game
  `.npc add 900001` spawn check requires an in-game GM character (the
  console does not expose `.npc add` — see
  [Worldserver console vs in-game GM commands](#worldserver-console-vs-in-game-gm-commands));
  it passed in [PRO-43](/PRO/issues/PRO-43) on 2026-10-27, closing the last
  PRO-2 acceptance item.
- **D2 — `ValidateSkillLearnedBySpells = 0` set in `worldserver.conf.dist`
  instead of the installer's runtime sed of `env/dist/etc/worldserver.conf`.**
  The runtime conf is generated into the bind-mounted `env/dist/etc` by the
  image entrypoint (`cp -n` from the build's `env/ref/etc`, i.e. from the
  `.dist` files) and is gitignored. Editing the `.dist` makes the value
  survive rebuilds from a clean checkout. Equivalent effect; the value is
  required (at 1, cross-class spells granted by unbound unlocks are stripped
  on login).
- **D3 — Tracked snapshot in `wrath-unbound/`.** The fork gitignores all
  provisioned content, so the payload's only upstream home is the embedded
  heredocs in Dad's installer script. `wrath-unbound/` vendors an
  extracted copy (byte-identical, provenance in its README) plus **both**
  compose overrides (the load-test one was added in PRO-180 — until then it
  existed only as an untracked root file, so a clean-clone reprovision lost
  it) so the tree can be rebuilt without re-downloading the
  installer.
- **D4 — mod-playerbots shallow clone.** `--depth 1` exactly as the base
  installer does it; its pinned head is recorded above. The team does not
  patch mod-playerbots; if they start to, fetch history first.

- **D5 — Dev DB durability relaxed (PRO-30).** `conf/mysql-dev.cnf`
  (tracked via the `!/conf/mysql-dev.cnf` gitignore exception added in
  PRO-178 — before that it was gitignored and unmounted) is bind-mounted
  into `ac-database` at `/etc/mysql/conf.d/maple-dev.cnf` by the **tracked**
  `wrath-unbound/docker-compose.override.yml`; the root
  `docker-compose.override.yml` is a generated copy of it (PRO-178).
  Values: `innodb_flush_log_at_trx_commit = 2`,
  `sync_binlog = 10000`, `binlog_expire_logs_seconds = 172800`,
  `max_binlog_size = 104857600`. The dev box is a KVM VM on shared storage;
  per-commit fsyncs (InnoDB log + binlog) wedged the worldserver main thread
  for minutes under I/O pressure, stalling external-client auth (see
  [PRO-30](/PRO/issues/PRO-30) and the section below). Trade-off: on power
  failure the dev DB may lose up to ~1 s of transactions. Do not carry these
  values into a production stack.
  Verify the mount and values are live:
  `docker inspect ac-database --format '{{json .Mounts}}'` lists the cnf
  bind mount. **Regression tripwire:** a permission drift or a stricter
  future image silently un-applies this drop-in (MySQL skips option files
  under `!includedir` whose permissions it considers unsafe). The check is:
  `docker exec ac-database mysql -u root -ppassword -N -e "SELECT
  @@innodb_flush_log_at_trx_commit, @@sync_binlog,
  @@binlog_expire_logs_seconds, @@max_binlog_size;"` must return
  `2 10000 172800 104857600`.
## PRO-30: auth hang defect, dev DB durability, and host I/O notes

**Defect (found by QA on 2026-10-19, tracked in
[PRO-30](/PRO/issues/PRO-30)):** every external WotLK client login hung at
auth — no `SMSG_AUTH_RESPONSE`, no server log line, no stuck MySQL query
visible from the client side. In-process playerbot logins were unaffected
until the stall.

**Root cause (verified with live backtraces on the host):**

1. The worldserver **main thread** executes synchronous DB commits on the
   per-tick bot-update path: `World::Update` -> `RandomPlayerbotMgr::UpdateAI`
   -> `SetEventValue` -> `PlayerbotsDatabasePool::CommitTransaction`
   (`DirectCommitTransaction`, mod-playerbots). The same pattern exists on
   the characters pool via async transaction tasks.
2. With `innodb_flush_log_at_trx_commit = 1` + `sync_binlog = 1` (MySQL
   defaults), every commit forces fsyncs on the shared KVM storage.
3. Under host I/O pressure (concurrent docker builds; root disk near full),
   commit flushes took minutes ("waiting for handler commit" in
   `information_schema.processlist`; transactions stuck in InnoDB
   `ACTIVE (PREPARED)` waiting on the binlog flush). The main thread sat in
   `SSL_read` inside `mysql_real_query("COMMIT")`, so `World::Update` never
   advanced, auth query callbacks were never processed, and external clients
   timed out.

**Fix applied (dev stack):**

- `conf/mysql-dev.cnf` (D5): no per-commit fsyncs; commits complete in
  microseconds even under I/O pressure. Historical note (PRO-178): at the
  October verification this file was gitignored and no compose mount
  referenced it, so the running server kept MySQL defaults
  (`@@innodb_flush_log_at_trx_commit = 1`, `@@sync_binlog = 1`); the tuning
  was never actually in effect until the tracked override mounted it on
  2026-11-03.
- Disk hygiene on the build host: `docker builder prune` (41 GB build cache
  -> ~31 GB) and `PURGE BINARY LOGS BEFORE` (16.5 GB binlogs -> 5 files).
  Root disk went 98% -> ~82%. Keep an eye on it: the MySQL data volume and
  the docker build cache share the 62 GB root disk.

**Verification:**

- `docker compose up -d --build`-fresh boot on the host: `ready...`,
  `[UNBOUND] Prereq map built.`, custom-class registry line, 1865/1865
  playerbots logged in, zero "waiting for handler commit" sessions under
  the bot login storm.
- External login re-verified with the tracked e2e suite
  (`e2e/smoke/smoke_e2e_test.go`, AzerothGhost harness v1.0.8):
  `TestSmoke_ValidLoginEnterWorld` PASS (auth -> `SMSG_AUTH_RESPONSE` ->
  in-world at Eastern Kingdoms). QA re-run is tracked as
  [PRO-31](/PRO/issues/PRO-31).

**Hardening (implemented + deployed, under independent review):** the
main-thread commit storm is batched in
`modules/mod-playerbots/src/Bot/RandomPlayerbotMgr.{h,cpp}` — per-tick
event writes are deferred into one transaction committed once at the end
of `UpdateAIInternal` (was: one commit per event), and the boot top-up
uses single `IN`-list SELECTs (was: per-row SELECTs). Crash-durability
delta: up to one tick (~1s) of event writes, re-synced from DB at next
boot. Live-file md5s and commit pattern: [PRO-31](/PRO/issues/PRO-31)
(independent verification by Scuff Rev).

**Debugging notes (reused during PRO-30):**

- The worldserver container has no capabilities (`CapEff=0`) and the
  rebuilt image has no gdb; attach with a PID-namespace-sharing helper:
  `docker run --rm --cap-add SYS_PTRACE --pid=container:ac-worldserver
  scuff/gdbprobe gdb -batch -p 1 -ex "thread apply all bt 20"`.
- Per-pool commit state: `SELECT id,db,command,time,state FROM
  information_schema.processlist WHERE command <> 'Sleep';` (stuck commits
  show `waiting for handler commit`; `SHOW ENGINE INNODB STATUS` shows the
  stuck trx as `ACTIVE (PREPARED)` when waiting on the binlog).

## Rules

- Never disable an upstream patch or core check to make a build pass —
  report the real error on the issue instead.
- **Compose overrides are generated (PRO-180):** the gitignored root
  `docker-compose.override.yml` and `docker-compose.loadtest.override.yml` are
  installed by `scripts/compose-sync.sh` from their `wrath-unbound/` snapshots.
  Edit the snapshots only, run `sudo -u pira scripts/compose-sync.sh` on the
  build host (the installer writes, and the tree is `pira`-owned; `--check` is
  read-only and safe as any user), then apply with
  `bash ops/deploy/worldserver-up.sh --force-recreate` — not a bare
  `docker compose up`, which skips the gate (see the pre-flight gate rule
  below) — and run
  `scripts/compose-sync.sh --check` before any `up`/`build`. Hand-editing the
  root copies is what produced the PRO-33/PRO-65/PRO-178 drift.
- **ALE Lua is installed, not hand-placed (PRO-269):** the gitignored
  `env/dist/etc/modules/lua_scripts/` is installed by `scripts/lua-sync.sh`
  from `wrath-unbound/env/dist/etc/modules/lua_scripts/`. Add new Lua to the
  snapshot, run `sudo -u pira scripts/lua-sync.sh` on the build host, and run
  `scripts/lua-sync.sh --check` before any `up`/`build`. Never `scp` a script
  straight into the deployed directory: `ranger_vex_gossip.lua` lived there
  untracked that way (PRO-163 red flag R6) and would have silently disappeared
  on the next sync or `--force-recreate`, which QA would have misread as a
  content regression. The check also fails on a deployed `*.lua` the snapshot
  does not have; the installer never deletes such an orphan — commit it or
  remove it deliberately.
- This fork's `AGENTS.md`: SQL outside `data/sql/updates/pending_db_*/` is
  immutable; module work lives under `modules/`; C++ is 4-space, no tabs,
  LF, ≤120 cols.
- All git commits in this tree carry `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.
- **No force-push to `Playerbot`, and none to any branch another agent may be
  building from.** `Playerbot` is the shared integration branch and the
  provenance of the running binary
  ([Git access on the compose root](#git-access-on-the-compose-root)); rewriting
  it loses someone else's work. The one allowed case is amending **your own
  unmerged feature branch** to answer review — the normal fixup path, which is
  how [PRO-205](/PRO/issues/PRO-205) re-landed `pro-198-agent-git-access`. Use
  `--force-with-lease`, never plain `--force`. That allowance covers nothing
  else. [PRO-69](/PRO/issues/PRO-69) step 2 is the decision record for the
  `Playerbot` landing push: "Forbidden: `--force`, `--force-with-lease`,
  history rewrite, deleting any remote branch" and "If the push is rejected,
  stop and report — do not force." The bullet was moved here from the
  compose-root permissions list at [PRO-203](/PRO/issues/PRO-203) request N6
  because it is a git rule, not a filesystem one.
- **Docker-only on the build host (board rule, [PRO-6](/PRO/issues/PRO-6)):** all
  deployments on `192.168.1.71` happen inside Docker containers — no code
  executes on that machine natively. Host facts: [Build host](#build-host).
- **Canonical agent access path:** `ssh 192.168.1.71 '<docker command>'` —
  build, apply SQL, and read logs from there. Do not ssh in and run a
  host-local compiler.
- **Single-writer lease (board rule, [PRO-67](/PRO/issues/PRO-67)):** no
  worldserver restart/recreate, `docker compose up`, DB migration, or e2e run
  without first holding `/mnt/data/wow-server/.stack/lease` — see
  [Single-writer lease](#single-writer-lease-one-worldserver-one-writer-at-a-time).
- **World-DB pre-flight gate (PRO-179):** never start, recreate, or restart
  `ac-worldserver` without the gate having passed — use
  `bash ops/deploy/worldserver-up.sh` (it forwards `--force-recreate`,
  `-f/--file`, `-p/--project-name` and honours `COMPOSE_FILE`), or
  `PREFLIGHT_CHECK_ONLY=1 bash ops/deploy/worldserver-up.sh` before an exact
  `docker compose` command the wrapper cannot express (`up -d --build`,
  `restart`). Take the lease first — the wrapper verifies it, the gate does not
  take it; keep `AC_UPDATES_ENABLE_DATABASES=4` and leave the Playerbots updater
  (`acore_playerbots`) alone. The wrapper **refuses** the flags that would let a
  boot step around what the gate read: `-f/--file`, `--env-file` and
  `-p/--project-name` after a bare `--` (pass them before `--`), `--compose-root`
  and `--gate` (set `COMPOSE_ROOT`/`PYTHON` instead, so the gate and the `cd`
  cannot disagree), and `--no-boot-path-check` unless `PREFLIGHT_CHECK_ONLY=1`.
  A restart that changes **no** SQL can skip the slow half of the gate — the
  scratch clone plus up to `--data-copy-max-rows` rows copied per referenced
  table — with `bash ops/deploy/worldserver-up.sh --no-dry-run`; it prints a
  warning on purpose, because it is exactly the fast path that must not be used
  after a SQL change. `--keep-scratch` keeps the scratch schema instead of
  dropping it, for post-mortem queries. See
  [Build & run](#build--run) and [`ops/preflight/README.md`](../ops/preflight/README.md).
- **Prohibited on the host:** a `cmake`/`make`/gcc build tree, binaries run
  outside a container, and any `docker`-alternative that shells out to a
  host-local toolchain. `docker compose up -d --build` is fine — it compiles
  inside a container, which is exactly what the board wants.

## Acceptance checklist (PRO-2)

- [x] AzerothCore 3.3.5a cloned from the Playerbots base; URL+branch+commit recorded above
- [x] `docker-compose.yml` stack present (stock file; services ac-database / ac-authserver / ac-worldserver + db-import + client-data-init)
- [x] mod-unbound C++ module staged; core patch in-tree on Playerbot (6 files, verified by `grep GetUnboundClassMask src/server/game/Entities/Player/Player.h`)
- [x] mod-custom-classes C++ module staged (PRO-9); core hook patch in-tree on Playerbot (6 files, verified by `grep OnPlayerAfterUpdateAllStats src/server/game/Entities/Unit/StatSystem.cpp`)
- [x] Mentor Lua staged where ALE scans; `mod_ale.conf` with `ALE.Enabled = 1` + absolute `ALE.ScriptPath`
- [x] SQL migrations staged with documented apply order (custom-class DDL + PRO-10 seed auto-apply via the DB updater)
- [x] `ValidateSkillLearnedBySpells = 0`
- [x] Setup doc in the repo (this file)
- [x] Build worldserver (on the build host; current running rev `34d6f4b0e8c7+`, 2026-10-25; first verified `fd6df4549+`, 2026-10-19)
- [x] Clean boot + `[UNBOUND] Prereq map built.` (re-verified 2026-10-27 on `rev. 34d6f4b0e8c7+`; no errors/crash)
- [x] NPC 900001 in the live world — template 900001 "The Mentor" (upstream addon `wrath-unbound/modules/mod-unbound/npc_setup.sql`, which ships no spawn row) + persistent spawn row (guid 9002020, `creature.id = 900001`, map 0 at -8830/614/93.5, `spawntimesecs` 300) in `acore_world`, owned by `data/sql/updates/pending_db_world/2026_11_03_00.sql`; `.npc load 9002020` is the console-side check — re-run owed on PRO-108, because the earlier `5300679` guid never existed
- [x] `.npc add 900001` from an in-game GM3 character — **PASS** in [PRO-43](/PRO/issues/PRO-43) (2026-10-27): new live unit + persisted `creature` row, no errors; test spawn deleted, persistent `9002020` intact
