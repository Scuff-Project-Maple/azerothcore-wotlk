# Custom Class Lua Hook Layer — Eluna/ALE boundary contract

**Companion to:** [`docs/custom-class-template.md`](./custom-class-template.md) (the C++
template/registry contract, PRO-9). This document is the **Lua half** of that contract and
is the only place that defines what a class script pack may bind to and what it may not
touch.

**Owner:** Scuff (Lua). **Audience:** anyone writing `*.lua` under
`env/dist/etc/modules/lua_scripts/`.

**Verified against:** branch `Playerbot` @ `117dcd3bd`, dev host
`scuff-dev@192.168.1.71:/mnt/data/wow-server`, ALE engine tree
`modules/mod-ale/src/LuaEngine/` (pinned upstream `1cb86c9600260c3731c96dc3c98d25b4fc3f2153`,
see `docs/dev-environment.md`).

Every `Hooks.h` / `LuaFunctions.cpp` citation below is a real path in that tree.

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
| Tracked home (commit here) | `wrath-unbound/env/dist/etc/modules/lua_scripts/` | `docs/content-playbook.md:41` |
| Deployed path (loaded by ALE) | `env/dist/etc/modules/lua_scripts/` (gitignored via `/env/dist/*`) | `docs/dev-environment.md:378-381` |
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
- There is **no `Unregister*Event` function** in the engine — the global table
  (`modules/mod-ale/src/LuaEngine/LuaFunctions.cpp:56-74`) exposes only `Register*` forms.
  Registration is once-per-process.

### 2.1 Idempotency contract (mandatory)

Because ALE has no unregister and this deployment does not hot-reload, a pack is required to:

1. Register every hook **exactly once at file scope**, never from inside another handler.
2. Guard per-player state in a module-local table keyed by GUID (`player:GetGUIDLow()`), and
   clear entries on `PLAYER_EVENT_ON_LOGOUT` (id `4`) so the table cannot grow unbounded.
3. Never assume a file is loaded twice-and-fine: if you need a shared table, put it in one
   library file and `require` it; do not duplicate it in two packs.
4. `require`-time work must be side-effect free except registration. A pack that writes to the
   DB or sends chat at load is a bug.
5. Because `ALE.BytecodeCache = true`, after editing a script confirm the worldserver actually
   re-read it (see §8) rather than trusting the file on disk.

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
| `HB-PCK-02` | Registry revalidation on config load | `RegisterServerEvent` | `9` `WORLD_EVENT_ON_CONFIG_LOAD` | `(event, reload)` | bootstrap file |
| `HB-PLY-01` | Identity attach on login | `RegisterPlayerEvent` | `3` `PLAYER_EVENT_ON_LOGIN` | `(event, player)` | `warden_core.lua` |
| `HB-PLY-02` | Pack state teardown | `RegisterPlayerEvent` | `4` `PLAYER_EVENT_ON_LOGOUT` | `(event, player)` | `warden_core.lua` |
| `HB-PLY-03` | First-login audit hook | `RegisterPlayerEvent` | `30` `PLAYER_EVENT_ON_FIRST_LOGIN` | `(event, player)` | `warden_core.lua` |
| `HB-SPEC-01` | Spec resolution | `RegisterPlayerEvent` | `16` `PLAYER_EVENT_ON_TALENTS_CHANGE` | `(event, player, points)` | `warden_core.lua` |
| `HB-SPEC-02` | Spec reset | `RegisterPlayerEvent` | `17` `PLAYER_EVENT_ON_TALENTS_RESET` | `(event, player, noCost)` | `warden_core.lua` |
| `HB-SPEC-03` | Talent-learn telemetry | `RegisterPlayerEvent` | `39` `PLAYER_EVENT_ON_LEARN_TALENTS` | `(event, player, talentId, talentRank, spellid)` | `warden_core.lua` |
| `HB-SPEC-04` | Spell-learn telemetry | `RegisterPlayerEvent` | `44` `PLAYER_EVENT_ON_LEARN_SPELL` | `(event, player, spellId)` | `warden_core.lua` |
| `HB-WL-01` | Whitelist observation | `RegisterPlayerEvent` | `5` `PLAYER_EVENT_ON_SPELL_CAST` | `(event, player, spell, skipCheck)` | `warden_spec_*.lua` |
| `HB-WL-02` | Cast pre-prepare observation | `RegisterSpellEvent` | `1` `SPELL_EVENT_ON_PREPARE` | `(event, caster, spell)` | `warden_spec_*.lua` |
| `HB-FEEL-01` | Combat window open | `RegisterPlayerEvent` | `33` `PLAYER_EVENT_ON_ENTER_COMBAT` | `(event, player, enemy)` | `warden_spec_*.lua` |
| `HB-FEEL-02` | Combat window close | `RegisterPlayerEvent` | `34` `PLAYER_EVENT_ON_LEAVE_COMBAT` | `(event, player)` | `warden_spec_*.lua` |
| `HB-FEEL-03` | Damage contribution hook | `RegisterPlayerEvent` | `72` `PLAYER_EVENT_ON_DEAL_DAMAGE` | `(event, player, target, damage, damagetype)` — **may return a new damage amount** | `warden_spec_*.lua` |
| `HB-FEEL-04` | Melee damage modifier | `RegisterPlayerEvent` | `69` `PLAYER_EVENT_ON_MODIFY_MELEE_DAMAGE` | `(event, player, target, damage)` — **may return a new damage amount** | `warden_spec_*.lua` |
| `HB-AURA-01` | Buff tracking (apply) | `RegisterPlayerEvent` | `64` `PLAYER_EVENT_ON_AURA_APPLY` | `(event, player, aura)` | `warden_spec_*.lua` |
| `HB-AURA-02` | Buff tracking (remove) | `RegisterPlayerEvent` | `67` `PLAYER_EVENT_ON_AURA_REMOVE` | `(event, player, aura, remove_mode)` | `warden_spec_*.lua` |
| `HB-PET-01` | Pet world-attach | `RegisterPlayerEvent` | `43` `PLAYER_EVENT_ON_PET_ADDED_TO_WORLD` | `(event, player, pet)` | `warden_pet.lua` |
| `HB-PET-02` | Pet summon acknowledgement | `RegisterCreatureEvent` | `22` `CREATURE_EVENT_ON_SUMMONED` | `(event, creature, summoner)` | `warden_pet.lua` |
| `HB-PET-03` | Pet death/despawn bookkeeping | `RegisterCreatureEvent` | `21` `CREATURE_EVENT_ON_SUMMONED_CREATURE_DIED` | `(event, creature, summon, killer)` | `warden_pet.lua` |
| `HB-PET-04` | Owner-attacked reaction | `RegisterCreatureEvent` | `12` `CREATURE_EVENT_ON_OWNER_ATTACKED` | `(event, creature, target)` | `warden_pet.lua` |
| `HB-PET-05` | Pet loss accounting | `RegisterPlayerEvent` | `58` `PLAYER_EVENT_ON_PET_KILL` | `(event, player, killer)` | `warden_pet.lua` |
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
`RELOAD CONFIG` re-reads the registry (`docs/custom-class-template.md` §10), so a template
edited on a live server does not leave the pack holding stale spec state. **May not touch:**
anything `HB-PCK-01` may not, plus it must not re-register hooks (no unregister exists — a
second `Register*` call for the same event stacks a second handler).

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

