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
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from preflight_worlddb import (
    MySQL, Preflight, REGION_NAME_STOPWORDS, Report, _ident_parts, _schema_of, mask_sql,
    parse_statement, split_qualified, split_statements, build_arg_parser, write_targets,
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
        pf.upstream_ref_state = lambda path: (Preflight.REF_VERIFIED, "stubbed: in ref",
                                              "verified")
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
                                             "stubbed: absent from origin/Playerbot",
                                             "absent")
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
        "regionScanConsultations": 0, "scanConsultationsWithoutRefusal": 0,
    }
    # Identical totals, one finding migrated upstream -> company.
    RESULT_DIRTY = {
        "files": 3, "filesUnderPolicy": 3, "statements": 3, "findings": 2,
        "byRule": {"unbounded-write": 2},
        "byRuleOrigin": {"unbounded-write": {"upstream": 1, "company": 1}},
        "byDir": {}, "perFile": {},
        "regionScanConsultations": 0, "scanConsultationsWithoutRefusal": 0,
    }
    # Same findings, but the write-region name scan was consulted twice with no
    # `multi-table-write` to account for it.
    RESULT_SCAN_ORPHAN = {
        "files": 3, "filesUnderPolicy": 3, "statements": 3, "findings": 2,
        "byRule": {"unbounded-write": 2},
        "byRuleOrigin": {"unbounded-write": {"upstream": 2}},
        "byDir": {}, "perFile": {},
        "regionScanConsultations": 2, "scanConsultationsWithoutRefusal": 2,
    }

    def _sweep(self, result, argv):
        import corpus_sweep as cs
        saved = cs.build_files, cs.tally
        cs.build_files = lambda engine, policy, workers: []
        cs.tally = lambda engine, files: dict(result)
        out = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp) / "baseline.json"
                base.write_text(json.dumps(self.RESULT_CLEAN))
                self._give_it_a_tree(tmp)
                with contextlib.redirect_stdout(out):
                    code = cs.main(["--diff", str(base), "--compose-root", tmp, *argv])
        finally:
            cs.build_files, cs.tally = saved
        return code, out.getvalue()

    @staticmethod
    def _give_it_a_tree(tmp: str) -> None:
        """cs.main() refuses a tree with no data/sql, so give the temp tree one.

        Without this the suite only passes when the test file happens to sit in a real
        repo checkout: default_root is corpus_sweep.py's parents[2], so an installed
        ops/preflight/ directory outside a checkout exits 2 on every sweep test.
        """
        (Path(tmp) / "data" / "sql").mkdir(parents=True, exist_ok=True)

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

    def test_an_orphan_region_scan_consultation_fails_the_sweep(self):
        # PRO-179 review rank 3, corpus form: the name scan is report text for a
        # refusal. A consultation no `multi-table-write` accounts for is a policy change
        # wearing a report fix, and the tool must not print a clean sheet for it.
        code, text = self._sweep(self.RESULT_SCAN_ORPHAN, [])
        self.assertEqual(1, code)
        self.assertIn("REGION SCAN", text)
        muted, text2 = self._sweep(self.RESULT_SCAN_ORPHAN, ["--measure-only"])
        self.assertEqual(0, muted)
        self.assertIn("REGION SCAN", text2)

    def _tally_over(self, sqls):
        """Run the real sweep tally over literal files, for the scan-consultation property."""
        import corpus_sweep as cs
        real = cs.pf._region_tables
        engine = cs.pf.Preflight(
            cs.Args(Path("."), Path("policy.json"), True, False, POLICY["liveDatabase"]),
            POLICY, cs.pf.Report())
        files = []
        with tempfile.TemporaryDirectory() as tmp:
            for i, sql in enumerate(sqls):
                path = Path(tmp) / f"f{i}.sql"
                path.write_text(sql)
                files.append({"rel": f"f{i}.sql", "path": path, "origin": "company",
                              "provenance": "company", "dir": {"db_path": "$/x"}})
            with contextlib.redirect_stdout(io.StringIO()):
                result = cs.tally(engine, files)
        self.assertIs(real, cs.pf._region_tables, "the sweep spy leaked into the module")
        return result

    def test_the_sweep_counts_one_consultation_per_multi_table_refusal(self):
        result = self._tally_over([
            "UPDATE `creature_template` SET `npcflag` = 3 WHERE `entry` = 900200;",
            "UPDATE `creature` c JOIN `updates` u SET u.hash = 'x' WHERE c.guid = 9002020;",
            "DELETE `u`, `c` FROM `updates` `u` JOIN `creature` `c`;",
            "DELETE FROM `creature` WHERE `guid` = 9002020;",
        ])
        self.assertEqual(2, result["byRule"].get("multi-table-write"))
        self.assertEqual(2, result["regionScanConsultations"])
        self.assertEqual(0, result["scanConsultationsWithoutRefusal"])

    def test_the_sweep_property_is_not_trivially_satisfied_by_no_refusals(self):
        # A corpus with nothing to refuse must also report zero consultations, so the
        # property cannot be satisfied by both sides being empty after the scan stops
        # running at all.
        result = self._tally_over([
            "UPDATE `creature_template` SET `npcflag` = 3 WHERE `entry` = 900200;",
        ])
        self.assertEqual(0, result["byRule"].get("multi-table-write", 0))
        self.assertEqual(0, result["regionScanConsultations"])

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
            with tempfile.TemporaryDirectory() as tmp:
                self._give_it_a_tree(tmp)
                with contextlib.redirect_stdout(io.StringIO()):
                    code = cs.main(["--compose-root", tmp])
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

    def _virgin(self, d):
        """Strip every trace of a fetch, so this really is a never-fetched clone.

        PRO-291 (PRO-248 review N1, opening 2): `missing` now has to tell "this clone
        has never fetched" from "the ref was here and someone deleted it", so a test
        that claims the first case has to leave no evidence of the second — no
        refs/remotes/<remote>/* refs, no reflogs, no FETCH_HEAD.
        """
        import subprocess
        for line in subprocess.run(["git", "for-each-ref", "--format=%(refname)",
                                    "refs/remotes/"], cwd=str(d),
                                   stdout=subprocess.PIPE, text=True).stdout.splitlines():
            name = line.strip()
            if name:
                self._git(d, "update-ref", "-d", name)
        shutil_rmtree = __import__("shutil").rmtree
        logs = d / ".git" / "logs" / "refs" / "remotes"
        if logs.exists():
            shutil_rmtree(logs, ignore_errors=True)
        (d / ".git" / "FETCH_HEAD").unlink(missing_ok=True)
        return d

    def _write_reflog(self, d, refname):
        """Leave a reflog file for `refname`, as a fetch or `git update-ref` would."""
        logs = d / ".git" / "logs" / refname
        logs.parent.mkdir(parents=True, exist_ok=True)
        logs.write_text("0" * 40 + " " + "a" * 40 + " Scuff <s@s> 1 +0000\n")
        return logs

    def _clone(self, single_branch=False, carry_ref=False):
        """A real `git clone` — the repository shape PRO-291 review B1 got wrong.

        `git clone` populates `refs/remotes/<remote>/*` itself and writes no reflog for
        those refs, which is precisely what the sibling probe must not read as a fetch.
        The seed holds only `master`, so the policy's `refs/remotes/origin/Playerbot`
        has never existed anywhere, which is the honest case that has to boot.

        `carry_ref` puts a real `Playerbot` *branch* on the seed first, so the clone
        arrives holding `refs/remotes/origin/Playerbot` with no reflog for it — the
        shape a remote that has the branch leaves behind, which PRO-300 needs in order
        to verify a boot inside a clone.
        """
        import shutil
        import tempfile
        seed = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, seed, ignore_errors=True)
        (seed / self.UP_REL).mkdir(parents=True)
        self._git(seed, "init", "-q", "-b", "master")
        self._git(seed, "config", "user.email", "bot@paperclip.local")
        self._git(seed, "config", "user.name", "Scuff Bot")
        base = seed / self.UP_REL / "2026_01_01_00.sql"
        base.write_text("DELETE FROM `creature_template` WHERE `entry` = 1;\n")
        self._git(seed, "add", "-A")
        self._git(seed, "commit", "-qm", "upstream update")
        if carry_ref:
            self._git(seed, "branch", "Playerbot", "HEAD")
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        argv = ["clone"]
        if single_branch:
            argv += ["--single-branch", "--branch", "master"]
        self._git(seed, *argv, str(seed), str(d))
        self._git(d, "config", "user.email", "bot@paperclip.local")
        self._git(d, "config", "user.name", "Scuff Bot")
        return d

    def test_missing_ref_does_not_refuse_an_offline_boot(self):
        d = self._virgin(self._repo())
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

    def test_a_local_branch_as_the_verified_ref_refuses_the_boot(self):
        # PRO-248 review B1: the policy and the README both say verifiedRef *must* be a
        # remote-tracking ref. A 'must' that costs a warning is the exemption with extra
        # steps — the file stays exempt while the gate says it is worried.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="master")
        origin, _ = self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual("company", origin,
                         "a ref the author can move still bought the exemption")
        self.assertIn("upstream-ref-not-remote", [r for r, _ in fails])
        msg = next(m for r, m in fails if r == "upstream-ref-not-remote")
        self.assertIn("refs/remotes/", msg)
        self.assertIn("pending_db_world", msg)
        self.assertTrue(pf.r.failed, "a non-remote verifiedRef must refuse this boot")

    def test_a_shadow_branch_does_not_answer_for_a_short_ref_name(self):
        # `git branch origin/Playerbot HEAD` makes the short name ambiguous: git then
        # reads refs/heads/origin/Playerbot for ls-tree AND refuses to name the ref for
        # `rev-parse --symbolic-full-name`. Before review B1 the unanswered question
        # defaulted to 'remote-tracking', so the forged local branch vouched for the
        # forged file and the reserved-band write was exempt again.
        d = self._repo()
        rel = f"{self.UP_REL}/2026_11_04_00.sql"
        p = self._commit(d, rel, self.IN_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "branch", "-f", "origin/Playerbot", head)
        pf = self._pf(d, verified_ref="origin/Playerbot")
        origin, _ = self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertEqual("company", origin, "the shadow branch vouched for the file")
        self.assertIn("upstream-ref-not-remote", [r for r, _ in fails])
        self.assertTrue(pf.r.failed)

    def test_a_shadow_branch_cannot_touch_a_fully_qualified_ref(self):
        # The other half of review B1: the qualified name must keep working, and keep
        # refusing, with the same shadow branch in place — so the fix is the ref name,
        # not a warning nobody reads.
        d = self._repo()
        rel = f"{self.UP_REL}/2026_11_04_00.sql"
        p = self._commit(d, rel, self.IN_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "branch", "-f", "origin/Playerbot", head)
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertEqual("company", origin)
        self.assertIn("unverified-upstream-origin", [r for r, _ in fails])
        self.assertTrue(pf.r.failed)

    def test_a_shadow_branch_does_not_refuse_a_genuine_pull(self):
        # ...and the qualified name must not fail closed just because someone made a
        # branch with a confusing name.
        d = self._repo()
        rel = f"{self.UP_REL}/2026_11_04_00.sql"
        p = self._commit(d, rel, self.IN_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "branch", "-f", "origin/Playerbot", "HEAD~1")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual("upstream", origin)
        self.assertEqual([], [r for r, _ in fails])
        self.assertIn("policy-exempt-origin", warns)
        self.assertFalse(pf.r.failed)

    # --- B1/N4: the two halves of the remote-tracking test, pinned separately ----- #

    def _stub_git(self, pf, named, rc=0, err=""):
        """Answer only the ref-name question; record every argv git is asked.

        Review N4: one `elif` covers both "exit 0 with no name" and "non-zero exit", and
        every real-git test in this file reaches both through the same branch. Only a
        stub can hand git the first shape on its own, which is the shape a shadow branch
        really produces — so a future edit that drops `or not full.strip()` would
        otherwise still pass the whole suite.
        """
        calls = []

        def fake(argv, timeout=60, errors="replace"):
            calls.append(list(argv))
            if argv[:2] == ["rev-parse", "--symbolic-full-name"]:
                return rc, named, err
            if argv[:2] == ["rev-parse", "--verify"]:
                return 0, "0" * 40, ""
            return 128, "", f"stub refused {argv}"

        pf._git = fake
        return calls

    def test_a_zero_exit_naming_no_ref_is_not_remote_tracking(self):
        # Measured on git 2.43.0: with `git branch origin/Playerbot HEAD` present,
        # `rev-parse --symbolic-full-name origin/Playerbot` exits 0 and prints nothing,
        # with the ambiguity on stderr. The name never came back, so the ref may not
        # vouch for anything (PRO-248 review B1).
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="origin/Playerbot")
        calls = self._stub_git(pf, "", rc=0,
                               err="warning: refname 'origin/Playerbot' is ambiguous.")
        up = pf.upstream_ref()
        self.assertEqual("ok", up["state"])
        self.assertFalse(up["remoteTracked"],
                         "an unanswered question defaulted to remote-tracking")
        self.assertIn("ambiguous", up["remoteDetail"])
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]],
                         "an untrusted ref was still asked for its tree")

    def test_a_nonzero_exit_is_not_remote_tracking_either(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="origin/Playerbot")
        calls = self._stub_git(pf, "", rc=1,
                               err="error: unknown revision 'origin/Playerbot'")
        up = pf.upstream_ref()
        self.assertFalse(up["remoteTracked"])
        self.assertIn("unknown revision", up["remoteDetail"])
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]])

    def test_a_non_remote_ref_refusal_names_the_refresh_command(self):
        # Review N2: the README table promises this FAIL names `refreshCommand`, so the
        # author of a gutted policy gets the command that would have fixed the ref.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="master")
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        msg = next(m for r, m in fails if r == "upstream-ref-not-remote")
        self.assertIn("git fetch --prune origin Playerbot", msg)
        self.assertIn("pending_db_world", msg)

    # --- B2: a filename git would quote must not look like a forgery ------------ #

    NON_ASCII = "2026_11_04_00_érfar.sql"

    def test_a_non_ascii_upstream_filename_boots(self):
        # `git ls-tree` without `-z` answers with "...\\303\\251rfar.sql", which can
        # never equal the name on disk, so a genuine upstream file became unverified and
        # the boot was refused — the worst direction for a false positive.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/{self.NON_ASCII}", self.IN_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertEqual("upstream", origin, "a quoted path was not matched to the ref")
        self.assertEqual([], [r for r, _ in fails])
        self.assertFalse(pf.r.failed, "an ordinary upstream pull must not refuse boot")

    def test_a_non_ascii_filename_is_still_refused_when_it_is_not_in_the_ref(self):
        # The passing case above is only meaningful if the same name is refused when the
        # ref really does not hold it.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/{self.NON_ASCII}", self.IN_BAND)
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertEqual("company", origin)
        self.assertIn("unverified-upstream-origin", [r for r, _ in fails])
        self.assertTrue(pf.r.failed)

    # --- N1: a case-only twin is tolerated, but not silently -------------------- #

    def test_a_case_only_twin_is_exempt_but_warned(self):
        d = self._repo()
        rel = f"{self.UP_REL}/2026_11_04_00.sql"
        self._commit(d, rel, self.IN_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        twin = f"{self.UP_REL}/2026_11_04_00.SQL"
        p = self._commit(d, twin, self.IN_BAND)
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual("upstream", origin,
                         "a case-insensitive checkout must still boot")
        self.assertEqual([], [r for r, _ in fails])
        self.assertIn("upstream-ref-case-divergence", warns)
        warn = next(x.message for x in pf.r.findings
                    if x.rule == "upstream-ref-case-divergence")
        self.assertIn("2026_11_04_00.sql", warn,
                      "the WARN has to name the ref path it matched modulo case")

    def test_a_case_only_twin_with_different_bytes_is_refused(self):
        # The tolerance is for checkouts, not for content: a twin whose bytes are not the
        # ref's bytes is the N2 case wearing a case difference.
        d = self._repo()
        rel = f"{self.UP_REL}/2026_11_04_00.sql"
        self._commit(d, rel, self.OUT_OF_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        twin = f"{self.UP_REL}/2026_11_04_00.SQL"
        p = self._commit(d, twin, self.IN_BAND)
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertEqual("company", origin)
        self.assertIn("unverified-upstream-content", [r for r, _ in fails])
        self.assertTrue(pf.r.failed)

    # --- N2: the name being in the ref is not the file being in the ref ---------- #

    def test_rewritten_content_under_a_verified_name_is_refused(self):
        # Measured in review N2: the file is in the ref, the body is replaced with a
        # reserved-band creature under a forged --author, the work tree is clean, and a
        # path-only check still says 'present in the ref'. The blob is what differs.
        d = self._repo()
        rel = f"{self.UP_REL}/2026_11_04_00.sql"
        self._commit(d, rel, self.OUT_OF_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        (d / rel).write_text(self.IN_BAND, encoding="utf-8")
        self._git(d, "commit", "-qam", "resurrect the Risen Watcher",
                  "--author=Someone <x@azerothcore.org>")
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, d / rel))
        fails, _ = self._outcomes(pf)
        self.assertEqual("company", origin,
                         "a rewritten body kept the upstream exemption")
        self.assertIn("unverified-upstream-content", [r for r, _ in fails])
        msg = next(m for r, m in fails if r == "unverified-upstream-content")
        self.assertIn("pending_db_world", msg)
        self.assertIn("2026_11_04_00.sql", msg)
        self.assertTrue(pf.r.failed)

    def test_a_verified_name_keeps_its_exemption_when_the_bytes_match(self):
        # The negative control for the blob check: same content, same name, clean tree.
        d = self._repo()
        rel = f"{self.UP_REL}/2026_11_04_00.sql"
        p = self._commit(d, rel, self.IN_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        pf = self._pf(d)
        f = self._file(d, p)
        self._run(pf, f)
        self.assertEqual(pf.REF_VERIFIED, f["upstream_ref_state"])
        self.assertEqual("verified", f["upstream_ref_cause"])

    def test_the_ref_listing_carries_blob_ids(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/{self.NON_ASCII}", self.IN_BAND)
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertEqual({"2026_01_01_00.sql", self.NON_ASCII},
                         {n.split("/")[-1] for n in up["paths"]})
        self.assertTrue(up["blobs"][f"{self.UP_REL}/{self.NON_ASCII}"].startswith(
            self._git(d, "hash-object", "--", f"{self.UP_REL}/{self.NON_ASCII}")[:12]))

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


    # --- PRO-291 / PRO-248 review N1: 'missing' is not one answer, it is three ---- #

    def _stub_git_shapes(self, pf, full_name=(0, "refs/remotes/origin/Playerbot", ""),
                         commit_peel=(0, "0" * 40, ""), obj=(1, "", ""),
                         obj_type=(1, "", ""), reflog_path=(0, ".git/logs/ABSENT", ""),
                         reflog_paths=None,
                         fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                         namespace=(0, "", ""), common_dir=None, remote_url=None,
                         merge_base=None, ls_tree=None):
        """Hand git each shape on its own, and record every argv it was asked (review N4).

        `_stub_git` above pins the remote-tracking question. This pins the *existence*
        question, whose whole point is that `git rev-parse --verify <ref>^{commit}`
        collapses "no such ref" and "the ref names a tree" into one exit code, and that
        a real repository cannot produce both shapes separately.

        `reflog_paths` answers `--git-path logs/<refname>` per ref name, so the ref the
        policy trusts and a sibling can be given different answers — which is the whole
        content of the sibling probe (PRO-291 review B1). Anything not listed falls
        back to `reflog_path`.

        The PRO-300 probes are refused by default, which is the shape they must survive:
        an unanswered `--git-common-dir` means "no state directory", never "no
        attestation" dressed up as a finding (AC 6). `common_dir` answers the placement
        question, `remote_url` the repo-binding question, `merge_base` the
        is-the-attested-commit-an-ancestor question (True/False/None → 0/1/128).
        """
        calls = []

        def fake(argv, timeout=60, errors="replace"):
            calls.append(list(argv))
            if argv[:2] == ["rev-parse", "--symbolic-full-name"]:
                return full_name
            if argv[:2] == ["rev-parse", "--git-common-dir"]:
                return common_dir if common_dir is not None else (128, "", "stub refused")
            if argv[:2] == ["rev-parse", "--verify"]:
                return commit_peel if argv[-1].endswith("^{commit}") else obj
            if argv[:1] == ["cat-file"]:
                return obj_type
            if argv[:2] == ["rev-parse", "--git-path"]:
                arg = argv[-1]
                if not arg.startswith("logs/"):
                    return fetch_head_path
                if reflog_paths and arg in reflog_paths:
                    return reflog_paths[arg]
                return reflog_path
            if argv[:1] == ["for-each-ref"]:
                return namespace
            if argv[:1] == ["config"]:
                return remote_url if remote_url is not None else (128, "", "stub refused")
            if argv[:1] == ["ls-tree"]:
                # Refused by default: a stub test that has not said what tree the ref
                # holds should not silently get an empty one.
                return ls_tree if ls_tree is not None else (128, "", "stub refused")
            if argv[:1] == ["merge-base"]:
                if merge_base is None:
                    return 128, "", "stub refused"
                return (0, "", "") if merge_base else (1, "", "")
            if argv[:1] == ["log"]:
                # The author probe reaches `_git` too since review N5, so the stub has
                # to answer it: every caller of this helper committed the file with an
                # upstream author, and a stub that cannot say so would silently turn an
                # upstream file into an unattributable one.
                return 0, "Someone <x@azerothcore.org>\tSomeone\tadd file", ""
            if argv[:1] == ["status"]:
                return 0, "", ""
            return 128, "", f"stub refused {argv}"

        pf._git = fake
        return calls

    # -- opening 1: a ref that is here but is not a commit ------------------------ #

    def test_a_ref_that_resolves_to_a_tree_fails_closed_as_not_a_commit(self):
        # Rev's POC, end to end against a real repository: point verifiedRef at a tree
        # object and the forged-author file used to boot on a WARN, because `^{commit}`
        # failed and the gate filed the ref as `missing`.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        tree = self._git(d, "rev-parse", "HEAD^{tree}")
        pf = self._pf(d, verified_ref=tree)
        up = pf.upstream_ref()
        self.assertEqual("not-a-commit", up["state"],
                         "a tree object was filed as a ref that does not exist")
        self.assertEqual("tree", up["objectType"])
        origin, _ = self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertEqual("company", origin, "a forged-author file kept the exemption")
        self.assertIn("upstream-ref-not-a-commit", [r for r, _ in fails])
        msg = next(m for r, m in fails if r == "upstream-ref-not-a-commit")
        self.assertIn("git fetch --prune origin Playerbot", msg)
        self.assertIn("pending_db_world", msg)
        self.assertTrue(pf.r.failed, "a non-commit verifiedRef must refuse this boot")

    def test_a_ref_that_resolves_to_a_tree_is_not_filed_as_missing(self):
        # The two shapes `^{commit}` collapses, produced separately. Measured on git
        # 2.43.0: for a tree, `rev-parse --symbolic-full-name` exits 0 printing nothing
        # and the peel exits 1, which is the same exit code a missing ref gives.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="abc123")
        calls = self._stub_git_shapes(
            pf, full_name=(0, "", ""), commit_peel=(1, "", "error: expected commit type"),
            obj=(0, "0d8a474fc67971fb3dd7616e26323d3066442555", ""),
            obj_type=(0, "tree", ""))
        up = pf.upstream_ref()
        self.assertEqual("not-a-commit", up["state"])
        self.assertEqual("tree", up["objectType"])
        self.assertFalse(up["remoteTracked"])
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]],
                         "a ref the gate does not trust was still asked for its tree")
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual(pf.REF_INDETERMINATE, state)
        self.assertEqual("not-a-commit", cause)
        self.assertIn("git fetch", pf.upstream_ref()["refresh"])

    def test_only_a_name_that_resolves_to_nothing_at_all_is_missing(self):
        # The other half of opening 1: `missing` must stay reachable, or the
        # never-fetched clone loses the boot the acceptance criteria promise it.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        calls = self._stub_git_shapes(
            pf, commit_peel=(1, "", "fatal: Needed a single revision"),
            obj=(1, "", ""), reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""))
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertFalse(up["wasTracked"])
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]])
        state, _detail, cause = pf.upstream_ref_state(p)
        self.assertEqual(pf.REF_UNCHECKED, state)
        self.assertEqual("missing", cause)

    def test_a_zero_exit_naming_no_object_is_missing_not_not_a_commit(self):
        # The mirror of the B1 shape, on the existence question: `rev-parse --verify
        # --quiet <ref>` can exit 0 and print nothing. Treating "exit 0" alone as "the
        # name resolved" would file a ref that is simply not there as `not-a-commit`
        # and refuse a clone that has never fetched.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(0, "", ""),
                              obj_type=(0, "tree", ""),
                              reflog_path=(0, ".git/logs/ABSENT", ""),
                              fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                              namespace=(0, "", ""))
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"],
                         "an empty answer was read as a resolved object")
        self.assertEqual(pf.REF_UNCHECKED, pf.upstream_ref_state(p)[0])

    def test_a_zero_object_type_still_fails_closed_as_not_a_commit(self):
        # `git rev-parse --verify --quiet 0000...0` exits 0 and prints the null sha, but
        # `cat-file -t` cannot name it. The name resolved, so it is not missing.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="0" * 40)
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(0, "0" * 40, ""),
                              obj_type=(128, "", "could not get object info"))
        up = pf.upstream_ref()
        self.assertEqual("not-a-commit", up["state"])
        self.assertEqual("non-commit object", up["objectType"])
        self.assertEqual(pf.REF_INDETERMINATE, pf.upstream_ref_state(p)[0])

    # -- opening 2: deleted vs never fetched -------------------------------------- #

    def test_a_deleted_upstream_ref_fails_closed_as_ref_deleted(self):
        # The attack the review named: one `git update-ref -d` is all that stands
        # between the boot gate and the pre-PRO-248 directory+author exemption.
        d = self._repo()
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/master", head)
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        self._git(d, "update-ref", "-d", "refs/remotes/origin/Playerbot")
        pf = self._pf(d)
        origin, _ = self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual("company", origin, "a deleted ref still bought the exemption")
        self.assertIn("upstream-ref-deleted", [r for r, _ in fails])
        self.assertNotIn("upstream-ref-unverified", warns)
        msg = next(m for r, m in fails if r == "upstream-ref-deleted")
        self.assertIn("git fetch --prune origin Playerbot", msg)
        self.assertIn("--allow-missing-upstream-ref", msg)
        self.assertIn("pending_db_world", msg)
        self.assertTrue(pf.r.failed)

    def test_a_ref_deleted_by_hand_leaves_its_reflog_as_evidence(self):
        # `git update-ref -d` deletes the reflog with the ref (measured on git 2.43.0),
        # so the namespace probe above is what catches that route. A ref removed some
        # other way — a stray `rm` of the loose ref — leaves the reflog behind, and the
        # reflog alone has to be enough.
        d = self._repo()
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/Playerbot", head)
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        loose = d / ".git" / "refs" / "remotes" / "origin" / "Playerbot"
        self.assertTrue(loose.exists(), "update-ref did not leave a loose ref to remove")
        loose.unlink()
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("reflog", up["wasTrackedDetail"])
        origin, _ = self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertEqual("company", origin)
        self.assertIn("upstream-ref-deleted", [r for r, _ in fails])
        self.assertTrue(pf.r.failed)

    def test_allow_missing_upstream_ref_opts_into_the_exemption(self):
        # The permissive path stays available, but it has to be typed.
        d = self._repo()
        head = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", "refs/remotes/origin/master", head)
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        self._git(d, "update-ref", "-d", "refs/remotes/origin/Playerbot")
        pf = self._pf(d)
        pf.a.allow_missing_upstream_ref = True
        origin, _ = self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual("upstream", origin)
        self.assertEqual([], [r for r, _ in fails])
        self.assertIn("upstream-ref-unverified", warns)
        warn = next(x.message for x in pf.r.findings if x.rule == "upstream-ref-unverified")
        self.assertIn("--allow-missing-upstream-ref", warn)
        self.assertFalse(pf.r.failed)

    def test_a_fetch_head_is_evidence_the_clone_fetched(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        # The shape git actually writes: `<sha>\t\tbranch '<name>' of <url>`. The gate
        # reads the sha, so a fixture whose id is shorter than a short id would pass
        # the assertion while exercising the unparsable branch instead (PRO-307 A1).
        (d / ".git" / "FETCH_HEAD").write_text(
            "0d8a474fc67971fb3dd7616e26323d3066442555\t\tbranch 'Playerbot' of origin\n")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("FETCH_HEAD", up["wasTrackedDetail"])
        self.assertIn("fetched ref", up["wasTrackedDetail"])
        self.assertIn("0d8a474fc679", up["wasTrackedDetail"])
        self.assertEqual("fetched", up["wasTrackedCertainty"])
        self.assertEqual(pf.REF_INDETERMINATE, pf.upstream_ref_state(p)[0])

    # -- a failed fetch leaves a FETCH_HEAD behind, and it is not a deletion ------- #

    def _one_signal(self, pf):
        """Answer every history probe but FETCH_HEAD, whose file the test writes."""
        return self._stub_git_shapes(
            pf, commit_peel=(1, "", "fatal: Needed a single revision"), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD", ""), namespace=(0, "", ""))

    def test_a_fetch_head_a_failed_fetch_left_behind_is_not_fetch_evidence(self):
        # PRO-307 A1, measured on the deployhost: `GIT_ALLOW_PROTOCOL=none git fetch
        # origin Playerbot` exits 128 and leaves .git/FETCH_HEAD present at 0 bytes
        # (sha256 e3b0c44298fc…, the empty digest). Reading `fhp.exists()` as "this
        # clone has fetched" reclassified a clone that had never fetched from the honest
        # `upstream-ref-unverified` WARN it is promised into a hard `upstream-ref-deleted`
        # refusal — the gate accusing an operator of a deletion they had not made.
        for shape in (b"", b"\n", b"  \n\t \n", b"\r\n"):
            with self.subTest(repr(shape)):
                d = self._repo()
                p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
                (d / ".git" / "FETCH_HEAD").write_bytes(shape)
                pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
                self._one_signal(pf)
                up = pf.upstream_ref()
                self.assertEqual("missing", up["state"])
                self.assertFalse(up["wasTracked"], up["wasTrackedDetail"])
                self.assertEqual("", up["wasTrackedCertainty"], up["wasTrackedDetail"])
                self._run(pf, self._file(d, p))
                fails, warns = self._outcomes(pf)
                self.assertNotIn("upstream-ref-deleted", [r for r, _ in fails],
                                 f"{shape!r} was read as a deletion: {[r for r, _ in fails]}")
                self.assertIn("upstream-ref-unverified", warns)
                self.assertFalse(pf.r.failed)
                self.assertEqual(pf.REF_UNCHECKED, pf.upstream_ref_state(p)[0])

    def test_a_fetch_head_that_names_no_fetched_ref_is_evidence_but_says_so(self):
        # Content that is not empty is still more than a failed fetch leaves, and the
        # gate must not hand the exemption back because a file holds something it did
        # not expect. What it must not do is claim a fetch it did not observe.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        (d / ".git" / "FETCH_HEAD").write_text("not a fetch record\n")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._one_signal(pf)
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("do not parse", up["wasTrackedDetail"])
        self.assertNotIn("so a fetch completed", up["wasTrackedDetail"])
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-deleted", [r for r, _ in fails])

    def _only_fetch_head(self, pf, declared):
        """No reflog, no siblings, and FETCH_HEAD answered by `declared` (a git-path)."""
        return self._stub_git_shapes(
            pf, commit_peel=(1, "", "fatal: Needed a single revision"), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, declared, ""), namespace=(0, "", ""))

    def test_an_abbreviated_oid_in_fetch_head_is_not_counted_as_a_fetched_ref(self):
        # Review N6: `git fetch` writes full object names in FETCH_HEAD — never the
        # abbreviated ones `git log` prints — so a 7-hex run is not a fetched-ref line.
        # It is still more content than a failed fetch leaves, so the refusal stands;
        # what must not come back is the gate reporting it as refs a fetch brought down.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_02.sql", self.IN_BAND)
        (d / ".git" / "FETCH_HEAD").write_text("0d8a474\t\tbranch 'Playerbot' of origin\n")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._only_fetch_head(pf, ".git/FETCH_HEAD")
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("do not parse", up["wasTrackedDetail"])
        self.assertNotIn("fetched ref(s)", up["wasTrackedDetail"])
        self.assertNotIn("so a fetch completed", up["wasTrackedDetail"])
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-deleted", [r for r, _ in fails])

    def test_a_fetch_head_the_os_will_not_read_is_not_answered_as_no_record(self):
        # Review N7: "unreadable" and "absent" are different facts and the README's rule
        # is that they must not share a sentence. The gate cannot read the bytes, so it
        # keeps what a non-empty file already proves — more than a failed fetch leaves —
        # and says it could not read them, without claiming a fetch it did not observe.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_03.sql", self.IN_BAND)
        (d / ".git" / "FETCH_HEAD").mkdir()
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._only_fetch_head(pf, ".git/FETCH_HEAD")
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"],
                        "an unreadable FETCH_HEAD was answered as no FETCH_HEAD at all")
        self.assertIn("cannot read", up["wasTrackedDetail"])
        self.assertIn("byte(s)", up["wasTrackedDetail"])
        self.assertNotIn("so a fetch completed", up["wasTrackedDetail"])
        self.assertNotIn("do not parse", up["wasTrackedDetail"])
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertIn("upstream-ref-deleted", [r for r, _ in fails],
                      f"the unreadable record was treated as no evidence: {warns}")

    def test_a_fetch_head_that_can_neither_be_read_nor_sized_is_no_evidence(self):
        # The other half of review N7. Where the gate observed nothing at all — git names
        # a FETCH_HEAD that is not there to read or stat — existence alone was never
        # fetch evidence (A1), and answering "no record" is what keeps a never-fetched
        # clone the WARN it is promised instead of a deletion accusation.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_04.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._only_fetch_head(pf, ".git/FETCH_HEAD_never_written")
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertFalse(up["wasTracked"], up["wasTrackedDetail"])
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertNotIn("upstream-ref-deleted", [r for r, _ in fails])
        self.assertIn("upstream-ref-unverified", warns)
        self.assertFalse(pf.r.failed)

    def test_an_absolute_git_path_for_fetch_head_is_read_too(self):
        # Review N9: `git rev-parse --git-path` answers relative in an ordinary clone and
        # absolute wherever the common dir was moved (linked worktrees, $GIT_DIR). Both
        # branches have to reach the same file; only the relative one was ever run.
        #
        # `git_root` is pointed at a tree that does not exist so the answer can only come
        # from the declared absolute path: a read that treated git's answer as relative
        # to the tracked tree (string-concatenating it, say) would report no FETCH_HEAD
        # here. Note what this pair of tests can and cannot pin: dropping the
        # `if not p.is_absolute()` guard in favour of an unconditional `git_root / p` is
        # NOT detectable on any filesystem, because pathlib's `/` replaces the left side
        # when the right is absolute. That revert is an equivalent mutant, so the guard
        # is documented as defensive clarity, not as behaviour this test proves.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_05.sql", self.IN_BAND)
        real = d / ".git" / "FETCH_HEAD"
        real.write_text("0d8a474fc67971fb3dd7616e26323d3066442555\t\tbranch "
                        "'Playerbot' of origin\n")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._only_fetch_head(pf, str(real))
        pf.git_root = d / "no-such-tree"
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("fetched ref(s)", up["wasTrackedDetail"])
        self.assertIn("0d8a474fc679", up["wasTrackedDetail"])
        self.assertEqual("fetched", up["wasTrackedCertainty"])

    # -- each of the three history signals, on its own (review N4) ----------------- #

    def test_the_reflog_alone_is_enough_history(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        calls = self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/refs/remotes/origin/Absent", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""), namespace=(0, "", ""))
        (d / ".git" / "logs" / "refs" / "remotes" / "origin").mkdir(parents=True,
                                                                    exist_ok=True)
        (d / ".git" / "logs" / "refs" / "remotes" / "origin" / "Absent").write_text(
            "0" * 40 + " " + "a" * 40 + " Scuff <s@s> 1 +0000\n")
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("reflog", up["wasTrackedDetail"])
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]])
        self.assertEqual("ref-deleted", pf.upstream_ref_state(p)[2])

    def test_a_sibling_ref_that_carries_a_reflog_is_enough_history(self):
        # PRO-291 review B1 rewrote this test. What makes a namespace sibling evidence of
        # a fetch is the *reflog* it carries — the file `git fetch`, `git pull` and
        # `git update-ref` leave behind — not the ref, which `git clone` writes itself.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        self._write_reflog(d, "refs/remotes/origin/Playerbot")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        calls = self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
            namespace=(0, "refs/remotes/origin/Playerbot\n", ""),
            reflog_paths={"logs/refs/remotes/origin/Playerbot":
                          (0, ".git/logs/refs/remotes/origin/Playerbot", "")})
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("refs/remotes/origin/Playerbot", up["wasTrackedDetail"])
        self.assertEqual("fetched", up["wasTrackedCertainty"])
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]])
        self.assertEqual("ref-deleted", pf.upstream_ref_state(p)[2])

    def test_a_sibling_ref_without_a_reflog_is_clone_output_not_history(self):
        # The twin the review measured on git 2.43.0: a clone leaves branch siblings
        # with no reflog at all. That is an untouched clone, and it used to be refused a
        # boot while the gate told the operator the tracked ref had been deleted.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
            namespace=(0, "refs/remotes/origin/Playerbot\n"
                          "refs/remotes/origin/master\n", ""))
        up = pf.upstream_ref()
        self.assertFalse(up["wasTracked"], up["wasTrackedDetail"])
        self.assertEqual("", up["wasTrackedCertainty"])
        state, _detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_UNCHECKED, "missing"), (state, cause))
        self._run(pf, self._file(d, p))
        self.assertFalse(pf.r.failed, "a clone nobody ever fetched was refused a boot")

    def test_origin_head_alone_is_not_enough_history(self):
        # A clone writes refs/remotes/<remote>/HEAD and its reflog without anyone ever
        # fetching a branch, so the namespace probe must not read it as evidence.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
            namespace=(0, "refs/remotes/origin/HEAD\n", ""))
        up = pf.upstream_ref()
        self.assertFalse(up["wasTracked"], up["wasTrackedDetail"])
        self.assertEqual(pf.REF_UNCHECKED, pf.upstream_ref_state(p)[0])
        self._run(pf, self._file(d, p))
        self.assertFalse(pf.r.failed, "a never-fetched clone must still boot")

    def test_no_local_history_at_all_is_a_genuine_never_fetched_clone(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        calls = self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""), namespace=(0, "", ""))
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertFalse(up["wasTracked"], up["wasTrackedDetail"])
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]])
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_UNCHECKED, "missing"), (state, cause))
        self.assertIn("git fetch --prune origin Playerbot", detail)

    def test_an_untouched_clone_of_a_remote_without_the_ref_still_boots(self):
        # AC 6 against the repository shape `git clone` really produces, rather than the
        # stripped one `_virgin()` builds. Both clone modes carry a populated
        # refs/remotes/origin/* namespace, and before B1 that was read as a deletion.
        for single_branch in (False, True):
            with self.subTest(single_branch=single_branch):
                d = self._clone(single_branch=single_branch)
                p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
                pf = self._pf(d, verified_ref="refs/remotes/origin/Playerbot")
                up = pf.upstream_ref()
                self.assertEqual("missing", up["state"])
                self.assertFalse(up["wasTracked"], up["wasTrackedDetail"])
                origin, _ = self._run(pf, self._file(d, p))
                fails, warns = self._outcomes(pf)
                self.assertEqual("upstream", origin,
                                 "an honest clone lost the upstream exemption")
                self.assertEqual([], [r for r, _ in fails])
                self.assertIn("upstream-ref-unverified", warns)
                self.assertFalse(pf.r.failed,
                                 "a clone that has never fetched was refused a boot")

    def test_it_is_the_reflog_and_not_the_sibling_ref_that_decides(self):
        # The B1 discriminator, on real git and in one repository: the same untouched
        # clone answers 'never fetched' while its siblings are clone output, and flips to
        # 'ref-deleted' the moment a sibling acquires a reflog the way a fetch leaves it.
        d = self._clone()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Playerbot")
        self.assertFalse(pf.upstream_ref()["wasTracked"],
                         "a cloned sibling ref was read as fetch evidence")
        self._git(d, "update-ref", "refs/remotes/origin/Other", "HEAD")
        pf2 = self._pf(d, verified_ref="refs/remotes/origin/Playerbot")
        up = pf2.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("refs/remotes/origin/Other", up["wasTrackedDetail"])
        self.assertEqual("fetched", up["wasTrackedCertainty"])
        self.assertEqual("ref-deleted", pf2.upstream_ref_state(p)[2])

    def test_the_ref_deleted_message_matches_the_evidence_behind_it(self):
        # Review N4: failing closed is correct, but the operator must not be told about a
        # deletion the gate did not observe. Sibling-reflog evidence says "somebody
        # fetched"; only the ref's own reflog says it was here.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        self._write_reflog(d, "refs/remotes/origin/Playerbot")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
            namespace=(0, "refs/remotes/origin/Playerbot\n", ""),
            reflog_paths={"logs/refs/remotes/origin/Playerbot":
                          (0, ".git/logs/refs/remotes/origin/Playerbot", "")})
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-deleted", [r for r, _ in fails])
        inferred = next(m for r, m in fails if r == "upstream-ref-deleted")
        self.assertIn("never existed", inferred)
        self.assertNotIn("so it was deleted rather than never fetched", inferred)
        # Review N5: the sentence *is* the deliverable here, so it is pinned as written.
        # "never existed" alone stayed true with the old over-claim restored, which is how
        # `this clone has fetch history` -> `this clone has fetched` survived a mutation
        # run. Both halves of the honest claim are now asserted, not just its tail.
        self.assertIn("does have fetch history", inferred)
        self.assertIn("was either deleted after it arrived or never existed in this clone",
                      inferred)
        self.assertIn("this clone has fetch history", inferred)
        self.assertNotIn("this clone has fetched,", inferred)

        d2 = self._repo()
        p2 = self._commit(d2, f"{self.UP_REL}/2026_11_04_01_00.sql", self.IN_BAND)
        self._git(d2, "update-ref", "refs/remotes/origin/Playerbot", "HEAD")
        loose = d2 / ".git" / "refs" / "remotes" / "origin" / "Playerbot"
        loose.unlink()
        pf2 = self._pf(d2)
        self.assertEqual("held", pf2.upstream_ref()["wasTrackedCertainty"])
        self._run(pf2, self._file(d2, p2))
        fails2, _ = self._outcomes(pf2)
        direct = next(m for r, m in fails2 if r == "upstream-ref-deleted")
        self.assertIn("deleted rather than never fetched", direct)
        # And the *other* sentence, the one only a reflog of this very ref earns.
        self.assertIn("demonstrably had and no longer does", direct)

    def _watch_git_verbs(self, pf):
        """Record every git verb the gate runs, while delegating to real git."""
        real = pf._git
        seen = []

        def spy(argv, *args, **kwargs):
            seen.append(list(argv))
            return real(argv, *args, **kwargs)

        pf._git = spy
        return seen

    def test_the_gate_never_fetches_on_any_ref_answer(self):
        # "The genuine `git pull` case still boots offline: no fetching from inside the
        # gate" is a claim about the argv the gate passes git, so it is checked at the
        # argv boundary — and it has to cover the three PRO-291 local-history probes,
        # which are the newest way to hand the boot gate a network command by accident.
        network = {"fetch", "pull", "push", "clone", "ls-remote", "fetch-pack",
                   "http-fetch", "http-pull", "archive", "upload-pack"}
        for label, ref, prep, build in (
                ("verified", "refs/remotes/origin/Playerbot",
                 lambda d: self._git(d, "update-ref", "refs/remotes/origin/Playerbot",
                                     "HEAD"),
                 self._repo),
                ("never-fetched", "refs/remotes/origin/Absent", self._virgin,
                 self._repo),
                ("untouched-clone", "refs/remotes/origin/Playerbot",
                 lambda d: None, lambda: self._clone()),
                ("ref-deleted", "refs/remotes/origin/Playerbot",
                 lambda d: (self._git(d, "update-ref", "refs/remotes/origin/master",
                                      "HEAD"),
                            self._git(d, "update-ref", "-d",
                                      "refs/remotes/origin/Playerbot")),
                 self._repo),
                ("not-a-commit", None, lambda d: None, self._repo),
        ):
            with self.subTest(label):
                d = build()
                p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
                prep(d)
                pf = self._pf(d, verified_ref=(self._git(d, "rev-parse", "HEAD^{tree}")
                                               if ref is None else ref))
                seen = self._watch_git_verbs(pf)
                self._run(pf, self._file(d, p))
                self.assertEqual(
                    [], [c for c in seen if c and c[0] in network],
                    f"the gate reached the network while deciding {label}")
                self.assertTrue(any(c[:1] == ["rev-parse"] for c in seen),
                                "the probes never ran, so this checked nothing")
                if label == "verified":
                    self.assertFalse(pf.r.failed,
                                     "a genuine upstream pull was refused offline")

    def test_a_missing_ref_is_not_rescued_by_the_flag_when_it_never_existed(self):
        # The flag widens the deleted-ref case only; it must not become a way to silence
        # the WARN for a ref that was simply never fetched, and it must never turn a
        # FAIL from another cause into a WARN.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        pf.a.allow_missing_upstream_ref = True
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                              reflog_path=(0, ".git/logs/ABSENT", ""),
                              fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                              namespace=(0, "", ""))
        self.assertEqual((pf.REF_UNCHECKED, "missing"),
                         pf.upstream_ref_state(p)[0::2])

    def test_the_flag_does_not_rescue_a_non_commit_ref(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="abc123")
        pf.a.allow_missing_upstream_ref = True
        self._stub_git_shapes(pf, full_name=(0, "", ""), commit_peel=(1, "", ""),
                              obj=(0, "0d8a474fc67971fb3dd7616e26323d3066442555", ""),
                              obj_type=(0, "tree", ""))
        self.assertEqual(pf.REF_INDETERMINATE, pf.upstream_ref_state(p)[0])
        self._run(pf, self._file(d, p))
        self.assertTrue(pf.r.failed,
                        "--allow-missing-upstream-ref widened past the missing case")

    # -- PRO-300: the durable last-verified attestation -------------------------- #

    _NO_VALUE = object()
    ATTEST_REF = "refs/remotes/origin/Playerbot"

    def _attest(self, d, ref=None, oid=None, when="2026-11-06T09:00:00+00:00",
                repo=_NO_VALUE, url=_NO_VALUE, version=1, extra=()):
        """Write the last-verified record by hand, so each test owns exactly one field.

        Hand-written rather than produced by `write_attestation()` on purpose: a test
        that fabricates its evidence with the code under test would still pass if that
        code wrote nonsense, and the whole value of the record is that a *satisfied*
        gate wrote it.
        """
        ref = ref or self.ATTEST_REF
        pf = self._pf(d)
        path = pf.attestation_file()
        self.assertIsNotNone(path, "the gate could not place a state directory")
        path.parent.mkdir(parents=True, exist_ok=True)
        if repo is self._NO_VALUE:
            repo = str((d / ".git").resolve())
        if url is self._NO_VALUE:
            url = pf.remote_url_for_ref(ref)
        lines = [f"version {version}"]
        if repo is not None:
            lines.append(f"repo {repo}")
        if url:
            lines.append(f"remote url {url}")
        lines.extend(extra)
        lines.append(f"last-verified {ref} = "
                     f"{oid or self._git(d, 'rev-parse', 'HEAD')} @ {when}")
        path.write_text("\n".join(lines) + "\n")
        return path

    def _record_line(self, path, ref=None):
        """The `last-verified` line for `ref` in `path`, or '' when it holds none."""
        ref = ref or self.ATTEST_REF
        for line in path.read_text().splitlines():
            if line.strip().startswith(f"last-verified {ref} = "):
                return line.strip()
        return ""

    def _attest_pair(self, path, ref=None):
        """(oid, when) from the record's line for `ref`."""
        line = self._record_line(path, ref)
        self.assertIn(" @ ", line, f"no line for {ref or self.ATTEST_REF} in {path}")
        head, when = line.split(" @ ", 1)
        return head.split(" = ", 1)[1], when

    def test_a_verified_boot_records_the_ref_it_read(self):
        # The write side of PRO-300: a ref that resolved under refs/remotes/, peeled to
        # a commit and could be listed is exactly the moment worth remembering.
        d = self._repo()
        head = self._git(d, "rev-parse", "HEAD")
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertEqual("ok", up["state"])
        self.assertEqual(head, up["oid"], "the gate did not keep the commit it peeled to")
        path = pf.attestation_file()
        self.assertEqual(d / ".git" / "paperclip-preflight" / "last-verified", path)
        self.assertTrue(path.exists(), f"a satisfied boot left no record at {path}")
        body = path.read_text()
        self.assertIn("version 1", body)
        self.assertIn(f"repo {(d / '.git').resolve()}", body)
        attested, when = self._attest_pair(path)
        self.assertEqual(head, attested)
        # The timestamp has to be a real one: "verified at some point" is not a fact a
        # reviewer can check the ref against.
        self.assertRegex(when,
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\+\d\d:\d\d|Z)$",
                         f"{when} is not an ISO-8601 timestamp")

    def test_the_record_is_what_turns_a_stripped_clone_into_a_failure(self):
        # PRO-297's residual, run end to end against real git: `git update-ref -d` plus
        # `rm FETCH_HEAD` plus `rm -rf .git/logs/refs/remotes` used to leave `.git`
        # indistinguishable from a clone that never carried the ref, and the forged
        # `origin:"upstream"` file booted on the WARN. The record is the one thing in
        # the tree those three commands do not reach.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        boot = self._pf(d)
        boot.upstream_ref()
        path = boot.attestation_file()
        self.assertTrue(path.exists(), "the verified boot left nothing behind to check")
        attested, when = self._attest_pair(path)
        self._virgin(d)
        pf = self._pf(d)
        seen = self._watch_git_verbs(pf)
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertFalse(up["wasTracked"],
                         "the fingerprints the gate is supposed to have lost are still "
                         f"there: {up['wasTrackedDetail']}")
        self.assertEqual("recorded", up["attestation"])
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        rules = [r for r, _ in fails]
        self.assertIn("upstream-ref-attested-missing", rules,
                      f"the stripped clone booted on {warns}")
        self.assertNotIn("upstream-ref-unverified", warns,
                         "a ref this gate had verified was reported as never verified")
        msg = next(m for r, m in fails if r == "upstream-ref-attested-missing")
        self.assertIn(attested[:12], msg,
                      "the FAIL must name the commit it was verified at")
        self.assertIn(when, msg, "the FAIL must name when it was verified")
        self.assertIn("git fetch --prune origin Playerbot", msg)
        self.assertIn("pending_db_world", msg)
        # Review N5: this verdict is what distinguishes a stripped clone from a clone
        # that never fetched, and shortening it re-blurs exactly that. It has to name the
        # fingerprints that are missing — including a FETCH_HEAD that names *nothing*
        # fetched, which is the A1 answer — and why that rules out a never-fetched clone.
        self.assertIn("no FETCH_HEAD that names a fetched ref", msg)
        self.assertIn("no sibling reflog", msg)
        self.assertIn("has genuinely never fetched has no attestation to lose", msg)
        self.assertTrue(pf.r.failed, "the residual POC was handed a boot")
        self.assertEqual([], [c for c in seen if c[:1] == ["ls-tree"]],
                         "the gate asked a ref it does not trust for its file list")

    def test_a_stripped_real_clone_of_a_remote_that_carried_the_ref_fails_closed(self):
        # The same attack on the repository `git clone` actually builds, so the answer
        # does not depend on `_virgin()` inventing a shape git never produces: the clone
        # arrives holding the ref with no reflog (clone output), verifies once, and is
        # then stripped.
        d = self._clone(carry_ref=True)
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        boot = self._pf(d)
        self.assertEqual("ok", boot.upstream_ref()["state"])
        path = boot.attestation_file()
        self.assertTrue(path.exists(), "a verified boot inside a clone wrote no record")
        attested, when = self._attest_pair(path)
        self._virgin(d)
        pf = self._pf(d)
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attested-missing"), (state, cause),
                         detail)
        self.assertIn(attested[:12], detail)
        self.assertIn(when, detail)

    def test_a_record_copied_from_another_repo_is_not_this_clone_s_history(self):
        # The obvious way to fake a satisfied past is to copy someone else's record in.
        # The `repo` binding is what makes that a FAIL instead of an exemption, and it
        # has to hold against a real file in a real clone.
        d = self._clone(carry_ref=True)
        other = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, repo=str((other / ".git").resolve()))
        self._virgin(d)
        pf = self._pf(d)
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"), (state, cause),
                         detail)
        self.assertIn(str((other / ".git").resolve()), detail,
                      "the FAIL must name the repo the record was written for")
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-attestation-untrusted", [r for r, _ in fails])
        self.assertTrue(pf.r.failed,
                        "a borrowed record was accepted as this clone's history")

    def test_a_ref_that_is_not_there_never_writes_an_attestation(self):
        # The record is evidence *for* a later deletion check. A run that could not see
        # the ref must not manufacture one, or every missing ref would arrive pre-attested.
        d = self._repo()
        self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self.assertEqual("missing", pf.upstream_ref()["state"])
        self.assertFalse(pf.attestation_file().exists(),
                         "a run that could not resolve the ref wrote a record for it")

    def test_a_fast_forwarded_ref_is_trusted_and_the_record_moves_forward(self):
        # The false-positive this branch has to avoid: `git pull` moves a
        # remote-tracking ref forward between boots, and the earlier record must not turn
        # that into an accusation. The reflog is removed first so the only thing that
        # can clear the new oid is the ancestor check.
        d = self._repo()
        old = self._git(d, "rev-parse", "HEAD")
        boot = self._pf(d)
        boot.upstream_ref()
        path = boot.attestation_file()
        self.assertIn(f"= {old} @", self._record_line(path))
        (d / self.UP_REL / "2026_11_06_01.sql").write_text(self.IN_BAND)
        self._git(d, "add", "-A")
        self._git(d, "commit", "-qm", "upstream advance")
        new = self._git(d, "rev-parse", "HEAD")
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        __import__("shutil").rmtree(d / ".git" / "logs" / "refs" / "remotes",
                                    ignore_errors=True)
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertIsNone(up["oidMismatch"],
                          "a fast-forward was read as a forged ref move")
        state, detail, cause = pf.upstream_ref_state(
            d / self.UP_REL / "2026_01_01_00.sql")
        self.assertEqual((pf.REF_VERIFIED, "verified"), (state, cause), detail)
        self.assertIn(f"= {new} @", self._record_line(path),
                      "the record stayed on a commit the ref no longer points at")

    def test_a_forged_update_ref_move_fails_closed_as_oid_mismatch(self):
        # AC: "oid mismatch with a ref that does exist". `git update-ref` is the one
        # command that puts the tracked ref on commits of the operator's choosing, and
        # every PRO-291 check still passes afterwards because the ref *is* there.
        d = self._repo()
        boot = self._pf(d)
        boot.upstream_ref()
        path = boot.attestation_file()
        attested, when = self._attest_pair(path)
        self._git(d, "checkout", "-q", "--orphan", "forge")
        self._git(d, "commit", "-qm", "unrelated root")
        forged = self._git(d, "rev-parse", "HEAD")
        self.assertEqual(forged, self._git(d, "rev-list", "--max-parents=0", "HEAD"),
                         "the forged commit shares history with the real one")
        self._git(d, "checkout", "-q", "-f", "master")
        self._git(d, "update-ref", self.ATTEST_REF, forged)
        __import__("shutil").rmtree(d / ".git" / "logs" / "refs" / "remotes",
                                    ignore_errors=True)
        pf = self._pf(d)
        seen = self._watch_git_verbs(pf)
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_02.sql", self.IN_BAND)
        up = pf.upstream_ref()
        self.assertTrue(up["oidMismatch"], "the moved ref was trusted on its attested oid")
        self.assertFalse(up["oidMismatch"]["explained"])
        self.assertTrue(up["treeSkipped"],
                        "the gate listed the tree of a ref it cannot vouch for")
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "oid-mismatch"), (state, cause), detail)
        self.assertIn(attested[:12], detail)
        self.assertIn(when, detail)
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-oid-mismatch", [r for r, _ in fails])
        self.assertTrue(pf.r.failed)
        self.assertEqual([], [c for c in seen if c[:1] == ["ls-tree"]])

    def test_a_force_push_explains_a_moved_ref_and_the_boot_continues(self):
        # The legitimate shape on the other side of the same git answer. A real
        # force-push leaves a reflog, and a gate that failed on it would refuse every
        # contributor who rebases, so the reflog is what separates the two.
        d = self._repo()
        boot = self._pf(d)
        boot.upstream_ref()
        self._git(d, "checkout", "-q", "--orphan", "forge")
        self._git(d, "commit", "-qm", "rewritten history")
        forged = self._git(d, "rev-parse", "HEAD")
        self._git(d, "checkout", "-q", "-f", "master")
        self._git(d, "update-ref", self.ATTEST_REF, forged)
        __import__("shutil").rmtree(d / ".git" / "logs" / "refs" / "remotes",
                                    ignore_errors=True)
        self._write_reflog(d, self.ATTEST_REF)
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertTrue(up["oidMismatch"])
        self.assertTrue(up["oidMismatch"]["explained"],
                        "a ref with a reflog was not believed about its own force-push")
        self.assertFalse(up["treeSkipped"])
        state, detail, cause = pf.upstream_ref_state(
            d / self.UP_REL / "2026_01_01_00.sql")
        self.assertEqual((pf.REF_VERIFIED, "verified"), (state, cause), detail)

    def test_an_unparsable_attestation_fails_closed_rather_than_passing(self):
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        path = self._attest(d, extra=["someone wrote a sentence here"])
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        calls = self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                                      reflog_path=(0, ".git/logs/ABSENT", ""),
                                      fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                                      namespace=(0, "", ""),
                                      common_dir=(0, ".git", ""))
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertEqual("untrusted", up["attestation"], up["attestationDetail"])
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertIn("upstream-ref-attestation-untrusted", [r for r, _ in fails],
                      f"a record the gate cannot read bought the exemption: {warns}")
        self.assertNotIn("upstream-ref-unverified", warns)
        self.assertTrue(pf.r.failed)
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]])
        self.assertIn(str(path), next(m for r, m in fails
                                      if r == "upstream-ref-attestation-untrusted"))

    def test_an_attestation_of_an_unknown_version_fails_closed(self):
        # "I do not understand this file" and "this file says nothing relevant" are
        # different answers, and only one of them is a WARN.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, version=2)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                              reflog_path=(0, ".git/logs/ABSENT", ""),
                              fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                              namespace=(0, "", ""), common_dir=(0, ".git", ""))
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"), (state, cause),
                         detail)
        self.assertIn("version 2", detail)

    def test_an_attestation_for_another_remote_url_fails_closed(self):
        # Two clones of two different remotes can hold the same ref name; the record has
        # to say which one earned it. The line names the ref under verification on
        # purpose: a record that says nothing about *this* ref is `absent` whatever its
        # bindings say (review B1), and pinning the binding needs a record that does.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, ref="refs/remotes/origin/Absent",
                     url="https://someone.else/Playerbot.git")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                              reflog_path=(0, ".git/logs/ABSENT", ""),
                              fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                              namespace=(0, "", ""), common_dir=(0, ".git", ""),
                              remote_url=(0,
                                          "https://github.com/azerothcore/Playerbot.git",
                                          ""))
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"), (state, cause),
                         detail)
        self.assertIn("https://someone.else/Playerbot.git", detail)

    def test_an_attestation_without_a_line_for_this_ref_is_no_attestation(self):
        # Re-pointing verifiedRef must not inherit the old ref's record — and must not
        # be punished for it either. No line, no evidence: the WARN path stays honest.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, ref="refs/remotes/origin/Other")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                              reflog_path=(0, ".git/logs/ABSENT", ""),
                              fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                              namespace=(0, "", ""), common_dir=(0, ".git", ""))
        up = pf.upstream_ref()
        self.assertEqual("absent", up["attestation"], up["attestationDetail"])
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_UNCHECKED, "missing"), (state, cause), detail)
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual([], [r for r, _ in fails],
                         "a record for another ref refused this boot")
        self.assertIn("upstream-ref-unverified", warns)
        self.assertFalse(pf.r.failed)

    def test_an_unanswered_common_dir_probe_is_not_a_failure(self):
        # AC 6 from the other side: a gate whose `git rev-parse --git-common-dir` is
        # refused has no state directory, which is the same evidence state as having
        # never verified — never a finding, and never a crash.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        calls = self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                                      reflog_path=(0, ".git/logs/ABSENT", ""),
                                      fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                                      namespace=(0, "", ""))
        self.assertIsNone(pf.attestation_file())
        up = pf.upstream_ref()
        self.assertEqual("absent", up["attestation"], up["attestationDetail"])
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual([], [r for r, _ in fails])
        self.assertIn("upstream-ref-unverified", warns)
        self.assertFalse(pf.r.failed)
        self.assertTrue([c for c in calls if c[:2] == ["rev-parse", "--git-common-dir"]],
                        "the gate never asked where its state directory was")

    def test_the_flag_does_not_rescue_an_untrusted_attestation(self):
        # `--allow-missing-upstream-ref` opts into "the ref is not here". It says
        # nothing about "a record exists and this gate cannot read it", and letting it
        # cover that would make the flag a way to look away from a corrupt record.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, extra=["garbage"])
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        pf.a.allow_missing_upstream_ref = True
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                              reflog_path=(0, ".git/logs/ABSENT", ""),
                              fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                              namespace=(0, "", ""), common_dir=(0, ".git", ""))
        state, _detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"), (state, cause))

    def test_the_flag_resumes_a_boot_refused_only_by_the_record(self):
        # The typed opt-in covers the attested deletion the way it covers a
        # reflog-recorded one: same shape, same operator decision, same WARN.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        boot = self._pf(d)
        boot.upstream_ref()
        self.assertTrue(boot.attestation_file().exists())
        self._virgin(d)
        pf = self._pf(d)
        pf.a.allow_missing_upstream_ref = True
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_UNCHECKED, "missing-allowed"), (state, cause), detail)
        self.assertIn("--allow-missing-upstream-ref", detail)
        self.assertIn("attests that an earlier run of this gate verified", detail)

    def test_an_unanswered_ancestor_probe_fails_closed_on_a_moved_ref(self):
        # A ref on an oid the record cannot account for, with git unable to say whether
        # the attested commit is an ancestor and no reflog to call it a force-push: the
        # gate has to fail closed on the unanswered question, not read the tree anyway.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, oid="1" * 40)
        pf = self._pf(d)
        calls = self._stub_git_shapes(pf, commit_peel=(0, "2" * 40, ""),
                                      reflog_path=(0, ".git/logs/ABSENT", ""),
                                      fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                                      namespace=(0, "", ""), common_dir=(0, ".git", ""))
        up = pf.upstream_ref()
        self.assertTrue(up["oidMismatch"])
        self.assertTrue(up["oidMismatch"]["why"].startswith("git could not answer"))
        self.assertTrue(up["treeSkipped"])
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "oid-mismatch"), (state, cause), detail)
        self.assertIn("1" * 12, detail)
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]],
                         "ls-tree was asked of a ref the attestation had already disowned")

    def test_a_reflog_explains_a_moved_ref_that_git_cannot_place(self):
        # The same git refusal, and the one local fact that answers it. Without this the
        # gate would fault every rebased branch on a probe it cannot run.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, oid="1" * 40)
        self._write_reflog(d, self.ATTEST_REF)
        pf = self._pf(d)
        calls = self._stub_git_shapes(pf, commit_peel=(0, "2" * 40, ""),
                                      reflog_path=(0, ".git/logs/ABSENT", ""),
                                      reflog_paths={
                                          f"logs/{self.ATTEST_REF}":
                                              (0, ".git/logs/refs/remotes/origin"
                                                   "/Playerbot", "")},
                                      fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                                      namespace=(0, "", ""), common_dir=(0, ".git", ""),
                                      ls_tree=(0, "", ""))
        up = pf.upstream_ref()
        self.assertTrue(up["oidMismatch"]["explained"])
        self.assertFalse(up["treeSkipped"])
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual(pf.REF_UNVERIFIED, state, detail)
        self.assertEqual("absent", cause)
        fails, _ = self._outcomes(pf)
        self.assertEqual([], [r for r, _ in fails if r == "upstream-ref-oid-mismatch"])
        self.assertTrue([c for c in calls if c[:1] == ["ls-tree"]],
                        "an explained force-push was still denied a tree read")

    def test_a_record_for_another_ref_survives_a_new_attestation(self):
        # One file, many refs: a second verified ref must not erase the first one's
        # evidence, or rotating verifiedRef would silently delete history.
        d = self._repo()
        self._git(d, "update-ref", "refs/remotes/origin/Other", "HEAD")
        other = self._attest(d, ref="refs/remotes/origin/Other", oid="3" * 40,
                             when="2026-01-01T00:00:00+00:00")
        pf = self._pf(d)
        pf.upstream_ref()
        self.assertIn("last-verified refs/remotes/origin/Other = " + "3" * 40
                      + " @ 2026-01-01T00:00:00+00:00", other.read_text())
        self.assertIn(f"= {self._git(d, 'rev-parse', 'HEAD')} @",
                      self._record_line(other))

    def test_the_attestation_is_never_a_precondition_for_booting(self):
        # AC 6, stated as the invariant it is: with a state directory the gate can place
        # and no record in it, an honest never-fetched clone still boots on the WARN.
        d = self._clone()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        pf = self._pf(d)
        self.assertIsNotNone(pf.attestation_file())
        self.assertFalse(pf.attestation_file().exists(),
                         "a clone of a remote that never had the ref arrived pre-attested")
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertEqual("absent", up["attestation"])
        origin, _ = self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual("upstream", origin)
        self.assertEqual([], [r for r, _ in fails])
        self.assertIn("upstream-ref-unverified", warns)
        self.assertFalse(pf.r.failed,
                         "the attestation had become a precondition for a boot")

    def test_an_attestation_with_no_version_line_fails_closed(self):
        # A file that never says what it is cannot promise anything. Reading "no
        # version" as "nothing relevant in here" would mean a truncated write — or a
        # hand-edited one — buys back the exemption the record was blocking.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        path = self._attest(d)
        path.write_text("\n".join(
            [ln for ln in path.read_text().splitlines()
             if not ln.strip().startswith("version ")]) + "\n", encoding="utf-8")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                              reflog_path=(0, ".git/logs/ABSENT", ""),
                              fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                              namespace=(0, "", ""), common_dir=(0, ".git", ""))
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"), (state, cause),
                         detail)
        self.assertIn("version", detail)

    def test_the_summary_names_the_record_a_satisfied_boot_relied_on(self):
        # The operator has to be able to see *which* answer the ref verdict rested on:
        # a record this gate wrote, or the absence of one. A bare "verified" in the
        # summary would not survive a review of a stripped clone.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        # The ref's own commit, not HEAD: `_repo()` parked the tracked ref on the base
        # commit and `_commit()` moved HEAD past it.
        at = self._git(d, "rev-parse", self.ATTEST_REF)
        pf = self._pf(d)
        self._run(pf, self._file(d, p))
        listed = " ".join(pf.r.sections.get("4", []))
        self.assertIn(f"last-verified {at[:12]}@", listed)
        notes = [f.message for f in pf.r.findings
                 if f.severity == "INFO" and f.rule == "upstream-ref-attestation"]
        self.assertEqual(1, len(notes), f"the run did not say what it wrote: {notes}")
        self.assertIn("recorded ", notes[0])
        self.assertIn(str(pf.attestation_file()), notes[0])

    def test_the_summary_says_the_ref_was_never_verified_here(self):
        # The other half of the same sentence: an honest clone gets `absent`, not a
        # silent omission, so a reader can tell "no record" from "record not shown".
        d = self._clone()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        pf = self._pf(d)
        self._run(pf, self._file(d, p))
        listed = " ".join(pf.r.sections.get("4", []))
        self.assertIn("last-verified absent", listed)
        self.assertEqual([], [f for f in pf.r.findings
                              if f.rule == "upstream-ref-attestation"
                              and f.severity == "INFO"],
                         "a clone with no record reported having written one")

    # -- PRO-300 review: the record may refuse, never exempt ---------------------- #

    def _forge_ref_move(self, d, message="unrelated root"):
        """Move the tracked ref to an unrelated root commit and hide the reflog.

        The forged shape from the PRO-297/PRO-300 reviews: `git update-ref` puts the
        remote-tracking ref on commits of the operator's choosing, and `git update-ref`
        does create a reflog, so the file is removed to model the silent variant.
        """
        self._git(d, "checkout", "-q", "--orphan", "forge")
        self._git(d, "commit", "-qm", message)
        forged = self._git(d, "rev-parse", "HEAD")
        self._git(d, "checkout", "-q", "-f", "master")
        self._git(d, "update-ref", self.ATTEST_REF, forged)
        __import__("shutil").rmtree(d / ".git" / "logs" / "refs" / "remotes",
                                    ignore_errors=True)
        return forged

    def test_a_forged_move_behind_a_junk_record_cannot_buy_the_exemption(self):
        # PRO-300 review, finding 1, end to end on real git objects. One appended junk
        # line used to make the record behave as if it were simply absent on the path
        # that ends in a write: the forged `update-ref` move booted on the exemption and
        # this very run then stamped the forged oid as `last-verified`, while the summary
        # told the operator the ref had been verified.
        d = self._repo()
        boot = self._pf(d)
        boot.upstream_ref()
        path = boot.attestation_file()
        attested, when = self._attest_pair(path)
        forged = self._forge_ref_move(d)
        self.assertNotEqual(attested, forged)
        with path.open("a", encoding="utf-8") as fh:
            fh.write("someone wrote a sentence here\n")
        before = path.read_text()
        pf = self._pf(d)
        seen = self._watch_git_verbs(pf)
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_03.sql", self.IN_BAND)
        up = pf.upstream_ref()
        self.assertEqual("untrusted", up["attestation"], up["attestationDetail"])
        self.assertEqual("junk", up["attestationCause"],
                         "the refusal must name which check refused")
        self.assertTrue(up["attestationRefused"],
                        "an untrusted record acted as no record on the path that writes")
        self.assertTrue(up["treeSkipped"])
        self.assertEqual([], [c for c in seen if c[:1] == ["ls-tree"]],
                         "the gate listed the tree of a ref behind an untrusted record")
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"),
                         (state, cause), detail)
        self.assertIn("someone wrote a sentence here", detail)
        self.assertIn(str(path), detail)
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-attestation-untrusted", [r for r, _ in fails])
        self.assertTrue(pf.r.failed, "one junk line bought back the upstream exemption")
        # The refused boot must not repair or re-stamp the record it just refused: the
        # forged oid becoming `last-verified` is the failure this test exists for.
        self.assertEqual(before, path.read_text(),
                         "a refused boot rewrote the record it refused")
        self.assertNotIn(forged, path.read_text())
        listed = " ".join(pf.r.sections.get("4", []))
        self.assertIn("last-verified refused", listed)
        self.assertIn("junk", listed)
        self.assertNotIn(f"last-verified {forged[:12]}", listed)

    def test_a_record_naming_another_repo_refuses_a_ref_that_is_otherwise_there(self):
        # The same refusal for the other shape the issue names as REF_INDETERMINATE:
        # "oid mismatch with a ref that does exist" and a borrowed record both have to
        # refuse on the satisfied path, not merely on the missing one.
        d = self._repo()
        other = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_04.sql", self.IN_BAND)
        self._attest(d, repo=str((other / ".git").resolve()))
        pf = self._pf(d)
        seen = self._watch_git_verbs(pf)
        up = pf.upstream_ref()
        self.assertEqual("ok", up["state"])
        self.assertEqual("untrusted", up["attestation"], up["attestationDetail"])
        self.assertEqual("repo", up["attestationCause"])
        self.assertTrue(up["attestationRefused"])
        self.assertEqual([], [c for c in seen if c[:1] == ["ls-tree"]],
                         "a ref behind a borrowed record was still asked for its tree")
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-attestation-untrusted", [r for r, _ in fails])
        self.assertTrue(pf.r.failed,
                        "a borrowed record was ignored because the ref happened to be there")

    def test_a_record_with_no_repo_binding_is_not_trusted(self):
        # Review N5: a record that never says which clone earned it is not one this gate
        # wrote, and `write_attestation()` now refuses to produce one for exactly this
        # reason. Compared only when git can name this tree's common dir.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, ref="refs/remotes/origin/Absent", repo=None)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                              reflog_path=(0, ".git/logs/ABSENT", ""),
                              fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                              namespace=(0, "", ""), common_dir=(0, ".git", ""))
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"), (state, cause),
                         detail)
        self.assertIn("repo", detail)

    def test_the_gate_writes_no_record_it_cannot_bind(self):
        # The write side of the same rule, and the `repo unknown` nit: an unbound record
        # is read back as untrusted, so writing one would only manufacture a FAIL.
        import tempfile
        d = self._repo()
        head = self._git(d, "rev-parse", self.ATTEST_REF)
        pf = self._pf(d)
        state_dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, state_dir, ignore_errors=True)
        pf.a.preflight_state_dir = str(state_dir)
        self._stub_git_shapes(pf, full_name=(0, self.ATTEST_REF, ""),
                              commit_peel=(0, head, ""), ls_tree=(0, "", ""))
        up = pf.upstream_ref()
        self.assertEqual("ok", up["state"])
        path = pf.attestation_file()
        self.assertEqual(state_dir / "last-verified", path)
        self.assertFalse(path.exists(),
                         "the gate wrote a record it could not bind to a repo")
        self.assertIn("not written", up["attestationNote"], up["attestationNote"])

    def test_a_record_the_os_will_not_hand_over_is_inert_on_a_ref_that_is_there(self):
        # The one untrusted shape that stays inert on the satisfied path, stated as a
        # test because it is a deliberate carve-out and not an oversight: an unreadable
        # file is an environment accident, and AC 6 says the record must never be a
        # precondition for booting. What earns the exemption there is the tree
        # comparison, which still runs and still has to match.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        boot = self._pf(d)
        boot.upstream_ref()
        path = boot.attestation_file()
        self.assertTrue(path.exists())
        path.unlink()
        path.mkdir()                      # any OSError shape will do; this one survives uid
        try:
            pf = self._pf(d)
            up = pf.upstream_ref()
            self.assertEqual("untrusted", up["attestation"], up["attestationDetail"])
            self.assertEqual("unreadable", up["attestationCause"])
            self.assertFalse(up["attestationRefused"],
                             "an unreadable record refused a boot it does not disprove")
            self.assertFalse(up["treeSkipped"])
            self.assertIn("could not write", up["attestationNote"])
            state, detail, cause = pf.upstream_ref_state(p)
            self.assertEqual((pf.REF_VERIFIED, "verified"), (state, cause), detail)
            self._run(pf, self._file(d, p))
            fails, _ = self._outcomes(pf)
            self.assertEqual([], [r for r, _ in fails],
                              "a file the OS would not read stopped a verified boot")
            self.assertFalse(pf.r.failed)
        finally:
            path.rmdir()

    def test_a_foreign_repo_line_that_names_nothing_here_is_not_an_accusation(self):
        # Review B1. A never-verified clone must not be failed by a record whose only
        # `last-verified` lines name other refs, whatever common dir that file claims:
        # the record is evidence *for* a deletion check, never a precondition for booting,
        # and `rm` must not be the operator's only way out.
        d = self._clone()
        other = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        path = self._attest(d, ref="refs/remotes/origin/Other",
                            repo=str((other / ".git").resolve()))
        self.assertIn("repo ", path.read_text())
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertEqual("absent", up["attestation"], up["attestationDetail"])
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_UNCHECKED, "missing"), (state, cause), detail)
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual([], [r for r, _ in fails],
                          f"a foreign record refused a boot it never covered: {fails}")
        self.assertIn("upstream-ref-unverified", warns)
        self.assertFalse(pf.r.failed,
                         "the attestation had become a precondition for a boot")

    def test_a_foreign_url_line_that_names_nothing_here_is_not_an_accusation(self):
        # Review B1, the other binding. Same answer, same reason.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, ref="refs/remotes/origin/Other",
                     url="https://someone.else/Playerbot.git")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                              reflog_path=(0, ".git/logs/ABSENT", ""),
                              fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                              namespace=(0, "", ""), common_dir=(0, ".git", ""),
                              remote_url=(0,
                                          "https://github.com/azerothcore/Playerbot.git",
                                          ""))
        up = pf.upstream_ref()
        self.assertEqual("absent", up["attestation"], up["attestationDetail"])
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_UNCHECKED, "missing"), (state, cause), detail)
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual([], [r for r, _ in fails],
                          f"a foreign url line refused a boot it never covered: {fails}")
        self.assertIn("upstream-ref-unverified", warns)
        self.assertFalse(pf.r.failed)

    def test_the_flag_does_not_rescue_an_oid_mismatch(self):
        # `--allow-missing-upstream-ref` is an answer to "the ref is not here". The ref
        # here *is* here, on a commit the record cannot account for, and a flag that
        # looked away from that would make the flag a way to bless a forged ref.
        d = self._repo()
        boot = self._pf(d)
        boot.upstream_ref()
        self.assertTrue(boot.attestation_file().exists())
        self._forge_ref_move(d)
        pf = self._pf(d)
        pf.a.allow_missing_upstream_ref = True
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_05.sql", self.IN_BAND)
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "oid-mismatch"), (state, cause), detail)
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-oid-mismatch", [r for r, _ in fails])
        self.assertTrue(pf.r.failed,
                        "--allow-missing-upstream-ref rescued a forged ref move")

    def test_a_blank_common_dir_answer_is_no_state_directory(self):
        # Review N3: `git rev-parse --git-common-dir` can answer successfully with
        # nothing in the answer. That is the same unanswered question as a refusal — no
        # state directory, never a guessed one — and must not become a finding.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        calls = self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                                      reflog_path=(0, ".git/logs/ABSENT", ""),
                                      fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                                      namespace=(0, "", ""), common_dir=(0, "   ", ""))
        self.assertIsNone(pf.attestation_file(),
                          "a blank common-dir answer was read as a state directory")
        self.assertEqual("absent", pf.upstream_ref()["attestation"])
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertEqual([], [r for r, _ in fails])
        self.assertIn("upstream-ref-unverified", warns)
        self.assertFalse(pf.r.failed)
        self.assertTrue([c for c in calls if c[:2] == ["rev-parse", "--git-common-dir"]])


