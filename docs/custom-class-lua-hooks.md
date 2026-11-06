# Custom Class Lua Hook Layer — Eluna/ALE boundary contract

**Companion to:** [`docs/custom-class-template.md`](./custom-class-template.md) (the C++
template/registry contract, PRO-9). This document is the **Lua half** of that contract and
is the only place that defines what a class script pack may bind to and what it may not
touch.

**Owner:** Scuff (Lua). **Audience:** anyone writing `*.lua` under
`env/dist/etc/modules/lua_scripts/`.

**Verified against:** branch `Playerbot` @ `3dc6ef7c5` (the [PRO-269](/PRO/issues/PRO-269)
`lua-sync.sh` merge, which landed while this revision was being written — this document is rebased
on it and every `docs/dev-environment.md` line citation below is against that tip), dev host
`scuff-dev@192.168.1.71:/mnt/data/wow-server`, ALE engine tree
`modules/mod-ale/src/LuaEngine/`. The engine tree is **staged content, not a nested repo**:
`.gitignore:9` (`/modules/*`) keeps it out of the index and there is no `.git` inside
`modules/mod-ale` on this host, so its provenance of record is the pin in
`docs/dev-environment.md:27` — upstream `azerothcore/mod-ale` @
`1cb86c9600260c3731c96dc3c98d25b4fc3f2153` (Wrath Unbound installer v1.2.2). Do not cite this
document's line numbers against a different ALE tree.

**Revision 2** (PRO-74 follow-up): the `9d513a0ef` first edition carried five API-surface errors
found by the [PRO-97](/PRO/issues/PRO-97) review gate and never corrected (the 2026-10-29 pause
landed first). Every claim changed below was re-checked at the **dispatch site**, not at the
`Hooks.h` comment — those comments are stale in at least three places (`WORLD_EVENT_ON_CONFIG_LOAD`
omits `isBefore`; `PLAYER_EVENT_ON_PET_KILL` says `(event, player, killer)` but the dispatch pushes
`(player, killed)`).

---

## 1. The layering rule (read this first)

The template registry (`mod-custom-classes`, C++) owns everything that changes **numbers and
gates**. The Lua pack owns everything that changes **feel and behaviour**.

| Concern | Owner | Why |
|---|---|---|
| Base stat curves, spellpower, melee crit base | C++ `OnPlayerAfterUpdateAllStats` | One tail hook, idempotent; a Lua re-write would double-apply |
| Spec gating / blocked spells, trainer state | C++ `OnPlayerGetTrainerSpellState`, `OnPlayerCanLearnSpell` | Must gate before the client is told a spell is learnable |
| Unbound mask bit registration on login | C++ `OnPlayerOnline` | Must run after all login handlers, order is unspecified otherwise |
| LFG role clamp | C++ `OnPlayerLfgSetRoles` | Core performs no class validation of roles |
| Starting kit, weapon/armor proficiencies | C++ (registry `starting_kit`, `weapon_prof_mask`, `armor_prof_mask`) | Economy path; must be one designed grant point |
| **Spec resolution from active talents, rotation helpers, cooldown/use logic, pet command behaviour, feedback text, encounter/NPC glue** | **Lua (this layer)** | Iteration speed; no rebuild, no upstream merge liability |

If you find yourself wanting to set a stat, grant a spell, or bypass a gate from Lua, you are
crossing the line — see §7 for the escalation path instead.

---

## 2. Where a class pack lives and how it is loaded

| Item | Value | Source |
|---|---|---|
| Tracked snapshot (commit here) | `wrath-unbound/env/dist/etc/modules/lua_scripts/` | `docs/content-playbook.md:41` |
| Deployed path (loaded by ALE) | `env/dist/etc/modules/lua_scripts/` — **not tracked**, `.gitignore:19-20` (`/env/dist/*`), see `docs/dev-environment.md:70-72` |
| Deploy mechanism | `scripts/lua-sync.sh` installs the deployed dir from the snapshot (PRO-269); the module `.conf`s are still `cp`-staged | `docs/dev-environment.md:741`, `:736-737` |
| Scanned directories | exactly **one** today: `ALE.ScriptPath` is a single path | `env/dist/etc/modules/mod_ale.conf` |
| In-container script path | `/azerothcore/env/dist/etc/modules/lua_scripts` | `ALE.ScriptPath`, `env/dist/etc/modules/mod_ale.conf` |
| Enabled | `ALE.Enabled = 1` | `env/dist/etc/modules/mod_ale.conf` |
| Auto-reload | `ALE.AutoReload = 0`-equivalent (`ALE.AutoReload = false`) | same |
| Bytecode cache | `ALE.BytecodeCache = true` | same |
| Extra require paths | `ALE.RequirePaths = ""`, `ALE.RequireCPaths = ""` | same |

Load semantics, all from `modules/mod-ale/src/LuaEngine/LuaEngine.cpp`:

- Scripts are collected by directory scan and **sorted by full filepath**
  (`ScriptPathComparator`, `LuaEngine.cpp:689-690`), so load order inside one directory is
  alphabetical by filename. `.ext` extensions are loaded **before** `.lua` scripts.
- Each script directory is appended to `package.path` as `<path>/?.lua`
  (`LuaEngine.cpp:626-629`), so a shared library file in the same directory is reachable with
  `require("warden_lib")` — no `ALE.RequirePaths` change needed.
- **Two scripts with the same filename in different scanned directories is a hard load
  failure**, not a warning: `"[ALE]: Error loading ... File with same name already loaded
  from ..."` (`LuaEngine.cpp:706`) and the second file is skipped. Prefix pack files with the
  class key (`warden_*.lua`) for this reason.
