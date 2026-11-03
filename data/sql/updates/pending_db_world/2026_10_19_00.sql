-- ============================================================================
-- 2026_10_19_00 -> T4.1 "The Murloc Tide" rehearsal quest chain content
-- (PRO-22). Authoritative spec: PRO-5 #document-spec v1 2026-10-19,
-- sections 2 (flow), 3 (content inventory, pinned ids) and 9 (schema facts
-- pinned for this fork).
--
-- Content of this file (additive only - no upstream row is modified,
-- playbook section 2.6; company reserved range 900000+ per section 2.4):
--   1. creature_template entry 900200 "Ranger Vex": Human (type 7) level
--      25/25 NPC, faction 35 (neutral), unit_class 1, npcflag 1. Row shape
--      modeled on 900001 (The Mentor); all fields copied from 900001 except
--      entry, name, and minlevel/maxlevel (80 -> 25) per spec section 3.
--      Documented deviation: subname set to '' (the Mentor's subname "Unbound
--      Class Trainer" is class-trainer specific; the spec names only
--      "Ranger Vex").
--   2. creature spawn row with fixed guid 9002001: map 0, zoneId 1519,
--      areaId 1519 (Stormwind, same as the Mentor's row), position
--      (-8845.0, 585.0, 94.0), orientation 0, spawnMask 1, phaseMask 1,
--      equipment_id 0, spawntimesecs 60 (death-respawn), wander_distance 0
--      (same as the 900001 spawn row). The z value is a known estimate
--      (spec assumption A4; QA verifies walkability in-game).
--   3. creature_queststarter rows: (900200, 900101) and (900200, 900102).
--   4. creature_questender rows: (900200, 900101) and (900200, 900102).
--   5. quest_template 900101 "The Murloc Tide": kill objective 5 x 1024
--      (Bluegill Murloc, RequiredNpcOrGo1/Count1), MinLevel 18, QuestLevel
--      21, RewardMoney 15000, one-shot (Flags 8 - the vanilla one-shot value
--      used by the 279 model row; this trimmed world has no repeatable-quest
--      infrastructure, daily_quest_vendor/banners do not exist), POI
--      continent 0 at (-3253, -1028) (the murloc beach). Row shape modeled
--      on vanilla quest 279 ("Claws from the Deep", kill 12 x 1024).
--   6. quest_template 900102 "Fins for the Ranger": collect objective
--      3 x 1468 (Murloc Fin, RequiredItemId1/Count1), MinLevel 18,
--      QuestLevel 21, RewardMoney 30000, RewardItem1 2046 (Bluegill Kukri)
--      x 1, one-shot, POI continent 0 at Vex (-8845, 585). QuestType 2 like
--      the 279 model row: this trimmed world carries no QuestType=4 rows
--      and the server loads but does not branch on the column (ObjectMgr
--      load query only), so the fork convention (2 for objective-bearing
--      quests, including collect objectives) is followed.
--   7. quest_template_addon row for 900102 with PrevQuestID 900101, so Q2
--      auto-starts on Q1 turn-in (vanilla chain precedent in DB: ID 5 <- 163).
--
-- Target database: acore_world
--
-- Idempotent: INSERT IGNORE on every table's natural key throughout; re-run
-- is a no-op; no ALTER statements (playbook section 2.2).
--
-- Id allocation record (playbook section 2.4):
--   quest_template.ID        900101  "The Murloc Tide"     (Q1)
--   quest_template.ID        900102  "Fins for the Ranger" (Q2)
--   creature_template.entry  900200  "Ranger Vex"
--   creature.guid            9002001 (hard-coded fixed spawn guid - flagged
--                                on PRO-22; reserved for this chain)
--
-- Existing references used - verified present in live acore_world at
-- authoring time (2026-10-19); none of these rows are modified:
--   creature 1024 (Bluegill Murloc, lv 20-21, 25 spawns on map 0)
--   item 1468 (Murloc Fin, quality 1 ilvl 16; drops 35 %/kill from
--   1024-1029 via existing creature_loot_template rows)
--   item 2046 (Bluegill Kukri, quality 2 ilvl 24)
--   creature 900001 "The Mentor" + its spawn row (row-shape precedent).
--   Comment-only correction 2026-11-03 (PRO-114 / PRO-171): this line
--   originally cited "spawn guid 5300679". 5300679 is not a spawn guid - it
--   is only the AUTO_INCREMENT value in data/sql/base/db_world/creature.sql,
--   and no creature row with that guid exists (measured read-only on the live
--   acore_world 2026-11-03). The Mentor's spawn row is guid 9002020 and is
--   owned by data/sql/updates/pending_db_world/2026_11_03_00.sql. No DML in
--   this file was changed.
--   quest 279 (row-shape precedent only)
--
-- ROLLBACK:
--   DELETE FROM `creature_template` WHERE `entry` = 900200;
--   DELETE FROM `creature` WHERE `guid` = 9002001;
--   DELETE FROM `creature_queststarter` WHERE `id` = 900200 AND `quest` IN (900101, 900102);
--   DELETE FROM `creature_questender` WHERE `id` = 900200 AND `quest` IN (900101, 900102);
--   DELETE FROM `quest_template` WHERE `ID` IN (900101, 900102);
--   DELETE FROM `quest_template_addon` WHERE `ID` = 900102;
-- ============================================================================

INSERT IGNORE INTO `creature_template`
(`entry`, `difficulty_entry_1`, `difficulty_entry_2`, `difficulty_entry_3`, `KillCredit1`, `KillCredit2`,
 `name`, `subname`, `IconName`, `gossip_menu_id`, `minlevel`, `maxlevel`, `exp`, `faction`, `npcflag`,
 `speed_walk`, `speed_run`, `speed_swim`, `speed_flight`, `detection_range`, `rank`, `dmgschool`, `DamageModifier`,
 `BaseAttackTime`, `RangeAttackTime`, `BaseVariance`, `RangeVariance`, `unit_class`, `unit_flags`, `unit_flags2`,
 `dynamicflags`, `family`, `type`, `type_flags`, `lootid`, `pickpocketloot`, `skinloot`, `PetSpellDataId`,
 `VehicleId`, `mingold`, `maxgold`, `AIName`, `MovementType`, `HoverHeight`, `HealthModifier`, `ManaModifier`,
 `ArmorModifier`, `ExperienceModifier`, `RacialLeader`, `movementId`, `RegenHealth`, `CreatureImmunitiesId`,
 `flags_extra`, `ScriptName`, `VerifiedBuild`)
VALUES
(900200, 0, 0, 0, 0, 0,
 'Ranger Vex', '', NULL, 0, 25, 25, 0, 35, 1,
 1, 1.14286, 1, 1, 18, 0, 0, 1,
 1500, 2000, 1, 1, 1, 768, 2048,
 0, 0, 7, 0, 0, 0, 0, 0,
 0, 0, 0, '', 0, 1, 1, 1,
 1, 1, 0, 0, 1, 0,
 2, '', 12340);

INSERT IGNORE INTO `creature`
(`guid`, `id`, `map`, `zoneId`, `areaId`, `spawnMask`, `phaseMask`, `equipment_id`,
 `position_x`, `position_y`, `position_z`, `orientation`, `spawntimesecs`, `wander_distance`,
 `currentwaypoint`, `curhealth`, `curmana`, `MovementType`, `npcflag`, `unit_flags`, `dynamicflags`,
 `ScriptName`, `VerifiedBuild`, `CreateObject`, `Comment`)
VALUES
(9002001, 900200, 0, 1519, 1519, 1, 1, 0,
 -8845.0, 585.0, 94.0, 0, 60, 0,
 0, 0, 0, 0, 0, 0, 0,
 '', 0, 0, 'Ranger Vex - T4 rehearsal chain (SW docks, PRO-22)');

INSERT IGNORE INTO `creature_queststarter` (`id`, `quest`) VALUES
(900200, 900101),
(900200, 900102);

INSERT IGNORE INTO `creature_questender` (`id`, `quest`) VALUES
(900200, 900101),
(900200, 900102);

INSERT IGNORE INTO `quest_template`
(`ID`, `QuestType`, `QuestLevel`, `MinLevel`, `QuestSortID`, `RewardXPDifficulty`, `RewardMoney`,
 `Flags`, `POIContinent`, `POIx`, `POIy`, `POIPriority`, `AllowableRaces`,
 `LogTitle`, `LogDescription`, `QuestDescription`, `AreaDescription`, `QuestCompletionLog`,
 `RequiredNpcOrGo1`, `RequiredNpcOrGoCount1`,
 `ObjectiveText1`, `VerifiedBuild`)
VALUES
(900101, 2, 21, 18, 11, 5, 15000,
 8, 0, -3253, -1028, 0, 1101,
 'The Murloc Tide',
 'Cull 5 Bluegill Murlocs.',
 'The bluegill tide has been chewing through the dock nets again. Cull five of them for me; they are working up the Wetlands coast, south-west of Stormwind.',
 '',
 'Return to Ranger Vex at the SW docks of Stormwind.',
 1024, 5,
 '', 12340);

INSERT IGNORE INTO `quest_template`
(`ID`, `QuestType`, `QuestLevel`, `MinLevel`, `QuestSortID`, `RewardXPDifficulty`, `RewardMoney`,
 `Flags`, `RewardItem1`, `RewardAmount1`,
 `POIContinent`, `POIx`, `POIy`, `POIPriority`, `AllowableRaces`,
 `LogTitle`, `LogDescription`, `QuestDescription`, `AreaDescription`, `QuestCompletionLog`,
 `RequiredItemId1`, `RequiredItemCount1`,
 `ObjectiveText1`, `VerifiedBuild`)
VALUES
(900102, 2, 21, 18, 11, 5, 30000,
 8, 2046, 1,
 0, -8845, 585, 0, 1101,
 'Fins for the Ranger',
 'Collect 3 Murloc Fins.',
 'The nets will hold better with proper reinforcement. Bring me three murloc fins from the bluegills on the Wetlands coast and I will share a blade from the last shipment.',
 '',
 'Return to Ranger Vex at the SW docks of Stormwind.',
 1468, 3,
 '', 12340);

INSERT IGNORE INTO `quest_template_addon`
(`ID`, `MaxLevel`, `AllowableClasses`, `SourceSpellID`, `PrevQuestID`, `NextQuestID`, `ExclusiveGroup`,
 `BreadcrumbForQuestId`, `RewardMailTemplateID`, `RewardMailDelay`, `RequiredSkillID`, `RequiredSkillPoints`,
 `RequiredMinRepFaction`, `RequiredMaxRepFaction`, `RequiredMinRepValue`, `RequiredMaxRepValue`,
 `ProvidedItemCount`, `SpecialFlags`)
VALUES
(900102, 0, 0, 0, 900101, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0);
