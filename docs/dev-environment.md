# AzerothCore + Wrath Unbound Dev Environment (PRO-2)

Dev environment for the Unbound Wrath WotLK project. Provisioned per
[PRO-2](/PRO/issues/PRO-2) on 2026-09-30. This doc is the single source of
truth for repo pinning, build/run commands, `env/` config, and the expected
healthy-boot log lines. QA and all engineers: start here.

**Status:** tree is fully provisioned (clone, modules, core patch, confs,
docs). **Build, boot, and in-game verification are pending a Docker-capable
host** — the agent sandbox where this was provisioned has no Docker, no root,
no C/C++ toolchain (see Deviations, D1).

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
│   │   └── unbound-core-access.patch     #   6-file core patch (already applied, kept for re-apply after rebases)
│   ├── mod-ale/                          # nested git repo @ 1cb86c96…
│   └── mod-playerbots/                   # nested git repo @ 7bae1b5c… (depth-1 master)
├── env/dist/etc/modules/
│   ├── mod_ale.conf                      # ALE.Enabled = 1, ALE.ScriptPath = /azerothcore/env/dist/etc/modules/lua_scripts
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

```bash
git clone --branch=Playerbot https://github.com/mod-playerbots/azerothcore-wotlk.git azerothcore
cd azerothcore && git checkout 7f12e89ee5f467a50e62eba1d525eac7dc953d03

git clone --depth 1 https://github.com/mod-playerbots/mod-playerbots.git --branch=master modules/mod-playerbots
git clone https://github.com/azerothcore/mod-ale.git modules/mod-ale
git -C modules/mod-ale checkout 1cb86c9600260c3731c96dc3c98d25b4fc3f2153

# stage the tracked Wrath Unbound snapshot (see wrath-unbound/README.md for the mapping)
cp -r wrath-unbound/modules/mod-unbound modules/
mkdir -p env/dist/etc/modules/lua_scripts
cp wrath-unbound/env/dist/etc/modules/mod_ale.conf env/dist/etc/modules/
cp wrath-unbound/env/dist/etc/modules/lua_scripts/unbound_mentor.lua env/dist/etc/modules/lua_scripts/
cp wrath-unbound/docker-compose.override.yml .

# core patch (6 files: Player.h/.cpp, Trainer.cpp, PlayerQuest.cpp,
# PlayerStorage.cpp, ConditionMgr.cpp). Idempotence check afterwards:
#   grep -n GetUnboundClassMask src/server/game/Entities/Player/Player.h
git apply modules/mod-unbound/unbound-core-access.patch

# ValidateSkillLearnedBySpells = 0 is baked into the tracked
# src/server/apps/worldserver/worldserver.conf.dist in this tree;
# if reprovisioning onto a vanilla checkout, also set:
sed -i 's/^ValidateSkillLearnedBySpells.*/ValidateSkillLearnedBySpells = 0/' src/server/apps/worldserver/worldserver.conf.dist
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

## Acceptance checklist (PRO-2)

- [x] AzerothCore 3.3.5a cloned from the Playerbots base; URL+branch+commit recorded above
- [x] `docker-compose.yml` stack present (stock file; services ac-database / ac-authserver / ac-worldserver + db-import + client-data-init)
- [x] mod-unbound C++ module staged; core patch applied (6 files, verified by `grep GetUnboundClassMask src/server/game/Entities/Player/Player.h`)
- [x] Mentor Lua staged where ALE scans; `mod_ale.conf` with `ALE.Enabled = 1` + absolute `ALE.ScriptPath`
- [x] SQL migrations staged with documented apply order
- [x] `ValidateSkillLearnedBySpells = 0`
- [x] Setup doc in the repo (this file)
- [ ] Build worldserver (pending Docker host — D1)
- [ ] Clean boot + `[UNBOUND] Prereq map built.` (pending Docker host)
- [ ] `.npc add 900001` spawn test (pending Docker host)
