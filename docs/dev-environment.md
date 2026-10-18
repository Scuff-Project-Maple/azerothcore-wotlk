# AzerothCore + Wrath Unbound Dev Environment (PRO-2)

Dev environment for the Unbound Wrath WotLK project. Provisioned per
[PRO-2](/PRO/issues/PRO-2) on 2026-09-30. This doc is the single source of
truth for repo pinning, build/run commands, `env/` config, and the expected
healthy-boot log lines. QA and all engineers: start here.

**Status:** tree is fully provisioned (clone, modules, core patch, confs,
docs). **Build, boot, and in-game verification are pending a Docker-capable
host** — the agent sandbox where this was provisioned has no Docker, no root,
no C/C++ toolchain (see Deviations, D1).

**Build host:** the stack now runs on a provisioned Docker host — see
[Build host](#build-host). The host tree differs from this doc's pinned
tree; that gap is documented in the Build host section and is an open
provenance question owned by [PRO-2](/PRO/issues/PRO-2).

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

## Git remotes (updated 2026-09-30, per board request)

The company forks live in the `Scuff-Project-Maple` GitHub org. All working
remotes now point at the forks; the upstreams are kept as an `upstream`
remote for merges (every core patch remains a merge risk against upstream).

| Repo | `origin` (working) | `upstream` (reference) |
|---|---|---|
| azerothcore-wotlk (this tree, branch `Playerbot`) | `https://github.com/Scuff-Project-Maple/azerothcore-wotlk.git` | `https://github.com/mod-playerbots/azerothcore-wotlk.git` |
| `modules/mod-ale` (nested repo) | `https://github.com/Scuff-Project-Maple/mod-ale.git` | `https://github.com/azerothcore/mod-ale.git` |
| `modules/mod-playerbots` (nested repo) | `https://github.com/Scuff-Project-Maple/mod-playerbots.git` | `https://github.com/mod-playerbots/mod-playerbots.git` |

Fork heads verified against the pins: `azerothcore-wotlk` `Playerbot` @
`7f12e89e…`, `mod-playerbots` `master` @ `7bae1b5c…` (the `mod-ale` fork's
`master` is at upstream HEAD; check out the pinned `1cb86c96…` when
provisioning — see table above).

**Push status (2026-10-05):** the T1/Wrath-Unbound commits (`f5b197b8d`,
`7c40d8102`, `0f5a3955a`, `2bfcc80c7`, `8c16d1194`) are in the fork —
`origin/Playerbot` is at `8c16d1194`. Three commits are still ahead of the
fork: `b732dfd71` (PRO-9 `mod-custom-classes` module, including the
in-tree core hook patch), `deaf1109c` (PRO-10 Warden seed + validation),
`9c0db4ef6` (PRO-9 DDL re-apply fix) — plus this doc update when it lands.
Pushing to the fork is pending the company's managed GitHub identity being
completed in Paperclip ("The managed GitHub identity is incomplete" from the
git credential broker; no agent secret or gh auth available in the sandbox).
The push will be a clean fast-forward. Until it lands, treat the tree in the
shared project workspace as the source of truth; the
[Reprovisioning](#reprovisioning-from-a-clean-clone-e-g-on-the-new-docker-host)
flow states the fork-sync prerequisite explicitly.

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
├── docker-compose.override.yml           # Playerbots env (random bots 1600–2000, DB auto-updates), ./modules mount
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

### Host tree layout (provenance gap)

The host tree at `/mnt/data/wow-server` is **not** byte-identical to the
provisioned tree described in this doc. The host layout is authoritative for
*running* the stack; the pinned-revision gap below is an **open provenance
question owned by [PRO-2](/PRO/issues/PRO-2)**. Do not "fix" it by
rebuilding or re-cloning — the host holds the running server, and that call
belongs to PRO-2.

| Item | Doc (PRO-2 pinned) | Host reality (verified 2026-10-18) |
|---|---|---|
| Core | Playerbot fork @ `7f12e89ee5f4` | @ `06234df3d5ab` (branch `Playerbot`, "Merge pull request #246 from mod-playerbots/test-staging", 2026-09-11) with the 6-file unbound core patch **applied but uncommitted** in the working tree |
| Modules | separate pinned clones: mod-ale @ `1cb86c960026`, mod-playerbots @ `7bae1b5c58c7` | in-tree plain source trees: `modules/mod-ale`, `modules/mod-arac`, `modules/mod-playerbots`, `modules/mod-unbound` (no `.git` in any of them) |
| Wrath Unbound | tracked payload snapshot + 6-file core patch | `modules/mod-unbound` (`src/`, `data/`, `npc_setup.sql`, `unbound-core-access.patch`) + the uncommitted working-tree patch |

Consequences for anyone working on the host:

- The `acore/ac-wotlk-worldserver:master` and
  `acore/ac-wotlk-authserver:master` images are **locally built from this
  tree**, not Docker Hub pulls (banner: `AzerothCore rev. 06234df3d5ab+ …
  (Playerbot branch)` — the `+` is the uncommitted patch).
- The host tree has **no** `mod-custom-classes` (PRO-9) module or conf —
  only the modules listed above.
- A `git checkout`/`git reset` in `/mnt/data/wow-server` would discard the
  uncommitted unbound patch the running server was built from. Treat the
  working tree as live content.

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

In-game test (GM3 account, once):

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

Clone from the company forks (`Scuff-Project-Maple`); the upstreams remain
available as `upstream` remotes on the provisioned tree.

**Prerequisite — the fork's `Playerbot` must be current.** Everything this
flow stages is committed on that branch: the in-tree core hook patches
(mod-unbound + mod-custom-classes), the `wrath-unbound/` snapshot (both
modules + confs), `docs/custom-class-template.md`, and the PRO-10 Warden
seed. The clone must reach at least `ae1d9e5d0` (PRO-9 mod-unbound
patch vendor — the pinned-base variant below pulls the snapshot from
there, so it must contain the staged `unbound-core-access.patch`).
While the execution workspace is ahead of the fork, sync it first —
`git push origin Playerbot` from the execution workspace (needs a GitHub
write token; tracked on the PRO-9 thread).

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

- **D1 — Build/boot not executed in the agent sandbox.** All provisioning
  here was done on the Paperclip agent host: an LXC container with no
  Docker/podman, no root or capabilities (`CapEff=0`), and no C/C++
  toolchain. Nothing was built or run; "clean boot" and `.npc add 900001`
  remain to be verified on a Docker-capable host (tracked as the PRO-2
  follow-up issue for the CEO).
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
- [ ] Build worldserver (pending Docker host — D1)
- [ ] Clean boot + `[UNBOUND] Prereq map built.` (pending Docker host)
- [ ] `.npc add 900001` spawn test (pending Docker host)