class TestGateDoesNotEatItsCallersStdin(unittest.TestCase):
    """PRO-307 A4: check [0] handed the DB client the gate's own stdin.

    `subprocess.run(argv, input=None)` leaves the child on the caller's fd 0, so
    `docker compose exec -T ac-database mysql` drained whatever had been piped to the
    gate. Measured on the deployhost: the same script piped into `ssh host 'bash -s'`
    printed its first marker and stopped dead after the first DB check, while run as a
    file it completed. A gate that eats the rest of an operator's script is a gate that
    reports nothing at all.
    """

    def _mysql(self):
        # a real cwd: _run passes compose_root as cwd, and a child cannot start in a
        # directory that does not exist
        args = build_arg_parser().parse_args(["--compose-root", tempfile.gettempdir()])
        return MySQL(args)

    def _capture(self, stdin):
        import preflight_worlddb as P
        seen = {}

        class Proc:
            returncode, stdout, stderr = 0, b"", b""

        def spy(argv, **kw):
            seen.update(kw)
            return Proc()

        saved = P.subprocess.run
        P.subprocess.run = spy
        try:
            self._mysql()._run(["docker", "compose", "exec", "-T", "ac-database", "mysql"],
                               stdin=stdin)
        finally:
            P.subprocess.run = saved
        return seen

    def test_no_input_means_devnull_not_the_callers_stdin(self):
        import subprocess
        kw = self._capture(None)
        self.assertNotIn("input", kw,
                         "input=None is not 'no stdin', it is 'stdin the caller still owns'")
        self.assertIs(subprocess.DEVNULL, kw.get("stdin"))

    def test_the_sql_a_caller_does_pass_still_reaches_the_child(self):
        sql = b"INSERT INTO t VALUES (1);\n"
        kw = self._capture(sql)
        self.assertEqual(sql, kw.get("input"))
        self.assertNotIn("stdin", kw, "input and stdin together is a ValueError at exec time")

    def test_no_spawn_in_the_tooling_leaves_a_child_on_the_callers_stdin(self):
        # Review N3. DEVNULL had to be added call by call, so the test has to be the
        # thing that scales: scan every `subprocess.run` in ops/preflight and refuse any
        # that names neither `stdin=` nor `input=`. The next probe written here that
        # forgets is the same silent-stop bug `ssh host 'bash -s'` measured (A4).
        import ast
        bare = []
        spreads = []
        for path in sorted(HERE.glob("*.py")):
            if path.name == Path(__file__).name:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "run" or not isinstance(node.func.value, ast.Name):
                    continue
                if node.func.value.id != "subprocess":
                    continue
                named = {kw.arg for kw in node.keywords}
                where = f"{path.name}:{node.lineno}"
                if None in named:
                    spreads.append(where)
                elif "stdin" not in named and "input" not in named:
                    bare.append(where)
        self.assertEqual([], bare,
                         "a subprocess.run in ops/preflight leaves its child on the "
                         "caller's fd 0 (PRO-307 A4)")
        # The one `**`-spread call is DbRunner._run's `feed`, whose DEVNULL default the
        # two spies above pin directly. A second spread would be an unchecked spawn.
        self.assertEqual(["preflight_worlddb.py"], sorted({s.split(":")[0] for s in spreads}),
                         f"unexpected **kwargs spawn sites: {spreads}")
        self.assertEqual(1, len(spreads), f"unexpected **kwargs spawn sites: {spreads}")

    def test_a_child_with_no_sql_of_its_own_reads_an_empty_stdin_and_returns(self):
        # The regression this pins is the silent stop, not an exception: an inherited
        # fd 0 still holding the tail of a piped script makes `cat` swallow it instead
        # of answering. DEVNULL is EOF at once, so this returns.
        rc, out, err = self._mysql()._run(["cat"], timeout=15)
        self.assertEqual((0, "", ""), (rc, out, err))