**`HB-WL-02` Cast pre-prepare observation.** Entry-agnostic spell observation for the
whitelist set. Same veto limitation and the same prohibitions as `HB-WL-01`. Additionally:
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
helpers can see them. **May not touch:** it must not `RemoveAura`/`AddAura` auras owned by the
template layer, the C++ gates, or another class's pack. Only auras whose spell ids are declared
in the pack's own whitelist are in scope. Nil-check the aura and its spell info before reading
ids.

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

**`HB-PET-05` Pet loss accounting.** Observe and log. **May not touch:** pet reuse/reward.

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
6. **Defensive nil checks are mandatory.** Before use: `player`, `player:GetEntry?`, `unit`,
   `pet`, `aura`, `aura:GetSpell?`, `spell:GetEntry?`, `creature`, `item`. A nil dereference in
   a player handler takes the feature down for everyone in the event, and with
   `ALE.TraceBack = false` you will not get a stack in the log.
7. **No global namespace pollution.** Only `Register*` calls and a single named pack table may
   exist at global scope. Never overwrite an engine global, never `require` another pack's file.
8. **No long-running work in a hot handler.** No queries, no loops over `GetPlayersInWorld()`,
   no file IO in `HB-FEEL-*` or `HB-AURA-*`. These fire per event per player.
9. **Console commands:** `PLAYER_EVENT_ON_COMMAND` (`42`) is the only sanctioned way to add a
   debug command, `player` is `nil` for console input so nil-check it, and it must be gated on
   `GetConfigValue` / security level. Do not register a command that grants anything.

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

---

## 8. Verification recipe (smallest check that proves it)

No worldserver rebuild is needed for Lua. Reload is a restart of the ALE state, which means a
worldserver restart — **coordinate in the issue thread first** when another agent owns a
stop/start pair on the same container (QA runs do this).

```bash
ssh -o BatchMode=yes 192.168.1.71 'cd /mnt/data/wow-server && docker compose ps'          # state first, never restart blind
ssh -o BatchMode=yes 192.168.1.71 'cd /mnt/data/wow-server && docker compose logs worldserver 2>&1 | grep -iE "eluna|\[ALE\]" | tail -30'
```

Expected signals (`LuaEngine.cpp`):

- `"[ALE]: Searching scripts from ..."` at INFO — proves the script path was scanned.
- `"[ALE]: Loaded N scripts in X ms"` — DEBUG level; raise the logger for the ALE channel if
  it is missing, do not "verify" by absence.
- Any `"[ALE]: Error loading ..."` line is a FAIL, including the duplicate-filename case.

Then, in game, prove the intended state change per boundary: login as a template character and
as a vanilla-class character, and confirm the pack acts on the first and is silent on the
second. `HB-SPEC-01` is proven by a talent dump change flipping the cached spec; `HB-PET-01` by
taming within `pet_family` and seeing the pack map populated; `HB-NPC-01` by opening the class
NPC gossip.
