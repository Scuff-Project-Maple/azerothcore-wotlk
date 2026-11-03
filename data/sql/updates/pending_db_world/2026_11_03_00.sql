-- ============================================================================
-- 2026_11_03_00 -> T4 "The Mentor" (900001) persistent creature spawn row
-- (PRO-114 item 3). Authority: PRO-5 spec v3 revision a627e583 section 3
-- (900001 "The Mentor", Unbound Class Trainer) and the docs/dev-environment.md
-- go-live gate that runs `.npc load` on this spawn.
--
-- Why this row needs a committed source (the PRO-114 commit-or-retire
-- decision - DECISION: COMMIT):
--   creature_template 900001 and its creature_template_model row come from the
--   upstream Wrath Unbound file wrath-unbound/modules/mod-unbound/npc_setup.sql
--   (tracked), which ships NO creature spawn row: line 13 tells the installer
--   to create one in-game with `.npc add 900001`. That command is
--   Console::No (in-game GM3 character only, see docs/dev-environment.md), it
--   spawns at the GM's position, and it assigns an auto-increment guid, so the
--   spawn was neither reproducible from the tree nor stable across rebuilds.
--   The live acore_world had exactly one creature row for id 900001 - guid
--   9002020 - with no tracked source anywhere in the repo. Committing the row
--   here makes the Mentor reproducible and pins its guid. Retiring it instead
--   would strand the `.npc load` go-live gate and the Unbound class-trainer
--   entry point (PRO-5 flow), and the row is not a duplicate: COUNT(*) for
--   id = 900001 is 1, so there is nothing to de-duplicate.
--
-- Content of this file (additive only - no upstream row is modified,
-- playbook section 2.6; company reserved range 900000+ per section 2.4):
--   1. creature spawn row with fixed guid 9002020: id 900001 (The Mentor),
--      map 0 (Eastern Kingdoms), position (-8830, 614, 93.5), orientation 0,
--      spawnMask 1, phaseMask 1, equipment_id 0, spawntimesecs 300,
--      wander_distance 0, currentwaypoint 0, curhealth 5342, curmana 0,
--      MovementType 0, npcflag 0, unit_flags 0, dynamicflags 0, ScriptName '',
--      CreateObject 0. Values are field-for-field the live row read read-only
--      on 2026-11-03, including zoneId/areaId 0 and the NULL VerifiedBuild and
--      Comment, so a tree rebuild reproduces the row the server runs today.
--      curhealth is a server-maintained column (the creature save writes it
--      back); it is committed verbatim for tree/live parity and is recomputed
--      from creature_template on respawn. The spawn row's npcflag stays 0 -
--      the Mentor's npcflag lives on creature_template (1, set by the upstream
--      npc_setup.sql) and is not overridden here.
--
-- HARD-CODED GUID (flagged per playbook section 2.4): creature.guid 9002020.
--   Guid allocation record for the custom band in this fork:
--     9002001 -> Ranger Vex (creature_template 900200) spawn, committed by
--                2026_10_19_00.sql (PRO-22).
--     9002020 -> The Mentor (creature_template 900001) spawn, this file.
--   creature.guid is a separate id space from creature_template.entry, so the
--   playbook section 2.4 "900000+" entry reservation must NOT be copied into
--   creature.guid literally: stock creature rows reach guid 5300678 and
--   already occupy six-digit values in that band (verified on the live
--   acore_world 2026-11-03: stock rows exist at guid 945802 and 945803). The
--   collision-safe custom spawn-guid band in this fork is guid >= 9000000 -
--   the 900<entry><seq> shape is safe exactly because it yields a seven-digit
--   value. Verified on 2026-11-03: no creature row has guid >= 9000000 except
--   9002001 and 9002020, and the highest stock guid is 5300678 (the 5300679
--   AUTO_INCREMENT in data/sql/base/db_world/creature.sql). The value
--   intentionally does not follow the 900<entry><seq> shape used by 9002001
--   because it is already referenced by the live database and by
--   docs/dev-environment.md - the tree must agree with what is running. Any
--   renumber must be a single paired migration (DELETE + INSERT of the new
--   guid) taken under the single-writer lease so tree and live never diverge.
--
-- Target database: acore_world
--
-- Idempotent: INSERT IGNORE on the primary key (guid); re-run is a no-op; no
-- ALTER statements (playbook section 2.2). Apply order is not sensitive: the
-- row only references creature_template 900001, which the Wrath Unbound setup
-- file installs per docs/dev-environment.md; a spawn row whose template is
-- absent is skipped by the worldserver at load and is not an error.
--
-- Live DB state at authoring time (verified read-only against acore_world on
-- 2026-11-03):
--   SELECT COUNT(*) FROM creature WHERE id = 900001;     -> 1  (no duplicate)
--   SELECT COUNT(*) FROM creature WHERE guid = 9002020;  -> 1
--   SELECT COUNT(*) FROM creature WHERE guid = 5300679;  -> 0
--   docs/dev-environment.md claimed the Mentor spawn was guid 5300679; that
--   value is only the AUTO_INCREMENT counter of
--   data/sql/base/db_world/creature.sql and no creature row has it. Corrected
--   in docs/dev-environment.md by this commit.
--
-- ROLLBACK (reverses this file exactly):
--   DELETE FROM `creature` WHERE `guid` = 9002020;
--   (this removes the only The Mentor spawn - run it only when re-creating the
--   row with a different guid, otherwise the Mentor is absent from the world)
-- ============================================================================

INSERT IGNORE INTO `creature`
(`guid`, `id`, `map`, `zoneId`, `areaId`, `spawnMask`, `phaseMask`, `equipment_id`,
 `position_x`, `position_y`, `position_z`, `orientation`, `spawntimesecs`, `wander_distance`,
 `currentwaypoint`, `curhealth`, `curmana`, `MovementType`, `npcflag`, `unit_flags`, `dynamicflags`,
 `ScriptName`, `VerifiedBuild`, `CreateObject`, `Comment`)
VALUES
(9002020, 900001, 0, 0, 0, 1, 1, 0,
 -8830, 614, 93.5, 0, 300, 0,
 0, 5342, 0, 0, 0, 0, 0,
 '', NULL, 0, NULL);
