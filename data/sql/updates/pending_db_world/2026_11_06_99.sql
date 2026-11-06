-- PRO-285 self-test. DO NOT MERGE.
-- This is a deliberate company-provenance policy violation, committed on a throwaway
-- branch, to prove that .github/workflows/worlddb-preflight.yml turns the corpus-sweep
-- check RED and names the rule. The violation: a creature spawn below the reserved
-- band (creature.guid must be >= 9000000), which is a boot refusal today.

DELETE FROM `creature` WHERE `guid` = 1234;

INSERT INTO `creature` (`guid`, `id1`, `map`, `position_x`, `position_y`, `position_z`,
                        `orientation`, `spawntimesecs`)
VALUES (1234, 900200, 1, 5000.0, 5000.0, 10.0, 0.0, 300);