class TestJsonReportPath(unittest.TestCase):
    """PRO-307 A5: an unusable --json path answered with a raw traceback."""

    def _main(self, argv):
        import preflight_worlddb as P
        err, out = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = P.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _argv(self, path):
        return ["--policy", str(HERE / "preflight-policy.json"), "--json", path]

    def test_a_missing_parent_directory_is_an_operational_error(self):
        import preflight_worlddb as P
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "not-created" / "report.json")
            code, _out, err = self._main(self._argv(path))
        self.assertEqual(P.EXIT_ERROR, code)
        self.assertIn("operational error", err)
        self.assertIn("--json", err)
        self.assertNotIn("Traceback", err)
        self.assertIn("not-created", err)

    def test_a_path_whose_parent_exists_is_not_refused_up_front(self):
        # The guard must reject the path, not the flag: with a real parent the run has
        # to get as far as the gate itself.
        import preflight_worlddb as P
        saved = P.Preflight

        def boom(*a, **k):
            raise RuntimeError("reached the gate")

        P.Preflight = boom
        try:
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RuntimeError):
                    self._main(self._argv(str(Path(tmp) / "report.json")))
        finally:
            P.Preflight = saved

    def test_the_flag_says_the_parent_has_to_exist(self):
        help_text = build_arg_parser().format_help()
        self.assertIn("--json", help_text)