- There is **no `Unregister*Event` function** in the engine (`LuaFunctions.cpp:56-74` is the
  `Register*` block). The same `GlobalMethods[]` table continues at
  `LuaFunctions.cpp:79-95` with 19 `Clear<Type>Events` globals (`ClearPlayerEvents` `:88`,
  `ClearSpellEvents` `:94`, `ClearAllCreatureEvents` `:95`), implemented at
  `methods/GlobalMethods.h:3156` / `:3216` / `:3340`. Called with no event argument they clear
  **every** binding of that type process-wide — including `unbound_mentor.lua`'s and every other
  pack's. A pack must therefore never call them (§5.10).
- Consequently **registration-once is pack policy, not an engine guarantee**, and the reason is
  blast radius, not permanence: see §2.1 for what a reload actually does.

### 2.1 Reload semantics and the idempotency contract (mandatory)

**Hot reload exists.** `ALE::OnCommand` (`hooks/PlayerHooks.cpp:42-57`) intercepts the command
text `reload ale` — from console (`player == nullptr`) or from a GM chat session at
`GetSession()->GetSecurity() >= SEC_ADMINISTRATOR` — and calls `ReloadALE()`. On the next world
update tick `hooks/ServerHooks.cpp:256-257` consumes the flag and runs `_ReloadALE()`
(`LuaEngine.cpp:154-179`), which:

```
eventMgr->SetStates(LUAEVENT_STATE_ERASE)   -- every timed/cron event erased
CloseLua()      -> OnLuaStateClose(); DestroyBindStores(); lua_close(L)   -- LuaEngine.cpp:235-248
LoadScriptPaths()
OpenLua()       -> CreateBindStores() (which itself calls DestroyBindStores() first) -- :250-271, :291-320
RunScripts()
```

So registration is **once-per-Lua-state, not once-per-process**, and a single `reload ale`
destroys every binding map **and every module-local Lua table** in the scanned tree, then
re-runs every file from scratch. Read the consequences as requirements:

1. Register every hook **exactly once at file scope**, never from inside another handler. This is
   what makes a pack survive reloads: `RunScripts()` re-executes the file, so file-scope
   registration re-binds the fresh stores. Registration from inside a handler double-binds on the
   first pass and again after every reload.
2. **Never treat module-local state as durable.** Cached registries, GUID-keyed player maps,
   combat windows and cooldown bookkeeping must all be rebuildable at load or lazily on first use.
   A pack that caches the registry row at load and has no lazy path will run with **no** cache
   after a GM reloads ALE — fail loud there, do not silently nil-dereference.
3. Guard per-player state in a module-local table keyed by GUID (`player:GetGUIDLow()`), and
   clear entries on `PLAYER_EVENT_ON_LOGOUT` (id `4`) so the table cannot grow unbounded between
   reloads.
4. Never assume a file is loaded twice-and-fine: if you need a shared table, put it in one library
   file and `require` it; do not duplicate it in two packs.
5. `require`-time work must be side-effect free except registration. A pack that writes to the DB
   or sends chat at load is a bug — and reload re-runs it, so it is a bug **twice**.
6. Because `ALE.BytecodeCache = true`, after editing a script confirm the worldserver actually
   re-read it (see §8) rather than trusting the file on disk.

### 2.2 Deploy and drift (the failure mode that already bit us)

The tracked snapshot and the loaded directory are **different directories**, and only the snapshot
is in git. `ranger_vex_gossip.lua` ran live for weeks with no tracked copy (PRO-269 finding **R6**)
and was rescued only by a read-only tree capture. Since [PRO-269](/PRO/issues/PRO-269) landed on
`Playerbot` the snapshot is the single source of truth and `scripts/lua-sync.sh` owns the deployed
directory. Rules so the Warden pack does not repeat it:

1. A pack file is **authored and committed in the tracked snapshot**, then installed with
   `scripts/lua-sync.sh` — which WRITES, so on the build host it runs as the tree owner
   (`sudo -u pira scripts/lua-sync.sh`; `docs/dev-environment.md:273-278`). Editing the deployed
   copy directly is prohibited: it is gitignored, and `lua-sync.sh` reports a `*.lua` the snapshot
   does not have as an **orphan** and fails the check rather than deleting it.
2. `scripts/lua-sync.sh --check` is the read-only pre-boot assertion for `lua_scripts/`: every
   tracked script must be byte-identical (`cmp`) and no orphan may be present
   (`docs/dev-environment.md:261-281`). Run it alongside `scripts/compose-sync.sh --check` before any
   `docker compose up`/`build` (`docs/dev-environment.md:307-308`) and paste both outputs into the
   pack deploy note — that is the evidence a pack is actually installed, not hand-placed.
   `compose-sync.sh` still owns only the two root compose copies (`docs/dev-environment.md:235-244`);
   there is still **no** drift check for `env/dist/etc/modules/*.conf`, which is where `OPS-01`
   lives.
3. `env/dist/etc/modules/mod_ale.conf` and its tracked snapshot
   `wrath-unbound/env/dist/etc/modules/mod_ale.conf` both set `ALE.Enabled = 1` and the container
   `ALE.ScriptPath`, and both **omit the three log lines the ALE dist ships** (`Appender.ALELog`,
   `Appender.ALEConsole`, `Logger.ALE=4,ALELog ALEConsole`,
   `modules/mod-ale/conf/mod_ale.conf.dist:157-159`). That is the cause of the silent-logs problem in
   §8 — a config gap belonging to the stack owner, not to the pack.

---

## 3. Registry access boundary

