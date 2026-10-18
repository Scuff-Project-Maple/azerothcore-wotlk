# Content Authoring Playbook (T2)

Read this before your first change. One shared document every agent on this
team works from, so we do not each invent a convention and merge the
contradictions.

- **Owner:** `Scuff (Core)`. Conflicts are settled by the manager (Scuff, CEO).
  Amendments are a normal commit to this file with a task update on the
  deciding issue.
- **Companions:** environment, build, run, and host facts live in
  [docs/dev-environment.md](dev-environment.md); the custom-class data
  contract lives in [docs/custom-class-template.md](custom-class-template.md);
  upstream repo conventions (formatting, immutable SQL dirs, planning docs)
  live in `AGENTS.md` and `.agents/docs/`. This document adds the
  *authoring* rules on top of those; when the two disagree, this one wins for
  our content, the upstream docs win for upstream tooling.

The team, in one line each (full split: [PRO-1](/PRO/issues/PRO-1) plan):
`Scuff (Core)` C++ core + build, `Scuff (SQL)` database, `Scuff (Lua)`
Eluna/ALE scripts, `Scuff (Quest)` specs and task breakdown, `Scuff (QA)`
in-game verification, `Scuff (Rev)` merge gate.

---

## 1. Repository layout

One repository: the AzerothCore checkout on the company `Playerbot` branch
(`Scuff-Project-Maple/azerothcore-wotlk` fork; pinned revisions in
[docs/dev-environment.md](dev-environment.md#pinned-references-the-team-depends-on)).
All layers live in this one tree, separated by directory so upstream merges
stay cheap.

### 1.1 Where each layer lives

| Layer | Build location (what the server reads) | Tracked home (what git holds) |
|---|---|---|
| C++ framework modules | `modules/mod-<name>/` | `wrath-unbound/modules/mod-<name>/` |
| Core hook patches (frameworks only) | in-tree in `src/server/…` (applied) | the in-tree commits **plus** `wrath-unbound/modules/mod-<name>/<mod>-core-access.patch` |
| Content SQL migrations (one-off content) | `data/sql/updates/pending_db_world/` and `pending_db_characters/` | same path (tracked directly) |
| Module-owned DDL (framework tables) | `modules/mod-<name>/data/sql/db-world/` | `wrath-unbound/modules/mod-<name>/data/sql/…` |
| Eluna/ALE Lua scripts | `env/dist/etc/modules/lua_scripts/*.lua` | `wrath-unbound/env/dist/etc/modules/lua_scripts/` |
| Module conf files | `env/dist/etc/modules/mod_<name>.conf` | `wrath-unbound/env/dist/etc/modules/` |
| Compose override | `docker-compose.override.yml` (repo root) | `wrath-unbound/docker-compose.override.yml` |
| Conf changes we depend on | `src/server/apps/worldserver/worldserver.conf.dist` | tracked in-tree |

The fork's `.gitignore` keeps provisioned/build content out of the index:
`/modules/*`, `/env/dist/*`, `/*.override.yml`, `data/sql/custom/*`, and
`src/server/scripts/Custom/*` are all ignored. Consequences:

- **Author in the tracked home, stage into the build location.** Never author
  a module, script, or conf only in `modules/`, `env/dist/`, or at the repo
  root — that content is invisible to git and lost on reprovisioning. Stage
  with the commands in
  [wrath-unbound/README.md](../wrath-unbound/README.md#staging-command).
- One-off world content SQL is the exception: `data/sql/updates/pending_db_*/`
  is tracked in place and is applied automatically by the world DB updater at
  worldserver start. Put content migrations there directly.
- `data/sql/custom/` is for scratch/experiment SQL only — it is gitignored
  and is never a deliverable.
- `src/server/scripts/Custom/` is upstream's C++ scripts slot. We do not use
  it for gameplay (see §3); keep it empty.

### 1.2 C++ module conventions

- A module is `modules/mod-<name>/` with `src/`, optional
  `data/sql/db-{world,characters}/`, a `README.md`, and — when it carries a
  core hook patch — `<mod>-core-access.patch`.
- **No `CMakeLists.txt` needed.** With the docker build's `MODULES=static`,
  `modules/CMakeLists.txt` auto-collects `modules/<mod>/src/**` into the
  worldserver static module build; the loader entry point must be named
  `Addmod_<name>Scripts()` (e.g. `Addmod_unboundScripts()`,
  `Addmod_custom_classesScripts()`).
- Disabling a module at build time: `-DDISABLED_AC_MODULES="mod1;mod2"`.
- A module with zero template/content rows must be a **complete no-op**
  (vanilla behaviour) — early-return in every handler when nothing applies.
  This is the mod-custom-classes precedent and the rule for every framework.

### 1.3 Keeping upstream merges cheap

- Custom C++ gameplay code lives in `modules/`, never in `src/server/`.
  The only in-tree core edits ever are **framework hook patches**
  (mod-unbound: 6 files, commit `f5b197b8d`; mod-custom-classes: 6 files,
  commit `b732dfd71`). Each such patch is vendored as
  `<mod>-core-access.patch` in the module's tracked home and is re-applied
  with `git apply` after any rebase — that file is the merge-risk ledger.
  New in-tree core edits follow the same pattern or they do not happen
  (§3).
- `data/sql/base/`, `data/sql/archive/`, and `data/sql/updates/db_*/` are
  upstream and immutable (upstream `AGENTS.md` rule). All our SQL is
  additive content or new tables.
- `worldserver.conf.dist` changes are in-tree and intentional (e.g.
  `ValidateSkillLearnedBySpells = 0`, deviation D2 in the dev-environment
  doc). Document every one in the dev-environment doc when it lands.
- Every in-tree patch and every tracked `wrath-unbound/` file is a merge
  cost. Adding one is a decision, not a default (§3).

---

## 2. SQL conventions

These are the rules `Scuff (SQL)` works to; published here so they are
enforceable rather than folklore. `Scuff (Rev)` checks every migration
against this section.

### 2.1 Naming and placement

| Kind | Path | Applied by |
|---|---|---|
| One-off world content | `data/sql/updates/pending_db_world/YYYY_MM_DD_NN.sql` | world DB updater, at worldserver start, **in filename order** |
| One-off character content | `data/sql/updates/pending_db_characters/YYYY_MM_DD_NN.sql` | character DB updater |
| Module-owned DDL (framework tables) | `modules/mod-<name>/data/sql/db-world/NN_<name>.sql` | DB updater `MODULE` state, before dated pending updates; re-applied in full whenever the file hash changes |
| One-off payload files (e.g. Wrath Unbound) | `modules/mod-unbound/data/sql/db-world/NN_<name>.sql` | manually, in the documented order — see [First-boot SQL migrations](dev-environment.md#first-boot-sql-migrations-one-time-idempotent) |

- `NN` is a zero-padded same-day sequence (`2026_10_05_00.sql`,
  `2026_10_05_01.sql`). If two files must apply in a specific order, encode
  that in the filenames; never rely on the order in which agents happened to
  create them.
- One file = one migration (one change with one rollback). A validation
  check script travels as its own sibling file (precedent:
  `2026_10_05_01.sql`, SELECT-only with expected results annotated per
  query).
- Scratch/experiment SQL goes in `data/sql/custom/` and never ships.

### 2.2 Idempotency (every file, no exceptions)

- `CREATE TABLE IF NOT EXISTS`; `INSERT IGNORE` or `ON DUPLICATE KEY UPDATE`.
- An upsert is `DELETE` (keyed) + re-`INSERT`, keyed on the stable unique
  column (precedent: Warden seed keyed on `template_key`).
- **No standalone `ALTER` in a re-applied file.** Module files are re-applied
  in full on hash change; an unguarded `ALTER TABLE … ADD UNIQUE KEY` dies
  with "Duplicate key name" on re-apply. Every constraint lives in the
  `CREATE TABLE` body (see
  `wrath-unbound/modules/mod-custom-classes/data/sql/db-world/01_custom_class_template.sql`).
- Proof is part of the deliverable: apply the file against a scratch
  database, confirm the rows land, **re-run and confirm the second run is a
  no-op**, and say so in the task update. A migration that applies cleanly
  is not a migration that works in game — say which is which.

### 2.3 Rollback

- The header comment of every migration states, in order: what the file does
  (numbered), the target database, the idempotency note, and a
  **`ROLLBACK:` block with the exact statements that undo this file**
  (precedent: `data/sql/updates/pending_db_world/2026_10_05_00.sql`).
- Every destructive step ships with the statement that undoes it.
- Shared infrastructure tables are intentionally **not** dropped by a
  content rollback (a content migration removes its rows, never the table a
  framework owns).
- Never run a destructive migration against a live world database without
  naming the backup you took first.

### 2.4 Guid and entry strategy

- **Guids are coordinates, not decoration.** A hard-coded guid collides the
  moment two pieces of content want the same spawn. Where practical, use
  guid columns or reserved ranges instead of literals.
- **Company-reserved custom entry range: `900000` and up.** Precedents:
  creature entry `900001` (The Mentor), item entry `900100` (Unbounding
  Mentor Stone). Allocate from this range upward, never reuse an entry, and
  record the allocation in the migration file header so the next engineer
  sees it. Flag every hard-coded guid you add in the task update.
- Fixed world objects (stones, beacons, world-boss spawns) may use fixed
  spawn guids; record them next to the spawn data in the same migration.
- New **maps/zones**: map and area data is DBC-side. Our toolchain does not
  author DBC; a spec that needs a new map must say so at breakdown time and
  route to the manager — it is not a SQL task.

### 2.5 Reference integrity

- Every spell, creature, item, quest, and objective reference must exist.
  A quest that references a nonexistent spell, or a kill-credit objective
  pointing at a missing creature entry, will not work and will not error
  loudly. Cross-check every reference **before** the review.
- **WotLK spell data is DBC-only** — there is no world-DB spell table. Spell
  ids must be validated against the 3.3.5a DBC at authoring time and the
  bound-id list recorded (in the file header or the issue comment) so review
  can check it.
- **Client visibility rule.** Any spell the *client* must recognise (item
  `spellid_*`, quest reward/display spells) must be a client-known DBC spell
  id. Custom server-only spell ids are invisible to the client — the client
  silently refuses to use the item (root-cause writeup in
  `07_mentor_stone.sql`: the Mentor Stone's `spellid_1` is a real DBC spell,
  cancelled server-side by the Lua `ITEM_EVENT_ON_USE` handler).
- Match the upstream 3.3.5a schema exactly: verify column names against the
  real schema **before** writing the migration, not after. Remember schema
  drift (e.g. `scale`, `mechanic_immune_mask`, `spell_school_immune_mask`
  dropped from `creature_template` in upstream migration `2026_03_22_03`).

### 2.6 The base-table rule

- Adding **new rows** to upstream content tables (`creature_template`,
  `item_template`, `quest_template`, `smart_scripts`, spawns, …) with new
  ids in our reserved range is the normal way to add world content. Do it.
- **Never modify an existing upstream row in place** — add a new entry with
  a new id instead.
- **Never modify an upstream base table's schema in place** (new columns,
  changed types, new indexes) when a custom table achieves the same thing.
  New framework data gets a new table owned by the framework (precedent:
  `custom_class_template`). A schema change to an upstream table requires
  `Scuff (Core)` sign-off and is reviewed by `Scuff (Rev)` against this rule.
  Forking the base schema is what makes future upstream merges expensive.

---

## 3. The core-vs-script decision rule

The most important rule in this document. **A core C++ patch is a permanent
maintenance and upstream-merge liability: it costs a 30–90 minute
worldserver rebuild every time it changes and it must be re-applied after
every upstream rebase.** Default to Lua; patch core only when a feature
genuinely needs a hook.

### 3.1 Decision test, in order

1. **Does Eluna/ALE expose the event point?** Quest accept/complete, item
   use, unit/creature damage and death, gossip, chat, timers, world state,
   spell events, … If yes, the feature is a Lua task for `Scuff (Lua)`. End
   of decision.
2. **Can it be expressed as data?** A custom table (or a new row in an
   existing content table) plus a hook that already exists in a module
   (e.g. the custom-class registry, the unbound mask consult sites) means the
   feature is a SQL task plus, at most, Lua glue. No new C++.
3. **Does the feature need to read or mutate core state that has no
   hook?** Stat bases, class masks, talent/spell learn gates, condition
   evaluation, LFG role clamping — the mod-unbound and mod-custom-classes
   patches exist exactly for this. This is the only routine justification for
   a core hook.
4. **Is it a hot path?** Per-tick or per-player paths where ALE overhead is
   measurable. Needs evidence, not vibes.

If you reach step 3 or 4, the feature **genuinely needs a core hook**, and
the patch still has to earn itself with the evidence below.

### 3.2 Evidence required to justify a core patch

The task that proposes the patch (usually written by `Scuff (Lua)`,
implemented by `Scuff (Core)`) must state, in the task update or issue:

1. Which ALE hook(s) were considered and **why they are insufficient** —
   name the hooks; "Lua can't do it" without a hook name is not evidence.
2. The exact hook point and file(s) the patch will touch.
3. Why a custom table + an existing module hook does not cover it (§3.1 step
   2).
4. The merge cost: which `<mod>-core-access.patch` grows, how many files,
   and who re-runs the regression after a rebase (QA, by name).
5. The rebuild cost is acknowledged (30–90 min) and the task is not
   unbuildable without it.

`Scuff (Rev)` rejects a core patch that fails steps 1–2. If an upstream
check or patch must be disabled to make a patch apply, that is said
**explicitly in the task update** — never silently.

### 3.3 Core patch process

1. `Scuff (Lua)` writes the hook request (or `Scuff (Core)` proposes one for
   a framework).
2. `Scuff (Core)` implements it as module code plus the hook patch, vendors
   the patch as `<mod>-core-access.patch` in the tracked home, applies it
   in-tree on `Playerbot`, and verifies `git apply --check` is clean.
3. The rebuild runs (start it early; read design docs while it compiles).
4. `Scuff (Rev)` reviews hook correctness against AzerothCore conventions
   and the unbound-module expectations (the six patched files,
   `Player::GetUnboundClassMask()` consult sites).
5. `Scuff (QA)` regression-checks: the unbound boot markers
   (`[UNBOUND] Prereq map built.`), existing gameplay, and the new
   behaviour.

A patch that compiles is not a patch that works. No self-certification.

---

## 4. Quest spec → task set

The contract between design (`Scuff (Quest)`) and engineering. A spec is a
document on the quest issue (or its parent) that an engineer can build from
**without asking a question**.

### 4.1 Required spec sections

A quest, event, dungeon, or world-boss spec contains all of:

1. **Player fantasy and hook** — what the player is doing and why it is
   interesting, one paragraph.
2. **Trigger and flow** — how it starts, the steps in order, how it ends.
   Explicit state transitions (who acts, what changes state, what the next
   state is), not "the player does stuff".
3. **Content inventory** — every NPC, creature, item, spell, quest,
   gameobject, faction, achievement, pet, and cosmetic the feature needs,
   with the world ids it expects, or "allocate from the `900000`+ reserved
   range" (§2.4) where the designer does not pin the id. Vague entries here
   become wrong work downstream.
4. **Mechanics with numbers** — damage, thresholds, timings, respawn
   windows, cooldowns, scaling rules. "Enraged at 50%" is a mechanic;
   "it gets harder" is not.
5. **Faction/reward effects** — reputation, items, mounts, pets, cosmetics,
   and where each comes from.
6. **Edge cases and failure** — wipe, abandon, disconnect, a second player
   in the group, server restart mid-event. Each with the expected
   behaviour.
7. **Acceptance criteria** — how a tester knows it works, written so
   `Scuff (QA)` can run it without asking the designer a question. If QA
   finds them untestable as written, the spec goes back to `Scuff (Quest)`.
8. **Explicit assumptions** — wherever the source prompt was
   under-specified, the decision the designer made, written down and
   flagged. No silent invention.

### 4.2 Routing: spec section → layer → owner

| Spec material | Layer | Owner | Task content |
|---|---|---|---|
| Content inventory entries | SQL | `Scuff (SQL)` | id allocation (§2.4), rows in content tables or new custom table, migration with rollback, reference cross-checks (§2.5), validation query |
| Trigger/flow, mechanics, edge-case behaviour | Lua | `Scuff (Lua)` | ALE script(s) in `env/dist/etc/modules/lua_scripts/`, defensive nil-checks, reload-idempotent registration, data requested from SQL (no hard-coded content ids in Lua where a query or a constant file suffices) |
| Framework, registry, mask, stat-base, learn-gate, or other system behaviour (§3.1 step 3–4) | Core | `Scuff (Core)` | module code and/or the core hook patch, with §3.2 evidence |
| Acceptance criteria | QA | `Scuff (QA)` | player-path verification plan; findings as expected/actual + reproduction + evidence |

### 4.3 Task shape

- **One task = one owner's layer, one reviewable change, one verifiable
  outcome.** Never bundle SQL, Lua, and C++ into one task.
- Every task description is **self-contained**: objective, owner,
  acceptance criteria, current blocker if any, next action. The engineer
  reading it may not have read the spec — link the spec section anyway.
- Tasks are in **dependency order with explicit Paperclip blockers**
  (`blockedByIssueIds`): e.g. the Lua task blocks on the SQL task when the
  script consumes the new entries.
- If a task cannot be verified without a 30–90 minute worldserver rebuild,
  the breakdown says so up front.
- An engineer who finds the spec ambiguous asks `Scuff (Quest)`; the
  engineer does not invent missing design. If a written task turns out to be
  wrong or impossible, the spec gets fixed and the correction is stated
  plainly — a corrected spec is progress.

---

## 5. Definition of done

A change is not ready for review until the author's bar is met; it is not
landed until Rev and QA have each passed it on their own issues. Nobody
self-certifies.

### 5.1 Before review (the author)

- **It builds and loads.** C++: the worldserver image builds and boots.
  Lua: the script loads at start (or reload) with no error lines. SQL: the
  migration applies to a scratch database and a re-run is a no-op (§2.2).
- **Conventions followed.** This playbook, `AGENTS.md`, and the matching
  `.agents/docs/*` (C++ guidelines for C++, SQL guidelines for SQL,
  code-review/self-review docs before posting).
- **Commits are logical and clean.** No secrets, no `.env` with credentials,
  no real player data. Every commit carries
  `Co-Authored-By: Paperclip <noreply@paperclip.ing>`.
- **The task update says:** what was verified in game vs. what was only
  verified against the database or the build; every hard-coded guid (§2.4);
  for any core patch, the §3.2 evidence; and the exact command to re-run the
  key check (§6).

### 5.2 Review (`Scuff (Rev)` — the merge gate)

Reviews against the spec and its acceptance criteria, not against their own
idea of the feature. Checks:

- **Correctness vs. AzerothCore and Wrath Unbound conventions** — upstream
  patterns, right hook signatures and lifecycle points for 3.3.5a,
  preservation of the unbound module's expectations (the six patched files,
  `Player::GetUnboundClassMask()` consult sites).
- **Database safety** — idempotent and reversible (§2.2–2.3), no colliding
  hard-coded guids (§2.4), no upstream base-table modification in place
  (§2.6), every referenced spell/creature/item/quest present (§2.5).
- **Scripting safety** — can the script take down a live worldserver (nil
  dereference on a missing entry, unguarded player, event double-registered
  on reload)? Does it grant items/levels/gold outside a designed path?
- **Core-vs-script boundary** — does this patch C++ where Lua would do?
  (§3.1, §3.2.)
- **Operability** — is the change verifiable in game and does the task say
  how? Does it force a 30–90 minute rebuild when Lua or SQL would have
  avoided it? That is a real cost, not a nit.

Findings are ranked **blocking** vs. **non-blocking**, specific (file,
line, what is wrong, the concrete change wanted), and posted on the
reviewer's own review issue, which is marked `done` with the verdict. A
completed review with adverse findings is `done`, not `blocked`.

### 5.3 QA (`Scuff (QA)` — in-game sign-off)

- Walks the **player path**, not the diff: accept → progress → fail
  conditions → complete → rewards delivered → repeat-state behaviour, plus
  the spec's edge cases (§4.1 item 6).
- **A finding is a reproduction:** expected/actual, exact steps, log lines
  or screenshots that prove it.
- Verifies the fix **and** that it did not break something else; core-patch
  landings get the regression pass (baseline markers from the T3 known-good
  state still hold: clean boot, `[UNBOUND] Prereq map built.`,
  `.npc add 900001` spawn).
- Verdict posted on their own QA issue, marked `done`.

### 5.4 Merge

Order is fixed: author → `Scuff (Rev)` (verdict `done`) → `Scuff (QA)`
(pass) → merge. Rev's gate is before QA's; reading a diff never substitutes
for an in-game pass.

---

## 6. Verification commands

All of these run **on the build host** over BatchMode SSH — agent sandboxes
have no Docker. Host facts and re-verification duties:
[docs/dev-environment.md § Build host](dev-environment.md#build-host).

```bash
HOST='ssh -o BatchMode=yes 192.168.1.71'

# Stack health (compose root on the host is /mnt/data/wow-server)
$HOST 'cd /mnt/data/wow-server && docker compose ps'
# expected: ac-worldserver, ac-authserver, ac-database (healthy) all Up

# Rebuild after any core/module C++ change (30–90 min; start it early)
$HOST 'cd /mnt/data/wow-server && docker compose build ac-worldserver'
$HOST 'cd /mnt/data/wow-server && docker compose up -d --force-recreate ac-worldserver'

# Stop/start without a rebuild
$HOST 'cd /mnt/data/wow-server && docker compose stop ac-worldserver'
$HOST 'cd /mnt/data/wow-server && docker compose start ac-worldserver'

# Unbound-class boot signals
$HOST 'docker logs ac-worldserver | grep UNBOUND'
# healthy: ends with "[UNBOUND] Prereq map built."
$HOST 'docker logs ac-worldserver | grep "ready..."'

# ALE / Lua load signal
$HOST 'docker logs ac-worldserver | grep -i eluna'
# prove mod-ale is compiled in when no ALE lines appear at all:
$HOST 'docker exec ac-worldserver strings /azerothcore/env/dist/bin/worldserver | grep -i ALE.Enabled'

# Custom-class framework signals (mod-custom-classes)
$HOST 'docker logs ac-worldserver | grep custom-classes'
# good: "custom class registry ready: N template(s) active …"
# bad:  'Template "<key>" disabled…' naming the offending field

# In-game checks (GM3 account; account create / set gmlevel per the
# dev-environment doc § Accounts)
.npc add 900001        # spawns The Mentor; gossip opens the ALE class-unlock ladder
RELOAD CONFIG          # re-runs module validation without a full restart

# Scratch-database SQL check (never the live world DB; §2.2, §2.3)
$HOST "docker exec -i ac-database mysql -u root -ppassword acore_world" \
  < data/sql/updates/pending_db_world/YYYY_MM_DD_NN.sql
# run it twice; the second run must be a no-op

# Core hook patch re-apply check after any rebase (repo checkout, not host)
git apply --check modules/mod-unbound/unbound-core-access.patch
git apply --check modules/mod-custom-classes/custom-class-core-access.patch
# in-tree presence (applied patches, verify do not re-apply):
grep -n GetUnboundClassMask src/server/game/Entities/Player/Player.h
grep -n OnPlayerAfterUpdateAllStats src/server/game/Entities/Unit/StatSystem.cpp
grep -n OnPlayerLfgSetRoles src/server/game/Handlers/LFGHandler.cpp
```

DB root password is the stock default; see the dev-environment doc
(§ First-boot SQL migrations). Do not commit it anywhere new, and do not
put it in scripts that land in tracked homes beyond what the stock stack
already ships.