class TestScanRootIsNamedHonestly(unittest.TestCase):
    """PRO-307 A3 item 2: the banner has to admit which tree the scan walked.

    `--sql-root` only feeds check 0b and the banner; the `updates_include` paths the
    file scan walks are `$`-relative to `--git-root`. PRO-303 ran the gate with
    `--git-root /mnt/data/wow-server --sql-root <worktree>/data/sql`, and the report
    printed the worktree as "sql root" while having counted 3060 files from the other
    tree — the one line that answers "which files did you actually look at" lied.
    """

    def test_the_banner_names_the_tree_the_scan_walked(self):
        from preflight_worlddb import Report, render
        report = Report()
        report.data["sql_root"] = "/worktree/data/sql"
        report.data["scanned_from"] = "/mnt/data/wow-server"
        text = render(report, build_arg_parser().parse_args(["--compose-root", "/nonexistent"]))
        self.assertIn("/mnt/data/wow-server", text)
        self.assertIn("scanned from", text)
        self.assertIn("/worktree/data/sql", text)
        # The two lines must not be interchangeable: each says what its root is for.
        self.assertIn("updates_include", text)
        self.assertIn("0b", text)

    def test_main_is_what_puts_the_scanned_tree_in_the_report(self):
        # Review N4: the test above hand-builds a Report, so deleting the one line in
        # main() that sets `scanned_from` left the suite green while the banner printed
        # an empty scan root. Drive main() and read what it printed.
        import preflight_worlddb as P
        captured = {}

        class FakePreflight:
            sql_root = "/worktree/data/sql"
            git_root = "/mnt/data/wow-server"

            def __init__(self, args, policy, report):
                captured["report"] = report

            def run(self):
                return 0

        saved = P.Preflight
        P.Preflight = FakePreflight
        try:
            err, out = io.StringIO(), io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                code = P.main(["--policy", str(HERE / "preflight-policy.json"),
                               "--compose-root", tempfile.gettempdir()])
        finally:
            P.Preflight = saved
        self.assertEqual(0, code, err.getvalue())
        self.assertEqual("/mnt/data/wow-server", captured["report"].data.get("scanned_from"),
                         "main() no longer tells the report which tree the scan walked")
        self.assertIn("scanned from", out.getvalue())
        self.assertIn("/mnt/data/wow-server", out.getvalue())
        # And it is the *banner line* that carries it, not just the data dict.
        self.assertIn("scanned from : /mnt/data/wow-server", out.getvalue())

    def test_the_two_flags_say_which_one_steers_the_scan(self):
        helps = {tuple(a.option_strings): (a.help or "")
                 for a in build_arg_parser()._actions}
        self.assertIn("does NOT choose which files are scanned", helps[("--sql-root",)])
        self.assertIn("boot-path mount check (check 0b)", helps[("--sql-root",)])
        self.assertIn("updates_include", helps[("--git-root",)])
        self.assertIn("decides which files the gate actually looked at",
                      helps[("--git-root",)])
        self.assertIn("parent directory must already exist", helps[("--json",)])