A class pack must never hard-code its own class identity, spec trees, whitelist, or pet
family. All of it comes from the registry row.

| Field | Value |
|---|---|
| Boundary id | `HB-REG-01` |
| Name | `custom_class_template` read access |
| Mechanism | `WorldDBQuery(sql)` → `ALEQuery` object (`LuaQuery` methods in `modules/mod-ale/src/LuaEngine/methods/ALEQueryMethods.h`: `IsNull`, `GetColumnCount`, `GetRowCount`, `GetBool`, `GetUInt8/16/32/64`, `GetInt8/16/32/64`, `GetFloat`, `GetDouble`, `GetString`, `NextRow`, `GetRow`) |
| Table | `custom_class_template` (world DB), DDL: `wrath-unbound/modules/mod-custom-classes/data/sql/db-world/01_custom_class_template.sql` |
| Columns a pack may read | `template_key`, `display_name`, `backing_class`, `primary_stat`, `specs`, `pet_family`, `enabled`, plus the curve columns read-only for display/telemetry |
| Registered in | the pack bootstrap file (`warden_core.lua`), once, at load |
| **May not touch** | **No writes.** No `INSERT`/`UPDATE`/`DELETE`/`REPLACE` on any table from Lua — registry mutation is SQL-owned (`Scuff (SQL)`). No `CharDBQuery`/`AuthDBQuery` on registry-adjacent tables. No query inside a per-tick or per-damage handler: query at load or on login, cache in a module-local table. `WorldDBQuery` runs synchronously on the world thread. |
| Failure mode | `WorldDBQuery` returning `nil`, or a row that fails to decode, is a **loud** failure: log via `print` (worldserver log) plus a GM-only `:SendNotification`/`:SendBroadcastMessage` and disable the pack (`PACK_ENABLED = false`). Never fall back to a hard-coded default class profile — that is how a pack silently runs the wrong spec data. |

`specs` is JSON (`[{"talent_tree": <TalentTab dbcid>, "role": "tank"|"healer"|"damage",
"spell_whitelist": [<talent dbcid>...]}]`). Decode it once. If the pack needs a JSON decoder,
vendor one inside the pack directory and `require` it; do not monkey-patch a global.

`backing_class` is the **only** legitimate class comparison available to a pack. `player:GetClass()`
may be used **only** to confirm the player's class equals the registry's `backing_class`.
Any literal class id in Lua (`if player:GetClass() == 2 then`) is a contract violation — the
second-class pack is the test that catches it.

---

## 4. Hook boundaries the pack relies on

`Event id` values are from `modules/mod-ale/src/LuaEngine/Hooks.h`. `Register call` names are
from the engine global table (`LuaFunctions.cpp:56-74`). Every handler receives the event id
as its first argument, as the signatures in `Hooks.h` show.

