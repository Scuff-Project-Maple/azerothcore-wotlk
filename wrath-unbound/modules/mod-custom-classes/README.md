# mod-custom-classes

Generic, data-driven custom class system for Wrath Unbound.

A **custom class** is a row in the world DB table `custom_class_template`
that re-skins an existing vanilla class: its own stat growth curves, spec
gating, starting kit, weapon/armor proficiencies, tameable pet family, and
one dedicated bit in the Wrath Unbound class mask.

**Design goals**

- Zero templates registered (or `CustomClasses.Enabled=false`) is a complete
  no-op: the server behaves exactly like vanilla.
- Adding or removing a class is a pure data operation (one table row).
- A broken row fails loud at startup: one log line naming the template key
  and the offending field, the template is disabled, the server keeps booting.

**Full contract:** [`docs/custom-class-template.md`](../../../docs/custom-class-template.md)
(field reference, JSON curve syntax, mask allocation policy, talent-tree
decision, worked example, designer guide).

## Layout (tracked home)

| Path | Purpose |
|---|---|
| `src/CustomClassSystem.h` | Registry (WorldScript) + template model |
| `src/CustomClassSystem.cpp` | Registry load/validation, JSON parser, player script hooks |
| `src/CustomClassSystem_loader.cpp` | Module entry point (`Addmod_custom_classesScripts`) |
| `custom-class-core-access.patch` | Core hook dispatch sites (additive, see below) |
| `data/sql/db-world/01_custom_class_template.sql` | Table DDL (no seed data; Warden seed is PRO-10) |

The build-time copy lives at `modules/mod-custom-classes/` (gitignored by
design, auto-collected by `modules/CMakeLists.txt` when `MODULES=static`).

## Core hook patch

`custom-class-core-access.patch` adds three dispatch sites to the core
(purely additive; no vanilla logic change):

| File | Addition | Why a core hook |
|---|---|---|
| `Handlers/CharacterHandler.cpp` | `sScriptMgr->OnPlayerOnline(pCurrChar)` after `OnPlayerLogin` | Deterministic "all login handlers done" point; the existing `OnPlayerLogin` order is unspecified between scripts, but mask registration must see mod-unbound's unlock mask first |
| `Entities/Unit/StatSystem.cpp` | `sScriptMgr->OnPlayerAfterUpdateAllStats(this)` at the end of `Player::UpdateAllStats()` | School-agnostic spell power (`m_baseSpellPower`) and the melee crit base have no existing per-stat consult point; one tail hook avoids touching four call sites |
| `Handlers/LFGHandler.cpp` | `sScriptMgr->OnPlayerLfgSetRoles(GetPlayer(), roles)` before `UpdateRoleCheck` | The core performs **no** class validation of LFG roles, so a template cannot clamp roles from a player-level hook |

Plus the matching `PlayerHook` enum values, `PlayerScript` virtuals,
`ScriptMgr` wrappers and declarations. No existing behaviour changes: every
new dispatch is a no-op unless a script registers for the hook.

## Configuration

`env/dist/etc/modules/mod_custom_classes.conf` (module confs auto-load from
`<confpath>/modules/*.conf`):

```
CustomClasses.Enabled = true
```

The key also has a safe built-in default (`true`), so the module works with
no conf file. Set `false` to force the vanilla no-op even if template rows
exist. Reload at runtime with the GM command `RELOAD CONFIG` (the registry
re-runs both validation stages).

## Verification status

Code is complete but **not yet compiled**: this host has no C++ toolchain
or Docker (blocked by PRO-6, the Docker-capable build host). Static
verification performed: exact-anchored core patch (`git apply --check`
passes in both directions), all hook signatures matched against
`ScriptDefines/PlayerScript.h`, all Player/Unit/DBC API calls matched
against headers. Boot + zero-template no-op check is recorded as pending
PRO-6.