class TestPolicyAuditPinsProvenance(unittest.TestCase):
    """PRO-248 review N3: the ref the exemption rests on is a policy invariant too.

    `audit_policy_schema.py` used to check only baseTables/customTables columns, so
    deleting the whole upstreamProvenance block — or pointing verifiedRef at a branch —
    audited clean while quietly re-handing out the upstream exemption. These assert the
    audit refuses each of those, and accepts what ships.
    """

    def _problems(self, policy):
        import audit_policy_schema
        return audit_policy_schema.provenance_problems(policy)

    def test_the_shipped_policy_passes(self):
        self.assertEqual([], self._problems(POLICY))

    def test_a_missing_block_is_a_problem(self):
        policy = json.loads(json.dumps(POLICY))
        policy.pop("upstreamProvenance", None)
        problems = self._problems(policy)
        self.assertEqual(1, len(problems))
        self.assertIn("upstreamProvenance", problems[0])

    def test_an_empty_verified_ref_is_a_problem(self):
        policy = json.loads(json.dumps(POLICY))
        policy["upstreamProvenance"]["verifiedRef"] = "  "
        self.assertIn("verifiedRef", " ".join(self._problems(policy)))

    def test_a_verified_ref_outside_refs_remotes_is_a_problem(self):
        # Both shapes review B1 measured: the ambiguous short name, and a plain branch.
        for ref in ("origin/Playerbot", "Playerbot", "HEAD"):
            policy = json.loads(json.dumps(POLICY))
            policy["upstreamProvenance"]["verifiedRef"] = ref
            problems = self._problems(policy)
            self.assertEqual(1, len(problems), ref)
            self.assertIn(ref, problems[0])
            self.assertIn("refs/remotes/", problems[0])

    def test_an_empty_refresh_command_is_a_problem(self):
        policy = json.loads(json.dumps(POLICY))
        policy["upstreamProvenance"]["refreshCommand"] = ""
        self.assertIn("refreshCommand", " ".join(self._problems(policy)))

    def test_a_peeled_verified_ref_is_a_problem(self):
        # PRO-291 (PRO-248 review N1, opening 1). The gate refuses a verifiedRef that
        # resolves to a tree/blob instead of a commit, so a policy that names one must
        # not audit clean: an audit that exits 0 while the boot refuses is exactly the
        # audit/gate divergence review N5 was about. These all pass the old
        # `startswith("refs/remotes/")` test, which is why they needed their own.
        for ref in ("refs/remotes/origin/Playerbot^{tree}",
                    "refs/remotes/origin/Playerbot^{blob}",
                    "refs/remotes/origin/Playerbot~1",
                    "refs/remotes/origin/Playerbot@{0}"):
            policy = json.loads(json.dumps(POLICY))
            policy["upstreamProvenance"]["verifiedRef"] = ref
            problems = self._problems(policy)
            self.assertEqual(1, len(problems), ref)
            self.assertIn("verifiedRef", problems[0])
            self.assertIn("not-a-commit", problems[0])

    def test_an_object_name_verified_ref_is_a_problem(self):
        # A mistyped object name is the other way `^{commit}` comes back empty without
        # the ref being absent (review N1). It is not a ref, so no fetch maintains it.
        for ref in ("0d8a474fc67971fb3dd7616e26323d3066442555", "abc123"):
            policy = json.loads(json.dumps(POLICY))
            policy["upstreamProvenance"]["verifiedRef"] = ref
            problems = self._problems(policy)
            self.assertEqual(1, len(problems), ref)
            self.assertIn("verifiedRef", problems[0])
            self.assertIn("object name", problems[0])

    def test_a_non_dict_block_is_a_problem(self):
        policy = json.loads(json.dumps(POLICY))
        policy["upstreamProvenance"] = "origin/Playerbot"
        self.assertEqual(1, len(self._problems(policy)))

    # -- the depth a text check cannot reach (PRO-307 A2, A3 item 1) ---------------- #

    REF = "refs/remotes/origin/Playerbot"

    def _git(self, d, *argv):
        import subprocess
        proc = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *argv],
                              cwd=str(d), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, text=True)
        self.assertEqual(0, proc.returncode, f"git {' '.join(argv)}: {proc.stderr}")
        return proc.stdout.strip()

    def _repo(self, tmp, with_ddl=True):
        d = Path(tmp)
        if with_ddl:
            (d / "data/sql/base/db_world").mkdir(parents=True)
        (d / "README").write_text("seed\n")
        self._git(d, "init", "-q", "-b", "master", ".")
        self._git(d, "add", "-A")
        self._git(d, "commit", "-qm", "seed")
        return d

    def _prov_policy(self, ref=None):
        """The shipped policy's provenance block, with the column half taken away.

        A temp repo cannot hold the 300-odd base tables the policy claims, and column
        drift is not what these four cases are about; `upstreamProvenance` itself is
        the shipped one, unchanged.
        """
        policy = {"upstreamProvenance": json.loads(json.dumps(POLICY["upstreamProvenance"]))}
        self.assertEqual(POLICY["upstreamProvenance"], policy["upstreamProvenance"])
        if ref is not None:
            policy["upstreamProvenance"]["verifiedRef"] = ref
        return policy

    def _audit(self, repo, policy):
        import audit_policy_schema
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            pfile = Path(tmp) / "policy.json"
            pfile.write_text(json.dumps(policy))
            with contextlib.redirect_stdout(out):
                code = audit_policy_schema.main(
                    ["--repo", str(repo), "--policy", str(pfile), "--no-modules"])
        return code, out.getvalue()

    def _problems_in(self, policy, repo):
        import audit_policy_schema
        return audit_policy_schema.provenance_problems(policy, repo)

    def test_a_verified_ref_naming_a_tree_is_refused_where_a_repo_can_say_so(self):
        # PRO-303 measured this on the deployhost: a policy copy naming
        # refs/remotes/origin/pro303-drill-tree, created with
        # `git update-ref ... $(git rev-parse 194697d5d^{tree})`, made the gate refuse
        # the boot as `upstream-ref-not-a-commit` while the audit on the same repo and
        # the same file printed "0 problem(s)" and exited 0. That is the divergence
        # f96c40313 was written to close, and the `_comment` claiming the auditor
        # "exits 1 on all three" was untrue for this shape.
        with tempfile.TemporaryDirectory() as tmp:
            d = self._repo(tmp)
            tree = self._git(d, "rev-parse", "HEAD^{tree}")
            self.assertEqual("tree", self._git(d, "cat-file", "-t", tree))
            self._git(d, "update-ref", self.REF, tree)
            policy = self._prov_policy()
            self.assertEqual([], self._problems(policy),
                             "the shape check alone still passes it — that is the bug")
            problems = self._problems_in(policy, d)
            self.assertEqual(1, len(problems), problems)
            self.assertIn("not a commit", problems[0])
            self.assertIn(tree[:12], problems[0])
            self.assertIn("not-a-commit", problems[0])
            code, out = self._audit(d, policy)
            self.assertEqual(1, code, out)
            self.assertIn("RESOLVED", out)
            self.assertIn("not a commit", out)
            self.assertNotIn("0 problem(s)", out)

    def test_a_verified_ref_deleted_from_the_clone_is_refused_where_a_repo_can_say_so(self):
        # PRO-307 A3 item 1: the README said the auditor exits 1 when verifiedRef is
        # "deleted", and it only ever meant a deleted *field*. With the unmodified
        # shipped policy and a clone that no longer carries the ref, the gate refused
        # the boot as upstream-ref-deleted while the audit exited 0.
        with tempfile.TemporaryDirectory() as tmp:
            d = self._repo(tmp)
            self.assertEqual([], self._problems(POLICY))
            problems = self._problems_in(POLICY, d)
            self.assertEqual(1, len(problems), problems)
            self.assertIn("upstream-ref-deleted", problems[0])
            self.assertIn(POLICY["upstreamProvenance"]["refreshCommand"], problems[0])
            code, out = self._audit(d, self._prov_policy())
            self.assertEqual(1, code, out)
            self.assertIn("upstream-ref-deleted", out)

    def test_a_verified_ref_that_peels_to_a_commit_audits_clean_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._repo(tmp)
            self._git(d, "update-ref", self.REF, self._git(d, "rev-parse", "HEAD"))
            code, out = self._audit(d, self._prov_policy())
            self.assertEqual(0, code, out)
            self.assertIn("RESOLVED", out)
            self.assertIn("0 problem(s)", out)
            self.assertNotIn("shape-only pass does not mean", out)

    def test_the_output_says_which_depth_the_provenance_check_ran(self):
        # "0 problem(s)" must never be readable as more than it means: with no repo the
        # audit has only ever read the name.
        import audit_policy_schema
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "tree"
            plain.mkdir()
            (plain / "data/sql/base/db_world").mkdir(parents=True)
            self.assertEqual("shape-only",
                             audit_policy_schema.provenance_mode(POLICY, None)[0])
            self.assertEqual("shape-only", audit_policy_schema.provenance_mode(POLICY, plain)[0])
            code, out = self._audit(plain, self._prov_policy())
            self.assertEqual(0, code, out)
            self.assertIn("SHAPE-ONLY", out)
            self.assertIn("does not mean verifiedRef resolves", out)
            d = self._repo(str(Path(tmp) / "git"))
            self.assertEqual("resolved", audit_policy_schema.provenance_mode(POLICY, d)[0])

    def test_an_unanswerable_object_type_is_not_reported_as_a_non_commit_object(self):
        # Review N1. `cat-file -t` can fail to answer at all — no git on PATH, a timeout,
        # a corrupt object store. The old fall-through still printed "which is a <kind>,
        # not a commit", asserting a fact about the object this run never established: the
        # same over-claim class PRO-307 exists to close, one layer down in the audit.
        import audit_policy_schema
        policy = self._prov_policy()
        answers = [(1, "", "fatal: Needed a single revision"),
                   (0, "0d8a474fc67971fb3dd7616e26323d3066442555", ""),
                   (None, "", "git: cat-file: not found")]

        def fake_git(repo, argv):
            return answers.pop(0) if answers else (0, "", "")

        saved = audit_policy_schema._git
        audit_policy_schema._git = fake_git
        try:
            problems = audit_policy_schema.resolve_provenance_problems(policy, Path("/repo"))
        finally:
            audit_policy_schema._git = saved
        self.assertEqual(1, len(problems), problems)
        self.assertIn("cannot be determined", problems[0])
        self.assertIn("git: cat-file: not found", problems[0])
        self.assertIn("the gate fails closed on the same question", problems[0])
        self.assertNotIn("non-commit object", problems[0])
        self.assertNotIn("which is a", problems[0])

    def test_the_depth_it_prints_is_the_depth_that_actually_ran(self):
        # Review N2. provenance_mode() and the resolution half each called repo_is_git()
        # for themselves, so the two could disagree and the run could print RESOLVED while
        # the half that peels verifiedRef had decided there was no repo and returned no
        # problems at all — "0 problem(s)" read as a resolved pass that never happened.
        import audit_policy_schema
        with tempfile.TemporaryDirectory() as tmp:
            d = self._repo(tmp)
            self._git(d, "update-ref", self.REF, self._git(d, "rev-parse", "HEAD"))
            probed = []
            real = audit_policy_schema.repo_is_git

            def counting(repo):
                probed.append(repo)
                return real(repo)

            saved = audit_policy_schema.repo_is_git
            audit_policy_schema.repo_is_git = counting
            try:
                code, out = self._audit(d, self._prov_policy())
            finally:
                audit_policy_schema.repo_is_git = saved
            self.assertEqual(0, code, out)
            self.assertIn("RESOLVED", out)
            self.assertEqual(1, len(probed),
                             f"the audit asked {len(probed)} times whether {d} is a git "
                             f"tree; the depth it prints and the depth that ran have to "
                             f"be the same single answer")

    def test_the_shared_answer_also_holds_when_the_tree_is_not_git(self):
        # Review N2, the other direction: one answer is only honest if a false one keeps
        # both halves shape-only rather than leaving the resolution half to guess.
        import audit_policy_schema
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "tree"
            (plain / "data/sql/base/db_world").mkdir(parents=True)
            probed = []
            saved = audit_policy_schema.repo_is_git
            audit_policy_schema.repo_is_git = lambda repo: probed.append(repo) or False
            try:
                code, out = self._audit(plain, self._prov_policy())
            finally:
                audit_policy_schema.repo_is_git = saved
            self.assertEqual(0, code, out)
            self.assertIn("SHAPE-ONLY", out)
            self.assertIn("0 problem(s)", out)
            self.assertEqual(1, len(probed),
                             f"{len(probed)} independent git-ness probes for one run")

    def test_a_malformed_name_is_reported_once_and_never_resolved(self):
        # The two depths must not file the same defect twice, and a name the shape
        # check already rejects is not worth a peel.
        import audit_policy_schema
        policy = json.loads(json.dumps(POLICY))
        policy["upstreamProvenance"]["verifiedRef"] = "Playerbot"
        with tempfile.TemporaryDirectory() as tmp:
            d = self._repo(tmp)
            problems = self._problems_in(policy, d)
            self.assertEqual(1, len(problems), problems)
            self.assertIn("refs/remotes/", problems[0])
            self.assertEqual("shape-only", audit_policy_schema.provenance_mode(policy, d)[0])