| ID | Name | Register call | Event id | Handler signature | Registered in |
|---|---|---|---|---|---|
| `HB-PCK-01` | Pack startup self-check | `RegisterServerEvent` | `33` `ALE_EVENT_ON_LUA_STATE_OPEN` | `(event)` | bootstrap file |
| `HB-PCK-02` | Registry revalidation on config load | `RegisterServerEvent` | `9` `WORLD_EVENT_ON_CONFIG_LOAD` | `(event, reload, isBefore)` — dispatch pushes **two** values (`hooks/ServerHooks.cpp:230-236`), so it fires **twice per load** (before and after) | bootstrap file |
| `HB-PLY-01` | Identity attach on login | `RegisterPlayerEvent` | `3` `PLAYER_EVENT_ON_LOGIN` | `(event, player)` | `warden_core.lua` |
| `HB-PLY-02` | Pack state teardown | `RegisterPlayerEvent` | `4` `PLAYER_EVENT_ON_LOGOUT` | `(event, player)` | `warden_core.lua` |
| `HB-PLY-03` | First-login audit hook | `RegisterPlayerEvent` | `30` `PLAYER_EVENT_ON_FIRST_LOGIN` | `(event, player)` | `warden_core.lua` |
| `HB-SPEC-01` | Spec resolution | `RegisterPlayerEvent` | `16` `PLAYER_EVENT_ON_TALENTS_CHANGE` | `(event, player, points)` | `warden_core.lua` |
| `HB-SPEC-02` | Spec reset | `RegisterPlayerEvent` | `17` `PLAYER_EVENT_ON_TALENTS_RESET` | `(event, player, noCost)` | `warden_core.lua` |
| `HB-SPEC-03` | Talent-learn telemetry | `RegisterPlayerEvent` | `39` `PLAYER_EVENT_ON_LEARN_TALENTS` | `(event, player, talentId, talentRank, spellid)` | `warden_core.lua` |
| `HB-SPEC-04` | Spell-learn telemetry | `RegisterPlayerEvent` | `44` `PLAYER_EVENT_ON_LEARN_SPELL` | `(event, player, spellId)` | `warden_core.lua` |
| `HB-WL-01` | Whitelist observation | `RegisterPlayerEvent` | `5` `PLAYER_EVENT_ON_SPELL_CAST` | `(event, player, spell, skipCheck)` | `warden_spec_*.lua` |
| `HB-WL-02` | Cast pre-prepare observation | `RegisterSpellEvent(spellEntry, eventId, handler, shots)` — **entry-keyed**, `spellEntry` first (`methods/GlobalMethods.h:1358` → `RegisterEntryHelper` `:593`-`606`, args `(entry, event, function, shots=0)`) | `1` `SPELL_EVENT_ON_PREPARE` | `(event, caster, spell)` | `warden_spec_*.lua`, once per whitelisted spell entry |
| `HB-FEEL-01` | Combat window open | `RegisterPlayerEvent` | `33` `PLAYER_EVENT_ON_ENTER_COMBAT` | `(event, player, enemy)` | `warden_spec_*.lua` |
| `HB-FEEL-02` | Combat window close | `RegisterPlayerEvent` | `34` `PLAYER_EVENT_ON_LEAVE_COMBAT` | `(event, player)` | `warden_spec_*.lua` |
| `HB-FEEL-03` | Damage contribution hook | `RegisterPlayerEvent` | `72` `PLAYER_EVENT_ON_DEAL_DAMAGE` | `(event, player, target, damage, damagetype)` — **may return a new damage amount** | `warden_spec_*.lua` |
| `HB-FEEL-04` | Melee damage modifier | `RegisterPlayerEvent` | `69` `PLAYER_EVENT_ON_MODIFY_MELEE_DAMAGE` | `(event, player, target, damage)` — **may return a new damage amount** | `warden_spec_*.lua` |
| `HB-AURA-01` | Buff tracking (apply) | `RegisterPlayerEvent` | `64` `PLAYER_EVENT_ON_AURA_APPLY` | `(event, player, aura)` — read the spell id with `aura:GetAuraId()`; `Aura` has **no** `GetSpell` (`AuraMethods[]`, `LuaFunctions.cpp:1081-1101`: `GetCaster`, `GetCasterGUID`, `GetCasterLevel`, `GetDuration`, `GetMaxDuration`, `GetAuraId`, `GetStackAmount`, `GetOwner`, setters, `Remove`) | `warden_spec_*.lua` |
| `HB-AURA-02` | Buff tracking (remove) | `RegisterPlayerEvent` | `67` `PLAYER_EVENT_ON_AURA_REMOVE` | `(event, player, aura, remove_mode)` — same `GetAuraId()` rule | `warden_spec_*.lua` |
| `HB-PET-01` | Pet world-attach | `RegisterPlayerEvent` | `43` `PLAYER_EVENT_ON_PET_ADDED_TO_WORLD` | `(event, player, pet)` | `warden_pet.lua` |
| `HB-PET-02` | Pet summon acknowledgement | `RegisterCreatureEvent` | `22` `CREATURE_EVENT_ON_SUMMONED` | `(event, creature, summoner)` | `warden_pet.lua` |
| `HB-PET-03` | Pet death/despawn bookkeeping | `RegisterCreatureEvent` | `21` `CREATURE_EVENT_ON_SUMMONED_CREATURE_DIED` | `(event, creature, summon, killer)` | `warden_pet.lua` |
| `HB-PET-04` | Owner-attacked reaction | `RegisterCreatureEvent` | `12` `CREATURE_EVENT_ON_OWNER_ATTACKED` | `(event, creature, target)` | `warden_pet.lua` |
| `HB-PET-05` | Kill **credit** for a creature killed by the player's pet | `RegisterPlayerEvent` | `58` `PLAYER_EVENT_ON_PET_KILL` | `(event, player, killed)` — dispatched from `ALE::OnCreatureKilledByPet(Player*, Creature* killed)` (`hooks/PlayerHooks.cpp:702-707`), which pushes `killed`. The `Hooks.h:225` comment `(event, player, killer)` is **wrong**; this is not a pet-death hook | `warden_pet.lua` |
| `HB-NPC-01` | Class NPC gossip open | `RegisterCreatureGossipEvent` | `1` `GOSSIP_EVENT_ON_HELLO` | `(event, player, object)` | `warden_npc.lua` |
| `HB-NPC-02` | Class NPC gossip select | `RegisterCreatureGossipEvent` | `2` `GOSSIP_EVENT_ON_SELECT` | `(event, player, object, sender, intid, code, menu_id)` | `warden_npc.lua` |

`HB-PET-02/03/04` are entry-keyed: `RegisterCreatureEvent(entryId, eventId, handler)`. The
pet **entry ids for the Warden pet family are data, not Lua** — request them from
`Scuff (SQL)` (see `openItem-3` in the PRO-74 report). Never scan `creature_template` to
guess them.

### 4.1 Boundaries: what each hook may and may not do

**`HB-PCK-01` Pack startup self-check.** Verify the pack's registry row exists and decodes,
and that the spec trees referenced by `specs` are the ones the pack was written for. Permits:
read-only `WorldDBQuery`, `print`/log, setting `PACK_ENABLED`. **May not touch:** no player
iteration, no DB writes, no spell/item creation, no registration (registration already
happened at file scope). This is the fail-loud gate: if it fails, the pack disables itself and
says why, rather than half-running.

**`HB-PCK-02` Registry revalidation on config load.** Same checks as `HB-PCK-01`, re-run when
config load re-reads the registry (`docs/custom-class-template.md` §10), so a template edited on a
live server does not leave the pack holding stale spec state. The dispatch signature is
`(event, reload, isBefore)` and it **fires twice per load** — gate on `isBefore` (or on `reload`)
and do all work on one pass, or your checks and their log lines run twice. **May not touch:**
anything `HB-PCK-01` may not, plus it must not re-register hooks: a second `Register*` call for the
same event stacks a second handler for the life of the Lua state (§2.1). Re-registration happens
automatically via `RunScripts()` on `reload ale`, so there is nothing to gain here.

