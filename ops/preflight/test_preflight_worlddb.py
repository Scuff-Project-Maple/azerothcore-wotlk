#!/usr/bin/env python3
"""Policy-engine tests for the PRO-179 world-DB pre-flight gate.

These run without MySQL: they exercise the statement splitter, the comment
masker and the write-policy engine only.

    python3 ops/preflight/test_preflight_worlddb.py
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from argparse import Namespace
from pathlib import Path

from preflight_worlddb import (
    MySQL, Preflight, Report, mask_sql, parse_statement, split_statements,
    build_arg_parser,
)

HERE = Path(__file__).resolve().parent
POLICY = json.loads((HERE / "preflight-policy.json").read_text())


def make_preflight() -> Preflight:
    args = build_arg_parser().parse_args(["--compose-root", "/nonexistent"])
    args.live_db = POLICY["liveDatabase"]
    args.scratch_db = "acore_world_preflight_test"
    pf = Preflight(args, POLICY, Report())
    pf.r = Report()
    pf.violations = []
    return pf


def rules_for(sql: str) -> list[str]:
    pf = make_preflight()
    fake_file = {"rel": "fixture.sql", "path": Path("fixture.sql")}
    for text, offset in split_statements(sql):
        masked = mask_sql(text)
        if not masked.strip():
            continue
        pf._policy_statement(fake_file, parse_statement(text, masked, offset),
                             pf.line_of(sql, offset))
    return [f.rule for f in pf.r.findings if f.severity == "FAIL"]


def lines_for(sql: str) -> list[int]:
    pf = make_preflight()
    fake_file = {"rel": "fixture.sql", "path": Path("fixture.sql")}
    out = []
    for text, offset in split_statements(sql):
        masked = mask_sql(text)
        if not masked.strip():
            continue
        pf._policy_statement(fake_file, parse_statement(text, masked, offset),
                             pf.line_of(sql, offset))
    out = [f.line for f in pf.r.findings if f.severity == "FAIL"]
    return out


def findings_for(sql: str) -> list[tuple[str, str]]:
    """(rule, message) for every FAIL a statement earns — for assertions on wording."""
    pf = make_preflight()
    fake_file = {"rel": "fixture.sql", "path": Path("fixture.sql")}
    for text, offset in split_statements(sql):
        masked = mask_sql(text)
        if not masked.strip():
            continue
        pf._policy_statement(fake_file, parse_statement(text, masked, offset),
                             pf.line_of(sql, offset))
    return [(f.rule, f.message) for f in pf.r.findings if f.severity == "FAIL"]


class TestStatements(unittest.TestCase):
    def test_semicolon_in_string_does_not_split(self):
        sql = "INSERT INTO `custom_class_template` (`template_key`,`label`) VALUES ('a;b','c');\n" \
              "DELETE FROM `custom_class_template` WHERE `template_key` = 'a;b';\n"
        self.assertEqual(len(split_statements(sql)), 2)

    def test_rollback_comment_is_not_executed_sql(self):
        sql = ("-- ROLLBACK: ALTER TABLE creature_template DROP COLUMN x;\n"
               "-- DELETE FROM quest_template;\n"
               "UPDATE `creature_template` SET `npcflag` = 3 WHERE `entry` = 900200;\n")
        self.assertEqual(rules_for(sql), [])

    def test_block_comment_is_masked_but_version_comment_is_live(self):
        self.assertEqual(rules_for("/* ALTER TABLE x DROP y; */\n"), [])
        self.assertIn("forbidden-statement",
                      rules_for("/*!40000 ALTER TABLE `creature_template` ADD COLUMN x INT */;\n"))

    def test_delimiter_directive(self):
        sql = ("DELIMITER ;;\n"
               "INSERT INTO `creature_template` (`entry`) VALUES (900200);;\n"
               "DELIMITER ;;\n")
        self.assertEqual(rules_for(sql), [])


class TestForbidden(unittest.TestCase):
    def test_schema_change_verbs_are_rejected(self):
        for sql in ("ALTER TABLE `creature_template` ADD COLUMN x INT;",
                    "DROP TABLE `creature`;",
                    "TRUNCATE `quest_template`;",
                    "RENAME TABLE `creature` TO `creature_bak`;",
                    "CREATE VIEW v AS SELECT 1;",
                    "CREATE TRIGGER t BEFORE INSERT ON creature_template FOR EACH ROW SET @x=1;",
                    "GRANT ALL ON *.* TO 'x';"):
            self.assertIn("forbidden-statement", rules_for(sql), sql)

    def test_ledger_tables_are_writable_only_by_the_updater(self):
        self.assertIn("ledger-table",
                      rules_for("DELETE FROM `updates` WHERE `name` = 'x';"))
        self.assertIn("ledger-table",
                      rules_for("UPDATE `updates_include` SET `state` = 'MODULE';"))

    def test_set_global_is_rejected_but_session_tuning_is_not(self):
        self.assertIn("forbidden-statement", rules_for("SET GLOBAL read_only = 0;"))
        self.assertEqual(rules_for("SET NAMES utf8mb4;"), [])
        self.assertEqual(rules_for("SET @x = 1;"), [])


class TestBoundedness(unittest.TestCase):
    def test_unbounded_writes_are_rejected(self):
        self.assertIn("unbounded-write",
                      rules_for("UPDATE `creature_template` SET `npcflag` = 3;"))
        self.assertIn("unbounded-write", rules_for("DELETE FROM `quest_template`;"))
        self.assertIn("unbounded-write",
                      rules_for("UPDATE `creature_template` SET `npcflag` = 3 "
                                "WHERE `subname` = 'Guard';"))

    def test_range_predicate_on_key_is_not_a_bound(self):
        self.assertIn("unbounded-write",
                      rules_for("DELETE FROM `creature` WHERE `id` < 900000;"))

    def test_non_literal_key_predicate_fails_closed(self):
        self.assertIn("unbounded-write",
                      rules_for("UPDATE `creature_template` SET `npcflag` = 3 "
                                "WHERE `entry` = @other;"))

    def test_top_level_or_is_rejected_but_nested_or_is_fine(self):
        self.assertIn("or-in-where",
                      rules_for("UPDATE `creature_template` SET `npcflag` = 1 "
                                "WHERE `entry` = 900200 OR `entry` = 900201;"))
        self.assertEqual(rules_for(
            "UPDATE `creature_template` SET `npcflag` = 1 WHERE `entry` = 900200 "
            "AND (`Verification` = 1 OR `Verification` = 0);"), [])

    def test_key_column_cannot_be_reassigned(self):
        self.assertIn("key-column-assigned",
                      rules_for("UPDATE `creature_template` SET `entry` = 5 "
                                "WHERE `entry` = 900200;"))


class TestReservedBand(unittest.TestCase):
    def test_in_band_insert_is_accepted(self):
        self.assertEqual(rules_for(
            "INSERT INTO `creature_template` (`entry`,`npcflag`) VALUES (900200, 3);"), [])
        self.assertEqual(rules_for(
            "INSERT INTO `creature` (`guid`,`id`,`map`,`x`,`y`,`z`,`o`) "
            "VALUES (9002020, 900001, 0, 0, 0, 0, 0);"), [])

    def test_out_of_band_insert_is_rejected(self):
        self.assertIn("reserved-band", rules_for(
            "INSERT INTO `creature_template` (`entry`,`npcflag`) VALUES (12345, 3);"))
        self.assertIn("reserved-band", rules_for(
            "INSERT INTO `creature` (`guid`,`id1`,`map`,`x`,`y`,`z`,`o`) "
            "VALUES (1234, 900001, 0, 0, 0, 0, 0);"))
        self.assertIn("reserved-band", rules_for(
            "INSERT INTO `creature` (`guid`,`id1`,`map`,`x`,`y`,`z`,`o`) "
            "VALUES (9002020, 48101, 0, 0, 0, 0, 0);"))

    def test_second_row_out_of_band_is_rejected(self):
        self.assertIn("reserved-band", rules_for(
            "INSERT INTO `creature_template` (`entry`) VALUES (900200),(55);"))

    def test_band_column_must_be_named(self):
        self.assertIn("insert-columns-required",
                      rules_for("INSERT INTO `creature_template` VALUES (900200, 3);"))
        self.assertIn("band-column-missing",
                      rules_for("INSERT INTO `creature_template` (`npcflag`) VALUES (3);"))

    def test_out_of_band_predicate_is_rejected(self):
        self.assertIn("reserved-band", rules_for(
            "UPDATE `quest_template` SET `RewardMoney` = 1 WHERE `ID` = 8999;"))
        self.assertEqual(rules_for(
            "UPDATE `quest_template` SET `RewardMoney` = 1 "
            "WHERE `ID` IN (900101, 900102);"), [])

    def test_non_numeric_band_operand_fails_closed(self):
        self.assertIn("band-not-literal", rules_for(
            "UPDATE `quest_template` SET `RewardMoney` = 1 WHERE `ID` = 'abc';"))


class TestAllowedShapes(unittest.TestCase):
    def test_company_pending_corpus_statements_pass(self):
        corpus = [
            "INSERT IGNORE INTO `creature_queststarter` (`id`,`quest`) "
            "VALUES (900200,900101),(900200,900102);",
            "DELETE FROM `creature_template_model` WHERE `CreatureID` = 900200;",
            "DELETE FROM `custom_class_template` WHERE `template_key` = 'warden';",
            "CREATE TABLE IF NOT EXISTS `custom_class_template` "
            "(`template_key` varchar(64) NOT NULL, PRIMARY KEY (`template_key`));",
            "UPDATE `creature_template` SET `npcflag` = 3 WHERE `entry` = 900200;",
        ]
        for sql in corpus:
            self.assertEqual(rules_for(sql), [], sql)

    def test_unknown_table_fails_closed(self):
        # creature_equip_template became an approved table (PRO-179 review N3), so this
        # has to name a table that really does not exist
        self.assertIn("table-not-allowed",
                      rules_for("DELETE FROM `zz_no_such_content_table` WHERE `id` = 900200;"))

    def test_newly_allowlisted_tables_are_bounded_on_their_real_key(self):
        self.assertEqual(rules_for(
            "DELETE FROM `creature_equip_template` WHERE `CreatureID` = 900200 AND `ID` = 1;"), [])
        # partial key is no longer enough (N1)
        self.assertIn("unbounded-write",
                      rules_for("DELETE FROM `creature_equip_template` WHERE `CreatureID` = 900200;"))

    def test_create_of_non_custom_table_is_rejected(self):
        self.assertIn("custom-table-only",
                      rules_for("CREATE TABLE `widget` (`id` INT);"))

    def test_cross_schema_write_is_rejected(self):
        self.assertIn("cross-schema-write",
                      rules_for("DELETE FROM `acore_characters`.`character` "
                                "WHERE `guid` = 900200;"))

    def test_insert_select_and_odku_are_rejected(self):
        self.assertIn("insert-select", rules_for(
            "INSERT INTO `creature_template` (`entry`) SELECT `entry` FROM `creature_template`;"))
        self.assertIn("on-duplicate-key", rules_for(
            "INSERT INTO `creature_template` (`entry`) VALUES (900200) "
            "ON DUPLICATE KEY UPDATE `npcflag` = 3;"))

    def test_finding_carries_the_statement_line(self):
        sql = ("-- header\n"
               "UPDATE `creature_template` SET `npcflag` = 3 WHERE `entry` = 900200;\n"
               "DELETE FROM `quest_template`;\n")
        self.assertEqual(lines_for(sql), [3])


class TestPreamble(unittest.TestCase):
    def test_empty_file_has_no_findings(self):
        self.assertEqual(rules_for(""), [])
        self.assertEqual(rules_for("\n\n-- nothing here\n"), [])

    def test_read_only_statements_are_ignored(self):
        self.assertEqual(rules_for("SELECT * FROM creature_template WHERE entry = 1;"), [])
        self.assertEqual(rules_for("SET @x = (SELECT COUNT(*) FROM quest_template);"), [])



class TestProvenance(unittest.TestCase):
    """PRO-179 review B1: the write policy may never be skipped on a company file."""

    @staticmethod
    def pf_with_author(author):
        pf = make_preflight()
        pf.git_author_origin = lambda path: author
        # These tests are about the directory tag and the author; the tracked-ref
        # question (PRO-248) has its own class and needs no real git here.
        pf.upstream_ref_state = lambda path: (Preflight.REF_VERIFIED, "stubbed: in ref")
        return pf

    @staticmethod
    def file_in(db_path, origin):
        return {"rel": f"{db_path}/x.sql", "path": Path("x.sql"),
                "dir": {"db_path": db_path, "origin": origin}}

    def test_company_directory_wins_over_foreign_author(self):
        pf = self.pf_with_author(("upstream", "someone@else.example\tSomeone\tcontent"))
        origin, _ = pf.classify_origin(
            self.file_in("$/data/sql/updates/pending_db_world", "company"))
        self.assertEqual(origin, "company")

    def test_company_author_wins_over_upstream_directory(self):
        pf = self.pf_with_author(("company", "dev@paperclip.local\tDev\tcontent"))
        origin, _ = pf.classify_origin(self.file_in("$/data/sql/updates/db_world", "upstream"))
        self.assertEqual(origin, "company")

    def test_upstream_needs_both_tags(self):
        pf = self.pf_with_author(("upstream", "ac@azerothcore.org\tAC\tfix"))
        origin, _ = pf.classify_origin(self.file_in("$/data/sql/updates/db_world", "upstream"))
        self.assertEqual(origin, "upstream")

    def test_upstream_also_needs_the_tracked_ref(self):
        # PRO-248: a non-company author is a claim, not evidence. The third tag — the
        # file existing in the ref the tree tracks — is what makes it upstream.
        pf = self.pf_with_author(("upstream", "ac@azerothcore.org\tAC\tfix"))
        pf.upstream_ref_state = lambda path: (Preflight.REF_UNVERIFIED,
                                             "stubbed: absent from origin/Playerbot")
        origin, _ = pf.classify_origin(self.file_in("$/data/sql/updates/db_world", "upstream"))
        self.assertEqual(origin, "company")

    def test_untracked_never_gets_the_exemption(self):
        pf = self.pf_with_author(("unknown", "untracked"))
        for origin_tag in ("company", "upstream"):
            origin, _ = pf.classify_origin(self.file_in("$/data/sql/updates/db_world", origin_tag))
            self.assertEqual(origin, "unknown")

    def test_unknown_origin_is_not_policy_exempt(self):
        pf = make_preflight()
        f = self.file_in("$/data/sql/updates/pending_db_world", "company")
        f["origin"] = "unknown"
        f["provenance"] = "cannot attribute: untracked"
        f["path"] = Path(__file__).with_name("preflight_worlddb.py")  # any readable file
        pf.check_policy([f])
        self.assertFalse(any(x.rule == "policy-exempt-origin" for x in pf.r.findings))


class TestKeyBounding(unittest.TestCase):
    """PRO-179 review N1: a partial key must not bound a write."""

    def test_partial_key_rejected_on_custom_table(self):
        self.assertIn("unbounded-write", rules_for(
            "DELETE FROM `module_string` WHERE `module` = 'mod-x';"))

    def test_full_key_accepted_on_custom_table(self):
        self.assertEqual(rules_for(
            "DELETE FROM `module_string` WHERE `module` = 'mod-x' AND `id` = 900001;"), [])

    def test_alternative_key_set_accepted(self):
        self.assertEqual(rules_for(
            "DELETE FROM `creature` WHERE `id1` = 900200 AND `map` = 539;"), [])

    def test_alternative_key_set_still_band_checked(self):
        self.assertIn("reserved-band", rules_for(
            "DELETE FROM `creature` WHERE `id1` = 48101 AND `map` = 539;"))

    def test_single_column_of_composite_key_rejected(self):
        self.assertIn("unbounded-write", rules_for(
            "UPDATE `npc_trainer` SET `MoneyCost` = 100 WHERE `ID` = 900200;"))
        self.assertEqual(rules_for(
            "UPDATE `npc_trainer` SET `MoneyCost` = 100 WHERE `ID` = 900200 "
            "AND `SpellID` = 100;"), [])


class TestScratchName(unittest.TestCase):
    """PRO-179 review N5: the DROP DATABASE target cannot be operator-chosen."""

    def test_rejects_foreign_schema_names(self):
        for name in ("acore_world", "acore_characters", "mysql", "world",
                     "acore_world_preflight", "prefix_acore_world_preflight_1",
                     "acore_world_preflight/../x"):
            pf = make_preflight()
            pf.a.scratch_db = name
            with self.assertRaises(Exception, msg=name):
                pf.scratch_name()

    def test_accepts_generated_name(self):
        pf = make_preflight()
        pf.a.scratch_db = "acore_world_preflight_42_20261103T080000Z"
        self.assertEqual(pf.scratch_name(), "acore_world_preflight_42_20261103T080000Z")

    def test_no_scratch_prefix_flag(self):
        # the flag that let an operator choose the drop prefix must be gone
        opts = {tuple(a.option_strings) for a in build_arg_parser()._actions}
        self.assertNotIn(("--scratch-prefix",), opts)


class TestBootPath(unittest.TestCase):
    """Check 0b (review B2): the scanned tree must be the tree the boot applies."""

    TARGET = "/azerothcore/data/sql"
    SOURCE = "/nonexistent/data/sql"          # make_preflight --compose-root /nonexistent

    def _pf(self, services, docker_raises=False):
        import preflight_worlddb as P

        cfg = {"services": {"ac-worldserver": services}}

        class Proc:
            returncode = 0
            stdout = json.dumps(cfg).encode()
            stderr = b""

        def fake_run(argv, **kwargs):
            if docker_raises:
                raise FileNotFoundError("docker")
            fake_run.argv = argv
            return Proc()

        pf = make_preflight()
        pf._check_live_container = lambda *a, **k: None
        self._orig = P.subprocess.run
        P.subprocess.run = fake_run
        self._run = fake_run
        return pf

    def tearDown(self):
        import preflight_worlddb as P
        P.subprocess.run = self._orig

    def _fail_rules(self, pf):
        pf.check_boot_path()
        return [f.rule for f in pf.r.findings if f.severity == "FAIL"]

    def _mount(self, ro=True, source=None, target=TARGET, typ="bind"):
        return {"type": typ, "source": source if source is not None else self.SOURCE,
                "target": target, "read_only": ro}

    def test_clean_boot_path_passes(self):
        pf = self._pf({"volumes": [self._mount()],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"},
                       "restart": "unless-stopped"})
        self.assertEqual([], self._fail_rules(pf))

    def test_no_docker_fails_closed(self):
        pf = self._pf({"volumes": [self._mount()],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"}}, docker_raises=True)
        self.assertIn("boot-path-unreadable", self._fail_rules(pf))

    def test_none_compose_file_is_normalised(self):
        pf = self._pf({"volumes": [self._mount()],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"}})
        self.assertEqual([], pf.a.compose_file)
        self._fail_rules(pf)
        self.assertNotIn("-f", self._run.argv)
        self.assertNotIn(None, self._run.argv)

    def test_writable_sql_mount_is_rejected(self):
        pf = self._pf({"volumes": [self._mount(ro=False)],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"}})
        self.assertIn("sql-mount-writable", self._fail_rules(pf))

    def test_foreign_mount_source_is_rejected(self):
        pf = self._pf({"volumes": [self._mount(source="/srv/other/data/sql")],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"}})
        self.assertIn("sql-mount-source", self._fail_rules(pf))

    def test_missing_mount_is_rejected(self):
        pf = self._pf({"volumes": [], "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"}})
        self.assertIn("sql-mount-missing", self._fail_rules(pf))

    def test_named_volume_is_rejected(self):
        pf = self._pf({"volumes": [self._mount(typ="volume")],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"}})
        self.assertIn("sql-mount-type", self._fail_rules(pf))

    def test_widened_updater_flag_is_rejected(self):
        pf = self._pf({"volumes": [self._mount()],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "7"}})
        self.assertIn("updater-env-widened", self._fail_rules(pf))

    def test_world_updater_off_is_rejected(self):
        pf = self._pf({"volumes": [self._mount()],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "0"}})
        self.assertIn("updater-env-world-off", self._fail_rules(pf))

    def test_missing_updater_flag_is_rejected(self):
        pf = self._pf({"volumes": [self._mount()], "environment": {}})
        self.assertIn("updater-env-unknown", self._fail_rules(pf))

    def test_unknown_service_is_rejected(self):
        import preflight_worlddb as P

        class Proc:
            returncode = 0
            stdout = json.dumps({"services": {}}).encode()
            stderr = b""

        pf = make_preflight()
        self._orig = P.subprocess.run
        P.subprocess.run = lambda argv, **kw: Proc()
        self.assertIn("boot-path-service", self._fail_rules(pf))

    def test_short_syntax_ro_is_honoured(self):
        pf = self._pf({"volumes": [f"{self.SOURCE}:{self.TARGET}:ro"],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"}})
        self.assertEqual([], self._fail_rules(pf))

    def test_short_syntax_rw_is_rejected(self):
        pf = self._pf({"volumes": [f"{self.SOURCE}:{self.TARGET}:rw"],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"}})
        self.assertIn("sql-mount-writable", self._fail_rules(pf))
        # the summary line must not describe a writable mount as read-only
        # (host probe PRO-179: the rw override printed "(read-only bind)")
        from preflight_worlddb import render
        pf.check_boot_path()
        text = render(pf.r, pf.a)
        self.assertIn("WRITABLE bind", text)
        self.assertNotIn("(read-only bind)", text)

    def test_summary_line_reports_read_only_only_when_it_is(self):
        pf = self._pf({"volumes": [f"{self.SOURCE}:{self.TARGET}:ro"],
                       "environment": {"AC_UPDATES_ENABLE_DATABASES": "4"}})
        from preflight_worlddb import render
        pf.check_boot_path()
        text = render(pf.r, pf.a)
        self.assertIn("(read-only bind)", text)
        self.assertNotIn("WRITABLE bind", text)


class TestRenderCoversEveryCheck(unittest.TestCase):
    def test_boot_path_section_is_rendered(self):
        """A check that cannot render cannot report a failure to a human."""
        from preflight_worlddb import CHECK_TITLES, Report, render
        self.assertIn("0b", CHECK_TITLES)
        pf = make_preflight()
        pf.r.fail("0b", "the /azerothcore/data/sql mount is not read-only",
                  rule="sql-mount-writable")
        text = render(pf.r, pf.a)
        self.assertIn("[0b] boot path (mount/env)", text)
        self.assertIn("sql-mount-writable", text)
        self.assertIn("RESULT: FAIL", text)

    def test_every_check_id_in_the_source_is_rendered(self):
        """Every check id the checker can report must have a rendered section."""
        import re
        from preflight_worlddb import CHECK_TITLES
        src = (HERE / "preflight_worlddb.py").read_text()
        used = set(re.findall(r'self\.r\.(?:fail|warn|line)\(\s*"([^"]+)"', src))
        self.assertTrue(used, "no findings found in the source - did the pattern rot?")
        self.assertEqual(set(), used - set(CHECK_TITLES),
                         f"unrendered check ids: {sorted(used - set(CHECK_TITLES))}")


class TestMultiTableWrite(unittest.TestCase):
    """PRO-179 rev-2 B-1: every table a write touches must be policy-checked."""

    def test_join_into_ledger_table_fails_both_rules(self):
        rules = rules_for("UPDATE `creature` c JOIN `updates` u SET u.hash = 'x' "
                          "WHERE c.guid = 9002020;")
        self.assertIn("multi-table-write", rules)
        self.assertIn("ledger-table", rules)

    def test_join_into_unallowlisted_table_is_named(self):
        rules = rules_for("UPDATE `creature` c JOIN `game_event` g SET g.eventType = 1 "
                          "WHERE c.guid = 9002020;")
        self.assertIn("multi-table-write", rules)
        self.assertIn("table-not-allowed", rules)

    def test_comma_separated_update_targets_are_rejected(self):
        self.assertIn("multi-table-write", rules_for(
            "UPDATE `creature_template` c, `creature` d SET c.npcflag = 3, d.script_name = 'x' "
            "WHERE c.entry = 900200 AND d.guid = 9002020;"))

    def test_multi_table_delete_is_rejected(self):
        self.assertIn("multi-table-write", rules_for(
            "DELETE c, s FROM `creature` c JOIN `smart_scripts` s "
            "WHERE c.guid = 9002020;"))

    def test_unparenthesised_subquery_target_is_rejected(self):
        self.assertIn("multi-table-write", rules_for(
            "UPDATE (SELECT 1) SET `npcflag` = 3 WHERE `entry` = 900200;"))

    def test_bare_alias_still_passes(self):
        self.assertEqual(rules_for(
            "UPDATE `creature_template` c SET c.npcflag = 3 WHERE c.entry = 900200;"), [])

    def test_as_alias_still_passes(self):
        self.assertEqual(rules_for(
            "UPDATE `creature_template` AS c SET c.npcflag = 3 WHERE c.entry = 900200;"), [])

    def test_foreign_qualified_predicate_leaves_the_key_unbounded(self):
        self.assertIn("unbounded-write", rules_for(
            "UPDATE `creature_template` SET `npcflag` = 3 WHERE u.entry = 900200;"))

    def test_foreign_qualified_assignment_is_rejected(self):
        self.assertIn("unparsable", rules_for(
            "UPDATE `creature_template` SET u.hash = 'x' WHERE `entry` = 900200;"))

    def test_partition_clause_is_not_a_second_table(self):
        self.assertEqual(rules_for(
            "DELETE FROM `creature` PARTITION (p1) WHERE `guid` = 9002020;"), [])

    def test_delete_order_limit_still_bounds_on_the_key(self):
        self.assertEqual(rules_for(
            "DELETE FROM `creature` WHERE `guid` = 9002020 ORDER BY `guid` LIMIT 1;"), [])

    def test_verb_abutting_the_backticked_table_is_still_a_single_table_write(self):
        # `UPDATE`t SET ...` is legal MySQL: the backtick ends the verb token, and
        # the AC corpus contains it (data/sql/archive/db_world/2024_07_14_00.sql).
        self.assertEqual(rules_for(
            "UPDATE`smart_scripts` SET `action_param2` = 2 WHERE `source_type` = 0 "
            "AND `id` = 0 AND `entryorguid` IN (9002020, 9002021);"), [])
        self.assertEqual(rules_for(
            "DELETE FROM`smart_scripts` WHERE `source_type` = 0 AND `entryorguid` = 9002020;"), [])
        # and the grammar still refuses when the region is not one table
        self.assertIn("multi-table-write", rules_for(
            "UPDATE`creature` c JOIN `game_event` ge ON c.guid = ge.guid SET c.unit_flags = 0;"))


class TestPredicateScanning(unittest.TestCase):
    """PRO-179 rev-2 B-2: parenthesised predicates and subqueries."""

    def test_parenthesised_conjunction_is_accepted(self):
        self.assertEqual(rules_for(
            "DELETE FROM `smart_scripts` WHERE (`source_type` = 0 "
            "AND `entryorguid` = 9002020);"), [])

    def test_nested_parentheses_are_accepted(self):
        self.assertEqual(rules_for(
            "DELETE FROM `smart_scripts` WHERE ((`source_type` = 0) "
            "AND (`entryorguid` = 9002020));"), [])

    def test_key_bounded_only_inside_subquery_is_rejected(self):
        self.assertIn("unbounded-write", rules_for(
            "DELETE FROM `smart_scripts` WHERE `entryorguid` = 9002020 AND `id` IN "
            "(SELECT `id` FROM `smart_scripts` WHERE `source_type` = 9);"))

    def test_whole_where_inside_subquery_is_rejected(self):
        self.assertIn("unbounded-write", rules_for(
            "DELETE FROM `creature` WHERE `guid` IN "
            "(SELECT `guid` FROM `creature` WHERE `id1` = 900200);"))

    def test_trailing_comment_cannot_supply_a_predicate(self):
        self.assertIn("unbounded-write", rules_for(
            "DELETE FROM `smart_scripts` WHERE `entryorguid` = 9002020 "
            "-- AND `source_type` = 0\n;"))

    def test_or_inside_parentheses_is_still_caught(self):
        self.assertIn("or-in-where", rules_for(
            "DELETE FROM `smart_scripts` WHERE (`source_type` = 0 "
            "OR `entryorguid` = 9002020);"))

    def test_in_list_with_key_values_is_accepted(self):
        self.assertEqual(rules_for(
            "DELETE FROM `smart_scripts` WHERE `source_type` = 0 AND `entryorguid` "
            "IN (9002020, 9002021);"), [])


class TestMaskedLiterals(unittest.TestCase):
    """PRO-179 rev-2 N-1: keywords inside string literals are not SQL keywords."""

    def test_semicolon_and_keywords_in_literal_do_not_trigger_rules(self):
        self.assertEqual(rules_for(
            "INSERT INTO `page_text` (`ID`, `Text`) VALUES "
            "(900001, 'Please select your destination, hero; the portal awaits.');"), [])

    def test_or_inside_literal_is_not_or_in_where(self):
        self.assertEqual(rules_for(
            "UPDATE `quest_template` SET `OfferText` = 'Hot or cold' WHERE `ID` = 900001;"), [])

    def test_statement_text_in_literal_is_not_a_forbidden_statement(self):
        self.assertEqual(rules_for(
            "INSERT INTO `custom_class_template` (`template_key`, `label`) "
            "VALUES ('DELETE FROM creature', 'x');"), [])

    def test_escaped_quote_in_literal_keeps_the_mask_aligned(self):
        self.assertEqual(rules_for(
            "UPDATE `quest_template` SET `OfferText` = 'He said \\'DROP TABLE x\\' loudly' "
            "WHERE `ID` = 900001;"), [])
        # a following real statement must still be parsed
        self.assertIn("unbounded-write", rules_for(
            "UPDATE `quest_template` SET `OfferText` = 'it\\'s fine' WHERE `ID` = 900001;\n"
            "UPDATE `quest_template` SET `OfferText` = 'x';\n"))

    def test_doubled_quote_in_literal_keeps_row_and_where_parsing_aligned(self):
        # `''` is how the AC corpus escapes an apostrophe. The mask has to blank
        # both characters, otherwise every offset-based splitter after the
        # literal is one quote out of phase (PRO-179 rev-3).
        self.assertEqual(rules_for(
            "UPDATE `quest_offer_reward` SET `RewardText` = 'You''ve been a tremendous "
            "help today; we now have a solid chance. Thank you, $N!' WHERE `ID` = 900001;"), [])
        self.assertEqual(rules_for(
            "INSERT INTO `page_text` (`ID`, `Text`) VALUES "
            "(900001, 'You''ve been a tremendous help; SELECT 1 -- nope');"), [])

    def test_doubled_quote_row_still_reports_a_real_band_violation(self):
        self.assertEqual(rules_for(
            "INSERT INTO `page_text` (`ID`, `Text`) VALUES "
            "(900001, 'You''ve been a tremendous help; SELECT 1');"), [])
        self.assertIn("reserved-band", rules_for(
            "INSERT INTO `page_text` (`ID`, `Text`) VALUES "
            "(12345, 'You''ve been a tremendous help; SELECT 1');"))

    def test_odd_number_of_doubled_quotes_does_not_swallow_later_statements(self):
        # An odd number of `''` escapes left the rev-2 mask with unbalanced
        # quotes, so the statement containing one swallowed every statement
        # after it and none of them were ever checked — 4720 statements hidden
        # in data/sql/updates/db_world/2026_05_30_08.sql alone (PRO-179 rev-3).
        sql = ("UPDATE `creature` SET `AIName` = 'Brian''s Script' WHERE `guid` = 9002020;\n"
               "DELETE FROM `game_event` WHERE `guid` = 9002020;\n")
        self.assertEqual(rules_for(sql), ["table-not-allowed"])
        self.assertEqual(lines_for(sql), [2])


class TestDirtyWorktree(unittest.TestCase):
    """PRO-179 rev-2 N-3: provenance is a HEAD claim, the work tree is the truth."""

    def _file(self, pf, sql):
        # git_root is the temp dir so is_dirty() sees a real repo-relative path
        import tempfile
        d = Path(tempfile.mkdtemp())
        p = d / "upstream.sql"
        p.write_text(sql, encoding="utf-8")
        pf.git_root = d
        return {"rel": "data/sql/updates/db_world/upstream.sql", "path": p,
                "origin": "upstream", "provenance": "upstream directory and author"}

    def test_dirty_upstream_file_is_checked_against_the_policy(self):
        pf = make_preflight()
        f = self._file(pf, "DELETE FROM `creature` WHERE `script_name` = 'x';")
        pf.dirty_paths = lambda: {"upstream.sql"}
        pf.check_policy([f])
        rules = [x.rule for x in pf.r.findings if x.severity == "FAIL"]
        self.assertIn("dirty-worktree", rules)
        self.assertIn("unbounded-write", rules)

    def test_clean_upstream_file_keeps_the_exemption(self):
        pf = make_preflight()
        f = self._file(pf, "DELETE FROM `creature` WHERE `script_name` = 'x';")
        pf.dirty_paths = lambda: set()
        pf.check_policy([f])
        rules = [x.rule for x in pf.r.findings]
        self.assertIn("policy-exempt-origin", rules)
        self.assertNotIn("dirty-worktree", rules)
        self.assertNotIn("unbounded-write", rules)

    def test_unknown_git_state_is_treated_as_dirty(self):
        pf = make_preflight()
        f = self._file(pf, "DELETE FROM `creature` WHERE `script_name` = 'x';")
        pf._dirty_cache = None          # git could not answer
        self.assertTrue(pf.is_dirty(f["path"]))


class TestRev3NonBlocking(unittest.TestCase):
    """PRO-179 rev-3 NB-1..NB-5: report noise, name-vs-syntax, INSERT PARTITION, NOT."""

    WRAPPER = HERE.parent / "deploy" / "worldserver-up.sh"

    # NB-2 — aliases are names, not write targets.
    def test_aliases_are_not_reported_as_unallowlisted_tables(self):
        found = findings_for("UPDATE `creature` `c` JOIN `updates` `u` SET u.hash = 'x' "
                             "WHERE c.guid = 9002020;")
        rules = [r for r, _ in found]
        self.assertIn("multi-table-write", rules)
        self.assertIn("ledger-table", rules)
        self.assertEqual([], [m for r, m in found if r == "table-not-allowed"],
                         "aliases were blamed as write targets")
        msg = next(m for r, m in found if r == "multi-table-write")
        self.assertIn("creature", msg)
        self.assertIn("updates", msg)
        self.assertIn("other names in region: c, u", msg)

    # NB-3 — a name spelled like syntax is not syntax.
    def test_alias_named_like_a_keyword_is_a_single_table_write(self):
        self.assertEqual([], rules_for(
            "UPDATE `creature_template` `join` SET `npcflag` = 3 WHERE `entry` = 900200;"))
        self.assertEqual([], rules_for(
            "DELETE FROM `creature` `using` WHERE `guid` = 9002020;"))

    def test_real_join_is_still_refused_despite_the_blanking(self):
        self.assertIn("multi-table-write", rules_for(
            "UPDATE `creature_template` `c` JOIN `game_event` `ge` ON c.entry = ge.guid "
            "SET c.npcflag = 3 WHERE c.entry = 900200;"))

    # NB-4 — INSERT carries the same PARTITION clause UPDATE/DELETE do.
    def test_insert_partition_clause_is_parsed(self):
        self.assertEqual([], rules_for(
            "INSERT INTO `page_text` (`ID`,`Text`) PARTITION (p1) VALUES (900001,'a');"))
        self.assertEqual([], rules_for(
            "INSERT INTO `page_text` PARTITION (p1) (`ID`,`Text`) VALUES (900001,'a');"))
        self.assertIn("reserved-band", rules_for(
            "INSERT INTO `page_text` (`ID`,`Text`) PARTITION (p1) VALUES (42,'a');"))
        self.assertIn("band-column-missing", rules_for(
            "INSERT INTO `page_text` (`Text`) PARTITION (p1) VALUES ('a');"))

    # NB-5 — the refusal must blame the negation, not the key that is present.
    def test_negated_group_says_so(self):
        found = findings_for(
            "UPDATE `smart_scripts` SET `comment` = 'x' WHERE `entryorguid` = 9002020 "
            "AND `source_type` = 0 AND NOT (`comment` = 'y');")
        rules = [r for r, _ in found]
        self.assertIn("unbounded-write", rules)
        msg = next(m for r, m in found if r == "unbounded-write")
        self.assertIn("NOT", msg)
        self.assertIn("entryorguid", msg)

    # NB-1 — the post-`--` refusal must match shapes, not exact tokens.
    def _run_wrapper(self, argv):
        """Run the wrapper against an empty COMPOSE_ROOT.

        The root has no `.stack/lease`, so a run that gets past argument parsing
        dies at the lease check: these tests can never reach `docker compose`, even
        on the deploy host where a real lease may be held.
        """
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, COMPOSE_ROOT=tmp, LEASE_FILE=str(Path(tmp) / "lease"))
            return subprocess.run(["bash", str(self.WRAPPER), *argv],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=60, env=env)

    def test_wrapper_refuses_compose_selectors_after_double_dash(self):
        for tail in (["--", "-f=/tmp/evil.yml"], ["--", "-f/tmp/evil.yml"], ["--", "-p=proj"],
                     ["--", "-f", "x"], ["--", "--env-file", "x"], ["--", "--project-name=proj"],
                     ["--", "--compose-file=x.yml"]):
            proc = self._run_wrapper(tail)
            self.assertEqual(proc.returncode, 2, f"{tail}: {proc.stdout[:200]}")
            self.assertIn("refusing to forward", proc.stdout, f"{tail}")

    def test_wrapper_still_forwards_benign_flags_after_double_dash(self):
        for tail in (["--", "--force-recreate"], ["--", "--wait"], ["--", "ac-worldserver"]):
            proc = self._run_wrapper(tail)
            self.assertNotIn("refusing to forward", proc.stdout, f"{tail}")
            self.assertIn("lease", proc.stdout, f"{tail}")


class TestRev4ReportFidelity(unittest.TestCase):
    """PRO-179 rev-4 NB-A/NB-B: the rejection path must name the tables it blames.

    None of these can turn a passing file into a failing one — `multi-table-write` is
    raised before any of this is consulted — but a report that blames the wrong name
    sends a reviewer chasing the wrong table, and a report that says "none
    identifiable" reads as "touches nothing" when it genuinely reaches the ledger.
    """

    def test_qualified_name_keeps_the_ledger_as_the_table(self):
        # NB-A: `acore_world`.`updates` used to promote the schema to table position.
        found = findings_for("UPDATE `acore_world`.`updates` `u` JOIN `creature` `c` "
                             "SET u.hash='x' WHERE c.guid=9002020;")
        rules = [r for r, _ in found]
        self.assertIn("multi-table-write", rules)
        self.assertIn("ledger-table", rules)
        self.assertEqual([], [m for r, m in found if r == "table-not-allowed"],
                          "the schema was blamed as a write target")
        msg = next(m for r, m in found if r == "multi-table-write")
        self.assertIn("updates", msg)
        self.assertNotIn("none identifiable", msg)

    def test_qualified_name_still_names_an_unallowlisted_table(self):
        found = findings_for("UPDATE `acore_world`.`creature` `c` JOIN `game_event` `g` "
                             "SET g.eventType=1 WHERE c.guid=9002020;")
        rules = [r for r, _ in found]
        self.assertIn("table-not-allowed", rules)
        self.assertIn("game_event", next(m for r, m in found if r == "table-not-allowed"))

    def test_multi_table_delete_target_list_names_the_real_tables(self):
        # NB-B: the second fragment's first name is the second delete target, not the
        # table after FROM, so `updates` used to be demoted to a mere name.
        found = findings_for("DELETE `u`, `c` FROM `updates` `u` JOIN `creature` `c`;")
        rules = [r for r, _ in found]
        self.assertIn("ledger-table", rules)
        self.assertEqual([], [m for r, m in found if r == "table-not-allowed"],
                          "delete-target aliases were blamed as write targets")
        msg = next(m for r, m in found if r == "multi-table-write")
        self.assertIn("creature", msg)
        self.assertIn("updates", msg)

    def test_unquoted_multi_table_delete_is_not_invisible(self):
        # NB-B: the name scan was backtick-only, so this reported "none identifiable".
        found = findings_for("DELETE updates, creature FROM updates u JOIN creature c;")
        rules = [r for r, _ in found]
        self.assertIn("ledger-table", rules)
        msg = next(m for r, m in found if r == "multi-table-write")
        self.assertNotIn("none identifiable", msg)
        self.assertIn("updates", msg)

    def test_unquoted_join_names_the_ledger(self):
        self.assertIn("ledger-table", rules_for(
            "UPDATE updates u JOIN creature c SET u.hash='x' WHERE c.guid=9002020;"))

    def test_index_hint_words_are_not_reported_as_tables(self):
        found = findings_for("UPDATE `creature_template` USE INDEX (PRIMARY) "
                             "SET `npcflag`=3 WHERE `entry`=900200;")
        rules = [r for r, _ in found]
        self.assertIn("multi-table-write", rules)
        self.assertEqual([], [m for r, m in found if r == "table-not-allowed"],
                          "index-hint keywords were blamed as write targets")

    def test_the_passing_path_is_unaffected_by_the_name_scan(self):
        self.assertEqual([], rules_for(
            "UPDATE `acore_world`.`creature` SET `script_name` = 'x' WHERE `guid` = 9002020;"))
        self.assertEqual([], rules_for(
            "DELETE FROM `acore_world`.`creature_template` WHERE `entry` = 900200;"))


class TestCorpusSweepCompanyColumn(unittest.TestCase):
    """PRO-179 rev-4 NB-C: the tool must measure the invariant its README names."""

    RESULT_CLEAN = {
        "files": 3, "filesUnderPolicy": 3, "statements": 3, "findings": 2,
        "byRule": {"unbounded-write": 2},
        "byRuleOrigin": {"unbounded-write": {"upstream": 2}},
        "byDir": {}, "perFile": {},
    }
    # Identical totals, one finding migrated upstream -> company.
    RESULT_DIRTY = {
        "files": 3, "filesUnderPolicy": 3, "statements": 3, "findings": 2,
        "byRule": {"unbounded-write": 2},
        "byRuleOrigin": {"unbounded-write": {"upstream": 1, "company": 1}},
        "byDir": {}, "perFile": {},
    }

    def _sweep(self, result, argv):
        import contextlib
        import io
        import tempfile
        import corpus_sweep as cs
        saved = cs.build_files, cs.tally
        cs.build_files = lambda engine, policy, workers: []
        cs.tally = lambda engine, files: dict(result)
        out = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp) / "baseline.json"
                base.write_text(json.dumps(self.RESULT_CLEAN))
                with contextlib.redirect_stdout(out):
                    code = cs.main(["--diff", str(base), *argv])
        finally:
            cs.build_files, cs.tally = saved
        return code, out.getvalue()

    def test_company_findings_drive_the_exit_status(self):
        clean, _ = self._sweep(self.RESULT_CLEAN, [])
        self.assertEqual(0, clean)
        dirty, text = self._sweep(self.RESULT_DIRTY, [])
        self.assertEqual(1, dirty)
        self.assertIn("COMPANY findings", text)

    def test_measure_only_keeps_the_exit_status_at_zero(self):
        code, text = self._sweep(self.RESULT_DIRTY, ["--measure-only"])
        self.assertEqual(0, code)
        self.assertIn("COMPANY findings", text)

    def test_diff_measures_the_company_column_not_just_the_totals(self):
        _, text = self._sweep(self.RESULT_DIRTY, [])
        self.assertIn("company", text)
        self.assertIn("new company finding", text)
        self.assertNotIn("(no rule changed)", text,
                         "a company finding appeared while the diff said nothing changed")

    def test_args_take_the_live_schema_from_the_policy(self):
        import corpus_sweep as cs
        args = cs.Args(Path("."), Path("policy.json"), False, False, "acore_world_alt")
        self.assertEqual("acore_world_alt", args.live_db)

    def test_main_threads_the_policy_live_schema_into_the_engine(self):
        import corpus_sweep as cs
        saved = cs.build_files, cs.tally, cs.pf.Preflight
        seen = {}
        real = cs.pf.Preflight

        class Spy(real):
            def __init__(self, a, policy, report):
                seen["live_db"] = a.live_db
                super().__init__(a, policy, report)

        cs.build_files = lambda engine, policy, workers: []
        cs.tally = lambda engine, files: dict(self.RESULT_CLEAN)
        cs.pf.Preflight = Spy
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                code = cs.main([])
        finally:
            cs.build_files, cs.tally, cs.pf.Preflight = saved
        self.assertEqual(0, code)
        self.assertEqual(POLICY["liveDatabase"], seen.get("live_db"),
                         "cross-schema-write was measured against a hard-coded schema")


class TestUpstreamRefProvenance(unittest.TestCase):
    """PRO-248 (PRO-179 review NB-E): an upstream-tagged dir is not a self-serve exemption.

    The POC in the issue is a real one, so it is reproduced here against a real git
    repository: write the file, commit it with a forged upstream `--author`, keep the
    work tree clean. The only thing that separates that from a `git pull` is whether the
    file exists in the ref the tree is tracking, so the ref is what these tests move.
    """

    UP_REL = "data/sql/updates/db_world"
    IN_BAND = ("INSERT INTO `creature_template` (`entry`,`DifficultyEntry`,`name`) "
               "VALUES (900200,0,'Risen Watcher');\n")
    OUT_OF_BAND = ("INSERT INTO `creature_template` (`entry`,`DifficultyEntry`,`name`) "
                   "VALUES (1234,0,'Risen Watcher');\n")
    EMPTY = "-- company content that parses to nothing at all\n"

    def _git(self, d, *args):
        import subprocess
        proc = subprocess.run(["git", *args], cwd=str(d), stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, f"git {' '.join(args)}: {proc.stdout[:300]}")
        return proc.stdout.strip()

    def _repo(self):
        import shutil
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        (d / self.UP_REL).mkdir(parents=True)
        self._git(d, "init", "-q")
        self._git(d, "config", "user.email", "bot@paperclip.local")
        self._git(d, "config", "user.name", "Scuff Bot")
        base = d / self.UP_REL / "2026_01_01_00.sql"
        base.write_text("DELETE FROM `creature_template` WHERE `entry` = 1;\n")
        self._git(d, "add", "-A")
        self._git(d, "commit", "-qm", "upstream update")
        # What a `git pull` leaves behind: a remote-tracking ref holding the history.
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", "HEAD")
        return d

    def _commit(self, d, rel, sql, author="Someone <x@azerothcore.org>"):
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(sql, encoding="utf-8")
        self._git(d, "add", "--", rel)
        self._git(d, "commit", "-qm", f"add {rel}", f"--author={author}")
        return p

    def _pf(self, d, verified_ref="refs/remotes/origin/Playerbot",
            refresh="git fetch --prune origin Playerbot"):
        pf = make_preflight()
        pf.git_root = d
        policy = json.loads(json.dumps(POLICY))
        if verified_ref is not None or refresh is not None:
            policy["upstreamProvenance"] = {"verifiedRef": verified_ref,
                                            "refreshCommand": refresh}
        else:
            policy.pop("upstreamProvenance", None)
        pf.p = policy
        return pf

    def _file(self, d, p):
        return {"rel": str(p.relative_to(d)), "path": p, "basename": p.name,
                "key": p.name.lower(), "size": p.stat().st_size, "sha1": "",
                "dir": {"db_path": "$/data/sql/updates/db_world", "state": "RELEASED",
                        "dir": d / self.UP_REL, "origin": "upstream"}}

    def _run(self, pf, f):
        """What check_ledger + check_policy do for one new file."""
        origin, prov = pf.classify_origin(f)
        f["origin"], f["provenance"] = origin, prov
        pf.check_policy([f])
        return origin, prov

    def _outcomes(self, pf):
        return ([(x.rule, x.message) for x in pf.r.findings if x.severity == "FAIL"],
                [x.rule for x in pf.r.findings if x.severity == "WARN"])

    # --- the POC must be refused ------------------------------------------------ #

    def test_forged_author_poc_is_a_boot_refusal_naming_pending_db_world(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d)
        origin, prov = self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        rules = [r for r, _ in fails]
        self.assertEqual("company", origin,
                         "a forged --author bought the upstream exemption")
        self.assertIn("unverified-upstream-origin", rules)
        msg = next(m for r, m in fails if r == "unverified-upstream-origin")
        self.assertIn("pending_db_world", msg)
        self.assertIn("refs/remotes/origin/Playerbot", msg)
        self.assertIn("2026_11_04_00.sql", msg)
        self.assertTrue(pf.r.failed, "the gate must refuse this boot")

    def test_forged_author_poc_is_refused_even_when_nothing_parses(self):
        # An empty file has no statement to blame, so the provenance finding cannot
        # depend on the parser finding something.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.EMPTY)
        pf = self._pf(d)
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("unverified-upstream-origin", [r for r, _ in fails])
        self.assertTrue(pf.r.failed)

    def test_forged_author_poc_still_reports_its_own_policy_violations(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.OUT_OF_BAND)
        pf = self._pf(d)
        self._run(pf, self._file(d, p))
        rules = [r for r, _ in self._outcomes(pf)[0]]
        self.assertIn("unverified-upstream-origin", rules)
        self.assertIn("reserved-band", rules,
                      "the file was refused for provenance but never policy-checked")

    def test_check_ledger_lists_the_ref_state_of_a_new_file(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d)
        f = self._file(d, p)
        pf.db = None                      # check_ledger's file loop needs no database
        pf.ledger = lambda: {}
        new = pf.check_ledger([f])
        self.assertEqual(1, len(new))
        self.assertEqual(pf.REF_UNVERIFIED, new[0]["upstream_ref_state"])
        listed = [t for t in pf.r.sections.get("2", []) if "NEW " in t]
        self.assertTrue(any("upstreamRef=unverified" in t for t in listed), listed)

    # --- the ordinary `git pull` must keep booting ------------------------------ #

    def test_genuine_upstream_pull_still_passes_offline(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        # Simulate the pull: the ref now carries the commit that introduced the file.
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual("upstream", origin)
        self.assertEqual([], [r for r, _ in fails])
        self.assertIn("policy-exempt-origin", warns)
        self.assertFalse(pf.r.failed, "an ordinary upstream pull must not refuse boot")

    def test_verified_file_is_reported_as_present_in_the_ref(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        pf = self._pf(d)
        f = self._file(d, p)
        pf.classify_origin(f)
        self.assertEqual(pf.REF_VERIFIED, f["upstream_ref_state"])

    def test_missing_ref_does_not_refuse_an_offline_boot(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        origin, _ = self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual("upstream", origin, "a clone that has not fetched must still boot")
        self.assertEqual([], [r for r, _ in fails])
        self.assertIn("upstream-ref-unverified", warns)
        warn = next(x.message for x in pf.r.findings if x.rule == "upstream-ref-unverified")
        self.assertIn("git fetch --prune origin Playerbot", warn)

    def test_unconfigured_ref_falls_back_to_the_legacy_directory_exemption(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref=None, refresh=None)
        origin, _ = self._run(pf, self._file(d, p))
        self.assertEqual("upstream", origin)
        self.assertFalse(pf.r.failed)

    def test_indeterminate_ref_fails_closed_naming_the_refresh_command(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d)
        pf.upstream_ref = lambda: {"state": "error", "ref": "refs/remotes/origin/Playerbot",
                                   "refresh": "git fetch --prune origin Playerbot",
                                   "paths": None, "detail": "git ls-tree exited 128",
                                   "remoteTracked": True}
        origin, _ = self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertEqual("company", origin)
        self.assertIn("unverified-upstream-origin", [r for r, _ in fails])
        msg = next(m for r, m in fails if r == "unverified-upstream-origin")
        self.assertIn("git fetch --prune origin Playerbot", msg)
        self.assertIn("Failing", msg)
        self.assertTrue(pf.r.failed)

    def test_a_local_branch_as_the_verified_ref_is_flagged(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="master")
        self._run(pf, self._file(d, p))
        self.assertIn("upstream-ref-not-remote", self._outcomes(pf)[1])

    def test_a_company_directory_never_needs_the_ref(self):
        d = self._repo()
        p = self._commit(d, "data/sql/updates/pending_db_world/2026_11_04_00.sql",
                         self.OUT_OF_BAND, author="Scuff <scuff@paperclip.local>")
        pf = self._pf(d)
        f = self._file(d, p)
        f["dir"] = {"db_path": "$/data/sql/updates/pending_db_world", "state": "PENDING",
                    "dir": d / "data/sql/updates/pending_db_world", "origin": "company"}
        origin, _ = self._run(pf, f)
        fails, _ = self._outcomes(pf)
        self.assertEqual("company", origin)
        self.assertNotIn("unverified-upstream-origin", [r for r, _ in fails])
        self.assertIn("reserved-band", [r for r, _ in fails])

    def test_company_author_in_an_upstream_dir_is_still_company(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_01.sql", self.IN_BAND,
                         author="Scuff <scuff@paperclip.local>")
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, p))
        self.assertEqual("company", origin)
        self.assertNotIn("unverified-upstream-origin",
                         [r for r, _ in self._outcomes(pf)[0]])

    def test_policy_stats_count_the_ref_outcomes(self):
        d = self._repo()
        forged = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d)
        self._run(pf, self._file(d, forged))
        self.assertEqual({pf.REF_UNVERIFIED: 1}, pf.policy_stats["upstreamRef"])
        listed = " ".join(pf.r.sections.get("4", []))
        self.assertIn("upstream ref refs/remotes/origin/Playerbot", listed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
