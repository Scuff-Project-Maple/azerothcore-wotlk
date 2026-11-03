-- ============================================================================
-- 2026_11_03_01 -> SELECT-only verification for the T4.1 custom world content
-- (PRO-114): the PRO-28 npcflag correction (2026_10_19_01.sql) and The Mentor
-- spawn row (2026_11_03_00.sql), plus the reference-integrity checks the
-- PRO-108 apply/verify pass needs. Playbook section 2.1: validation scripts
-- travel as their own sibling SELECT-only file - this file writes nothing.
--
-- Expected results are annotated per query. Queries marked MUST ARE load
-- bearing: a failure there means the T4.1 chain is not runnable in-game.
-- ============================================================================

-- 1) MUST PRO-28: Ranger Vex carries gossip + quest-giver.
-- Expect: one row, npcflag = 3, npcflag_ok = 1.
SELECT `entry`, `name`, `npcflag`, (`npcflag` = 3) AS `npcflag_ok`
FROM `creature_template`
WHERE `entry` = 900200;

-- 2) MUST PRO-114: exactly one The Mentor spawn row, pinned guid, and the
-- coordinates the docs gate uses. Expect: mentor_rows = 1, mentor_ok = 1.
SELECT
    COUNT(*) AS `mentor_rows`,
    (COUNT(*) = 1) AS `mentor_ok`
FROM `creature`
WHERE `guid` = 9002020
  AND `id` = 900001
  AND `map` = 0
  AND `position_x` = -8830
  AND `position_y` = 614
  AND `position_z` = 93.5
  AND `spawntimesecs` = 300;

-- 3) MUST no duplicate Mentor spawns: the Mentor must have exactly one spawn
-- row (an in-game `.npc add 900001` would add a second, unreferenced one).
-- Expect: 1.
SELECT COUNT(*) AS `mentor_spawn_rows_total`
FROM `creature`
WHERE `id` = 900001;

-- 4) MUST guid hygiene: the custom spawn-guid band in this fork is
-- guid >= 9000000, NOT 900000+. creature_template.entry is reserved at
-- 900000+ (playbook section 2.4), but creature.guid is a different id space
-- and stock creature rows reach guid 5300678 - including stock rows at
-- 945802/945803 - so a six-digit "900000+" spawn guid is NOT safe. The
-- 900<entry><seq> shape works precisely because it yields a seven-digit
-- value above every stock guid. Expect: stock_in_custom_band = 0,
-- unmapped_custom_guids = 0, max_stock_guid = 5300678.
SELECT
    (SELECT COUNT(*) FROM `creature` WHERE `guid` >= 9000000 AND `id` < 900000) AS `stock_in_custom_band`,
    (SELECT COUNT(*) FROM `creature`
       WHERE `guid` IN (9002001, 9002020)
         AND ((`guid` = 9002001 AND `id` <> 900200) OR (`guid` = 9002020 AND `id` <> 900001))) AS `unmapped_custom_guids`,
    (SELECT MAX(`guid`) FROM `creature` WHERE `guid` < 9000000) AS `max_stock_guid`;

-- 5) MUST spawn resolvable: every custom spawn needs both a template row and
-- a creature_template_model row, or the worldserver skips the spawn silently
-- (the PRO-29 failure mode). Expect: 2 rows, template_ok = 1, model_ok = 1.
SELECT
    `c`.`guid`,
    `c`.`id`,
    (`t`.`entry` IS NOT NULL) AS `template_ok`,
    (`m`.`CreatureID` IS NOT NULL) AS `model_ok`
FROM `creature` AS `c`
LEFT JOIN `creature_template` AS `t` ON `t`.`entry` = `c`.`id`
LEFT JOIN `creature_template_model` AS `m` ON `m`.`CreatureID` = `c`.`id`
WHERE `c`.`guid` IN (9002001, 9002020);

-- 6) MUST chain rows: both quests exist, both are startable and endable by
-- Vex, and Q2 is chained behind Q1.
-- Expect: quest_rows = 2, starters = 2, enders = 2, chain_link_ok = 1.
SELECT
    (SELECT COUNT(*) FROM `quest_template` WHERE `ID` IN (900101, 900102)) AS `quest_rows`,
    (SELECT COUNT(*) FROM `creature_queststarter` WHERE `id` = 900200 AND `quest` IN (900101, 900102)) AS `starters`,
    (SELECT COUNT(*) FROM `creature_questender` WHERE `id` = 900200 AND `quest` IN (900101, 900102)) AS `enders`,
    (SELECT COUNT(*) FROM `quest_template_addon` WHERE `ID` = 900102 AND `PrevQuestID` = 900101) AS `chain_link_ok`;

-- 7) MUST objective references exist (playbook section 2.5). A kill objective
-- pointing at a missing creature entry or a collect objective at a missing
-- item never errors at load; it just cannot be completed.
-- Expect: kill_target_ok = 1, collect_item_ok = 1, reward_item_ok = 1.
SELECT
    (SELECT COUNT(*) FROM `creature_template` WHERE `entry` = 1024) AS `kill_target_ok`,
    (SELECT COUNT(*) FROM `item_template` WHERE `entry` = 1468) AS `collect_item_ok`,
    (SELECT COUNT(*) FROM `item_template` WHERE `entry` = 2046) AS `reward_item_ok`;

-- 8) MUST spec rulings hold (PRO-5 spec v3 revision a627e583 section 10,
-- restated on PRO-114): Ranger Vex owes no loot and the numeric 900100 stays
-- reserved to the item, not a creature.
-- Expect: vex_loot_rows = 0, vex_lootid = 0, ref_loot_rows = 0,
--         phantom_creature_900100 = 0, phantom_item_135644 = 0.
SELECT
    (SELECT COUNT(*) FROM `creature_loot_template` WHERE `entry` = 900200) AS `vex_loot_rows`,
    (SELECT `lootid` FROM `creature_template` WHERE `entry` = 900200) AS `vex_lootid`,
    (SELECT COUNT(*) FROM `reference_loot_template` WHERE `entry` = 900200) AS `ref_loot_rows`,
    (SELECT COUNT(*) FROM `creature_template` WHERE `entry` = 900100) AS `phantom_creature_900100`,
    (SELECT COUNT(*) FROM `item_template` WHERE `entry` = 135644) AS `phantom_item_135644`;

-- 9) MUST migration registration: all four T4 files are recorded by the world
-- DB updater (state PENDING is expected for pending_db_world content).
-- Expect: 4 rows, all state = 'PENDING'. Hand-inserted updates rows are not
-- acceptable evidence - see PRO-114 item 4 / PRO-65.
SELECT `name`, `state`, `timestamp`, `speed`
FROM `updates`
WHERE `name` IN ('2026_10_19_00.sql', '2026_10_19_01.sql', '2026_10_25_00.sql', '2026_11_03_00.sql')
ORDER BY `name`;

-- 10) Informational: the Mentor template itself is upstream-owned
-- (wrath-unbound/modules/mod-unbound/npc_setup.sql) and must stay untouched
-- by this fork's migrations. Expect: one row, npcflag = 1.
SELECT `entry`, `name`, `subname`, `npcflag`, `faction`, `minlevel`, `maxlevel`
FROM `creature_template`
WHERE `entry` = 900001;