**`HB-PLY-01` Identity attach on login.** Resolve the template for this player via `HB-REG-01`
and cache `{templateKey, activeSpec, resourceMode}` in the pack's GUID-keyed table. Permits:
read, cache, `GetConfigValue`, GM-targeted messaging, sending addon-facing custom chat
prefixes. **May not touch:** `AddSpell`, `LearnSpell`, `AddItem`, `ModifyMoney`,
`SetPower`/`SetMaxPower`, `AddAura` — kit and resource maxima are the C++ template's job
(§1); a second grant path here is an economy exploit. It must **return immediately for
players whose class is not the registry `backing_class`** — zero vanilla regression is a hard
gate, and this hook fires for every login on the server.

**`HB-PLY-02` Pack state teardown.** Delete the player's GUID entry and any timers/callbacks
scheduled for them. **May not touch:** anything persistent; no saves, no DB writes.

**`HB-PLY-03` First-login audit hook.** Observe and log only (first login of a
`backing_class` character is when the C++ layer grants `starting_kit`). **May not touch:** the
kit itself — no `AddItem`/`AddSpell`/`LearnSpell`. If the kit looks wrong, that is a registry
row bug: report it, do not compensate in Lua.

**`HB-SPEC-01` Spec resolution.** Recompute the active spec by comparing learned talents
against `specs[].spell_whitelist`, then switch the pack's helper tables. This is the hook the
spec packs hang their identity on. Permits: read talents, recompute, cache, GM/SELF messaging.
**May not touch:** talent state itself (no talent writes, no spell removal/re-learn to
"enforce" a spec — spec gating is `OnPlayerGetTrainerSpellState`/`OnPlayerCanLearnSpell` in
C++), and no stat modification. It must be safe to fire mid-combat and on a player with zero
talents learned (new character) — a nil here kills the login path for a real player.

**`HB-SPEC-02` Spec reset.** Clear cached spec state so `HB-SPEC-01` recomputes from scratch.
**May not touch:** refunds or cost logic (`noCost` is informational here); talent reset is
core-owned.

**`HB-SPEC-03/04` Talent/spell-learn telemetry.** Observe-only. Use for whitelist drift
detection: log when a learned spell is outside every `spell_whitelist` of the template.
**May not touch:** it must not attempt to unlearn, block, or undo a learn. Blocking belongs
to the C++ trainer/learn gates.

**`HB-WL-01` Whitelist observation.** The pack's only cast-time visibility.
**This hook cannot cancel a cast** — `Hooks.h` documents no veto return for
`PLAYER_EVENT_ON_SPELL_CAST`, so treat it strictly as observe-and-notify. Permits: measure,
log, tell the casting player, feed rotation/cooldown bookkeeping. **May not touch:** damage or
heal amounts, aura mutation, or any attempt to fake a "cast blocked" outcome — the cast has
already gone through. Blocking a non-whitelisted cast needs a real core veto: see §7 `CORE-REQ-1`.

**`HB-WL-02` Cast pre-prepare observation.** Spell-scoped observation for the whitelist set. It is
**entry-keyed** — `RegisterSpellEvent(spellEntry, eventId, handler, shots)` binds one spell entry,
so the pack registers once per whitelisted spell id (from `HB-REG-01`, never a literal list typed
into the file). Same veto limitation and the same prohibitions as `HB-WL-01`. Additionally:
`caster` may be a `Unit` that is not a `Player` — nil-check before calling player methods.

**`HB-FEEL-01/02` Combat window.** Start/stop the pack's rotation and cooldown bookkeeping.
Permits: timers, cached combat state, self/GM messaging. **May not touch:** threat, aggro, or
combat flags (core-owned); and it must not assume `enemy` is non-nil on enter-combat.

**`HB-FEEL-03/04` Damage hooks.** Both **can return a new damage amount**, which makes them
the highest-risk surface in the pack. Rules: (a) return the incoming amount unchanged for any
player the pack does not own — a fall-through that touches damage for a vanilla class is a
vanilla-regression FAIL; (b) any modification must be a documented, spec-data-driven
multiplier with a logged cap, not an ad-hoc bonus; (c) they must never be used to implement
base stats, spellpower, or the melee crit base — that is `OnPlayerAfterUpdateAllStats` in C++
and doing it here double-dips. New damage is an **economy/ balance change** and needs
`Scuff (QA)` sign-off before it ships.

**`HB-AURA-01/02` Buff tracking.** Track the spec's own procs/ cooldown auras so rotation
helpers can see them. Read the spell id with **`aura:GetAuraId()`** — there is no `Aura:GetSpell`
in the engine, and calling it is a nil-method error inside a handler that fires for every aura on
every player. **May not touch:** it must not `Remove()` an aura owned by the template layer, the
C++ gates, or another class's pack (and `Remove` is the only mutator `Aura` exposes). Only auras
whose spell ids are declared in the pack's own whitelist are in scope. Nil-check the aura before
reading its id.

**`HB-PET-01` Pet world-attach.** Attach pack-owned bookkeeping to a newly added pet of the
template's `pet_family` (`CreatureFamily` dbcid). **May not touch:** taming eligibility — taming
is gated by the module's generic class-context hook (`docs/custom-class-template.md` §11,
PRO-11 audit) and by core spell effects; the Lua layer must not `CastSpell` a tame spell, must
not teach pet spells, and must not alter pet family spells. It must not create creatures: pet
creation is core/data-driven. Nil-check `pet` and its `GetEntry()` before reading.

**`HB-PET-02` Pet summon acknowledgement.** Permits: announce, tag owner→pet map, start
follow/formation bookkeeping. **May not touch:** return `true` to stop normal action unless the
pack genuinely replaces that behaviour — these creature hooks accept a
"stop normal action" return; using it casually breaks vanilla pet AI for the shared entry.

