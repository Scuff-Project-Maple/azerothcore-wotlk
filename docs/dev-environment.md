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

**Push status (updated 2026-10-27):** the fork `Playerbot` branch is the
source of truth; it carries the
T1/Wrath-Unbound base (`f5b197b8d`–`8c16d1194`), the PRO-9/PRO-10 module +
SQL work (`b732dfd71`–`4805857c4`), and the PRO-3/PRO-16/PRO-20 docs
(`e025c7dc9`–`f76e13468`). Pushes go through the company's managed GitHub
identity (PRO-7); cloning that branch is the reprovisioning path — see
[Reprovisioning](#reprovisioning-from-a-clean-clone-e-g-on-the-new-docker-host).

## Provisioned tree layout

This repo's `.gitignore` intentionally keeps provisioned content out of the
git index: `/modules/*`, `/env/dist/*`, and `/*.override.yml` are all
gitignored. Provisioned files therefore live in the **working tree** of this
checkout; only the core-source changes (`worldserver.conf.dist`), this doc,
and the `wrath-unbound/` snapshot are tracked in git.

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
 │   └── lua_scripts/unbound_mentor.lua    # The Mentor gossip Lua (loaded by ALE at worldserver start)
├── docker-compose.override.yml            # 50 random bots (DB auto-updates), Warden off for e2e, ./modules mount
├── docker-compose.loadtest.override.yml  # PRO-33: restores the 1600–2000 randombot volume for load tests (opt-in)
├── src/server/apps/worldserver/worldserver.conf.dist   # modified: ValidateSkillLearnedBySpells = 0
├── docs/dev-environment.md               # this doc
└── wrath-unbound/                        # tracked snapshot of the provisioned payload (reprovisioning source)
```

Services (from the stock `docker-compose.yml`, all started by one command):
`ac-database` (mysql:8.4), `ac-db-import`, `ac-client-data-init`,
`ac-worldserver`, `ac-authserver`. Compose project resolves from CWD —
**always `cd` into the repo root before compose commands**.

## Build & run

All commands in this section run **on the [build host](#build-host)**, over
BatchMode SSH (agent sandboxes have no Docker).

One documented command from a provisioned tree (fresh image build; 2–4 h on
a typical host, less on the Steam Deck baseline the guide targets):

```bash
cd <repo root>
docker compose up -d --build
```

After **any** core/module C++ change (the normal Scuff-Core loop —
incremental, ~30–90 min):

```bash
docker compose build ac-worldserver
docker compose up -d --force-recreate ac-worldserver
```

Stop/start without rebuild: `docker compose stop ac-worldserver` /
`docker compose start ac-worldserver`.

### Load testing (randombot volume, PRO-33)

Default boots load **50** randombots
(`AC_AI_PLAYERBOT_MIN_RANDOM_BOTS`/`MAX_RANDOM_BOTS` = `50` in
`docker-compose.override.yml`). The former 1600–2000 load-test volume is
restored by `docker-compose.loadtest.override.yml`, which is **not** picked
up by a plain `docker compose up`. From the repo root:

```bash
# load-test boot (1600–2000 randombots)
COMPOSE_FILE=docker-compose.yml:docker-compose.override.yml:docker-compose.loadtest.override.yml \
  docker compose up -d --force-recreate ac-worldserver

# back to the 50-bot default
docker compose up -d --force-recreate ac-worldserver
```

Verify the loaded count after boot: the `bot_count` row in
`acore_playerbots.playerbots_random_bots` (owner=0, bot=0) should read 50
(default) or 1600–2000 (load test):

```bash
docker exec ac-database mysql -u root -ppassword acore_playerbots \
  -e "SELECT event, value FROM playerbots_random_bots WHERE owner = 0 AND bot = 0;"
```

and the online `rndbot%` characters in `acore_characters` should match
within a few minutes:

```bash
docker exec ac-database mysql -u root -ppassword \
  -e "SELECT COUNT(*) AS online_bots FROM acore_characters c JOIN acore_auth a ON c.account = a.id WHERE a.username LIKE 'rndbot%' AND c.online > 0;"
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
| User | `scuff-dev` (member of groups `scuff-dev`, `sudo`, `docker`) |
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
| Conf | `env/dist/etc/modules/mod_ale.conf`, `mod_custom_classes.conf`, `lua_scripts/unbound_mentor.lua` in place; live `worldserver.conf` untouched |
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

plus ALE loading lines for `unbound_mentor.lua`. If **no** ALE lines appear
at all, prove mod-ale is compiled in before debugging further:

```bash
docker exec ac-worldserver strings /azerothcore/env/dist/bin/worldserver | grep -i ALE.Enabled
```

Also check `docker logs ac-worldserver` end-to-end for errors (a clean boot
has none) and the stock ready marker:

```bash
docker logs ac-worldserver | grep "ready..."
```

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
spawn row is **`guid 9002020`** (Eastern Kingdoms, -8830/614/93.5; the
earlier row `guid 5300679` was replaced during later data work):

```
.npc load 9002020
# → "Creature spawn 9002020 is already loaded."  (console-side proof the
#   NPC is in the live world; re-verified 2026-10-27)
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
mkdir -p env/dist/etc/modules/lua_scripts
cp wrath-unbound/env/dist/etc/modules/mod_ale.conf env/dist/etc/modules/
cp wrath-unbound/env/dist/etc/modules/mod_custom_classes.conf env/dist/etc/modules/
cp wrath-unbound/env/dist/etc/modules/lua_scripts/unbound_mentor.lua env/dist/etc/modules/lua_scripts/
cp wrath-unbound/docker-compose.override.yml .
cp wrath-unbound/docker-compose.loadtest.override.yml .

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

Then `docker compose up -d --build`, apply the SQL migrations above, and
verify per the sections above.

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
  extracted copy (byte-identical, provenance in its README) plus the
  compose override so the tree can be rebuilt without re-downloading the
  installer.
- **D4 — mod-playerbots shallow clone.** `--depth 1` exactly as the base
  installer does it; its pinned head is recorded above. The team does not
  patch mod-playerbots; if they start to, fetch history first.

## Rules

- Never disable an upstream patch or core check to make a build pass —
  report the real error on the issue instead.
- This fork's `AGENTS.md`: SQL outside `data/sql/updates/pending_db_*/` is
  immutable; module work lives under `modules/`; C++ is 4-space, no tabs,
  LF, ≤120 cols.
- All git commits in this tree carry `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.
- **Docker-only on the build host (board rule, [PRO-6](/PRO/issues/PRO-6)):** all
  deployments on `192.168.1.71` happen inside Docker containers — no code
  executes on that machine natively. Host facts: [Build host](#build-host).
- **Canonical agent access path:** `ssh 192.168.1.71 '<docker command>'` —
  build, apply SQL, and read logs from there. Do not ssh in and run a
  host-local compiler.
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
- [x] NPC 900001 in the live world — template 900001 "The Mentor" + persistent spawn row **guid 9002020** in `acore_world`; `.npc load 9002020` → "already loaded" (console-side proof)
- [x] `.npc add 900001` from an in-game GM3 character — **PASS** in [PRO-43](/PRO/issues/PRO-43) (2026-10-27): new live unit + persisted `creature` row, no errors; test spawn deleted, persistent `9002020` intact
