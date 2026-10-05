# Custom Class Template Contract

`mod-custom-classes` implements a generic, data-driven custom class system for
Wrath Unbound. This document is the authoritative contract for template
authors (designers/SQL agents): every table field, the JSON curve syntax, the
mask allocation policy, and the rules the server enforces.

**Module home:** `wrath-unbound/modules/mod-custom-classes/` (build copy at
`modules/mod-custom-classes/`).
**Table:** world DB `custom_class_template` (DDL:
`wrath-unbound/modules/mod-custom-classes/data/sql/db-world/01_custom_class_template.sql`).

---

## 1. What a template is

A custom class **re-skins one existing vanilla class** (`backing_class`).
Characters of that vanilla class are treated as the custom class by the
server: they get the template's stat bases, spec gating, starting kit,
proficiencies, pet family, and a dedicated bit in the Wrath Unbound class
mask (so all existing unbound consult sites — trainer class checks, quest
class requirements, conditions, item class locks — automatically accept the
custom class).

- Resolution is **class-level**: every character whose `class` equals a
  template's `backing_class` uses that template. (Per-character opt-in is a
  content concern and out of scope; the architecture leaves an extension
  point for it — see §8.)
- One template per backing class (enforced, `UNIQUE(backing_class)`).
- **Zero template rows = complete vanilla no-op.** Every handler in the
  module early-returns when no template applies.
- Bot characters (mod-playerbots) are never given templates.

## 2. Table fields

