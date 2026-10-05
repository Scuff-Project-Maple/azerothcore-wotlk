-- ============================================================================
-- mod-custom-classes
-- World DB table: custom class template registry
--
-- One row per custom class. A template re-skins an existing vanilla class
-- (backing_class) with its own stat curves, spec gating, starting kit,
-- proficiencies, pet family and an unbound class mask bit.
--
-- The module loads this table at worldserver start. Zero rows = complete
-- no-op (vanilla behaviour). See docs/custom-class-template.md for the full
-- field contract, JSON curve syntax and mask allocation policy.
--
-- No seed rows here: Warden (and any other shipped class) is seeded by
-- PRO-10 (SQL schema + migrations + Warden seed).
-- ============================================================================

CREATE TABLE IF NOT EXISTS `custom_class_template` (
    `template_key` VARCHAR(64) NOT NULL COMMENT 'Stable unique key, e.g. "warden"',
    `display_name` VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'Client-facing display name',
    `backing_class` TINYINT UNSIGNED NOT NULL COMMENT 'Vanilla ClassID this template is built on: 1-9 or 11 (druid). 10 is reserved.',
    `color` VARCHAR(16) NOT NULL DEFAULT '' COMMENT 'Informational client colour hint (hex, e.g. "9FC131")',
    `icon` VARCHAR(64) NOT NULL DEFAULT '' COMMENT 'Informational client icon (spelleditemicon / spell icon name)',
    `primary_stat` TINYINT UNSIGNED NOT NULL DEFAULT 0 COMMENT '0=STRENGTH 1=AGILITY 2=STAMINA 3=INTELLECT 4=SPIRIT; drives cosmetic/description only',
    `health_curve` TEXT COMMENT 'JSON {"level": value}: total base health per level. Empty = vanilla class base kept.',
    `mana_curve` TEXT COMMENT 'JSON {"level": value}: total base mana per level (ignored for 0-mana classes). Empty = vanilla kept.',
    `ap_curve` TEXT COMMENT 'JSON {"melee": {"level": value}, "ranged": {"level": value}}: total base attack power per level. melee required when set; ranged optional.',
    `spellpower_curve` TEXT COMMENT 'JSON {"level": value}: total base spell power per level. Empty = vanilla kept.',
    `crit_curve` TEXT COMMENT 'JSON {"level": value}: melee crit % base per level (0-100). Empty = agility-driven vanilla crit kept. Spell crit stays intellect/rating driven.',
    `specs` TEXT COMMENT 'JSON array: [{"talent_tree": <TalentTab dbcid>, "role": "tank"|"healer"|"damage", "spell_whitelist": [<talent dbcid>...]}]. Empty/NULL = no spec gating (inherits backing class spells). spell_whitelist empty = whole tree allowed.',
    `starting_kit` TEXT COMMENT 'JSON {"spells": [...], "items": [...], "action_bar": [...]}. action_bar is informational (client-side slots). Granted on first login.',
    `pet_family` SMALLINT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'CreatureFamily dbcid this template may tame; 0 = cannot tame.',
    `weapon_prof_mask` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'ItemSubClass_Weapon bitmask (bit = 1 << ITEM_SUBCLASS_WEAPON_X). Granted on login.',
    `armor_prof_mask` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'ItemSubClass_Armor bitmask. Granted on login.',
    `unbound_mask_bit` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT 'Single bit in 2048..2147483648 (1<<11..1<<31); unique across rows. OR-ed into Player::GetUnboundClassMask() so existing unbound consult sites (trainer/quest/condition/item class checks) accept the custom class.',
    `enabled` TINYINT(1) NOT NULL DEFAULT 1 COMMENT '0 = row ignored at load (template disabled without deleting data).',
    `description` TEXT COMMENT 'Free-form designer notes.'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

ALTER TABLE `custom_class_template`
    ADD UNIQUE KEY `uq_custom_class_template_backing` (`backing_class`);
