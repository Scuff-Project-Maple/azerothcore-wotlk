-- ============================================================================
-- 2026_10_05_01 -> 2026_10_05_01
-- Warden custom class seed validation (PRO-10 item 6): SELECT-only check
-- script. Expected results are annotated per query; all must hold for the
-- Warden seed to be considered valid.
--
-- Note: spell-id existence is NOT checkable in SQL in WotLK (spell data is
-- DBC-only, there is no world-DB spell table). The 10 bound kit spell ids
-- were verified against 3.3.5a Spell.dbc at authoring time (see
-- 2026_10_05_00.sql header and the PRO-10 comment for the bound-id list).
-- ============================================================================

-- 1) Warden row exists and is enabled. Expect: 1 row (warden, 2, 1).
SELECT `template_key`, `backing_class`, `enabled`
FROM `custom_class_template`
WHERE `template_key` = 'warden' AND `enabled` = 1;

-- 2) Exactly 3 specs, each with a talent tree, a role and a NON-EMPTY spell
-- whitelist. Expect: spec_count = 3, role_count = 3, empty_whitelists = 0.
SELECT
    (LENGTH(`specs`) - LENGTH(REPLACE(`specs`, '"talent_tree"', ''))) DIV LENGTH('"talent_tree"') AS `spec_count`,
    (LENGTH(`specs`) - LENGTH(REPLACE(`specs`, '"role"', ''))) DIV LENGTH('"role"') AS `role_count`,
    (LENGTH(`specs`) - LENGTH(REPLACE(`specs`, '"spell_whitelist": []', ''))) DIV LENGTH('"spell_whitelist": []') AS `empty_whitelists`
FROM `custom_class_template`
WHERE `template_key` = 'warden';

-- 3) Starting kit: non-empty spell list and all 10 bound spell ids present.
-- Expect: kit_ok = 1.
SELECT
    (`starting_kit` LIKE '%"spells": [%')
    AND (`starting_kit` NOT LIKE '%"spells": []%')
    AND (`starting_kit` LIKE '%1515%' AND `starting_kit` LIKE '%642%' AND `starting_kit` LIKE '%633%')
    AND (`starting_kit` LIKE '%4987%' AND `starting_kit` LIKE '%635%' AND `starting_kit` LIKE '%19740%')
    AND (`starting_kit` LIKE '%853%' AND `starting_kit` LIKE '%20154%' AND `starting_kit` LIKE '%20271%')
    AND (`starting_kit` LIKE '%465%') AS `kit_ok`
FROM `custom_class_template`
WHERE `template_key` = 'warden';

-- 4) Mask allocation policy: every row's unbound_mask_bit must be a single
-- bit inside the free range 2048..2147483648 (1<<11..1<<31). Expect: 0 rows.
SELECT `template_key`, `unbound_mask_bit`
FROM `custom_class_template`
WHERE `unbound_mask_bit` = 0
   OR `unbound_mask_bit` < 2048
   OR `unbound_mask_bit` > 2147483648
   OR `unbound_mask_bit` & (`unbound_mask_bit` - 1) <> 0;

-- 5) Mask bit collisions: no two templates may share a mask bit.
-- Expect: 0 rows.
SELECT
    `unbound_mask_bit`,
    COUNT(*) AS `n`
FROM `custom_class_template`
GROUP BY `unbound_mask_bit`
HAVING COUNT(*) > 1;

-- 6) Backing-class collisions: the unique key enforces this, but report it
-- anyway. Expect: 0 rows.
SELECT
    `backing_class`,
    COUNT(*) AS `n`
FROM `custom_class_template`
GROUP BY `backing_class`
HAVING COUNT(*) > 1;

-- 7) Warden stat curves are populated (health/mana/spellpower set, ap has a
-- melee component). Expect: curves_ok = 1.
SELECT
    (`health_curve` IS NOT NULL AND `health_curve` <> '')
    AND (`mana_curve` IS NOT NULL AND `mana_curve` <> '')
    AND (`spellpower_curve` IS NOT NULL AND `spellpower_curve` <> '')
    AND (`ap_curve` IS NOT NULL AND `ap_curve` LIKE '%melee%') AS `curves_ok`
FROM `custom_class_template`
WHERE `template_key` = 'warden';

-- 8) Backing class (2 = Paladin) has player-creation rows.
-- Expect: paladin_createinfo_rows > 0.
SELECT
    COUNT(*) AS `paladin_createinfo_rows`
FROM `playercreateinfo`
WHERE `class` = 2;