| Column | Type | Required | Meaning |
|---|---|---|---|
| `template_key` | `VARCHAR(64)` PK | yes | Stable unique key, e.g. `warden`. Used in every log line. |
| `display_name` | `VARCHAR(64)` | yes | Client-facing display name (informational; the client sees the backing class). |
| `backing_class` | `TINYINT` UNIQUE | yes | Vanilla `ClassID`: `1`=Warrior … `9`=Warlock, `11`=Druid. `10` is reserved and rejected. |
| `color` | `VARCHAR(16)` | no | Informational client colour hint, hex without `#`, e.g. `9FC131`. |
| `icon` | `VARCHAR(64)` | no | Informational client icon hint (spelleditemicon / spell icon name). |
| `primary_stat` | `TINYINT` | no | `0`=STRENGTH `1`=AGILITY `2`=STAMINA `3`=INTELLECT `4`=SPIRIT. Cosmetic/description only. |
| `health_curve` | `TEXT` | no | JSON per-level curve, §3. **Total base health** per level. Empty = vanilla class base kept. |
| `mana_curve` | `TEXT` | no | JSON per-level curve. Total base mana per level. Ignored for 0-mana classes. Empty = vanilla kept. |
| `ap_curve` | `TEXT` | no | JSON `{"melee": {...}, "ranged": {...}}`, §3. Total base attack power per level. `melee` required when the column is set; `ranged` optional (defaults to melee). |
| `spellpower_curve` | `TEXT` | no | JSON per-level curve. Total base spell power per level (school-agnostic base, feeds both spell damage and healing). Empty = vanilla kept. |
| `crit_curve` | `TEXT` | no | JSON per-level curve. **Melee** crit % base per level (0–100). Empty = vanilla agility-driven crit kept. |
| `specs` | `TEXT` | no | JSON array, §4. Empty/NULL = **no spec gating** (the character inherits the backing class' full spell set). |
| `starting_kit` | `TEXT` | no | JSON object, §5. Granted on first login. |
| `pet_family` | `SMALLINT` | no | `CreatureFamily` DBC id the template may tame; `0`/default = cannot tame. Taming works through the vanilla hunter-tame spell; the class gate (`IsClass(CLASS_HUNTER, CLASS_CONTEXT_PET)`) is overridden for this template when `pet_family != 0`. |
| `weapon_prof_mask` | `INT UNSIGNED` | no | `ItemSubClass_Weapon` bitmask; bit `n` = `1 << ITEM_SUBCLASS_WEAPON_X`. Granted idempotently on login. |
| `armor_prof_mask` | `INT UNSIGNED` | no | `ItemSubClass_Armor` bitmask. Same semantics. |
| `unbound_mask_bit` | `INT UNSIGNED` | yes* | A **single bit** in `2048..2147483648` (`1<<11..1<<31`), unique across all rows. OR-ed into the player's unbound class mask on login, see §6. |
| `enabled` | `TINYINT(1)` | no | `0` = row ignored at load (disable without deleting data). Default `1`. |
| `description` | `TEXT` | no | Free-form designer notes. |

\* `unbound_mask_bit` must be non-zero for the custom class to be recognisable
by unbound content; `0` fails validation for `enabled=1` rows.

## 3. JSON curve syntax

All curves are JSON objects mapping **level** to **value**:

```json
{ "1": 118, "10": 686, "20": 1240, "60": 3600, "80": 4800 }
```

- Levels: whole numbers `1..80`. Intermediate levels use the value of the
  greatest level ≤ the character's level (step function).
- Values: whole numbers ≥ 0.
- A curve column left **empty/NULL** means "keep the vanilla class base" for
  that stat — the module only overrides stats that have a curve.
- The value is a **total replacement of the class base**, not a modifier:
  items, auras, talents and ratings still stack on top of it (the module
  replaces the base term only, exactly where the vanilla class formula would
  place it).
- Only a JSON subset is parsed: objects, arrays, numbers, double-quoted
  ASCII strings, `true`/`false`/`null`. No comments, no trailing commas, no
  `\uXXXX` escapes. Anything else fails that row's load (see §7).

`ap_curve` is the one nested shape:

```json
{ "melee": { "1": 10, "80": 300 }, "ranged": { "1": 5, "80": 150 } }
```

## 4. Specs (`specs` column)

JSON array of spec objects:

```json
[
  { "talent_tree": 2, "role": "tank", "spell_whitelist": [24134, 24135] },
  { "talent_tree": 3, "role": "damage", "spell_whitelist": [] }
]
```

| Key | Type | Meaning |
|---|---|---|
| `talent_tree` | number | **Existing vanilla `TalentTab` DBC id** (see §4.1). |
| `role` | string | `"tank"` (0x02), `"healer"` (0x04), `"damage"` (0x08) — mirrors `lfg::PLAYER_ROLE_*`. |
| `spell_whitelist` | number[] | Learnable talent ids within that tree. **Empty/omitted = the whole tree is allowed.** |

**Semantics.** When a template has one or more specs, the module computes a
*blocked* set at load: every talent (talent id + rank spells + spell-book
add) offered by the backing class' talent tabs that is **not** in a
covered spec, and not whitelisted in that spec, and not in
`starting_kit.spells`, becomes unlearnable. Enforced at:

- `OnPlayerCanLearnTalent` (talent point spend — tree must be covered, spell must be whitelisted),
- `OnPlayerCanLearnSpell` (backstop for all spell-learn paths),
- `OnPlayerGetTrainerSpellState` (trainer lists show blocked spells as unavailable and they cannot be bought).

LFG roles: when the player changes their LFG role selection, the server
clamps the tank/healer/damage bits to the union of the template's declared
`role` values (leader/none bits are untouched).

### 4.1 Talent-tree decision (documented)

**Specs reuse existing vanilla `TalentTab` ids. There is no DBC authoring,
no new talent tree, and no new spell ids.**

- The template does not invent a talent tree; it declares which of the
  backing class' real talent tabs are offered and, per tab, which talents
  are learnable. The client sees the backing class' talent UI as-is; the
  server enforces the custom loadout.
- **Constraint (validated at load):** each `talent_tree` must exist in
  `TalentTab.dbc` **and** its `ClassMask` must include the backing class.
  A tree the backing class cannot see in the client is unusable — such a row
  is disabled with a clear error.
- Spell-whitelist enforcement is **server-side only** (talent learn gate +
  trainer gate). Client-side talent UI cosmetics are out of scope.
- A template with `specs` empty/NULL performs **no** spell gating: the
  character is a pure stat/kit/mask reskin of the backing class.

## 5. Starting kit (`starting_kit` column)

```json
{ "spells": [355, 197], "items": [1902, 1903], "action_bar": [1, 2, 17] }
```

| Key | Granted by | Notes |
|---|---|---|
| `spells` | first login | `learnSpell` for each id; spells already known are skipped (idempotent). |
| `items` | first login | stored in best free slots (1 each). |
| `action_bar` | none | **Informational only**: intended client-side action bar slot ids. The server does not manipulate the player's action bar. |

Spell and item ids are validated against `Spell.dbc` / `ItemTemplate` at
load (unknown id → row disabled, §7).

## 6. Unbound class mask allocation policy

Wrath Unbound already consults `Player::GetUnboundClassMask()` in a fixed set
of core sites (trainer class/pet requirements, quest class requirements,
`ConditionMgr` class conditions, spell class/race fit, item class lock
bypass). The custom class system plugs into that existing mechanism:

1. **Range:** template bits come from `1<<11` (`2048`) through `1<<31`
   (`2147483648`). Vanilla class bits are `1<<0..1<<8` plus `1<<10` (druid);
   `1<<9` is reserved; unbound *character unlocks* only ever use the vanilla
   class bits. The two spaces therefore **cannot collide** by construction.
2. **One bit per template, single-bit only:** `unbound_mask_bit` must be
   exactly one bit (`x & (x-1) == 0`) inside the range.
3. **Uniqueness:** two enabled templates may not share a bit (enforced at
   load; the `UNIQUE(backing_class)` key plus one template per class keeps
   the space small — 21 slots).
4. **Registration:** on login, after all `OnPlayerLogin` handlers (the
   deterministic `OnPlayerOnline` point), the template's bit is OR-ed into
   the player's existing unbound mask — so it composes with mod-unbound's
   per-character unlocks.
5. **Allocation ledger (current):** the range is free; assign the lowest
   free bit per template in `template_key` alphabetical order. Currently
   allocated: *(none yet — first allocation happens with the PRO-10 Warden
   seed, which takes `2048` = `1<<11`)*.

Content code that needs to test "is this player the custom class X" uses
`player->GetUnboundClassMask() & <bit>` — never a class check against the
backing class.

## 7. Fail-loud validation (no crash, ever)

Two validation stages, both at worldserver start (and on `RELOAD CONFIG`):

1. **Stage 1 — schema/JSON/range (before DBC load):** table exists; JSON
   parses; levels in `1..80`; values whole ≥ 0; `backing_class` playable and
   unique; `primary_stat` in range; mask single-bit, in range, unique;
   proficiency masks within the item sub-class ranges; `enabled=0` rows are
   skipped silently.
2. **Stage 2 — DBC cross-validation (after DBC/spell/item load):** each
   `talent_tree` exists in `TalentTab.dbc` and offers the backing class;
   whitelist ids exist in `Talent.dbc`; kit spell ids exist in `Spell.dbc`;
   kit item ids exist in the item template store; `pet_family` exists in
   `CreatureFamily.dbc`.

On failure the server logs **one line per broken template**, always in the
form:

```
[00:00:00] [ERROR] worldserver.custom-classes: Template "warden" disabled: specs[0].role: must be "tank", "healer" or "damage"
[00:00:00] [ERROR] worldserver.custom-classes: Template "warden" disabled after DBC validation: spec talent_tree 42 not found in TalentTab.dbc
```

…names the template key **and the offending field/reason**, disables that
template, and continues. A row that fails stage 2 is removed from the active
registry. Good rows are unaffected. A summary line reports rows read /
active / disabled-by-flag / rejected.

## 8. Worked example (fictional class)

A "Grimweaver" built on Rogue (`backing_class = 4`):

```sql
INSERT INTO custom_class_template
(template_key, display_name, backing_class, color, icon, primary_stat,
 health_curve, mana_curve, ap_curve, spellpower_curve, crit_curve,
 specs, starting_kit, pet_family, weapon_prof_mask, armor_prof_mask,
 unbound_mask_bit, enabled, description)
VALUES
('grimweaver', 'Grimweaver', 4, '4E8C3F', 'inv misc cloth 10', 3,
 '{"1": 92, "40": 1840, "80": 3520}',
 NULL,
 '{"melee": {"1": 14, "40": 180, "80": 260}}',
 '{"1": 6, "40": 120, "80": 210}',
 '{"1": 4, "80": 18}',
 '[{"talent_tree": 7, "role": "damage", "spell_whitelist": []}]',
 '{"spells": [36, 1752, 1766], "items": [1260, 5002], "action_bar": [1, 2, 3, 17, 18, 19]}',
 18,
 2,
 3,
 2048,
 1,
 'Fictional example row for the contract doc. Tree 7 = Rogue Assassination.');
```

Notes:

- `mana_curve` NULL → vanilla rogue has no mana; a 0-mana backing class keeps 0.
- `specs` covers only the Assassination tree (tab id 7), whole tree, damage
  role → Combat/Precision talents of the rogue are blocked, Assassination is
  fully learnable; LFG role selection is clamped to damage.
- `pet_family = 18` (a real `CreatureFamily` id, e.g. a wisp-like family) →
  the grimweaver can use a hunter-tame spell to tame that family.
- `weapon_prof_mask = 2` (`1 << ITEM_SUBCLASS_WEAPON_DAGGERS`) → dagger
  proficiency granted on login; `armor_prof_mask = 3` → leather + cloth.
- `unbound_mask_bit = 2048` → the first allocation per §6.
- `action_bar` is recorded for client tooling; the server ignores it.

## 9. Designer guide: how to add a class

1. **Pick a backing class** (vanilla `ClassID`). One template per class.
2. **Choose a free mask bit** from §6's ledger (lowest free bit,
   alphabetical order) and record it in the ledger.
