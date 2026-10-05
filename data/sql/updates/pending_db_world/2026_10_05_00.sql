-- ============================================================================
-- 2026_10_05_00 -> 2026_10_05_00
-- Warden custom class template seed (PRO-10: world DB registry schema,
-- migrations and Warden seed data).
--
-- Content of this file:
--   1. Guarded mirror of the module DDL (CREATE TABLE IF NOT EXISTS), so the
--      seed applies even on a fresh DB where mod-custom-classes is disabled.
--      On a normal boot the module DDL (01_custom_class_template.sql, state
--      MODULE, sorts first) creates the table and this statement is a no-op.
--      The unique key is folded inline here (the module file's trailing
--      ALTER TABLE ADD UNIQUE KEY is not idempotent on re-apply; flagged on
--      PRO-9), so this file is safe to re-run.
--   2. Delete plus re-insert of the single Warden template row (idempotent
--      upsert keyed on template_key).
--
-- Idempotent: safe to re-run. Every statement is re-appliable.
--
-- ROLLBACK (reverses this file exactly; the table is shared infrastructure
-- owned by mod-custom-classes and is intentionally NOT dropped):
--   DELETE FROM `custom_class_template` WHERE `template_key` = 'warden';
--
-- Documented assumptions (no Warden content spec existed at authoring time):
--   * backing_class = 2 (Paladin): the only vanilla class with one talent
--     tree per LFG role; Warden = tank/healer/dps reference class (PRO-8).
--   * health_curve = vanilla paladin total base HP + 8% (tank-leaning
--     deviation so QA can verify the module curve path in-game).
--   * spellpower_curve = non-zero design choice (vanilla paladin base spell
--     power is 0) to give the Holy/healer spec a healing identity.
--   * weapon/armor prof masks = 0: Warden characters are paladin characters
--     and inherit the vanilla paladin proficiencies; no extra profs granted.
--   * No new playercreateinfo rows: Warden characters are created as
--     paladins (class-level resolution, contract section 1); the starting
--     kit is granted by the module on first login.
--
-- Spell ids verified against 3.3.5a Spell.dbc (official client data);
-- 635/465/20154/20271 additionally live-verified in mod-unbound SQL.
-- 10321 (Judgement) deliberately excluded: trainer TEACH id whose grant path
-- silently fails (mod-unbound 14_judgement_fix.sql).
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
    `description` TEXT COMMENT 'Free-form designer notes.',
    PRIMARY KEY (`template_key`),
    UNIQUE KEY `uq_custom_class_template_backing` (`backing_class`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

DELETE FROM `custom_class_template` WHERE `template_key` = 'warden';
INSERT INTO `custom_class_template` (`template_key`, `display_name`, `backing_class`, `color`, `icon`, `primary_stat`, `health_curve`, `mana_curve`, `ap_curve`, `spellpower_curve`, `crit_curve`, `specs`, `starting_kit`, `pet_family`, `weapon_prof_mask`, `armor_prof_mask`, `unbound_mask_bit`, `enabled`, `description`) VALUES
('warden', 'Warden', 2, '8C7AE6', 'ability_paladin_sealofprotection', 0, '{"1": 74, "10": 238, "20": 455, "30": 773, "40": 1199, "50": 1735, "60": 2377, "70": 4749, "80": 8839}', '{"1": 80, "10": 287, "20": 612, "30": 1022, "40": 1442, "50": 1862, "60": 2282, "70": 3918, "80": 5574}', '{"melee": {"1": 27, "10": 72, "20": 124, "30": 176, "40": 240, "50": 294, "60": 370, "70": 442, "80": 522}}', '{"1": 6, "10": 18, "20": 36, "30": 54, "40": 72, "50": 96, "60": 120, "70": 150, "80": 180}', NULL, '[{"talent_tree": 381, "role": "damage", "spell_whitelist": [1401, 1402, 1403, 1407, 1410, 1411, 1441, 1464, 1481, 1631, 1632, 1633, 1634, 1755, 1756, 1757, 1758, 1759, 1761, 1823, 2147, 2148, 2149, 2150, 2176, 2179]}, {"talent_tree": 382, "role": "healer", "spell_whitelist": [1432, 1433, 1435, 1443, 1444, 1446, 1449, 1450, 1461, 1463, 1465, 1502, 1627, 1628, 1742, 1743, 1744, 1745, 1746, 1747, 2190, 2191, 2192, 2193, 2198, 2199]}, {"talent_tree": 383, "role": "tank", "spell_whitelist": [1421, 1422, 1423, 1425, 1426, 1429, 1430, 1431, 1442, 1501, 1521, 1629, 1748, 1750, 1751, 1753, 1754, 2185, 2194, 2195, 2196, 2200, 2204, 2280, 2281, 2282]}]', '{"spells": [1515, 642, 633, 4987, 635, 19740, 853, 20154, 20271, 465], "items": [], "action_bar": []}', 4, 0, 0, 2048, 1, 'Warden: first custom class (PRO-10 reference instance of the mod-custom-classes template). Backed by client ClassID 2 (Paladin) via the Wrath Unbound unbound-class path. No Warden content spec existed at authoring time; backing-class choice, health +8% deviation and the non-zero spellpower curve are documented design assumptions for QA to confirm in-game (PRO-14). Prof masks 0: Warden characters are paladin characters and inherit the vanilla paladin proficiencies; the module ORs any future delta bits idempotently. Bound spell ids (3.3.5a Spell.dbc): 1515 Tame Beast, 642 Divine Shield, 633 Lay on Hands, 4987 Cleanse, 635 Holy Light, 19740 Blessing of Might, 853 Hammer of Justice, 20154 Seal of Righteousness, 20271 Judgement of Light, 465 Devotion Aura. Talent whitelists = full vanilla Talent.dbc id lists of TalentTab 381 (Retribution), 382 (Holy), 383 (Protection). Mask bit 2048 (1<<11) is the first allocation in the 2048..2147483648 ledger per docs/custom-class-template.md section 6.');