**`HB-PET-03` Pet death/despawn bookkeeping.** Clear owner→pet maps. **May not touch:** respawn
timers, resurrection, or loot.

**`HB-PET-04` Owner-attacked reaction.** Permits: react with pack-declared pet abilities on
the whitelisted family. **May not touch:** global aggro manipulation, and it must early-return
when the creature's owner is not a template-owned player.

**`HB-PET-05` Kill credit.** `PLAYER_EVENT_ON_PET_KILL` fires when the player's pet kills a
creature, handing you `(player, killed)` — it is scoring/telemetry, **not** pet-death bookkeeping.
Observe and log; use it for kill credit, never as the trigger that clears owner→pet maps (that is
`HB-PET-03`). **May not touch:** loot, rewards, or anything `killed`-derived that writes state —
`killed` may already be departing the world when the hook runs.

**`HB-NPC-01/02` Class NPC gossip.** Permits: gossip text, menu branching and presentation for the class NPC using the
engine's real gossip surface — `:GossipClearMenu()`, `:GossipMenuAddItem()`, `:GossipSendMenu()`,
`:GossipComplete()` (the names the live Mentor script actually uses). **May not touch:** spell purchase or class unlock — the
Mentor (`env/dist/etc/modules/lua_scripts/unbound_mentor.lua`, NPC `900001`, stone `900100`)
is the **sole** unlock/purchase path, and `mod-unbound` owns the purchase prereqs. A second
gossip path that hands out spells or items is an economy exploit by construction. Also: do not
register gossip on an entry owned by another system — entry ownership belongs to SQL/content.

---

## 5. Universal prohibitions (apply to every boundary above)

1. **No economy writes.** `AddItem`, `AddSpell`, `LearnSpell`, `ModifyMoney` (and any level or
   XP write) are forbidden from the class pack. They exist in the engine
   (`LuaFunctions.cpp`) and they are exactly how a custom class becomes a duplication exploit.
   Designed grants live in the registry (`starting_kit`) and are applied by C++.
2. **No stat writes.** `SetPower`, `SetMaxPower`, `AddAura`-as-buff, and damage hooks used as
   stat systems are out of scope. Stats belong to the template.
3. **No class literals.** `player:GetClass()` compared to a hard-coded number is a contract
   violation. Compare to `backing_class` from `HB-REG-01`, or to nothing.
4. **No content ids invented in Lua.** Creature entries, item ids, spell ids beyond the
   whitelist set, quest ids, pet entries → request from `Scuff (SQL)` and keep them in one
   constants block that names its source issue.
5. **No DB writes of any kind** from a class pack, including characters-DB bookkeeping tables.
   If the pack needs persistence, that is a schema request to `Scuff (SQL)`.
6. **Defensive nil checks are mandatory.** Before use: `player`, `unit`, `pet`, `aura` (then
   `aura:GetAuraId()` — `Aura` has no `GetSpell`), `spell`, `creature`, `item`. A nil dereference
   in a player handler takes the feature down for everyone in the event, and with
   `ALE.TraceBack = false` (set in `env/dist/etc/modules/mod_ale.conf`) you will not get a stack
   in the log.
7. **No global namespace pollution.** Only `Register*` calls and a single named pack table may
   exist at global scope. Never overwrite an engine global, never `require` another pack's file.
8. **No long-running work in a hot handler.** No queries, no loops over `GetPlayersInWorld()`,
   no file IO in `HB-FEEL-*` or `HB-AURA-*`. These fire per event per player.
9. **Console commands:** `PLAYER_EVENT_ON_COMMAND` (`42`) is the only sanctioned way to add a
   debug command, `player` is `nil` for console input so nil-check it, and it must be gated on
   `GetConfigValue` / security level. Do not register a command that grants anything.
10. **Never call `Clear*Events` or `ReloadALE` from a pack.** The engine exposes 19
    `Clear<Type>Events` globals (`LuaFunctions.cpp:79-95`); called without an event argument they
    clear that binding type **process-wide**, and with an event argument they clear every handler
    registered for that event **by any script in the scanned tree** — your `PLAYER_EVENT_ON_LOGIN`
    cleanup would silently strip the Mentor's login handler too. Reload is likewise an operator
    action (`reload ale`, §2.1), not something a pack triggers. A pack that needs to change a
    binding was registered wrong in the first place.

---

## 6. Vanilla-regression rules

Zero regression for the 12 vanilla classes is a hard gate on every hook in §4.

- Every handler's first action after nil checks must be the template ownership test.
- Register creature/gossip/item hooks **entry-keyed**, never `RegisterAllCreatureEvent`, for
  class behaviour: the all-creature surface fires for every creature on the server.
- Hooks that can return a value (`HB-FEEL-03/04`, creature `stop normal action` returns, gossip
  veto) must return the *unchanged* value / no-veto for any non-template subject.
- A pack that cannot determine its subject's template must **no-op**, never assume.

---

## 7. Known gaps — these need a real core hook, not Lua

Each of these was reached from a §4 boundary and cannot be closed in Lua. They are filed to
`Scuff (Core)`; **do not patch core from a Lua task.**