def targets_for(sql: str, known_names: set[str] | None = None) -> list[tuple[str | None, str]]:
    """The write targets a rejected region names, for assertions on the name scan."""
    text, offset = split_statements(sql)[0]
    masked = mask_sql(text)
    return write_targets(parse_statement(text, masked, offset), known_names)[0]


class TestRev5RegionCoordinates(unittest.TestCase):
    """PRO-179 rev-5/rev-6: the name scan and the text it slices must agree.

    NB-1 (a PARTITION clause blanked to a shorter string) and NB-2 (clause keywords
    located in text that still contains backticked content) are one defect seen twice:
    the fragment bounds `_region_tables()` works in have to be offsets into the same
    text it slices. So the headline POC below is a single statement carrying both, and
    it only passes if neither defect comes back.

    Read the direction of these changes correctly. Nothing here can turn a *passing*
    file into a failing one — `multi-table-write` is raised before the scan is ever
    consulted. What rank 2 changed runs the other way: locating the clause keywords in
    backtick-blanked text moved statements that the parent commit refused into the
    accepted set, because a backticked alias spelled `set` or `where` used to be read as
    the clause keyword and truncated the write region. `test_a_backticked_alias_...`
    below is written to fail against the parent commit for exactly that reason; the
    `DELETE `from` FROM ...` form is not, since it earns the same two rules either way,
    and is kept only as the report-fidelity half of the change.
    """

    COMBINED_POC = ("DELETE `from`, `c` FROM `updates` PARTITION (p1,p2,p3) `from` "
                    "JOIN `creature` `c` WHERE `from`.`hash` = 'x';")

    def test_partition_blank_and_backticked_keyword_together(self):
        # NB-1 + NB-2 in one statement: the PARTITION blank must not move the text
        # under the offsets, and `from` must not be read as the FROM keyword.
        self.assertEqual([(None, "updates"), (None, "creature")], targets_for(self.COMBINED_POC))
        found = findings_for(self.COMBINED_POC)
        rules = [r for r, _ in found]
        self.assertIn("multi-table-write", rules)
        self.assertIn("ledger-table", rules, "`updates` was not named as a write target")
        self.assertEqual([], [m for r, m in found if r == "table-not-allowed"],
                          "partition names were blamed as write targets")
        msg = next(m for r, m in found if r == "multi-table-write")
        self.assertIn("before FROM", msg, "the delete-target list was not recognised as such")
        # Every name in the table list is quoted, so the bare-scan label does not
        # belong on it even though the partition names are in the noise list.
        self.assertIn("tables named: creature, updates", msg)
        self.assertNotIn("heuristic", msg)

    def test_partition_clause_alone_keeps_the_ledger_in_the_table_position(self):
        sql = ("DELETE `u`, `c` FROM `updates` PARTITION (p1,p2,p3) `u` JOIN `creature` `c` "
               "WHERE `u`.`hash` = 'x';")
        self.assertEqual([(None, "updates"), (None, "creature")], targets_for(sql))
        self.assertIn("ledger-table", rules_for(sql))

    def test_a_backticked_alias_spelled_like_a_clause_keyword_is_accepted(self):
        # NB-2, and the case that actually pins it: the parent commit located FROM/WHERE
        # in text that still held the backticked name, so the alias `where` became the
        # WHERE keyword, the region was truncated to `creature` ``, and a legal
        # single-table DELETE was refused as malformed. Against that commit this
        # assertion fails with `multi-table-write`; the `DELETE `from` FROM ...` form
        # above does not distinguish the two, so it is not the regression test.
        self.assertEqual([], rules_for("DELETE FROM `creature` `where` WHERE `guid` = 9002020;"))
        self.assertEqual([], rules_for(
            "DELETE FROM `acore_world`.`creature_template` `from` WHERE `entry` = 900200;"))
        # The sibling forms: still refused, but for the real reason now.
        self.assertIn("unbounded-write", rules_for(
            "UPDATE `creature` `set` SET `npcflag` = 3 WHERE `entry` = 900200;"))
        self.assertIn("table-not-allowed", rules_for(
            "DELETE FROM `where` WHERE `guid` = 9002020;"),
            "a table really named `where` must still be named, not skipped as a keyword")

    def test_a_backticked_keyword_is_never_read_as_a_clause_keyword(self):
        # Report fidelity for the same location fix: the delete-target list is still
        # recognised as one, and the ledger is still named out of it.
        found = findings_for("DELETE `from` FROM `updates` `from`;")
        rules = [r for r, _ in found]
        self.assertIn("multi-table-write", rules)
        self.assertIn("ledger-table", rules)
        self.assertIn("before FROM", next(m for r, m in found if r == "multi-table-write"))
        self.assertEqual([(None, "updates")], targets_for("DELETE `from` FROM `updates` `from`;"))

    def test_a_bare_qualified_name_does_not_invent_a_schema(self):
        # NB-3: `a.b.c` is three bare tokens to the scan. Believing the first half was
        # a schema accused a file that never names a schema of writing outside it.
        sql = "UPDATE a.b.c JOIN creature c SET c.guid = 1 WHERE c.guid = 2;"
        rules = rules_for(sql)
        self.assertIn("multi-table-write", rules)
        self.assertIn("table-not-allowed", rules, "the table the region names was not checked")
        self.assertNotIn("cross-schema-write", rules)
        msg = next(m for r, m in findings_for(sql) if r == "multi-table-write")
        self.assertIn("tables named (heuristic — unquoted region)", msg)

    def test_a_quoted_qualified_name_is_still_believed(self):
        self.assertIn("cross-schema-write", rules_for(
            "UPDATE `other`.`updates` u JOIN `creature` c SET u.hash = 'x' "
            "WHERE c.guid = 9002020;"))

    def test_a_name_containing_a_dot_is_one_component(self):
        # PRO-179 review rank 1: `str.split(".")` cut `` `acore_world`.`my.table` `` into
        # three pieces, so the last one was not quote-balanced and a fully quoted name
        # was reported as a bare-scan guess under a mangled table name.
        self.assertEqual(["`acore_world`", "`my.table`"], _ident_parts("`acore_world`.`my.table`"))
        self.assertEqual(("acore_world", "my.table"), split_qualified("`acore_world`.`my.table`"))
        self.assertEqual("acore_world", _schema_of(_ident_parts("`acore_world`.`my.table`")))
        found = findings_for("UPDATE `acore_world`.`my.table` SET x = 1 WHERE entry = 900200;")
        rules = [r for r, _ in found]
        self.assertIn("multi-table-write", rules, "an unparsable region must still be refused")
        self.assertIn("table-not-allowed", rules)
        msg = next(m for r, m in found if r == "table-not-allowed")
        self.assertIn("'my.table'", msg, "the table name was mangled by the dot split")
        self.assertNotIn("heuristic", next(m for r, m in found if r == "multi-table-write"),
                         "a fully quoted name must not be labelled a bare-scan guess")

    def test_a_quoted_schema_is_believed_even_when_the_table_half_is_bare(self):
        # rank 1, other direction: the all-quoted test dropped `other` from
        # ``UPDATE `other`.updates u JOIN ...`` , losing the `cross-schema-write` the
        # parent commit reported. Belief is per component: quoted is believed, bare is
        # never read as a schema.
        self.assertIn("cross-schema-write", rules_for(
            "UPDATE `other`.updates u JOIN `creature` c SET u.hash = 'x' WHERE c.guid = 9002020;"))
        sql = "UPDATE `other`.updates u JOIN `creature` c SET u.hash = 'x' WHERE c.guid = 9002020;"
        self.assertEqual(("other", "updates"), targets_for(sql)[0],
                         "the quoted half of the name is the schema the file wrote")
        self.assertIsNone(_schema_of(["other", "`updates`"]),
                          "a bare qualifier is never believed")

    def test_both_name_lists_carry_the_heuristic_label_together(self):
        # PRO-179 review rank 4: the table list and `other names in region` come out of
        # the same bare scan, so labelling only the first let the second read as fact.
        sql = "UPDATE a.b.c JOIN creature c SET c.guid = 1 WHERE c.guid = 2;"
        msg = next(m for r, m in findings_for(sql) if r == "multi-table-write")
        self.assertIn("tables named (heuristic — unquoted region):", msg)
        self.assertIn("other names in region (heuristic — unquoted region):", msg)
        # And neither carries it when every name was quoted.
        quoted = next(m for r, m in findings_for(
            "DELETE `u`, `c` FROM `updates` PARTITION (p1,p2,p3) `u` JOIN `creature` `c` "
            "WHERE `u`.`hash` = 'x';") if r == "multi-table-write")
        self.assertNotIn("heuristic", quoted)
        self.assertIn("other names in region:", quoted)

    def test_a_policy_table_named_like_a_keyword_is_still_named(self):
        # NB-4: the stopword list removes syntax noise where a bare name is expected,
        # so a real table called `duplicate` used to disappear from the report. The
        # caller hands the scan the policy and ledger names, which are never noise.
        sql = "UPDATE duplicate JOIN creature c SET c.guid = 1 WHERE c.guid = 2;"
        self.assertEqual([(None, "creature")], targets_for(sql))
        self.assertEqual([(None, "duplicate"), (None, "creature")],
                         targets_for(sql, {"duplicate"}))

    def test_no_current_policy_table_collides_with_a_stopword(self):
        # The escape hatch above should stay unused: a name collision is tolerable
        # because it is handled, but it should not be invisible either.
        names = {k.lower() for k in POLICY["baseTables"]} | {
            k.lower() for k in POLICY["customTables"] if not k.startswith("_")}
        self.assertEqual(set(), names & set(REGION_NAME_STOPWORDS),
                         "a policy table spells a keyword; the region scan now depends on "
                         "known_names to keep it visible")

    def test_targets_come_back_in_order_of_first_appearance(self):
        # NB-5: the docstring promises first-appearance order, and pre-FROM delete
        # targets used to be appended after every FROM-list table.
        self.assertEqual([(None, "creature"), (None, "updates")],
                         targets_for("DELETE creature FROM updates u JOIN creature c;"))