3. **Write the row.** Start with the minimal useful set:
   `template_key`, `display_name`, `backing_class`, `unbound_mask_bit`, and
   whichever curves/specs/kit you actually want. Every empty column means
   "vanilla".
4. **Validate your ids** (DBC ids, not server-side numbers):
   - talent trees from `TalentTab.dbc` (must show for the backing class);
   - talent ids from `Talent.dbc`;
   - spells from `Spell.dbc`; items from the item template store;
   - pet family from `CreatureFamily.dbc`.
5. **Apply the DDL once** (idempotent):
   `wrath-unbound/modules/mod-custom-classes/data/sql/db-world/01_custom_class_template.sql`,
   then `INSERT` your row (migrations/seeds belong in the content SQL issue,
   e.g. PRO-10 for Warden — this module ships no seed rows by design).
6. **Restart the worldserver** (or `RELOAD CONFIG` as GM). Check the log:
   - good: `custom class registry ready: 1 template(s) active (0 removed after DBC validation)`;
   - bad: one `Template "<key>" disabled...` line naming the field — fix the row, reload.
7. **Test in game:** log a character of the backing class; verify the stat
   bases, the trainer/talent restrictions, the starting kit (new
   character), the LFG role clamp, taming (if any), and that vanilla
   characters of *other* classes are untouched.

Removing a class = `DELETE` the row (or set `enabled = 0`) and reload.

## 10. Reload & operations

- `RELOAD CONFIG` (GM) re-reads the config key and re-runs both validation
  stages (DBC stores are already live at that point), so table edits can be
  applied on a running server without a full restart. Newly registered
  templates take effect for the mask on the next login.
- No dedicated GM command ships with PRO-9: adding one requires a core RBAC
  permission enum entry (out of scope). A `.customclass reload|list` command
  is a natural future extension; until then, use `RELOAD CONFIG` plus the
  `worldserver.custom-classes` log tag.

## 11. Extensibility points (intentionally open)

- **Per-character opt-in:** resolution is class-level today; a
  character-DB flag (like mod-unbound's `unbound_character_unlocks`) can be
  consulted in `Registry::GetPlayerTemplate` without touching the hooks.
- **Pet spells per family:** the tamed pet learns its family's vanilla
  passive spells (`PetFamilySpells`); custom pet spell loadouts are a future
  module concern.
- **Client cosmetics:** `color`/`icon`/`action_bar` are informational hints
  for client-side tooling (e.g. an addon); the server does not enforce
  client appearance.