| ID | Need | Why Lua cannot do it |
|---|---|---|
| `CORE-REQ-1` | Blocking spell-veto for a template `spell_whitelist`: e.g. `bool PlayerScript::OnPlayerCanCastCustomSpell(Player*, SpellInfo const*)` returning false, dispatched before cast commit | `PLAYER_EVENT_ON_SPELL_CAST` (`5`) and `SPELL_EVENT_ON_CAST` (`2`) expose no veto return (`Hooks.h`); Lua can only observe a cast that already happened |
| `CORE-REQ-2` | A Lua-visible registry accessor, e.g. globals `GetCustomClassTemplate(keyOrPlayer)` / `GetCustomClassTemplateSpec(player)` returning the validated row | The registry is C++-side (`CustomClassSystem.cpp`); Lua currently re-parses `custom_class_template` and its JSON, so the two layers can disagree after a partial `RELOAD CONFIG` |
| `CORE-REQ-3` | A power-change hook (`OnPlayerPowerChange(Player*, PowerType, int32& newValue)`) | The Warden's resource behaviour is spec-critical and there is no Lua surface for a power gain/loss tick; `mod-unbound` solves its equivalent in C++ (`Player::HasActivePowerType`) |

While `CORE-REQ-1/2/3` are open, the pack ships the **observe-and-report** behaviour described
in `HB-WL-01`/`HB-REG-01` and must not pretend to enforce.

**Status of record (2026-11-05):** all three are filed to `Scuff (Core)` as
[PRO-98](/PRO/issues/PRO-98) (child of [PRO-12](/PRO/issues/PRO-12)), still `todo`. None of them
blocks this contract or the pack's observe-only increment — they block the *enforcing* increment,
and the pack must not fake enforcement while they are open.

---

## 8. Verification recipe (smallest check that proves it)

No worldserver rebuild is needed for Lua, and **no restart is needed either** — ALE hot-reloads
from the console or as a GM at `SEC_ADMINISTRATOR`:

```bash
ssh -o BatchMode=yes scuff-dev@192.168.1.71 'cd /mnt/data/wow-server && docker compose ps'   # state first, never restart blind
# install from the tracked snapshot (§2.2), assert no drift, then reload the Lua state:
#   sudo -u pira scripts/lua-sync.sh   &&   scripts/lua-sync.sh --check
#   console:  reload ale        (or in game as a GM:  .reload ale)
ssh -o BatchMode=yes scuff-dev@192.168.1.71 'cd /mnt/data/wow-server && docker compose logs ac-worldserver 2>&1 | grep -iE "eluna|\[ALE\]" | tail -30'
```

`ALE_LOG_*` all route to the logger channel `"ALE"` (`LuaEngine/ALEUtility.h:36-38`:
`LOG_INFO/ERROR/DEBUG("ALE", ...)`). Expected signals (`LuaEngine.cpp`):

- `"[ALE]: Searching scripts from ..."` at INFO — proves the script path was scanned.
- `"[ALE]: Loaded N scripts in X ms"` at DEBUG.
- Any `"[ALE]: Error loading ..."` line is a FAIL, including the duplicate-filename case.

**Known gap on this stack (do not "verify" by absence).** `docker compose logs ac-worldserver |
grep -iE "eluna|\[ALE\]"` currently returns **nothing** — verified on the live stack 2026-11-05,
with `ac-worldserver` up and the two deployed scripts present. Cause: neither the deployed
`env/dist/etc/modules/mod_ale.conf` nor its tracked snapshot
`wrath-unbound/env/dist/etc/modules/mod_ale.conf` carries the `Appender.ALELog`,
`Appender.ALEConsole` and `Logger.ALE=4,ALELog ALEConsole` lines that
`modules/mod-ale/conf/mod_ale.conf.dist:157-159` ships, so the `ALE` channel falls back to
`Logger.root` (`Logger.root=2`) and its output never reaches the log buffer. That contradicts
`docs/dev-environment.md:603-606`, which tells the reader to expect ALE loading lines for
`unbound_mentor.lua` and `ranger_vex_gossip.lua` and to treat their absence as "prove mod-ale is
compiled in" — on this stack their absence is expected and means nothing. Until the stack owner
restores those lines (config-only, but it needs a worldserver restart — coordinate under the
single-writer lease, `OPS-01`), the only trustworthy pack-load signal is in-game behaviour plus
§8.1. Do not report "no `[ALE]` errors" as a pass.

### 8.1 Pack self-check signal that does not need the log channel

`HB-PCK-01` must make the pack's own verdict visible without the ALE channel: on success it sets
its module table and stays quiet; on failure it disables the pack and sends a GM-targeted
notification naming the failed check. That is the checkable proof a pack actually loaded, and it is
why `HB-PCK-01` is mandatory rather than decorative.

Then, in game, prove the intended state change per boundary: login as a template character and
as a vanilla-class character, and confirm the pack acts on the first and is silent on the
second. `HB-SPEC-01` is proven by a talent dump change flipping the cached spec; `HB-PET-01` by
taming within `pet_family` and seeing the pack map populated; `HB-NPC-01` by opening the class
NPC gossip.

---

## 9. Registry instance under test (live facts, verified 2026-11-05)

`HB-REG-01` stopped being theoretical when the SQL content landed: the world DB has a live row and
the C++ module loaded it. Verified against the running stack — `docker compose logs ac-worldserver`
printed `custom_class_template: 1 row(s) read, 1 template(s) active, 0 disabled by flag, 0 rejected
at load` and `custom class registry ready: 1 template(s) active (0 removed after DBC validation)`.

| Field | Live value | Consequence for the pack |
|---|---|---|
| `template_key` | `warden` | the pack's file prefix (§2) and its registry lookup key |
| `backing_class` | `2` | the **only** legal class test is `player:GetClass() == registry.backing_class`; the literal `2` must not appear in Lua |
| `pet_family` | `4` | pet hooks bind only after a family match; the creature **entries** in this family are still SQL's to supply (open item below) |
| `unbound_mask_bit` | `2048` | C++-owned (§1); Lua must never read or write it |
| `enabled` | `1` | if it flips to `0`, `HB-PCK-01` must disable the pack, not half-run it |
| `specs` | 3 entries: `381`→`damage`, `382`→`healer`, `383`→`tank`, each with a 24–26 id `spell_whitelist` | see the role-from-data rule below |
| `starting_kit` | `spells: [1515, 642, 633, 4987, 635, 19740, 853, 20154, 20271, 465]`, `items: []`, `action_bar: []` | granted by C++ on first login; `HB-PLY-03` observes only |