class TestRev5RegionScanImpliesRefusal(unittest.TestCase):
    """PRO-179 rev-5 NB-6: the name scan exists only because multi-table writes are refused.

    `_region_tables()` is the report text for a statement `_policy_multi_table()` has
    already rejected — it is not a second way to pass, and it is never consulted for a
    write the gate accepts. If a future change lets a multi-table write through while
    still consulting the heuristic, that is a policy change and this test names it as
    one instead of letting the path quietly acquire a second meaning.
    """

    REJECTED = [
        "UPDATE `creature` c JOIN `updates` u SET u.hash = 'x' WHERE c.guid = 9002020;",
        "UPDATE `creature` c, `creature_template` d SET c.npcflag = 3 WHERE c.guid = 9002020;",
        "UPDATE (SELECT 1) SET `npcflag` = 3 WHERE `entry` = 900200;",
        "UPDATE `creature_template` USE INDEX (PRIMARY) SET `npcflag` = 3 WHERE `entry` = 900200;",
        "DELETE c, s FROM `creature` c JOIN `smart_scripts` s WHERE c.guid = 9002020;",
        "DELETE updates, creature FROM updates u JOIN creature c;",
        "DELETE `u`, `c` FROM `updates` `u` JOIN `creature` `c`;",
        "DELETE `from`, `c` FROM `updates` PARTITION (p1,p2,p3) `from` JOIN `creature` `c` "
        "WHERE `from`.`hash` = 'x';",
        "UPDATE a.b.c JOIN creature c SET c.guid = 1 WHERE c.guid = 2;",
        "UPDATE `acore_world`.`updates` `u` JOIN `creature` `c` SET u.hash = 'x' "
        "WHERE c.guid = 9002020;",
        # Both tables are allow-listed custom tables, so this is the entry that used to be
        # missing: a change that stopped refusing a multi-table write once every table it
        # names is company-approved — the plausible "custom tables are ours, let them
        # through" edit — would have left every other literal here still green.
        "UPDATE `custom_class_template` a JOIN `unbound_class_catalog` b "
        "SET a.label = b.name;",
    ]
    ACCEPTED = [
        "UPDATE `creature_template` c SET c.npcflag = 3 WHERE c.entry = 900200;",
        "UPDATE`smart_scripts` SET `action_param2` = 2 WHERE `source_type` = 0 AND `id` = 0 "
        "AND `entryorguid` IN (9002020, 9002021);",
        "DELETE FROM `creature` PARTITION (p1) WHERE `guid` = 9002020;",
        "DELETE FROM `acore_world`.`creature_template` WHERE `entry` = 900200;",
        "DELETE FROM `creature` WHERE `guid` = 9002020 ORDER BY `guid` LIMIT 1;",
    ]

    def test_the_heuristic_is_only_ever_reached_on_a_refusal(self):
        import preflight_worlddb as pw
        seen: list[str] = []
        real = pw._region_tables

        def spy(masked, lo, hi, from_end=None, known_names=None):
            seen.append(masked[lo:hi])
            return real(masked, lo, hi, from_end, known_names)

        pw._region_tables = spy
        try:
            for sql in self.REJECTED:
                seen.clear()
                rules = rules_for(sql)
                self.assertTrue(seen, f"{sql!r} was expected to consult the region scan")
                self.assertIn("multi-table-write", rules,
                              f"{sql!r} consulted the region scan but was never refused")
            for sql in self.ACCEPTED:
                seen.clear()
                self.assertEqual([], rules_for(sql), f"{sql!r} should pass")
                self.assertEqual([], seen, f"{sql!r} passed but consulted the region scan")
        finally:
            pw._region_tables = real


if __name__ == "__main__":
    unittest.main(verbosity=2)
