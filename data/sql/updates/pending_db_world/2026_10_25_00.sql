-- ============================================================================
-- 2026_10_25_00 -> T4.6 Ranger Vex (900200) creature_template_model display row
-- (PRO-29). Authority: PRO-5 coordinator comment 6a1c7e75 on PRO-29
-- (2026-10-25); CreatureDisplayID 19097 pinned by PRO-5 spec v1 section 3
-- (Ranger display id shared by 900001/900100/900200).
--
-- Content of this file (additive only - no upstream row is modified,
-- playbook section 2.6; company reserved range 900000+ per section 2.4):
--   1. creature_template_model row for 900200 "Ranger Vex":
--      CreatureID 900200, Idx 0, CreatureDisplayID 19097, DisplayScale 1.00,
--      Probability 1, VerifiedBuild 12340. Mirrors the 900001 (The Mentor)
--      row shape; VerifiedBuild 12340 matches the fork's pinned client build
--      used throughout the T4 content.
--      Gap this closes: creature_template entry 900200 exists (created by
--      2026_10_19_00.sql / PRO-22, npcflag corrected by the PRO-28 fix),
--      but had no creature_template_model row, so the worldserver's model
--      load for the NPC fails and it cannot spawn - blocking PRO-26
--      in-game QA of the T4 Murloc Tide chain.
--
-- Target database: acore_world
--
-- Idempotent: targeted DELETE + single INSERT on the table's natural key
-- (CreatureID, Idx); re-run is a no-op; no ALTER statements.
--
-- Live DB state at authoring time (verified 2026-10-25):
--   creature_template_model held exactly one row (900001); 900200 and
--   900100 had no rows. This file adds only the 900200 row.
--
-- ROLLBACK (reverses this file exactly):
--   DELETE FROM `creature_template_model` WHERE `CreatureID` = 900200;
-- ============================================================================

DELETE FROM `creature_template_model` WHERE `CreatureID` = 900200;
INSERT INTO `creature_template_model`
(`CreatureID`, `Idx`, `CreatureDisplayID`, `DisplayScale`, `Probability`, `VerifiedBuild`)
VALUES
(900200, 0, 19097, 1.00, 1, 12340);