**Role comes from data, never from the tree id.** The live row maps `381→damage`, `382→healer`,
`383→tank`. Those `TalentTab` ids are the paladin's vanilla tabs, whose conventional vanilla roles
are **not** that mapping — which is exactly the point. A pack that infers "tree 382 must be the tank
spec" silently disagrees with the registry the moment SQL re-roles the class. Rules:

1. Iterate `specs[]` and key behaviour by the `role` **string**; never branch on a `talent_tree`
   literal.
2. Assert the shape you were written for in `HB-PCK-01` (three specs; roles exactly
   `damage`/`healer`/`tank`; every whitelist non-empty) and disable-with-message on mismatch.
3. Treat the whitelist ids as opaque ids from the row: you may iterate them, never add to them
   (§5.4 still applies).

---

## 10. Pack file manifest (the contract for the next increment)

File names are normative because ALE hard-fails on duplicate filenames across scanned dirs (§2) and
because this manifest is what a reviewer diffs a pack against.

| File | Registers | Boundaries owned |
|---|---|---|
| `warden_lib.lua` | nothing — no `Register*` call. Shared helpers: registry decode (`HB-REG-01`), GUID-keyed state tables, role/whitelist lookup, nil-safe accessors | — |
| `warden_core.lua` | server + login/logout/talent hooks | `HB-PCK-01/02`, `HB-PLY-01/02/03`, `HB-SPEC-01/02/03/04` |
| `warden_spec_dmg.lua` | whitelist + feel hooks for the `damage` role | `HB-WL-01/02`, `HB-FEEL-01/02/03/04`, `HB-AURA-01/02` |
| `warden_spec_heal.lua` | same for the `healer` role | same |
| `warden_spec_tank.lua` | same for the `tank` role | same |
| `warden_pet.lua` | entry-keyed creature hooks for the registry `pet_family` | `HB-PET-01/02/03/04/05` |
| `warden_npc.lua` | entry-keyed gossip for the class NPC | `HB-NPC-01/02` |

Each file registers at file scope, `require`s `warden_lib`, exposes exactly one global (its own pack
table), and is deployable and reloadable independently (§2.1/§2.2). The second-class proof pack
([PRO-12](/PRO/issues/PRO-12) acceptance item 4) must be the same shapes under a different
`template_key` prefix with **no** new file kinds — if it needs a new hook shape, that is a contract
change to this document, not a new pattern in a pack.

---

## 11. Open items this increment found (named, not silently absorbed)

None of these is a Lua fix; each is listed with its owner.

| ID | Item | Owner |
|---|---|---|
| `LUA-01` | No pack exists yet: `warden_lib/core/spec_*/pet/npc.lua` (§10) are **absent** from both the tracked snapshot and the deployed dir, which today hold only `unbound_mentor.lua` and `ranger_vex_gossip.lua`. The hook layer is documentation until the pack ships. | Scuff (Lua), next increment |
| `LUA-02` | No JSON decoder is vendored for `specs`/`starting_kit` decode (§3). Must live inside the pack directory and be `require`d. | Scuff (Lua) |
| `LUA-03` | `pet_family 4` has no creature **entry** list supplied. Pet hooks cannot be entry-keyed without them, and §5.4 forbids guessing them. | Scuff (SQL) |
| `LUA-04` | The class NPC entry for `HB-NPC-01/02` is undefined (the Mentor `900001`/stone `900100` is the unlock path and is **not** available for class gossip reuse). Needs an entry + gossip owner. | Scuff (SQL) / Scuff (Quest) |
| `OPS-01` | Neither `env/dist/etc/modules/mod_ale.conf` nor its tracked snapshot `wrath-unbound/env/dist/etc/modules/mod_ale.conf` carries `Appender.ALELog` / `Appender.ALEConsole` / `Logger.ALE` from `modules/mod-ale/conf/mod_ale.conf.dist:157-159`, so the `ALE` log channel emits nothing (§8) while `docs/dev-environment.md:603-606` tells testers to expect those lines. Fix = add the three lines to the snapshot, re-stage the deployed copy, coordinated worldserver restart under the single-writer lease. | Scuff (Core) |
| `OPS-02` | Resolved for Lua, still open for conf. `scripts/lua-sync.sh` (+ `--check`) now owns `env/dist/etc/modules/lua_scripts/` and fails on drift or hand-placed orphans ([PRO-269](/PRO/issues/PRO-269)). There is still **no** equivalent assertion for `env/dist/etc/modules/*.conf` — `compose-sync.sh` covers only the two root compose copies — which is how the `Logger.ALE` omission in `OPS-01` stayed invisible. | Scuff (Core) |
| `OPS-03` | `modules/mod-ale` is untracked (`.gitignore:9`) with no nested `.git` on this host, so the engine tree the whole contract cites cannot be version-checked in place — only against the `docs/dev-environment.md:27` pin. Ties into the upstream-ref provenance work. | Scuff (Core) |
| `CORE-REQ-1/2/3` | Spell veto, Lua-visible registry accessor, power-change hook (§7). Filed as [PRO-98](/PRO/issues/PRO-98), `todo`. | Scuff (Core) |
