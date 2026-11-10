#!/usr/bin/env python3
"""Policy-engine tests for the PRO-179 world-DB pre-flight gate.

These run without MySQL: they exercise the statement splitter, the comment
masker and the write-policy engine only.

    python3 ops/preflight/test_preflight_worlddb.py
"""

from __future__ import annotations

import contextlib
import difflib
import io
import json
import re
import tempfile
import unittest
from unittest import mock
from argparse import Namespace
from pathlib import Path

from preflight_worlddb import (
    MySQL, Preflight, REGION_NAME_STOPWORDS, Report, _ident_parts, _schema_of, mask_sql,
    parse_statement, split_qualified, split_statements, build_arg_parser, write_targets,
    NO_JSON_PAYLOAD_NOTE, BLANK_JSON_TARGET_NOTE,
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
        import shutil
        import tempfile
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
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

    def _sweep_with_spy(self, tmp_argv):
        """Run cs.main() with the corpus stubbed out, and say whether it was reached."""
        import corpus_sweep as cs
        swept = []
        saved = cs.build_files, cs.tally

        def build(engine, policy, workers):
            swept.append(1)
            return []

        cs.build_files = build
        cs.tally = lambda engine, files: dict(self.RESULT_CLEAN)
        out, err = io.StringIO(), io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                self._give_it_a_tree(tmp)
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cs.main(["--compose-root", tmp, *tmp_argv])
        finally:
            cs.build_files, cs.tally = saved
        return code, out.getvalue(), err.getvalue(), swept

    def test_an_unusable_out_path_is_refused_before_the_sweep_runs(self):
        # PRO-295 review N3. The In CI row quotes `$RUNNER_TEMP/corpus-sweep.json`, and
        # that variable exists only on a runner; locally it expands to a path at the
        # filesystem root. The tally was written at the very end of main(), so the
        # reviewer reproducing a red check paid for every `git log` first and then died
        # on an OSError ~4.5 minutes in.
        code, _, err, swept = self._sweep_with_spy(
            ["--out", "/no-such-dir-here/corpus-sweep.json"])
        self.assertEqual(2, code)
        self.assertEqual([], swept, "the corpus was swept before discovering there was "
                                    "nowhere to write the tally")
        self.assertIn("--out", err)
        self.assertIn("/tmp/corpus-sweep.json", err,
                      "the refusal must name a path that works locally")

    def test_a_missing_diff_baseline_is_refused_before_the_sweep_runs(self):
        code, _, err, swept = self._sweep_with_spy(
            ["--diff", "/no-such-dir-here/before.json"])
        self.assertEqual(2, code)
        self.assertEqual([], swept, "the diff is compared after the whole corpus, so a "
                                    "missing baseline has to be caught first")
        self.assertIn("--diff", err)

    def test_a_usable_out_path_still_writes_the_tally(self):
        # Positive control: the startup check must not become an unconditional refusal.
        import corpus_sweep as cs
        with tempfile.TemporaryDirectory() as tmp:
            self._give_it_a_tree(tmp)
            target = Path(tmp) / "corpus-sweep.json"
            code, _, err, _ = self._sweep_with_spy(["--out", str(target)])
            self.assertEqual(0, code, err)
            self.assertTrue(target.is_file(), "--out stopped writing the tally")
            self.assertEqual(3, json.loads(target.read_text())["files"])

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

    @staticmethod
    def _git_argv(argv):
        """An argv with any leading global `-c key=value` pairs removed.

        Kept defensive rather than load-bearing: `dirty_paths()` asks for raw paths with
        `-z` now, not with a `-c` config (PRO-301 review B1), so nothing in the gate
        leads with `-c` today. A stub that read `argv[0]` would break the moment
        something does.
        """
        i = 0
        while i + 1 < len(argv) and argv[i] == "-c":
            i += 2
        return list(argv[i:])

    @classmethod
    def _git_verb(cls, argv):
        """The git verb an argv asks for, ignoring leading global `-c key=value`.

        PRO-301 review N2: a stub or a spy that reads `argv[0]` as the verb can stop
        seeing `status` at all — which is how a naive matcher silently turns "git
        refused" into "the work tree is dirty" and, worse, how the no-network assertion
        stops covering the very call it was extended to cover. This is only the verb
        *parser*; the no-network check itself is an allowlist (review N3), because a
        leading `-C`/`--git-dir=`/`--exec-path=` would hide the verb from anything.
        """
        rest = cls._git_argv(argv)
        return rest[0] if rest else ""

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
            argv = self._git_argv(argv)
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

    def test_a_fetch_head_that_is_not_a_file_is_named_as_one_without_a_byte_count(self):
        # Review N7 asked that "unreadable" not borrow the sentence of "absent"; review
        # NB-4 noticed this fixture answers it with a directory, and a directory has a
        # size — the old wording told the operator FETCH_HEAD "holds 2 byte(s)" about a
        # thing that holds no bytes at all. So: name what it is, say the gate cannot
        # read a record out of it, and quote no size. The genuinely unreadable *file*
        # keeps the byte count in the test below.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_03.sql", self.IN_BAND)
        (d / ".git" / "FETCH_HEAD").mkdir()
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._only_fetch_head(pf, ".git/FETCH_HEAD")
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"],
                        "a FETCH_HEAD that is not a file was answered as no FETCH_HEAD")
        self.assertIn("cannot read", up["wasTrackedDetail"])
        self.assertIn("regular file", up["wasTrackedDetail"])
        self.assertNotIn("byte(s)", up["wasTrackedDetail"],
                         "a byte count was quoted for a thing that is not a file (NB-4)")
        self.assertNotIn("so a fetch completed", up["wasTrackedDetail"])
        self.assertNotIn("do not parse", up["wasTrackedDetail"])
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertIn("upstream-ref-deleted", [r for r, _ in fails],
                      f"the unreadable record was treated as no evidence: {warns}")

    def test_a_fetch_head_that_exists_but_cannot_be_read_keeps_its_byte_count(self):
        # The other half of review N7, in the shape that actually has bytes: a regular
        # file the OS refuses to hand over is content somebody put there. It is still
        # evidence the never-fetched clone does not have — the boot still fails closed —
        # but the gate says it could not read it rather than claiming a fetch.
        import os
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_03b.sql", self.IN_BAND)
        fh = d / ".git" / "FETCH_HEAD"
        fh.write_bytes(b"0d8a474e1b2c3d4e5f60718293a4b5c6d7e8f901\t\tbranch 'x'\n")
        os.chmod(fh, 0)
        self.addCleanup(os.chmod, fh, 0o644)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._only_fetch_head(pf, ".git/FETCH_HEAD")
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("cannot read", up["wasTrackedDetail"])
        self.assertIn("byte(s)", up["wasTrackedDetail"])
        self.assertNotIn("so a fetch completed", up["wasTrackedDetail"])
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-deleted", [r for r, _ in fails])

    def test_an_empty_fetch_head_the_os_will_not_read_is_still_no_evidence(self):
        # Review NB-1: the `st_size == 0` carve-out inside the unreadable branch is
        # load-bearing and nothing pinned it, so deleting it left all the other FETCH_HEAD
        # tests green while a never-fetched clone was refused for a deletion nobody made.
        # A 0-byte FETCH_HEAD this process cannot open is the same fact as one it can:
        # nothing was ever fetched. The unparsable branch above cannot catch this shape —
        # the file is empty, not unreadable-and-non-empty — so it needs its own case.
        import os
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_03c.sql", self.IN_BAND)
        fh = d / ".git" / "FETCH_HEAD"
        fh.write_bytes(b"")
        os.chmod(fh, 0)
        self.addCleanup(os.chmod, fh, 0o644)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._only_fetch_head(pf, ".git/FETCH_HEAD")
        up = pf.upstream_ref()
        self.assertFalse(up["wasTracked"],
                         f"an empty unreadable FETCH_HEAD became evidence: {up}")
        self.assertEqual("", up["wasTrackedDetail"])
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertNotIn("upstream-ref-deleted", [r for r, _ in fails])
        self.assertIn("upstream-ref-unverified", warns)
        self.assertFalse(pf.r.failed,
                         "the boot was refused on the strength of an empty unreadable file")

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

    # -- PRO-301 review N1: a reflog that holds nothing observed nothing ----------- #

    def _empty_own_reflog(self, d, refname):
        """Leave a 0-byte `logs/<refname>` — a file, but not a record.

        `git update-ref` creates the file and a fetch that changes nothing leaves it
        empty, which is the shape the review named: `os.path.exists()` answers yes to
        it, so presence keeps failing closed while the sentence about a deletion has
        to fall away.
        """
        logs = d / ".git" / "logs" / refname
        logs.parent.mkdir(parents=True, exist_ok=True)
        logs.write_bytes(b"")
        self.assertEqual(0, logs.stat().st_size)
        return logs

    def test_a_zero_byte_reflog_still_fails_closed(self):
        # The fail-closed half of the nit: an empty reflog is still a file a clone that
        # never fetched does not have, so the boot stays refused.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        calls = self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/refs/remotes/origin/Absent", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""), namespace=(0, "", ""))
        self._empty_own_reflog(d, "refs/remotes/origin/Absent")
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertTrue(up["wasTracked"],
                        "a 0-byte reflog was read as a clone that never fetched")
        self.assertEqual([], [c for c in calls if c[:1] == ["ls-tree"]])
        state, _detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "ref-deleted"), (state, cause))
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-deleted", [r for r, _ in fails])
        self.assertTrue(pf.r.failed, "a 0-byte reflog let an upstream file keep the "
                                     "exemption")

    def test_a_zero_byte_reflog_does_not_assert_a_deletion(self):
        # The wording half, on the same shape: nothing was ever written in that file,
        # so the gate does not get to say it watched the ref go away.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/refs/remotes/origin/Absent", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""), namespace=(0, "", ""))
        self._empty_own_reflog(d, "refs/remotes/origin/Absent")
        up = pf.upstream_ref()
        self.assertNotEqual("held", up["wasTrackedCertainty"],
                            "an empty reflog was reported as a record of the ref")
        _state, detail, _cause = pf.upstream_ref_state(p)
        self.assertNotIn("deleted rather than never fetched", detail)
        self._run(pf, self._file(d, p))
        msg = next(m for r, m in self._outcomes(pf)[0] if r == "upstream-ref-deleted")
        self.assertNotIn("so it was deleted rather than never fetched", msg)
        self.assertIn("records nothing", msg)

    def test_only_a_reflog_with_an_entry_in_it_asserts_a_deletion(self):
        # The discriminator the two above share: byte-identical stub, one byte of
        # difference in the file. Without this twin the empty-reflog test would also
        # pass in a build that simply dropped the own-reflog signal altogether.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/refs/remotes/origin/Absent", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""), namespace=(0, "", ""))
        self._write_reflog(d, "refs/remotes/origin/Absent")
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertEqual("held", up["wasTrackedCertainty"])
        _state, detail, _cause = pf.upstream_ref_state(p)
        self.assertIn("deleted rather than never fetched", detail)

    def test_an_empty_sibling_reflog_fails_closed_without_claiming_a_fetch(self):
        # The same nit one level down: a sibling's 0-byte reflog is the sibling's empty
        # file, not the sibling's fetch. It still withdraws the exemption; it does not
        # earn the sentence "this clone has fetched".
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
            namespace=(0, "refs/remotes/origin/Playerbot\n", ""),
            reflog_paths={"logs/refs/remotes/origin/Playerbot":
                          (0, ".git/logs/refs/remotes/origin/Playerbot", "")})
        self._empty_own_reflog(d, "refs/remotes/origin/Playerbot")
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertEqual("unobserved", up["wasTrackedCertainty"])
        self.assertEqual("ref-deleted", pf.upstream_ref_state(p)[2])
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertTrue(pf.r.failed)
        msg = next(m for r, m in fails if r == "upstream-ref-deleted")
        self.assertNotIn("this clone has fetched", msg)

    def test_an_empty_own_reflog_with_a_real_fetch_head_stays_fetched(self):
        # PRO-301 review N5, the twin the ladder needs: the own reflog holds nothing but
        # FETCH_HEAD does, so certainty must climb to 'fetched' and must not be dragged
        # down to 'unobserved' by the empty file beside it. Review N1 is the other half
        # of this test — on that tier the gate has a reflog *file* and no reflog *entry*,
        # and the sentence has to say the second thing while the evidence list names the
        # first.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/refs/remotes/origin/Absent", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD", ""), namespace=(0, "", ""))
        self._empty_own_reflog(d, "refs/remotes/origin/Absent")
        (d / ".git" / "FETCH_HEAD").write_text(
            "0d8a474e1b2c3d4e5f60718293a4b5c6d7e8f901\t\tbranch 'Playerbot' of origin\n")
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertEqual("fetched", up["wasTrackedCertainty"],
                         "a real FETCH_HEAD was over-corrected by the empty reflog "
                         "beside it")
        self.assertEqual("ref-deleted", pf.upstream_ref_state(p)[2])
        _state, detail, _cause = pf.upstream_ref_state(p)
        self.assertIn("holds no entries", detail,
                      "the evidence list stopped naming the empty reflog file")
        self.assertIn("no reflog entry of its own", detail)
        self.assertNotIn("has no reflog of its own", detail,
                         "the sentence contradicts the evidence beside it")
        self._run(pf, self._file(d, p))
        self.assertTrue(pf.r.failed)

    def test_a_zero_byte_fetch_head_is_no_evidence_at_all(self):
        # PRO-307 A1, and a deliberate correction of PRO-301 review N2's shape. N2 kept
        # the 0-byte FETCH_HEAD as a trace that fails closed, but the shape is not one a
        # successful fetch ever leaves: `git fetch` opens FETCH_HEAD and truncates it
        # before transport has produced anything, so `GIT_ALLOW_PROTOCOL=none git fetch
        # origin Playerbot` exits 128 and leaves exactly this file. A clone that merely
        # lost its network — an air-gapped host, a wrong remote URL, a blocked run — was
        # refused a boot and told to restore a ref it never had.
        #
        # Nothing fail-closed is lost by the WARN: the exemption is not granted on this
        # path either (the file is still treated as company content), and an operator who
        # really wanted the deletion answer never had to leave an empty file — `rm
        # .git/FETCH_HEAD` reaches the identical state today. The durable PRO-300
        # last-verified record, which those commands cannot reach, is what answers
        # "this clone did once carry the ref".
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD", ""), namespace=(0, "", ""))
        (d / ".git" / "FETCH_HEAD").write_bytes(b"")
        up = pf.upstream_ref()
        self.assertFalse(up["wasTracked"],
                         "the 0-byte FETCH_HEAD a failed fetch leaves was read as fetch "
                         "history and bought an upstream-ref-deleted refusal")
        self.assertEqual("", up["wasTrackedCertainty"],
                         "an empty file was given a certainty tier")
        self.assertNotEqual("ref-deleted", pf.upstream_ref_state(p)[2],
                            "a clone that has never fetched was refused a boot")
        self._run(pf, self._file(d, p))
        fails, warns = self._outcomes(pf)
        self.assertNotIn("upstream-ref-deleted", [r for r, _ in fails])
        self.assertIn("upstream-ref-unverified", warns)
        self.assertFalse(pf.r.failed,
                         "a failed fetch alone refused the boot")

    def test_a_reflog_that_is_not_a_regular_file_answers_that_nothing_was_observed(self):
        # PRO-301 review N4: every other probe here is a `git` call with a 60s timeout.
        # These two are bare file reads, so a fifo at `logs/<ref>` — which `open()` waits
        # on for a writer that never comes — would hang the boot gate silently while it
        # holds the single-writer lease. The alarm is the failure mode: without the
        # stat-first guard this test dies on the signal instead of on an assertion.
        import os
        import signal
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/refs/remotes/origin/Absent", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""), namespace=(0, "", ""))
        logs = d / ".git" / "logs" / "refs" / "remotes" / "origin" / "Absent"
        logs.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(str(logs))
        self.addCleanup(lambda: logs.unlink(missing_ok=True))

        def on_alarm(signum, frame):
            raise AssertionError("the reflog probe blocked on a non-regular file")

        previous = signal.signal(signal.SIGALRM, on_alarm)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.alarm(10)
        try:
            self.assertFalse(pf._file_observations(logs),
                             "a fifo was read as a record of something")
            up = pf.upstream_ref()
        finally:
            signal.alarm(0)
        self.assertTrue(up["wasTracked"],
                        "a fifo where the reflog belongs was read as a never-fetched clone")
        self.assertEqual("unobserved", up["wasTrackedCertainty"])
        self.assertEqual("ref-deleted", pf.upstream_ref_state(p)[2])

    # -- PRO-301 review N2: git quoting a path out of the dirty exemption ---------- #

    def test_a_dirty_non_ascii_path_is_not_read_as_clean(self):
        # `core.quotePath` defaults to true, so `git status --porcelain` answers
        # `"\303\251rfar.sql"` for `érfar.sql`. That string can never equal the on-disk
        # relpath, is_dirty() says clean, and the check that is supposed to withdraw the
        # upstream exemption on hand-edited bytes withdraws nothing.
        import subprocess
        d = self._repo()
        rel = f"{self.UP_REL}/2026_11_04_00_érfar.sql"
        p = self._commit(d, rel, self.IN_BAND)
        p.write_text(self.OUT_OF_BAND, encoding="utf-8")
        raw = subprocess.run(["git", "status", "--porcelain"], cwd=str(d),
                             stdout=subprocess.PIPE, text=True,
                             timeout=60).stdout
        self.assertIn("\\303\\251rfar", raw,
                      "the fixture stopped being the shape this test is about")
        self.assertNotIn("érfar", raw,
                         "git answered plainly, so nothing here tests the quoting")
        pf = self._pf(d)
        self.assertIn(rel.lower(), pf.dirty_paths())
        self.assertTrue(pf.is_dirty(p),
                        "a hand-edited non-ASCII file was reported clean")
        f = self._file(d, p)
        f["origin"], f["provenance"] = "upstream", "upstream directory and author"
        pf.check_policy([f])
        self.assertIn("dirty-worktree",
                      [x.rule for x in pf.r.findings if x.severity == "FAIL"],
                      "the exemption survived a hand-edit of upstream bytes")

    def test_a_dirty_path_git_cannot_decode_is_not_read_as_clean(self):
        # Review B2's surrogateescape reasoning on the work-tree side: the other way a
        # path goes missing between git and the comparison is a decode that replaces
        # the bytes it could not read.
        d = self._repo()
        rel = f"{self.UP_REL}/2026_11_04_00_bad\udcff_name.sql"
        p = self._commit(d, rel, self.IN_BAND)
        with open(p, "w", encoding="utf-8", errors="surrogateescape") as fh:
            fh.write(self.OUT_OF_BAND)
        pf = self._pf(d)
        self.assertIn(rel.lower(), pf.dirty_paths())
        self.assertTrue(pf.is_dirty(p),
                        "a path git cannot decode was reported clean by replacement")

    def test_the_dirty_check_is_the_call_that_asks_git_for_raw_paths(self):
        # PRO-301 review B1: the property worth pinning is "paths come back raw whatever
        # the clone's core.quotePath says", not "we pass a config flag". Plain porcelain
        # C-quotes `"` and `\` even under `-c core.quotePath=false`, so only `-z` is
        # config-independent. The stub is the discriminator: it answers `-z` with raw
        # NUL-separated fields and any other call with the quoted line real git prints
        # without `-z`, and the negative control below shows the parser does not rescue
        # that quoted line — so a regression that drops `-z` flips is_dirty() to clean
        # right here.
        d = self._repo()
        rel = f'{self.UP_REL}/2026_11_04_00_we"ird.sql'
        p = self._commit(d, rel, self.IN_BAND)
        pf = self._pf(d)
        quoted = ' M "data/sql/updates/db_world/2026_11_04_00_we\\"ird.sql"'
        calls = []

        def fake(argv, timeout=60, errors="replace"):
            calls.append(list(argv))
            if self._git_verb(argv) == "status":
                if "-z" in argv:
                    return 0, f" M {rel}\0", ""
                return 0, quoted, ""
            return 128, "", f"stub refused {argv}"

        pf._git = fake
        self.assertTrue(pf.is_dirty(p),
                        "the gate took git's quoted path as a clean work tree")
        self.assertTrue(any(self._git_verb(c) == "status" and "-z" in c
                            for c in calls),
                        "status was never asked NUL-delimited")
        self.assertFalse(any("core.quotePath" in " ".join(c) for c in calls),
                         "the fix went back to a config flag, which git ignores for "
                         "C-quoted names")
        naive = self._pf(d)
        naive._git = lambda argv, timeout=60, errors="replace": (
            (0, quoted, "") if self._git_verb(argv) == "status"
            else (128, "", f"stub refused {argv}"))
        self.assertFalse(naive.is_dirty(p),
                         "the parser rescued the quoted path, so the `-z` assertion "
                         "above would be testing the stub")

    def test_a_dirty_path_git_would_quote_is_not_read_as_clean(self):
        # PRO-301 review B1: the érfar repro above fixes the character class that
        # happened to be in the repro, not the mechanism. `core.quotePath` governs only
        # bytes >= 0x80; git still C-quotes `"`, `\`, a tab and a newline regardless of
        # it, so each name here was a hand-edited upstream file the gate called clean
        # and left exempt. Plain porcelain is the fixture guard — if git ever stops
        # quoting these, this test stops testing anything and says so.
        import subprocess
        d = self._repo()
        shapes = {
            "quote": 'we"ird.sql',
            "trailing-quote": 'tail".sql',
            "quote-at-end": 'quote_at_end.sql"',
            "space-at-end": "pad.sql ",
            "backslash": "back\\slash.sql",
            "tab": "tab\tchar.sql",
            "newline": "nl\nname.sql",
            "space": "sp ace.sql",
        }
        edited = {}
        for label, name in shapes.items():
            rel = f"{self.UP_REL}/2026_11_04_00_{name}"
            edited[label] = (rel, self._commit(d, rel, self.IN_BAND))
        for _label, (_rel, path) in edited.items():
            path.write_text(self.OUT_OF_BAND, encoding="utf-8")
        raw = subprocess.run(["git", "status", "--porcelain"], cwd=str(d),
                             stdout=subprocess.PIPE, text=True,
                             timeout=60).stdout
        self.assertIn('\\"', raw, "git stopped quoting a quote character")
        self.assertIn("\\\\", raw, "git stopped quoting a backslash")
        self.assertIn("sp ace.sql", raw, "git stopped quoting a space")
        pf = self._pf(d)
        for label, (rel, path) in edited.items():
            with self.subTest(label):
                self.assertIn(rel.lower(), pf.dirty_paths())
                self.assertTrue(pf.is_dirty(path),
                                "a hand-edited upstream file was reported clean")
                f = self._file(d, path)
                f["origin"], f["provenance"] = ("upstream",
                                                "upstream directory and author")
                pf.check_policy([f])
                self.assertIn("dirty-worktree",
                              [x.rule for x in pf.r.findings if x.severity == "FAIL"],
                              "the exemption survived a hand-edit of upstream bytes")

    def test_a_dirty_rename_is_dirty_on_both_sides_of_the_name(self):
        # Under `-z` a rename's original path is the *following* NUL field, not
        # `"old -> new"`. Parsing it the old way loses one of the two paths, and the
        # path that disappears is the one that used to be upstream content.
        d = self._repo()
        old = self._commit(d, f"{self.UP_REL}/2026_11_04_00_old name.sql", self.IN_BAND)
        self._git(d, "mv", "--", str(old.relative_to(d)),
                  f"{self.UP_REL}/2026_11_04_00_new name.sql")
        pf = self._pf(d)
        dirty = pf.dirty_paths()
        self.assertIn(f"{self.UP_REL}/2026_11_04_00_new name.sql".lower(), dirty)
        self.assertIn(f"{self.UP_REL}/2026_11_04_00_old name.sql".lower(), dirty,
                      "the rename's original path vanished from the dirty set")

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
        # PRO-301 review N3: an allowlist, not a denylist over the first token. A
        # denylist passes whatever it did not think to name, and `_git_verb` only skips
        # leading `-c` pairs — a `-C`, `--git-dir=` or `--exec-path=` prefix would hide
        # the real verb and the check would wave it through. Anything the gate has not
        # been seen to ask for is now a failure.
        allowed = {"cat-file", "for-each-ref", "hash-object", "log", "ls-tree",
                   "rev-parse", "status"}

        # PRO-300 adds one read: the record names the `remote url` it was earned against,
        # so the gate asks `git config --get remote.<name>.url` — and only when the record
        # actually carries such a line, so the boot of a remote-less clone never asks it.
        # `git config` is a local file read, not a network verb, but the whole point of an
        # allowlist is that nothing runs that this list has not seen, so the one argv shape
        # is spelled out here rather than widening the verb set to every `git config`
        # someone might later think to ask the gate for. `an_attestation_for_another_remote
        # _url_fails_closed` pins the argv it earns that place, so the allowance cannot rot
        # into a verb the gate no longer asks for.
        def off_allowlist(argv):
            verb = self._git_verb(argv)
            if verb in allowed:
                return False
            rest = self._git_argv(argv)
            if verb == "config":
                return not (len(rest) == 3 and rest[1] == "--get"
                            and rest[2].startswith("remote.")
                            and rest[2].endswith(".url"))
            if verb == "merge-base":
                # PRO-300 review NIT 7: `oid_is_ancestor()` asks git this, and it was the
                # one verb the gate is known to ask that this loop had never reached, so
                # an allowlist that simply listed `merge-base` would have been an
                # unexercised allowance. Admitted by shape instead — `--is-ancestor`
                # pinned in its own position and exactly two object names after it — and
                # pinned as *asked* by the attestation scenario below.
                return not (len(rest) == 4 and rest[1] == "--is-ancestor"
                            and rest[2] and rest[3])
            return True
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
                ("attestation-oid-mismatch", "refs/remotes/origin/Playerbot",
                 lambda d: (self._git(d, "update-ref", "refs/remotes/origin/Playerbot",
                                      "HEAD"),
                            self._attest(d),
                            self._forge_ref_move(d)),
                 self._repo),
                ("attestation-stripped", "refs/remotes/origin/Playerbot",
                 lambda d: (self._git(d, "update-ref", "refs/remotes/origin/Playerbot",
                                      "HEAD"),
                            self._attest(d),
                            self._virgin(d)),
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
                    [], [c for c in seen if off_allowlist(c)],
                    f"the gate asked git for a verb this allowlist does not cover "
                    f"while deciding {label}")
                self.assertTrue(any(c[:1] == ["rev-parse"] for c in seen),
                                "the probes never ran, so this checked nothing")
                if label == "attestation-oid-mismatch":
                    # PRO-300 review NIT 7: an allowlist entry the loop has never been
                    # seen to ask for is coverage the loop does not have. This scenario
                    # is what makes the `merge-base` allowance above mean something.
                    self.assertTrue(
                        any(self._git_verb(c) == "merge-base" for c in seen),
                        "the attestation never reached `oid_is_ancestor()`, so the "
                        "merge-base allowance was covering nothing")
                if label == "attestation-stripped":
                    self.assertIn(
                        "upstream-ref-attested-missing",
                        [x.rule for x in pf.r.findings if x.severity == "FAIL"],
                        "the record never reached the boot decision, so this scenario "
                        "exercised no attestation probe at all")
                if label == "verified":
                    self.assertFalse(pf.r.failed,
                                     "a genuine upstream pull was refused offline")
                    # The dirty exemption is the one answer in this table a mis-parsed
                    # `git status` can buy, so the argv spy has to be shown to have seen
                    # that call and not only the ref probes (PRO-301 review N2).
                    self.assertTrue(
                        any(self._git_verb(c) == "status" for c in seen),
                        "the work-tree check never reached _git, so the argv spy "
                        "covered nothing there")

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
        calls = self._stub_git_shapes(pf, commit_peel=(1, "", ""), obj=(1, "", ""),
                                      reflog_path=(0, ".git/logs/ABSENT", ""),
                                      fetch_head_path=(0, ".git/FETCH_HEAD_ABSENT", ""),
                                      namespace=(0, "", ""), common_dir=(0, ".git", ""),
                                      remote_url=(0,
                                                  "https://github.com/azerothcore/Playerbot"
                                                  ".git",
                                                  ""))
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"), (state, cause),
                         detail)
        self.assertIn("https://someone.else/Playerbot.git", detail)
        # This is the whole shape the no-network allowlist makes room for: one local
        # `config --get` of the tracked ref's remote, asked only because this record
        # carries a `remote url` line to compare it with.
        self.assertEqual([["config", "--get", "remote.origin.url"]],
                         [c for c in calls if self._git_verb(c) == "config"],
                         "the url binding was read some other way, or when it had "
                         "nothing to compare")

    def test_a_record_with_no_url_line_buys_the_gate_no_remote_read(self):
        # The counterpart of the allowance in the no-network allowlist: the gate asks git
        # for a remote url only when the record carries one to compare. A record that
        # names this ref and no url must be read without a `config` verb at all, or the
        # boot pays for a comparison that has nothing on the other side of it.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._attest(d, url="")
        pf = self._pf(d)
        head = self._git(d, "rev-parse", "HEAD")
        # `remote_url` is left refused, as it is in a clone with no remote: the one
        # question the boot is still allowed to ask is the writer's, filling the line it
        # is about to put down. A second one means the reader asked for a url the record
        # never named, which is the comparison that cannot happen.
        calls = self._stub_git_shapes(pf, commit_peel=(0, head, ""),
                                      ls_tree=(0, "", ""), common_dir=(0, ".git", ""))
        up = pf.upstream_ref()
        self.assertEqual("recorded", up["attestation"], up["attestationDetail"])
        self.assertEqual([["config", "--get", "remote.origin.url"]],
                         [c for c in calls if self._git_verb(c) == "config"],
                         "reading a record that names no `remote url` should cost no "
                         "remote question of its own")
        self.assertNotIn("remote url", pf.attestation_file().read_text(),
                         "a clone with no remote wrote a url line it cannot support")

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
        # Not merely "something about the repo": an empty binding must be reported as a
        # missing line, not as a record written for a repo named "".
        self.assertIn("names no `repo`", detail)

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


    # --- PRO-300 review: the record read itself, and the writes it feeds ---------- #

    def test_a_fifo_at_the_record_path_cannot_hang_the_boot(self):
        # PRO-300 review, finding 1 — the blocking one. The record used to be read with
        # a bare `read_text()`, and `open()` on a fifo nobody is writing to blocks in
        # the kernel: no `OSError`, so no `unreadable` cause, no timeout, and no boot —
        # a gate that never returns while it holds the single-writer lease. That is the
        # hazard PRO-301 review N4 closed for the fetch fingerprints in
        # `_file_observations()`; this reader had been left behind by the same rule the
        # README states generally. The clock is the failure mode, exactly as in the
        # PRO-301 review N4 test for the reflog: the bug is a hang rather than a wrong
        # answer, so without the stat-first guard this dies on the signal instead of on
        # an assertion.
        import os
        import signal
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        pf = self._pf(d)
        path = pf.attestation_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(str(path))
        self.addCleanup(lambda: path.unlink(missing_ok=True))

        def on_alarm(signum, frame):
            raise AssertionError(
                "the last-verified read blocked on a non-regular file")

        previous = signal.signal(signal.SIGALRM, on_alarm)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.alarm(10)
        try:
            up = pf.upstream_ref()
        finally:
            signal.alarm(0)
        # The read answered `unreadable` and stayed inert. The *status* ends up
        # `recorded`, because this satisfied run then wrote a fresh record over the fifo
        # — the finding-3 path, and the reason the cause and the detail are what carry
        # the answer here rather than the status.
        self.assertEqual("unreadable", up["attestationCause"], up["attestationDetail"])
        self.assertIn("fifo", up["attestationDetail"])
        self.assertTrue(up.get("attestationPriorUnreadable"),
                        "the record written over an unreadable one was reported as an "
                        "ordinary verified boot")
        # Inert, as AC 6 requires: what earns the boot is the tree, not the record.
        self.assertFalse(up["attestationRefused"],
                         "an unreadable record refused a boot it does not disprove")
        self.assertFalse(up.get("treeSkipped"))
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_VERIFIED, "verified"), (state, cause), detail)

    def test_a_record_replaced_after_being_unreadable_says_so(self):
        # PRO-300 review, finding 3. The README promised that a run which could not
        # read the record says so; on the path that then *writes* one it did not — the
        # status was overwritten with `recorded` and the summary printed an ordinary
        # `last-verified <oid>@<when>`, which is a sentence about a continuity this run
        # never actually read. AC 6 still forbids the FAIL. It does not buy the silence.
        import os
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        pf = self._pf(d)
        path = pf.attestation_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(str(path))
        self.addCleanup(lambda: path.unlink(missing_ok=True))
        self._run(pf, self._file(d, p))
        warns = [(x.rule, x.message) for x in pf.r.findings if x.severity == "WARN"]
        named = [m for r, m in warns if r == "upstream-ref-attestation-unreadable"]
        self.assertTrue(named,
                        f"an unreadable record was rewritten in silence: {warns}")
        self.assertIn(str(path), named[0])
        fails, _ = self._outcomes(pf)
        self.assertEqual([], [r for r, _ in fails],
                          "the WARN about an unreadable record turned into a FAIL")
        self.assertFalse(pf.r.failed)

    def test_a_multiline_remote_url_never_becomes_a_second_record_line(self):
        # PRO-300 review, finding 2. `write_attestation()` interpolated
        # `remote.<name>.url` raw, so a url holding a newline wrote a second
        # `last-verified` line the gate never decided to attest. The record then failed
        # this gate's own strict read: a self-inflicted `attestation-untrusted` FAIL on
        # a boot where the operator changed nothing, with `rm <record>` — destroying the
        # evidence — as the only printed remedy.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        smuggled = ("https://example.invalid/acro.git\nlast-verified "
                    + self.ATTEST_REF + " = " + "1" * 40 + " @ "
                    + "2020-01-01T00:00:00+00:00")
        self._git(d, "config", "remote.origin.url", smuggled)
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertEqual("ok", up["state"])
        body = pf.attestation_file().read_text()
        lines = [ln for ln in body.splitlines() if ln.startswith("last-verified")]
        self.assertEqual(1, len(lines),
                         f"the record holds a line this gate never attested: {body!r}")
        self.assertIn(f"last-verified {self.ATTEST_REF} = ", lines[0])
        self.assertNotIn("remote url", body,
                         "a url that cannot fit one line was written anyway")
        # The boot that matters: nothing has changed for the operator, so the next boot
        # must not be the one that discovers the record is unreadable to its own author.
        pf2 = self._pf(d)
        up2 = pf2.upstream_ref()
        self.assertEqual("recorded", up2["attestation"], up2["attestationDetail"])
        self.assertEqual("", up2["attestationCause"])
        self._run(pf2, self._file(d, p))
        fails, _ = self._outcomes(pf2)
        self.assertEqual([], [r for r, _ in fails],
                          f"a boot was refused by the gate's own record: {fails}")

    def test_a_record_this_gate_did_not_write_contributes_no_lines(self):
        # PRO-300 review, finding 4. The strict reader correctly answers `absent` for a
        # foreign record whose lines name only another ref and whose `repo` is another
        # clone (review B1). The *pre-write* reader used to be a second, laxer reader of
        # the same bytes and carried those foreign lines forward beneath *this* tree's
        # bindings — so "a satisfied boot starts a fresh history" was false, and after
        # a `verifiedRef` rename this clone could be failed for deleting a ref it had
        # never verified.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        other = self._repo()
        path = self._attest(d, ref="refs/remotes/origin/Other",
                            repo=str((other / ".git").resolve()))
        self.assertIn("refs/remotes/origin/Other", path.read_text())
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertEqual("recorded", up["attestation"], up["attestationDetail"])
        body = path.read_text()
        self.assertNotIn("refs/remotes/origin/Other", body,
                         "foreign lines were laundered into a record bound to this "
                         f"clone: {body!r}")
        self.assertIn(f"last-verified {self.ATTEST_REF} = ", body)
        self.assertIn(f"repo {(d / '.git').resolve()}", body)

    def test_an_unbound_record_is_untrusted_even_under_a_state_dir_override(self):
        # PRO-300 review, finding 5. `--preflight-state-dir` removes the record's
        # dependence on the common dir, and the `repo` line was required only *when git
        # could name that dir* — so with the override plus an unanswered
        # `--git-common-dir`, a file holding nothing but `version 1` and a
        # `last-verified` line was read as this clone's verified history. The line is
        # required unconditionally now, which costs nothing: `write_attestation()`
        # already refuses to emit a record without one.
        import tempfile
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        state_dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, state_dir, ignore_errors=True)
        (state_dir / "last-verified").write_text(
            f"version 1\nlast-verified {self.ATTEST_REF} = "
            f"{self._git(d, 'rev-parse', self.ATTEST_REF)} @ "
            f"2020-01-01T00:00:00+00:00\n", encoding="utf-8")
        pf = self._pf(d)
        pf.a.preflight_state_dir = str(state_dir)
        self._stub_git_shapes(pf, common_dir=(128, "", "stub"))
        status, why, _oid, _when, cause = pf.attestation_record(self.ATTEST_REF)
        self.assertEqual((pf.ATTEST_UNTRUSTED, "repo"), (status, cause), why)
        state, detail, rcause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"),
                         (state, rcause), detail)
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-attestation-untrusted", [r for r, _ in fails],
                      "a record with no binding at all bought the upstream exemption")
        self.assertTrue(pf.r.failed)

    def test_an_untrusted_record_with_no_cause_still_refuses_the_tree_read(self):
        # Review NIT 6. The ok-path refusal used to require a truthy `cause` as well as
        # the untrusted status. Every untrusted return names a cause today, so that
        # extra test guarded nothing — and it set the default the wrong way round, since
        # a future return that forgot one would have been silently inert rather than
        # refused. The mutation is made by hand precisely because nothing produces this
        # shape in the wild yet.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_06_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        pf = self._pf(d)
        seen = self._watch_git_verbs(pf)
        pf.attestation_record = lambda ref, url_ref="": (
            pf.ATTEST_UNTRUSTED, "a cause-less refusal", "", "", "")
        up = pf.upstream_ref()
        self.assertTrue(up.get("treeSkipped"),
                        "an untrusted record with no cause was treated as no record")
        self.assertTrue(up.get("attestationRefused"))
        self.assertEqual([], [c for c in seen if self._git_verb(c) == "ls-tree"],
                         "the tree of a ref the gate does not trust was read anyway")
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"),
                         (state, cause), detail)


    # -- PRO-300 review NB round: the six non-blocking findings ------------------- #

    def _pad_record(self, path, cap):
        """Grow `path` past `cap` with comment lines, leaving it otherwise parseable.

        Comments are the filler precisely because the parser skips them: the file has to
        be one this gate would happily read if it could read all of it, so the only thing
        the size can change is how much of it was seen.
        """
        import os
        with path.open("a", encoding="utf-8") as fh:
            while os.path.getsize(str(path)) <= cap:
                fh.write("# " + "x" * 60 + "\n")
        return path

    def test_the_bounded_read_distinguishes_a_prefix_from_the_whole_file(self):
        # Review NB-1. Nothing pinned the read cap, so raising `_OBSERVATION_READ_CAP`
        # (1<<16 -> 1<<24 is the obvious "fix" for a record that got refused) leaves this
        # suite green while every caller below silently stops being exercised. The cap is
        # the contract, so it is pinned at both edges: a file of exactly `cap` bytes was
        # read whole, one byte more was not.
        import os
        import tempfile
        d = self._repo()
        pf = self._pf(d)
        # The value itself, not just the comparison below it: this is the bound that
        # keeps a file someone else controls from turning into an unbounded read, and
        # every test in this class pads relative to it, so a suite that only ever asked
        # "larger than whatever the cap is now" would stay green at 1 GiB.
        self.assertEqual(1 << 16, pf._OBSERVATION_READ_CAP,
                         "the read cap was moved; that is a decision about how much a "
                         "hostile record may make this gate read, not a tuning knob")
        cap = pf._OBSERVATION_READ_CAP
        with tempfile.TemporaryDirectory() as td:
            whole = Path(td) / "whole"
            whole.write_bytes(b"#" * cap)
            self.assertEqual((os.path.getsize(str(whole)), False),
                             (whole.stat().st_size,
                              pf._read_bounded(whole)[2]),
                             "a file of exactly the cap was reported as a prefix")
            over = Path(td) / "over"
            over.write_bytes(b"#" * cap + b"\n")
            self.assertTrue(pf._read_bounded(over)[2],
                            "a file one byte past the cap was reported as the whole "
                            "file, which is the mutation NB-1 was written for")

    def test_a_record_too_large_to_read_is_not_reported_as_holding_nothing(self):
        # Review NB-1, run against the shape the README forbids. `_read_bounded()` read
        # one capped chunk and said nothing about having stopped, so a record padded past
        # the cap had its `last-verified` line land in the part nobody looked at. Every
        # reader then answered its own honest question about a prefix — "no `version`",
        # "no `last-verified` line for this ref" — and a forged `git update-ref` that a
        # readable record would have failed as `oid-mismatch` booted with
        # `attestation=recorded`, no tree skip, and no finding at all.
        import os
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_07_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        path = self._attest(d)
        self._pad_record(path, self._pf(d)._OBSERVATION_READ_CAP)
        self._forge_ref_move(d)
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertEqual((pf.ATTEST_UNTRUSTED, "unreadable"),
                         (up["attestation"], up["attestationCause"]),
                         up["attestationDetail"])
        self.assertIn("larger than", up["attestationDetail"],
                      "an over-cap record was refused without saying why")
        self.assertNotIn("holds no `last-verified` line", up["attestationDetail"],
                         "a record that was only partly read was reported as one that "
                         "holds nothing")
        # AC 6: the size of a file the gate wrote is an environment fact, not a
        # disproof, so the satisfied path stays inert exactly as `unreadable` does.
        self.assertFalse(up["attestationRefused"])
        self.assertFalse(up["treeSkipped"])
        # And it was not rewritten: rebuilding the file from the prefix that could be
        # read would drop every other ref's line, so the operator's file stands.
        self._run(pf, self._file(d, p))
        self.assertFalse(pf.r.failed,
                         "an over-cap record refused a boot AC 6 says it may not")
        self.assertGreater(path.stat().st_size, pf._OBSERVATION_READ_CAP,
                           "the record was rewritten from a prefix of itself")
        self.assertIn("was not rewritten", pf.upstream_ref()["attestationNote"],
                      "a satisfied boot quietly declined to update the record")

    def test_an_over_cap_record_still_fails_the_missing_ref_closed(self):
        # The other half of NB-1, and the half that keeps the fix honest: inert on the
        # path where the tree itself carries the boot, refusing on the path where the
        # record was the only thing standing between a stripped `.git` and the upstream
        # exemption. Otherwise "make the record long" is a clean escape hatch.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_07_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        path = self._attest(d)
        self._pad_record(path, self._pf(d)._OBSERVATION_READ_CAP)
        self._virgin(d)
        pf = self._pf(d)
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"),
                         (state, cause), detail)
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertIn("upstream-ref-attestation-untrusted", [r for r, _ in fails],
                      f"a long enough record bought back the exemption: {fails}")
        self.assertTrue(pf.r.failed)

    def test_a_short_verifiedRef_still_carries_the_remote_url_binding(self):
        # Review NB-2. `remote_url_for_ref()` parsed a remote name out of a ref only when
        # it began `refs/remotes/`, and the binding check only ever compared a url the
        # record already carried. `verifiedRef: origin/Playerbot` therefore wrote no
        # `remote url` line at all, and `git remote set-url origin
        # https://evil.invalid/...` — moving the record's remote under it — read as
        # `recorded`. The canonical name `upstream_ref()` had already resolved is
        # `fullRef`; using it for the lookup is what keeps the binding in force.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_07_00.sql", self.IN_BAND)
        url = "https://example.invalid/acro.git"
        self._git(d, "remote", "add", "origin", url)
        pf = self._pf(d, verified_ref="origin/Playerbot")
        up = pf.upstream_ref()
        self.assertEqual("ok", up["state"], up["detail"])
        self.assertTrue(up["remoteTracked"], up["remoteDetail"])
        self.assertEqual(self.ATTEST_REF, up["fullRef"],
                         "the canonical name policy spells short was not kept")
        body = pf.attestation_file().read_text()
        self.assertIn(f"remote url {url}", body,
                      "a short `verifiedRef` wrote a record with no url binding to "
                      f"compare against: {body!r}")
        pf2 = self._pf(d, verified_ref="origin/Playerbot")
        self.assertEqual("recorded", pf2.upstream_ref()["attestation"],
                         "the untouched tree was already untrusted, so the attack below "
                         "proves nothing")
        self._git(d, "remote", "set-url", "origin", "https://evil.invalid/acro.git")
        pf3 = self._pf(d, verified_ref="origin/Playerbot")
        up3 = pf3.upstream_ref()
        self.assertEqual((pf3.ATTEST_UNTRUSTED, "url"),
                         (up3["attestation"], up3["attestationCause"]),
                         up3["attestationDetail"])
        self._run(pf3, self._file(d, p))
        fails, _ = self._outcomes(pf3)
        self.assertIn("upstream-ref-attestation-untrusted", [r for r, _ in fails],
                      "the record's remote moved under it and the boot did not notice")
        self.assertTrue(pf3.r.failed)

    def test_a_dropped_url_binding_says_so_on_the_report(self):
        # Review NB-3. A url that cannot fit on one line is dropped rather than written
        # (finding 2), which is the right call — but the record then permanently carries
        # no `remote url` line, and a record with no url line is exactly the record a
        # later `git remote set-url` cannot be caught by. Section 4 said nothing, so the
        # one binding the gate had chosen not to enforce was invisible.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_07_00.sql", self.IN_BAND)
        self._git(d, "remote", "add", "origin", "https://example.invalid/acro.git")
        self._git(d, "config", "remote.origin.url",
                  "https://example.invalid/acro.git\rlast-verified "
                  + self.ATTEST_REF + " = " + "1" * 40 + " @ 2020-01-01T00:00:00+00:00")
        pf = self._pf(d)
        self._run(pf, self._file(d, p))
        infos = [(x.rule, x.message) for x in pf.r.findings if x.severity == "INFO"]
        named = [m for r, m in infos if r == "upstream-ref-attestation"]
        self.assertTrue(any("remote url" in m and "omitted" in m for m in named),
                        f"a binding the gate itself declined to write went unreported: "
                        f"{named}")
        self.assertNotIn("remote url", pf.attestation_file().read_text(),
                         "the url was written anyway, so the report line proves nothing")

    def test_a_repo_line_this_gate_cannot_compare_is_untrusted(self):
        # Review NB-4. Finding 5 required a `repo` line but compared it only `if
        # expect_repo`, so under `--preflight-state-dir` plus an unanswered
        # `--git-common-dir` a record naming somebody else's common dir was trusted
        # outright. Presence without comparability is not a satisfied binding: anyone who
        # can write this file can write any `repo` line, so the line is worth only what
        # the gate can check it against.
        import tempfile
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_07_00.sql", self.IN_BAND)
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        state_dir = Path(tempfile.mkdtemp())
        self.addCleanup(__import__("shutil").rmtree, state_dir, ignore_errors=True)
        head = self._git(d, "rev-parse", self.ATTEST_REF)
        for repo_line in ("/some/other/host/checkout/.git",
                          str((d / ".git").resolve())):
            with self.subTest(repo=repo_line):
                # Both halves matter. The first is the attack; the second is the
                # deliberate contract change it forces — a record of *this* clone is
                # untrusted too when the gate has nothing to compare it against, because
                # trusting it would trust the file's own word that it belongs here.
                (state_dir / "last-verified").write_text(
                    f"version 1\nrepo {repo_line}\nlast-verified {self.ATTEST_REF} = "
                    f"{head} @ 2020-01-01T00:00:00+00:00\n", encoding="utf-8")
                pf = self._pf(d)
                pf.a.preflight_state_dir = str(state_dir)
                self._stub_git_shapes(pf, common_dir=(128, "", "stub"))
                status, why, _oid, _when, cause = pf.attestation_record(self.ATTEST_REF)
                self.assertEqual((pf.ATTEST_UNTRUSTED, "repo"), (status, cause), why)
                self.assertIn("cannot determine", why)
                state, detail, rcause = pf.upstream_ref_state(p)
                self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"),
                                 (state, rcause), detail)

    def test_the_pre_write_read_refuses_unparsable_lines_too(self):
        # Review NB-6. The strict read refused a record with an unparsable line; the
        # pre-write read asked only about version and bindings, then carried the rest of
        # the file forward into a rewrite this gate signs its own name to. The two
        # readers now ask one question, asked in `_record_content_refusal()`.
        d = self._repo()
        self._git(d, "update-ref", self.ATTEST_REF, "HEAD")
        pf = self._pf(d)
        path = pf.attestation_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"version 1\nrepo {(d / '.git').resolve()}\n"
            f"this line is not a record of anything\n"
            f"last-verified refs/remotes/origin/Other = {'2' * 40} @ "
            f"2020-01-01T00:00:00+00:00\n", encoding="utf-8")
        detail, when = pf.write_attestation(self.ATTEST_REF,
                                            self._git(d, "rev-parse", self.ATTEST_REF))
        self.assertTrue(when, f"nothing was written: {detail}")
        body = path.read_text()
        self.assertIn(f"last-verified {self.ATTEST_REF} = ", body)
        self.assertNotIn("this line is not a record of anything", body,
                         "the strict reader refuses this line and the pre-write reader "
                         f"preserved it: {body!r}")
        self.assertNotIn("refs/remotes/origin/Other", body,
                         "a record the strict reader will not vouch for had its lines "
                         "carried forward anyway")

    def test_a_record_swapped_for_a_fifo_after_the_stat_cannot_block_the_boot(self):
        # Review NB-6, second half. `_read_bounded()` decided "regular file" from a `stat`
        # by path and then opened the path again, which is a second lookup of a name
        # somebody else controls. The window is the whole point: an honest-looking file at
        # `stat` time and a fifo at `open()` time blocks the boot inside the kernel, where
        # no `except OSError` can reach it. `O_NONBLOCK` plus a `fstat()` of the
        # descriptor actually opened closes it, so this answers instead of hanging.
        import os
        import signal
        import tempfile
        d = self._repo()
        pf = self._pf(d)
        with tempfile.TemporaryDirectory() as td:
            decoy = Path(td) / "decoy"
            decoy.write_text("version 1\n", encoding="utf-8")
            fifo = Path(td) / "last-verified"
            os.mkfifo(str(fifo))
            real_stat = os.stat

            def lying_stat(path, *a, **kw):
                # What `stat` said about the path before it was swapped.
                return real_stat(str(decoy))

            def on_alarm(signum, frame):
                raise AssertionError(
                    "the last-verified read blocked on a file that was not a regular "
                    "file by the time it was opened")

            previous = signal.signal(signal.SIGALRM, on_alarm)
            self.addCleanup(signal.signal, signal.SIGALRM, previous)
            signal.alarm(10)
            try:
                with mock.patch("os.stat", side_effect=lying_stat):
                    text, why_not, partial = pf._read_bounded(fifo)
            finally:
                signal.alarm(0)
        self.assertTrue(why_not,
                        "a fifo that arrived after the stat was read as a regular file")
        self.assertFalse(partial)
        self.assertEqual("", text)

    def test_a_fifo_with_a_writer_is_answered_without_ever_being_read(self):
        # The companion to the test above, and the one that pins the other half of NB-6.
        # With no writer a fifo makes `open()` itself fail, so `O_NONBLOCK` alone answers
        # it; with a writer holding it open the `open()` succeeds and the only thing
        # between this gate and a read that never returns is the `fstat()` of the
        # descriptor that was actually opened. That is the answer `stat` by path cannot
        # give once the path has been swapped.
        import os
        import signal
        import tempfile
        d = self._repo()
        pf = self._pf(d)
        with tempfile.TemporaryDirectory() as td:
            decoy = Path(td) / "decoy"
            decoy.write_text("version 1\n", encoding="utf-8")
            fifo = Path(td) / "last-verified"
            os.mkfifo(str(fifo))
            keeper = os.open(str(fifo), os.O_RDWR | os.O_NONBLOCK)
            self.addCleanup(os.close, keeper)
            real_stat = os.stat

            def lying_stat(path, *a, **kw):
                return real_stat(str(decoy))

            def on_alarm(signum, frame):
                raise AssertionError(
                    "the last-verified read went looking inside a fifo because the only "
                    "thing it consulted about the opened file was a stat of its path")

            previous = signal.signal(signal.SIGALRM, on_alarm)
            self.addCleanup(signal.signal, signal.SIGALRM, previous)
            signal.alarm(10)
            try:
                with mock.patch("os.stat", side_effect=lying_stat):
                    text, why_not, partial = pf._read_bounded(fifo)
            finally:
                signal.alarm(0)
        self.assertIn("fifo", why_not, why_not)
        self.assertEqual("", text)
        self.assertFalse(partial)


# --------------------------------------------------------------------------- #
# PRO-307 A4 / review N3 + NB-2 + NB-3: the spawn audit
#
# DEVNULL had to be added call by call, so the guard has to be the thing that
# scales: every spawn in ops/preflight is scanned and refused unless it closes
# the child's stdin. The scanner is a module-level function taking a source
# string so it can be aimed at fixtures as well as at the real files — a
# predicate nobody can feed a mutant is a predicate that silently stops
# checking anything the day someone writes `stdin=None`.
# --------------------------------------------------------------------------- #

_SPAWN_VERBS = frozenset({"run", "Popen", "call", "check_call", "check_output",
                          "getoutput", "getstatusoutput"})
# `PIPE` is honest too: the parent owns the pipe, so the child cannot reach
# fd 0 whatever else happens. What is not safe is a name that merely *looks*
# like an answer — `stdin=None` names the hazard and re-opens it (NB-2).
_SAFE_STDIN_ATTRS = frozenset({"DEVNULL", "PIPE"})
_OS_SHELL_VERBS = frozenset({"system", "popen"})


def _ast_label(node) -> str:
    import ast
    try:
        return ast.unparse(node)
    except (AttributeError, ValueError):  # pragma: no cover - py<3.9
        return type(node).__name__


def _subprocess_spawn_sites(source: str, filename: str = "<source>"):
    """Return `(bare, spreads, aliases, shell)` for spawn sites in `source`.

    `bare` — a spawn whose stdin is unhandled, or is named but is not DEVNULL /
    PIPE. `spreads` — spawns whose keywords arrive via `**`, which the scan
    cannot read and which must therefore stay rare and reviewed. `aliases` —
    spawns reached through a name bound to a subprocess verb, which the naive
    `ast.Name("subprocess")` + `attr == "run"` predicate does not see at all
    (review NB-3). `shell` — `os.system` / `os.popen`, which inherit fd 0 into
    a shell with no keyword that closes it.
    """
    import ast
    tree = ast.parse(source, filename=filename)
    sub_names = set()
    verb_names = set()
    os_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "subprocess":
                    sub_names.add(a.asname or "subprocess")
                elif a.name == "os":
                    os_names.add(a.asname or "os")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for a in node.names:
                    if a.name in _SPAWN_VERBS:
                        verb_names.add(a.asname or a.name)
            elif node.module == "os":
                for a in node.names:
                    if a.name in _OS_SHELL_VERBS:
                        verb_names.add(a.asname or a.name)

    # `R = subprocess.run` / `R = run` puts the spawn behind a name the predicate
    # never looks at. Refuse the indirection rather than chase dataflow: nothing in
    # this tooling has a reason to rename a spawn verb.
    aliases = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        tgt, val = node.targets[0], node.value
        if not isinstance(tgt, ast.Name):
            continue
        if isinstance(val, ast.Attribute) and isinstance(val.value, ast.Name):
            if val.value.id in sub_names and val.attr in _SPAWN_VERBS:
                aliases[tgt.id] = f"{val.value.id}.{val.attr}"
        elif isinstance(val, ast.Name) and val.id in verb_names:
            aliases[tgt.id] = val.id

    bare, spreads, alias_sites, shell = [], [], [], []

    def check(node, how):
        where = f"{filename}:{node.lineno} ({how})"
        named = {kw.arg for kw in node.keywords}
        if None in named:
            spreads.append(where)
            return
        if "input" in named:
            return
        kw = next((k for k in node.keywords if k.arg == "stdin"), None)
        if kw is None:
            bare.append(where)
        elif isinstance(kw.value, ast.Attribute) and kw.value.attr in _SAFE_STDIN_ATTRS:
            return
        else:
            bare.append(f"{where} stdin={_ast_label(kw.value)})"
                        .replace("))", ")"))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            base = f.value
            base_name = base.id if isinstance(base, ast.Name) else None
            if base_name in sub_names and f.attr in _SPAWN_VERBS:
                check(node, f"{base_name}.{f.attr}")
            elif base_name in os_names and f.attr in _OS_SHELL_VERBS:
                shell.append(f"{filename}:{node.lineno} ({base_name}.{f.attr})")
        elif isinstance(f, ast.Name):
            if f.id in verb_names:
                check(node, f.id)
            elif f.id in aliases:
                alias_sites.append(f"{filename}:{node.lineno} "
                                   f"({f.id} <- {aliases[f.id]})")
    return bare, spreads, alias_sites, shell


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
        # Review N3, strengthened for NB-2/NB-3: DEVNULL had to be added call by
        # call, so the test has to be the thing that scales. Every spawn verb in
        # ops/preflight is refused unless stdin is DEVNULL/PIPE or the caller
        # supplies `input=` — and the predicate now sees through `import
        # subprocess as sp`, a verb imported by name, an alias, and os.system.
        bare, spreads, alias_sites, shell = [], [], [], []
        scanned = []
        for path in sorted(HERE.glob("*.py")):
            if path.name == Path(__file__).name:
                continue
            scanned.append(path.name)
            b, sp_, al, sh = _subprocess_spawn_sites(
                path.read_text(encoding="utf-8"), filename=path.name)
            bare += b
            spreads += sp_
            alias_sites += al
            shell += sh
        self.assertGreaterEqual(len(scanned), 3,
                                f"the scan walked {scanned}; an empty glob would make "
                                "this test green by checking nothing")
        self.assertEqual([], bare,
                         "a spawn in ops/preflight leaves its child on the caller's "
                         "fd 0 (PRO-307 A4)")
        self.assertEqual([], alias_sites,
                          "a spawn reached through an aliased verb name is invisible "
                          "to a name-and-attribute scan (review NB-3)")
        self.assertEqual([], shell,
                          "os.system/os.popen hand fd 0 to a shell with no keyword "
                          "that closes it (review NB-3)")
        # The one `**`-spread call is DbRunner._run's `feed`, whose DEVNULL default the
        # two spies above pin directly. A second spread would be an unchecked spawn.
        self.assertEqual(["preflight_worlddb.py"], sorted({s.split(":")[0] for s in spreads}),
                         f"unexpected **kwargs spawn sites: {spreads}")
        self.assertEqual(1, len(spreads), f"unexpected **kwargs spawn sites: {spreads}")

    # --- the scanner itself, aimed at fixtures (review NB-2, NB-3) -------------- #
    # The scan is only a guard while it fails on the shapes it is meant to fail on.
    # Each of these is the defect re-introduced in three lines of source; a scan
    # that stays green for any of them is not protecting the next probe written here.

    def _scan(self, src):
        return _subprocess_spawn_sites(src, filename="fixture.py")

    def test_a_named_stdin_of_none_is_not_an_answer(self):
        # NB-2: the old predicate asked only whether `stdin` was *named*, so the
        # single most likely way to write the bug — `stdin=None`, which is exactly
        # what A4 was — passed the guard while leaving the child on fd 0.
        bare, _sp, _al, _sh = self._scan(
            "import subprocess\n"
            "subprocess.run(['git', 'status'], stdin=None, stdout=subprocess.PIPE)\n")
        self.assertEqual(1, len(bare), bare)
        self.assertIn("stdin=None", bare[0])

    def test_a_named_stdin_of_the_callers_own_stream_is_not_an_answer(self):
        bare, _sp, _al, _sh = self._scan(
            "import subprocess, sys\n"
            "subprocess.run(['git', 'status'], stdin=sys.stdin)\n")
        self.assertEqual(1, len(bare), bare)
        self.assertIn("sys.stdin", bare[0])

    def test_the_scan_follows_an_aliased_subprocess_module(self):
        # NB-3: `import subprocess as sp` hid every spawn from the old scan.
        bare, _sp, _al, _sh = self._scan(
            "import subprocess as sp\n"
            "sp.run(['git', 'status'], stdout=sp.PIPE)\n")
        self.assertEqual(1, len(bare), bare)
        self.assertIn("sp.run", bare[0])

    def test_the_scan_follows_a_verb_imported_by_name(self):
        bare, _sp, _al, _sh = self._scan(
            "from subprocess import check_output\n"
            "check_output(['git', 'status'])\n")
        self.assertEqual(1, len(bare), bare)
        self.assertIn("check_output", bare[0])

    def test_the_scan_refuses_a_spawn_reached_through_an_alias(self):
        bare, _sp, alias_sites, _sh = self._scan(
            "import subprocess\n"
            "R = subprocess.run\n"
            "R(['git', 'status'], stdin=subprocess.DEVNULL)\n")
        self.assertEqual([], bare)
        self.assertEqual(1, len(alias_sites), alias_sites)
        self.assertIn("R <- subprocess.run", alias_sites[0])

    def test_the_scan_refuses_the_other_spawn_verbs(self):
        for verb in sorted(_SPAWN_VERBS - {"run"}):
            with self.subTest(verb=verb):
                bare, _sp, _al, _sh = self._scan(
                    f"import subprocess\nsubprocess.{verb}(['git', 'status'])\n")
                self.assertEqual(1, len(bare), f"{verb}: {bare}")

    def test_the_scan_refuses_a_shell_spawned_through_os(self):
        bare, spreads, aliases, shell = self._scan(
            "import os as o\n"
            "o.system('git status')\n"
            "o.popen('git status').read()\n")
        self.assertEqual(2, len(shell), shell)
        self.assertFalse(bare or spreads or aliases)

    def test_the_scan_still_passes_the_two_shapes_that_are_answers(self):
        # Negative controls: a guard that refuses everything is deleted in one
        # commit, so the passing shapes have to be pinned by name too.
        for src in (
                "import subprocess\n"
                "subprocess.run(['git', 'status'], stdin=subprocess.DEVNULL)\n",
                "import subprocess\n"
                "subprocess.run(['mysql'], input=b'SELECT 1;')\n",
                "import subprocess\n"
                "subprocess.Popen(['git', 'status'], stdin=subprocess.PIPE)\n"):
            with self.subTest(src=src):
                self.assertEqual((), tuple(x for x in self._scan(src) if x))

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
        # PRO-307 A5 asked for an operational answer rather than a raw Python traceback
        # when `--json` names a path whose parent does not exist. The gate got that from
        # the payload half of PRO-313/PRO-325's rework: the write is guarded and its
        # failure becomes a named, non-zero refusal, so there is nothing left for a
        # traceback to be. What is pinned here is the property the operator actually
        # needs — no stack trace, the path named, a documented rc, boot refused.
        #
        # The gate itself is stubbed: unstubbed it stops at check [0] with a
        # db-access operational error on a host with no container, which is a different
        # (and correct) answer and would never reach the payload write.
        import preflight_worlddb as P

        class StubPreflight:
            def __init__(self, args, policy, report):
                self.args, self.policy, self.report = args, policy, report
                self.sql_root = Path(args.compose_root) / "data" / "sql"
                self.git_root = Path(args.compose_root)

            def run(self):
                return P.EXIT_OK

        saved = P.Preflight
        P.Preflight = StubPreflight
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = str(Path(tmp) / "not-created" / "report.json")
                code, _out, err = self._main(self._argv(path))
        finally:
            P.Preflight = saved
        self.assertEqual(P.EXIT_ERROR, code)
        self.assertIn("--json", err)
        self.assertNotIn("Traceback", err)
        self.assertIn("not-created", err)
        self.assertIn("could not write", err)
        self.assertIn("boot is refused", err)

    def test_a_path_whose_parent_exists_is_not_refused_up_front(self):
        # The path is the thing that must be refused, not the flag: with a real parent
        # the run has to get as far as the gate itself. This is also the guard against
        # re-adding an up-front `--json` parent-directory check, which would pre-empt the
        # payload-vs-verdict split PRO-341 review B1 pinned.
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

class TestCorpusSweepNamesTheOffendingCompanyFile(unittest.TestCase):
    """PRO-285: the CI check fails on the company column, so it has to name it."""

    class StubEngine:
        def __init__(self, violations):
            self.violations = violations
            self.policy_stats = {"files": 2, "statements": 4}

        def check_policy(self, files):
            pass

    def _tally(self):
        import corpus_sweep as cs
        files = [
            {"rel": "pending.sql", "origin": "company", "author": "scuff@paperclip.local",
             "dir": {"db_path": "$/pending"}},
            {"rel": "archive.sql", "origin": "upstream", "author": "someone@azerothcore.org",
             "dir": {"db_path": "$/archive"}},
        ]
        violations = [
            {"file": "pending.sql", "line": 7, "rule": "table-not-allowed",
             "detail": "writes `updates`"},
            {"file": "archive.sql", "line": 3, "rule": "reserved-band",
             "detail": "entry 1234 below the band"},
        ]
        return cs.tally(self.StubEngine(violations), files)

    def test_tally_records_the_company_rows_and_the_provenance_file_counts(self):
        result = self._tally()
        self.assertEqual([{"file": "pending.sql", "line": 7, "rule": "table-not-allowed",
                           "detail": "writes `updates`"}], result["companyFindingList"],
                         "the upstream finding leaked into the company list")
        self.assertEqual({"company": 1, "upstream": 1}, result["provenanceFiles"])

    def test_the_written_row_list_is_capped_and_says_so(self):
        # A shallow checkout once wrote a 120 MB tally by listing all 444 726 company
        # rows; the guard reads counts, so the JSON carries a cap and a flag.
        import corpus_sweep as cs
        violations = [{"file": "pending.sql", "line": i, "rule": "reserved-band",
                       "detail": "below the band"}
                      for i in range(cs.COMPANY_JSON_LIMIT + 5)]
        result = cs.tally(self.StubEngine(violations), [
            {"rel": "pending.sql", "origin": "company", "author": "scuff@paperclip.local",
             "dir": {"db_path": "$/pending"}}])
        self.assertEqual(cs.COMPANY_JSON_LIMIT, len(result["companyFindingList"]))
        self.assertTrue(result["companyFindingListTruncated"],
                        "a cap nobody can see is how rows get hidden")

    def test_the_company_rows_are_printed_with_rule_and_file(self):
        import corpus_sweep as cs
        result = dict(TestCorpusSweepCompanyColumn.RESULT_DIRTY)
        result["companyFindingList"] = [
            {"file": "data/sql/updates/pending_db_world/x.sql", "line": 12,
             "rule": "table-not-allowed", "detail": "writes `updates`"}]
        result["provenanceFiles"] = {"company": 1, "upstream": 2}
        saved = cs.build_files, cs.tally
        cs.build_files = lambda engine, policy, workers: []
        cs.tally = lambda engine, files: dict(result)
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                code = cs.main([])
        finally:
            cs.build_files, cs.tally = saved
        text = out.getvalue()
        self.assertEqual(1, code)
        self.assertIn("table-not-allowed", text)
        self.assertIn("pending_db_world/x.sql:12", text,
                      "the check fails without saying which file and rule refused")

    def test_a_tally_without_the_new_keys_still_exits_on_the_company_column(self):
        # A baseline captured before PRO-285 has no companyFindingList; the exit status
        # must not depend on a key the baseline never had.
        import corpus_sweep as cs
        saved = cs.build_files, cs.tally
        cs.build_files = lambda engine, policy, workers: []
        cs.tally = lambda engine, files: dict(TestCorpusSweepCompanyColumn.RESULT_DIRTY)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(1, cs.main([]))
        finally:
            cs.build_files, cs.tally = saved


class TestCiProvenanceGuard(unittest.TestCase):
    """PRO-285: a company column that collapsed to zero must not read as a pass."""

    # The healthy tally this tree actually measures: 3 060 files, 131 625 statements,
    # 444 726 findings, every one of them upstream (companyFindings=0) over a 20 279-commit
    # history with 8 distinct authors. The per-rule counts are the sweep's own output at
    # the head the README baseline was taken at, and `findings` is their sum because
    # `corpus_sweep.py` derives it that way — a fixture that could drift from its own
    # arithmetic would quietly unpin the guard's add-up clauses (PRO-298 review N8).
    BY_RULE = {
        "reserved-band": 323749, "table-not-allowed": 83025, "band-not-literal": 35537,
        "unbounded-write": 1985, "forbidden-statement": 167,
        "insert-columns-required": 100, "key-column-assigned": 68,
        "custom-table-only": 43, "or-in-where": 22, "ledger-table": 15,
        "unknown-statement": 10, "insert-select": 4, "multi-table-write": 1,
    }

    GOOD = {
        "head": "0123456789abcdef0123456789abcdef01234567",
        "files": 3060, "filesUnderPolicy": 3060, "statements": 131625,
        "findings": sum(BY_RULE.values()),
        "byRule": BY_RULE,
        "byRuleOrigin": {r: {"upstream": n} for r, n in BY_RULE.items()},
        "companyFindings": 0, "companyFindingList": [],
        # The production shape, key absence and all: `provenanceFiles` is a
        # `dict(Counter(...))`, so a class with no members is missing rather than 0.
        "provenanceFiles": {"company": 7, "upstream": 3053},
        "shallow": False, "commits": 20279, "authors": 8,
    }

    def _origins(self, origin="upstream", **splits):
        """The healthy per-rule breakdown, with named rules re-split.

        Each rule keeps a breakdown that sums to its `byRule` count, so a test that
        mutates an origin column tests that column and not the guard's arithmetic.
        """
        origins = {r: {origin: n} for r, n in self.BY_RULE.items()}
        origins.update(splits)
        return origins

    def _run(self, tally=None, write=True):
        import ci_provenance_guard as guard
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sweep.json"
            if write:
                path.write_text(json.dumps(self.GOOD if tally is None else tally))
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = guard.main([str(path)])
        return code, out.getvalue(), err.getvalue()

    def test_an_attributable_tally_passes(self):
        code, out, _ = self._run()
        self.assertEqual(0, code)
        self.assertIn("company=7", out)
        self.assertIn("upstream=3053", out)

    def test_unattributable_files_fail(self):
        tally = dict(self.GOOD)
        tally["provenanceFiles"] = {"company": 0, "upstream": 0, "unknown": 3060}
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("fetch-depth", err)

    def test_findings_in_the_unknown_column_fail(self):
        tally = dict(self.GOOD)
        tally["byRuleOrigin"] = self._origins(
            **{"table-not-allowed": {"upstream": 83020, "unknown": 5}})
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("table-not-allowed=5", err)
        self.assertEqual(1, len([l for l in err.splitlines()
                                 if l.startswith("GUARD FAIL")]),
                         "the unknown column has its own clause; a second reason here "
                         "means the fixture stopped adding up")

    def test_a_sweep_that_scanned_nothing_fails(self):
        tally = dict(self.GOOD, files=0, filesUnderPolicy=0)
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("no .sql", err)

    def test_a_partial_run_without_strict_all_fails(self):
        tally = dict(self.GOOD, filesUnderPolicy=8)
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("--strict-all", err)

    def test_a_head_that_is_not_a_sha_fails(self):
        code, _, err = self._run(dict(self.GOOD, head="unknown"))
        self.assertEqual(2, code)
        self.assertIn("not a commit sha", err)

    def test_a_pre_provenance_tally_is_never_skipped_silently(self):
        tally = {k: v for k, v in self.GOOD.items() if k != "provenanceFiles"}
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("provenanceFiles", err)

    def test_a_shallow_checkout_fails_even_though_nothing_says_unknown(self):
        # Measured on a `--depth 1` clone: the grafted root answers `git log -- <path>`
        # for every file, so every file is the tip author's and `unknown` stays empty.
        tally = dict(self.GOOD, shallow=True, commits=1, authors=1,
                     provenanceFiles={"company": 3060, "upstream": 0, "unknown": 0},
                     byRuleOrigin=self._origins("company"))
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("shallow", err)
        self.assertIn("fetch-depth", err)

    def test_the_silent_green_shallow_case_fails_on_history_alone(self):
        # The dangerous shape: an upstream-authored tip puts every finding in `upstream`,
        # the column the CI check gates on reads 0, and nothing about the provenance
        # columns themselves looks wrong. History is the only thing left that can see it,
        # so this tally keeps a healthy provenanceFiles and fails on the history clauses
        # alone.
        tally = dict(self.GOOD, shallow=True, commits=1, authors=1)
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        for needle in ("shallow", "commits=1", "authors=1", "upstream"):
            self.assertIn(needle, err)

    def test_the_measured_shallow_collapse_is_also_refused_by_the_company_column(self):
        # What a `--depth 1` clone of this repo actually produced (PRO-285), now caught
        # twice over: the history clauses, and since PRO-295 B1, the gated column having
        # no company file behind it.
        tally = dict(self.GOOD, shallow=True, commits=1, authors=1,
                     provenanceFiles={"company": 0, "upstream": 3060, "unknown": 0})
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        for needle in ("shallow", "commits=1", "authors=1", "upstream",
                       "attributed to `company`"):
            self.assertIn(needle, err)

    def test_a_single_author_across_the_corpus_fails(self):
        code, _, err = self._run(dict(self.GOOD, authors=1))
        self.assertEqual(2, code)
        self.assertIn("single commit author", err)

    def test_a_corpus_with_no_upstream_files_fails(self):
        tally = dict(self.GOOD, provenanceFiles={"company": 3060, "upstream": 0,
                                                 "unknown": 0})
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("attributed to `upstream`", err)

    def test_a_corpus_with_no_company_files_fails(self):
        # PRO-295 B1. Drop `pending_db_world` from `includedDirs` and the gated column
        # has nothing behind it: files=3053, provenanceFiles.company=0, companyFindings=0.
        # That is the same zero a compliant corpus produces, and until this clause the
        # guard read it as a verdict.
        tally = dict(self.GOOD, files=3053, filesUnderPolicy=3053,
                     provenanceFiles={"company": 0, "upstream": 3053, "unknown": 0})
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("attributed to `company`", err)
        self.assertIn("not a policy", err)

    def test_a_tally_omitting_the_company_key_fails(self):
        # The shape above is what a hand-written tally looks like; this is what the sweep
        # writes. `provenanceFiles` is `dict(Counter(...))` (corpus_sweep.py), so a class
        # with no members is an absent key, not a zero — and the whole company corpus is
        # the 7 files of `pending_db_world`, so dropping that directory from
        # `includedDirs` produces exactly this file (PRO-298 review N5). `company=0` and
        # no `company` at all are the same claim about the corpus and must not be read
        # differently: defaulting the lookup to anything but 0 brings the PRO-295 B1
        # green back with every other test in this class still passing.
        tally = dict(self.GOOD, files=3053, filesUnderPolicy=3053,
                     provenanceFiles={"upstream": 3053})
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("attributed to `company`", err)
        self.assertIn("not a policy", err)

    def test_a_sweep_that_parsed_no_statements_fails(self):
        # PRO-295 B2. files>0 with statements=0 means the engine was handed the corpus
        # and read nothing out of it; every count below it, gated column included, is a
        # count over nothing.
        code, _, err = self._run(dict(self.GOOD, statements=0))
        self.assertEqual(2, code)
        self.assertIn("statements=0", self._only_reason(err))

    def test_a_sweep_that_found_nothing_at_all_fails(self):
        # PRO-295 B2. This tree measures 444 726 findings and always will, so findings=0
        # over 3 060 files is a dead engine — an emptied `forbiddenStatements`, a rule
        # registry that failed to load — not a clean corpus. The refusal points at the
        # README baseline instead of quoting a count, because a frozen number inside a
        # red run's error message goes false the first time the corpus legitimately
        # changes (PRO-298 review N7). The add-up clause keeps out of the way: a dead
        # engine has one name in the log, not two, so nobody diffs byRule against a total
        # that was always going to be 0.
        code, _, err = self._run(dict(self.GOOD, findings=0))
        self.assertEqual(2, code)
        reason = self._only_reason(err)
        self.assertIn("findings=0", reason)
        self.assertIn("ops/preflight/README.md", reason)
        self.assertNotIn("444 726", err)

    def test_a_sweep_with_no_rule_that_fired_fails(self):
        # The per-rule form of the same claim: findings>0 but nothing in byRule is the
        # tally describing its own counts inconsistently, and an empty byRule with
        # findings=0 is the dead engine again.
        code, _, err = self._run(dict(self.GOOD, byRule={}, findings=0,
                                      byRuleOrigin={}))
        self.assertEqual(2, code)
        self.assertIn("byRule is empty", err)

    def _only_reason(self, err):
        reasons = [line for line in err.splitlines() if line.startswith("GUARD FAIL")]
        self.assertEqual(1, len(reasons),
                         "expected one refusal so no clause masks another, got:\n"
                         + "\n".join(reasons))
        return reasons[0]

    def test_a_findings_total_the_rules_do_not_add_up_to_fails(self):
        # PRO-298 review N8. `corpus_sweep.py` sets `findings = sum(by_rule.values())`,
        # so on the CI path these two cannot disagree; a tally where they do was not
        # written by the sweep, and the guard is being asked to attest a document.
        code, _, err = self._run(dict(self.GOOD,
                                      findings=sum(self.BY_RULE.values()) + 1))
        self.assertEqual(2, code)
        self.assertIn("does not add up", self._only_reason(err))

    def test_a_rule_with_no_origin_breakdown_fails(self):
        # The load-bearing half of N8, and not bookkeeping: `company` findings are
        # counted out of `byRuleOrigin` (corpus_sweep.company_by_rule), so a rule that is
        # in `byRule` and missing from `byRuleOrigin` shows up in the printed report while
        # contributing nothing to the column the check gates on. Here the whole
        # `reserved-band` corpus — 323 749 findings, company-authored ones included —
        # vanishes from the verdict while `companyFindings` stays 0.
        origins = dict(self.GOOD["byRuleOrigin"])
        del origins["reserved-band"]
        code, _, err = self._run(dict(self.GOOD, byRuleOrigin=origins))
        self.assertEqual(2, code)
        self.assertIn("no breakdown for it", self._only_reason(err))

    def test_an_origin_breakdown_that_does_not_sum_to_its_rule_fails(self):
        # Same clause one level down: a split that loses rows hides them from the gated
        # column exactly as a missing rule does, so the total and the column are counted
        # from different sets of rows.
        code, _, err = self._run(dict(
            self.GOOD,
            byRuleOrigin=self._origins(**{"reserved-band": {"upstream": 23749}})))
        self.assertEqual(2, code)
        self.assertIn("different sets of rows", self._only_reason(err))

    def test_a_tally_without_a_company_row_list_is_never_skipped_silently(self):
        # PRO-295 B2. `companyFindingList` was the only gated key whose absence the guard
        # tolerated; provenanceFiles, shallow/commits and authors are all hard refusals
        # when missing and this had better match them.
        tally = {k: v for k, v in self.GOOD.items() if k != "companyFindingList"}
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("companyFindingList", err)
        tally = dict(self.GOOD, companyFindingList=None)
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("companyFindingList", err)

    def test_history_keys_absent_are_never_skipped_silently(self):
        tally = {k: v for k, v in self.GOOD.items() if k not in ("shallow", "commits")}
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("shallow", err)

    def test_an_author_count_absent_is_never_skipped_silently(self):
        tally = {k: v for k, v in self.GOOD.items() if k != "authors"}
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("authors", err)

    def test_a_capped_row_list_that_adds_up_still_passes(self):
        row = {"file": "a.sql", "line": 1, "rule": "reserved-band", "detail": ""}
        tally = dict(self.GOOD, companyFindings=444726,
                     companyFindingList=[dict(row) for _ in range(2000)],
                     companyFindingListTruncated=True)
        code, _, _ = self._run(tally)
        self.assertEqual(0, code)

    def test_a_truncation_flag_that_lies_fails(self):
        row = {"file": "a.sql", "line": 1, "rule": "reserved-band", "detail": ""}
        tally = dict(self.GOOD, companyFindings=1, companyFindingList=[dict(row)],
                     companyFindingListTruncated=True)
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("rows it did not have", err)

    def test_a_self_contradicting_company_column_fails(self):
        tally = dict(self.GOOD, companyFindings=3)
        code, _, err = self._run(tally)
        self.assertEqual(2, code)
        self.assertIn("contradicts itself", err)

    def test_company_findings_are_the_sweep_verdict_not_the_guards(self):
        tally = dict(self.GOOD, companyFindings=1,
                     companyFindingList=[{"file": "p.sql", "line": 1,
                                          "rule": "table-not-allowed", "detail": ""}])
        code, _, _ = self._run(tally)
        self.assertEqual(0, code, "the guard re-litigating the policy verdict is a "
                                  "second owner for one decision")

    def test_a_missing_or_unparseable_tally_is_a_usage_error(self):
        code, _, err = self._run(write=False)
        self.assertEqual(1, code)
        self.assertIn("no tally there", err)
        import ci_provenance_guard as guard
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sweep.json"
            path.write_text("{not json")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(1, guard.main([str(path)]))


class TestTheCiWorkflowMatchesItsDocumentation(unittest.TestCase):
    """The README says what CI runs; CI had better run exactly that.

    PRO-285 exists because this file claimed a check that no workflow implemented. These
    are the tests that make the two artefacts fail together instead of drifting, plus
    the properties that were each measured to be load-bearing (full history, a guard
    that survives a red sweep, no repository guard that can retire the check in
    silence, and no step that needs MySQL, Docker or the deploy host).
    """

    REPO = Path(__file__).resolve().parents[2]
    WORKFLOW = REPO / ".github" / "workflows" / "worlddb-preflight.yml"
    README = REPO / "ops" / "preflight" / "README.md"

    @classmethod
    def setUpClass(cls):
        cls.yaml = cls.WORKFLOW.read_text()
        cls.readme = cls.README.read_text()
        start = cls.readme.index("### In CI")
        rest = cls.readme[start + len("### In CI"):]
        end = rest.index("\n## ")
        cls.in_ci = rest[:end]

    @staticmethod
    def flat(text):
        return " ".join(text.split())

    @property
    def code(self):
        """The workflow with comments stripped — prose may describe a thing it does not do."""
        return "\n".join(line.split("#", 1)[0] for line in self.yaml.splitlines())

    def job_block(self, job):
        _, _, tail = self.code.partition("\n  %s:" % job)
        return tail

    def readme_commands(self):
        return [m for m in re.findall(r"`(python3 ops/preflight/[^`]+)`", self.in_ci)]

    def test_every_command_the_readme_promises_for_ci_actually_runs(self):
        commands = self.readme_commands()
        self.assertGreaterEqual(len(commands), 4, "the In CI table lost a row")
        for command in commands:
            self.assertIn(self.flat(command), self.flat(self.yaml),
                          "README promises `%s` and no workflow step runs it" % command)

    def test_the_verdict_table_rows_are_run_lines_not_prose(self):
        # The check above matches against the whole file, comments included, because the
        # prose legitimately quotes the local form of a command. That is too weak for the
        # table, which is the thing that claims a step runs it: every command in a table
        # row has to survive comment stripping, so a comment can never satisfy a promise
        # the table makes (PRO-295 review N3).
        rows = [line for line in self.in_ci.splitlines() if line.strip().startswith("|")]
        promised = [c for row in rows
                    for c in re.findall(r"`(python3 ops/preflight/[^`]+)`", row)]
        self.assertEqual(4, len(promised),
                         "the In CI table no longer lists the four steps, so the verdict "
                         "column is describing something other than this workflow")
        for command in promised:
            self.assertIn(self.flat(command), self.flat(self.code),
                          "the table promises `%s` and no run line contains it" % command)

    def test_the_check_cannot_be_retired_by_a_repository_guard(self):
        # sql-codestyle.yml is the cautionary tale: it only runs on azerothcore/azerothcore-wotlk,
        # so on this fork the check does not fail, it simply stops existing.
        self.assertNotIn("github.repository", self.code)
        self.assertNotIn("github.event.pull_request.draft", self.code,
                         "a draft PR is exactly when a policy violation should be visible")

    def test_the_sweep_checkout_carries_the_history_the_provenance_needs(self):
        self.assertIn("fetch-depth: 0", self.job_block("corpus-sweep"))

    def test_the_guard_still_runs_when_the_sweep_has_already_failed(self):
        _, _, tail = self.code.partition("name: Provenance attribution guard")
        step, _, _ = tail.partition("\n      - ")
        condition = [line for line in step.splitlines() if line.strip().startswith("if:")]
        self.assertEqual(1, len(condition), "the guard step has no single explicit condition")
        self.assertIn("!cancelled()", condition[0],
                      "skipping the guard on a red sweep removes the only answer to "
                      "'is this company column real or a provenance collapse'")
        self.assertIn("ci_provenance_guard.py", step)

    def test_no_step_needs_mysql_docker_or_the_deploy_host(self):
        for banned in ("services:", "mysql", "docker", "self-hosted", "ssh ", "deployhost",
                       "docker-compose"):
            self.assertNotIn(banned, self.code,
                             "a step that needed `%s` would not belong in this workflow" % banned)

    def test_the_sweep_job_carries_an_explicit_budget(self):
        block = self.job_block("corpus-sweep")
        budget = [line for line in block.splitlines() if "timeout-minutes" in line]
        self.assertEqual(1, len(budget), "a hung `git log` with no budget burns the runner")
        minutes = int(budget[0].split("timeout-minutes:")[1].strip())
        self.assertGreaterEqual(minutes, 10, "the sweep alone measures ~5 min")

    def test_the_sweep_has_exactly_one_ci_caller(self):
        callers = [p.name for p in (self.REPO / ".github" / "workflows").glob("*.y*ml")
                   if "corpus_sweep.py" in p.read_text()]
        self.assertEqual(["worlddb-preflight.yml"], callers,
                         "a second caller is a second definition of what the gate means")

    def test_the_two_claims_that_started_this_do_not_come_back(self):
        for gone in ("wired into CI rather than read by eye",
                     "CI runs `--policy-all` report-only",
                     "the CI / merge-gate rehearsal form",
                     "nothing under `.github/workflows/` references `ops/preflight`",
                     "nothing in .github/workflows/ calls corpus_sweep.py"):
            self.assertNotIn(gone, self.readme,
                             "README re-adopted a CI claim about a check that does not exist: %r" % gone)


class TestTheVerdictSurvivesTheOperatorsCodec(unittest.TestCase):
    """PRO-308 QA finding B: the report layer may not lose the verdict it produced.

    `dirty_paths()` decodes `git status` with `surrogateescape` (PRO-301 review B1/B2)
    precisely so a path that is not valid UTF-8 counts as dirty instead of reading
    clean. The price is that the name reaches the report as a lone surrogate, and
    whether that survives the trip to a human is a property of the operator's locale,
    not of the gate. On the deploy host it did not survive: `print(render(...))` raised
    `UnicodeEncodeError: 'utf-8' codec can't encode character '\\udcff'` between the
    verdict and the `--json` write, so the boot was refused (rc=1, correctly) while the
    operator got a traceback, no reason, and no payload.

    A gate that fails closed and withholds why is the worst shape this file can be in.
    These pin the answer rather than the symptom, and each carries a negative control
    proving the fixture is still the shape under test — the box these run on answers
    `surrogateescape` for stdout by default, which would let the regression back in.
    """

    RAW = "data/sql/updates/db_world/2026_11_04_00_qa_raw\udcff.sql"

    def _report(self) -> Report:
        r = Report()
        r.fail("3", f"{self.RAW} is upstream-provenanced at HEAD but has uncommitted "
                    "changes, so it is checked against the write policy",
               file=self.RAW, rule="dirty-worktree")
        r.data["elapsed_s"] = 0.1
        return r

    def _payload(self, r: Report) -> dict:
        """The same dict `main()` builds, so the payload test cannot drift from it."""
        return {"result": "fail" if r.failed else "pass",
                "data": r.data,
                "sections": r.sections,
                "findings": [f.__dict__ for f in r.findings]}

    def _strict_stream(self):
        """A stream with a strict error handler — the deploy host's shape, forced."""
        buf = io.BytesIO()
        return io.TextIOWrapper(buf, encoding="utf-8", errors="strict"), buf

    def test_a_strict_stdout_that_refuses_the_name_still_gets_the_verdict(self):
        from preflight_worlddb import _emit, render
        r = self._report()
        text = render(r, build_arg_parser().parse_args(["--compose-root", "/nonexistent"]))
        stream, buf = self._strict_stream()
        with self.assertRaises(UnicodeEncodeError):
            stream.write(text + "\n")
        self.assertEqual(b"", buf.getvalue(),
                         "the fixture stopped being the shape this test is about")
        stream, buf = self._strict_stream()
        _emit(stream, text)
        written = buf.getvalue().decode("utf-8", "backslashreplace")
        self.assertIn("RESULT: FAIL", written,
                      "the codec swallowed the verdict, which is finding B")
        self.assertIn("dirty-worktree", written,
                      "the verdict arrived without the reason for it")
        self.assertIn("qa_raw", written)
        self.assertIn("\\udcff", written,
                      "the offending name was dropped rather than escaped; an operator "
                      "has to be able to identify the file it is about")

    def test_the_json_payload_lands_when_a_path_name_is_not_utf_8(self):
        from preflight_worlddb import _write_json_payload
        r = self._report()
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "verdict.json"
            _write_json_payload(out, self._payload(r))
            self.assertTrue(out.is_file(), "the machine-readable verdict was never written")
            raw = out.read_bytes()
            self.assertTrue(raw.isascii(),
                            "a non-ASCII byte in the payload is what makes the write "
                            "depend on the locale again")
            self.assertIn(b"\\udcff", raw)
            back = json.loads(raw.decode("ascii"))
            self.assertEqual("fail", back["result"])
            self.assertEqual(self.RAW, back["findings"][0]["file"],
                             "the escape lost the name instead of preserving it")
            # Negative control: the flag is load-bearing, not stylistic. Anyone who
            # "tidies" it away re-opens the hole on a locale that cannot hold the byte.
            self.assertFalse(json.dumps(self._payload(r), ensure_ascii=False).isascii())

    class _ReportingStream:
        """A text stream with the deploy host's shape and a refusal we can arm.

        Deliberately not an `io.TextIOBase` subclass: that base owns `encoding` and
        will not let a stand-in set one. `write_through=True` means the bytes are in
        `buffer` without a flush, so the stream can still be read back after it starts
        refusing.
        """

        def __init__(self, encoding="ascii", errors="strict", refuse=None,
                     error=None):
            self.encoding = encoding
            self.errors = errors
            self.refuse = refuse
            self.error = error if error is not None else BrokenPipeError(32, "Broken pipe")
            self.buffer = io.BytesIO()
            self.inner = io.TextIOWrapper(self.buffer, encoding=encoding, errors=errors,
                                          write_through=True)

        def write(self, s):
            if self.refuse == "write":
                raise self.error
            return self.inner.write(s)

        def flush(self):
            if self.refuse == "flush":
                raise self.error
            self.inner.flush()

        def value(self) -> bytes:
            return self.buffer.getvalue()

    class _HostileFinding:
        """A finding that raises the moment the presentation layer reads it.

        `fail()/warn()/info()` cannot produce one, which is exactly why PRO-324 NB1 was
        non-blocking; the degraded path still has to survive it, because the degraded
        path is a guard and a guard that raises back into `main()` is the defect it
        exists to cover. `severity` stays readable so `report.failed` — which `main()`
        needs for the payload and this class needs for the header — still answers; the
        attributes only the *listing* touches are the ones that blow up.
        """

        def __init__(self):
            self.severity = "FAIL"

        @property
        def check(self):
            raise RuntimeError("fixture: .check is unreadable")

        @property
        def file(self):
            raise RuntimeError("fixture: .file is unreadable")

        @property
        def rule(self):
            raise RuntimeError("fixture: .rule is unreadable")

        @property
        def message(self):
            raise RuntimeError("fixture: .message is unreadable")

    class _HostilePayloadFinding:
        """A finding the *presentation* layer can read but the payload build cannot.

        `render()` touches `severity/check/file/rule/message`; the machine-readable
        half asks each finding for `__dict__` instead. That makes this the one shape
        that fails the evidence half alone, which is what PRO-339 review NB-Rev-2 is
        about: the build used to sit outside the guard that covers the write, so a
        report like this escaped `main()` as a traceback.
        """

        __slots__ = ()

        severity = "FAIL"
        check = "3"
        file = ""
        rule = "dirty-worktree"
        message = "unreadable to the payload build"

        @property
        def __dict__(self):
            raise RuntimeError("fixture: __dict__ is unreadable")

    def _drive_main(self, stdout, stderr, *, render_raises=False, json_target="default",
                    stub_rc=None, stub_findings=True, hostile_report=False,
                    hostile_payload=False, op_error=None, expect_raises=False):
        """Run the real `main()` with the gate stubbed and the operator's streams swapped.

        `main()` had no test before this class, which is why the first version of these
        pins reached for `inspect.getsource`. A source-text ordering assertion cannot
        tell you which half loses when the other one throws, and it pins the shape that
        caused finding B in the first place (PRO-313 review B1). This drives the real
        function and measures both halves.

        `stub_rc` is what the gate's own checks return (PRO-324 NB3): with it pinned to
        `EXIT_FAIL` every payload case ran on an already-failing run, so the one shape
        that changes what boots — checks pass, `--json` asked for, payload unwritable —
        was untested. `stub_findings=False` makes that stub honest about a passing run
        instead of pairing an rc of 0 with a FAIL finding. `hostile_report` adds
        `_HostileFinding` for the NB1 pins, and `hostile_payload` adds
        `_HostilePayloadFinding` for the PRO-339 NB-Rev-2 pin — a report the terminal can
        still read but whose evidence half cannot be built.

        The `_render_or_say_why` wrapper is the ordering pin (PRO-324 NB1 corollary).
        It asks the presentation half, at the moment it runs, whether the evidence half
        already landed — which is the property the ordering exists to buy. Hoisting the
        `_emit(sys.stdout, _render_or_say_why(...))` call above the payload write used to
        survive the suite, because the ordering rested on that function never raising;
        now that it genuinely cannot, the ordering gets a test of its own rather than
        inheriting one.

        `op_error` is the shape where the checks never ran at all because the environment
        refused them: the stub raises the gate's own `OperationalError` rather than
        returning a finding (PRO-359 / PRO-382 §1).
        """
        import os
        import sys
        import types
        import preflight_worlddb as P

        raw = self.RAW
        hostile_cls = self._HostileFinding
        hostile_payload_cls = self._HostilePayloadFinding

        class StubPreflight:
            def __init__(self, args, policy, report):
                self.args, self.policy, self.report = args, policy, report
                self.sql_root = Path(args.compose_root) / "data" / "sql"
                # PRO-307 A3: main() reports which tree the scan walked, so the stub
                # has to carry it the way the real Preflight does.
                self.git_root = Path(args.compose_root)

            def run(self):
                if op_error is not None:
                    raise P.OperationalError(op_error)
                if stub_findings:
                    self.report.fail("3",
                                     f"{raw} is upstream-provenanced at HEAD but has "
                                     "uncommitted changes, so it is checked against the "
                                     "write policy",
                                     file=raw, rule="dirty-worktree")
                if hostile_report:
                    self.report.findings.append(hostile_cls())
                if hostile_payload:
                    self.report.findings.append(hostile_payload_cls())
                return P.EXIT_FAIL if stub_rc is None else stub_rc

        def boom(report, args):
            raise RuntimeError("fixture: render() blew up")

        seen = {}
        real_render_or_say_why = P._render_or_say_why

        def spy_render_or_say_why(report, args, *, payload_note):
            # Existence is not the property; complete bytes are (PRO-339 review
            # NB-Rev-1). A write that touched the path empty and filled it after
            # presentation used to satisfy `is_file()` while leaving the audit log
            # holding a zero-byte "evidence" file if presentation then threw — the
            # PRO-313 B1 failure mode wearing a disguise. So read what is actually on
            # disk at the moment the presentation half runs and compare it later.
            #
            # `payload_note` is passed through untouched, keyword-only and required,
            # exactly as `main()` computes it (PRO-341 review N1): the spy must not
            # become a second caller that can omit the claim it exists to pin.
            existed = False
            content = None
            if out_path is not None:
                try:
                    existed = out_path.is_file()
                    if existed:
                        content = out_path.read_bytes()
                except Exception as exc:                  # a fixture may break the path
                    seen["payload_read_error"] = type(exc).__name__
            seen["payload_existed"] = existed
            seen["payload_bytes"] = content
            return real_render_or_say_why(report, args, payload_note=payload_note)

        original = (P.Preflight, P.render, P._render_or_say_why,
                    sys.stdout, sys.stderr, sys.__stderr__)
        P.Preflight = StubPreflight
        P._render_or_say_why = spy_render_or_say_why
        if render_raises:
            P.render = boom
        # `sys.__stderr__` is where `_emit` names a presentation failure; capture it so
        # the suite stays readable and the notice itself is assertable.
        fallback = self._ReportingStream()
        sys.stdout, sys.stderr, sys.__stderr__ = stdout, stderr, fallback
        try:
            with tempfile.TemporaryDirectory() as td:
                tree = Path(td) / "tree"
                (tree / "data" / "sql").mkdir(parents=True)
                policy = Path(td) / "policy.json"
                policy.write_text("{}")
                if json_target == "default":
                    out_path = Path(td) / "verdict.json"
                    json_arg = str(out_path)
                elif json_target == "missing-dir":
                    out_path = Path(td) / "no-such-dir" / "verdict.json"
                    json_arg = str(out_path)
                elif json_target == "empty":
                    # `--json ""` as an operator's `GATE_ARGS=(--json "$VAR")` yields it:
                    # the flag is there, the path is not, so nothing may claim a file.
                    out_path = Path(td) / "verdict.json"
                    json_arg = ""
                elif json_target == "whitespace":
                    # `--json "   "` is the same family and the dangerous one: a real
                    # write with this argument *succeeds* and drops a file literally
                    # named `   ` in the cwd, so a gate that only tests `== ""` would
                    # boot on it. `out_path` stays None — there is no legitimate file to
                    # look for. The proof is `leftovers`: the drive runs in an empty
                    # scratch cwd, so a truthiness gate leaves `   ` in that directory and
                    # the pin fails on a non-empty listing rather than on a `None` that was
                    # fixed by construction.
                    out_path = None
                    json_arg = "   "
                elif json_target == "relative":
                    # A *legitimately* relative `--json` target. The drive chdirs into the
                    # empty scratch cwd below, so `verdict.json` names a file inside it and
                    # the payload really is written there. That makes this the one case whose
                    # `leftovers` listing is supposed to come back non-empty — the positive
                    # control for the witness every other pin uses only to assert emptiness
                    # (PRO-378 NB-C). A `leftovers` that had quietly degraded to always
                    # returning `[]` would pass all of those and fail here.
                    out_path = Path(td) / "cwd" / "verdict.json"
                    json_arg = "verdict.json"
                else:
                    out_path = None
                    json_arg = None
                argv = ["--compose-root", str(tree), "--policy", str(policy)]
                if json_arg is not None:
                    argv += ["--json", json_arg]
                # Run the drive inside an empty scratch cwd. Everything `main()` is handed
                # here is absolute, so no case depends on the process cwd — which is the
                # point: a stray relative write has nowhere to land except this directory,
                # where `leftovers` can see it. Without this the whitespace pin's
                # "wrote no junk file" assertion is a tautology, because `out_path` is
                # already None for that case and a real `   ` file would simply appear in
                # the repo unnoticed (PRO-361 NB1).
                scratch = Path(td) / "cwd"
                scratch.mkdir()
                cwd_before = os.getcwd()
                rc = None
                raised = None
                leftovers = None
                try:
                    os.chdir(scratch)
                    try:
                        rc = P.main(argv)
                    except BaseException as exc:  # a fixture may raise one on purpose
                        if not expect_raises:
                            raise
                        raised = type(exc).__name__
                finally:
                    os.chdir(cwd_before)
                leftovers = sorted(p.name for p in scratch.iterdir())
                payload = None
                if out_path is not None and out_path.is_file():
                    payload = out_path.read_bytes()
        finally:
            (P.Preflight, P.render, P._render_or_say_why,
             sys.stdout, sys.stderr, sys.__stderr__) = original
        return types.SimpleNamespace(rc=rc, raised=raised, payload=payload,
                                      leftovers=leftovers,
                                      out=stdout.value(), err=stderr.value(),
                                      fallback=fallback.value(),
                                      payload_existed_at_render=seen.get("payload_existed"),
                                      payload_bytes_at_render=seen.get("payload_bytes"),
                                      payload_read_error=seen.get("payload_read_error"))

    def test_the_payload_survives_a_stdout_that_refuses_to_flush(self):
        """PRO-313 review B1 state S2: the printed half must not cost the evidence.

        `_emit`'s `flush()` used to sit outside its own guard, so a stream that refused
        the flush took the payload with it — finding B one layer down.
        """
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(refuse="flush"),
                               self._ReportingStream())
        self.assertIsNotNone(res.payload,
                             "the terminal refused us and the machine-readable verdict "
                             "went with it, which is finding B again")
        self.assertEqual(P.EXIT_FAIL, res.rc)
        back = json.loads(res.payload.decode("ascii"))
        self.assertEqual("fail", back["result"])
        self.assertEqual(self.RAW, back["findings"][0]["file"])
        note = res.fallback.decode("ascii")
        self.assertIn("could not write the report", note,
                      "a swallowed presentation failure is as useless as a traceback")
        self.assertIn("BrokenPipeError", note)

    def test_a_signal_during_presentation_still_leaves_the_evidence_written(self):
        """Ordering is the last line of defence, so pin it by behaviour, not by spelling.

        `_emit` guards everything but `BaseException` — a Ctrl-C or a `SystemExit` is
        not ours to swallow — and `_render_or_say_why` guards `render()`. So the failure
        that can still escape the presentation half is one we deliberately let through.
        Writing the payload first is what makes that survivable; the old
        `assertLess(src.index("_emit…"), src.index("_write_json_payload"))` only
        pretended to protect it, and forbade the fix (PRO-313 review B1).
        """
        class Escape(self._ReportingStream):
            def write(self, s):
                raise KeyboardInterrupt("fixture: Ctrl-C during the report")

        res = self._drive_main(Escape(), self._ReportingStream(), expect_raises=True)
        self.assertEqual("KeyboardInterrupt", res.raised,
                         "a signal is not ours to swallow; if this stops escaping, this "
                         "test has stopped testing the ordering")
        self.assertIsNotNone(res.payload,
                             "presentation was still holding the evidence hostage")
        self.assertEqual(self.RAW, json.loads(res.payload.decode("ascii"))["findings"][0]["file"])
        self.assertIsNone(res.rc)

    def test_a_stream_that_refuses_the_write_at_all_still_does_not_raise(self):
        """Presentation failures are named on a fallback stream, never re-raised."""
        import sys
        from preflight_worlddb import _emit
        for refuse, landed in (("write", b""), ("flush", b"verdict text\n")):
            stream = self._ReportingStream(refuse=refuse)
            fallback = self._ReportingStream()
            original = sys.__stderr__
            sys.__stderr__ = fallback
            try:
                _emit(stream, "verdict text")      # must not raise
            finally:
                sys.__stderr__ = original
            self.assertEqual(landed, stream.value(),
                             f"the {refuse} refusal changed what reached the stream")
            note = fallback.value().decode("ascii")
            self.assertIn("could not write the report", note)
            self.assertIn("BrokenPipeError", note)
            self.assertIn("_ReportingStream", note,
                          "the operator cannot chase a failure that names no stream")

    def test_a_fallback_stream_that_refuses_too_still_does_not_raise(self):
        """The last resort has to be a last resort."""
        import sys
        from preflight_worlddb import _emit
        original = sys.__stderr__
        sys.__stderr__ = self._ReportingStream(refuse="write")
        try:
            _emit(self._ReportingStream(refuse="write"), "verdict text")
        finally:
            sys.__stderr__ = original

    def test_the_printed_verdict_and_the_payload_both_land_on_a_strict_stream(self):
        """Both halves, in the shape PRO-308 measured — not one at a time."""
        res = self._drive_main(self._ReportingStream(), self._ReportingStream())
        text = res.out.decode("ascii")
        self.assertIn("RESULT: FAIL", text)
        self.assertIn("dirty-worktree", text, "the verdict arrived without its reason")
        self.assertIn("qa_raw", text)
        self.assertIn("\\udcff", text, "the offending name was dropped, not escaped")
        self.assertIsNotNone(res.payload)
        self.assertTrue(res.payload.isascii(),
                        "a non-ASCII byte in the payload makes the write depend on the "
                        "host locale again, which is the whole finding")
        self.assertEqual(self.RAW, json.loads(res.payload.decode("ascii"))["findings"][0]["file"])
        # Negative control: the fixture really is the shape under test — the same text
        # written the way `main()` used to write it still raises.
        probe = self._ReportingStream()
        with self.assertRaises(UnicodeEncodeError):
            probe.write("data/sql/x/\udcff.sql\n")

    def test_a_payload_we_cannot_write_costs_the_boot_but_not_the_verdict(self):
        """PRO-313 review B1 state S4, reversed: the halves are held apart."""
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               json_target="missing-dir")
        self.assertIn("RESULT: FAIL", res.out.decode("ascii"),
                      "the payload half took the printed verdict with it")
        self.assertIsNone(res.payload)
        err = res.err.decode("ascii")
        self.assertIn("could not write the --json payload", err)
        self.assertIn("boot is refused", err)
        self.assertEqual(P.EXIT_ERROR, res.rc,
                         "a run that was asked for evidence and produced none must not "
                         "let the boot through on the checks' own rc")

    # PRO-359 (PRO-348 review N2). `ops/deploy/worldserver-up.sh` refuses the boot on any
    # non-zero gate exit, then tells the operator to escalate to Scuff (SQL) *unless* the
    # gate printed one of two quoted lines — those two are an environment failure or an
    # evidence-write failure, not a finding about anyone's SQL. Nothing on the gate side
    # pinned either string, so a rename here silently mis-routes whoever is locked out:
    # the wrapper quotes fragments of what the gate emits, so the gate owns the wording
    # and the wrapper owns the promise. Each pair is (the fragment the wrapper quotes
    # verbatim, the prefix the gate must put on the line); the halves are checked against
    # each other before either test trusts them. The fragments are stored bare; the
    # wrapper-side check adds the quoting quotes back, so a reworded quote style in the
    # shell is still a failure rather than an accidental substring hit.
    WRAPPER_EXEMPTED_GATE_LINES = (
        ("preflight: operational error:", "preflight: operational error: "),
        ("could not write the --json payload",
         "preflight: could not write the --json payload to "),
    )
    # The third thing the refusal block quotes is not exempted at all: `'!!'` is what
    # still *counts* as a finding about the operator's SQL. It is in the pinned set
    # because PRO-359 review B1 showed a guard that only looked at `preflight:`-prefixed
    # quotes cannot see the rest of the sentence it is supposed to be protecting.
    WRAPPER_FINDING_MARKER_QUOTED = "!!"
    WRAPPER_QUOTED_FRAGMENTS = tuple(
        frag for frag, _ in WRAPPER_EXEMPTED_GATE_LINES
    ) + (WRAPPER_FINDING_MARKER_QUOTED,)
    WRAPPER = HERE.parent / "deploy" / "worldserver-up.sh"
    # Anchor for the refusal block, and nothing looser: the wrapper prints other
    # `REFUSING BOOT` lines from the lease checks, and the guard below has to cover one
    # sentence, not a 300-line shell script (PRO-359 review B1).
    REFUSAL_BLOCK_ANCHOR = "REFUSING BOOT — pre-flight failed"
    # The statement that opens the refusal block, in column 0. PRO-413 re-review B1: the
    # guard locates the block by the anchor, so this is the one line the anchor-based
    # search cannot see for itself, and it is the line the block's whole body sits inside.
    REFUSAL_BLOCK_OPENER = "if (( rc != 0 )); then"

    # PRO-413 review B1: the safety net is this constant, not the rule that finds the
    # block. The region below is lines 461-538 of `worldserver-up.sh` as merged — the
    # `if (( rc != 0 )); then` that opens the refusal block through the `fi` that closes
    # it — and the guard refuses any wrapper whose scanned region is not byte-identical
    # to it.
    #
    # PRO-413 re-review B1 is why the golden starts on the opener and not on the anchor.
    # `_refusal_block_from_lines()` finds the block by its anchor, and the anchor is the
    # block's *second* line: pinning from the anchor down leaves the one line between the
    # opener and the anchor inside the block bash runs but outside the region compared to
    # the golden, which is a gap a fourth single-quoted notice can be dropped into. The
    # opener is therefore part of the golden, and `REFUSAL_BLOCK_OPENER` — asserted to be
    # the line directly above the anchor before anything is scanned — is what makes that
    # top edge a checked fact rather than an assumption.
    #
    # Bounding the region by a rule was the design PRO-407 found fail-open, and PRO-413's
    # nesting bound narrowed but did not close it: both bounds key on "a line whose
    # comment-stripped content starts with `fi`", so a column-0 `fi` inside a heredoc body,
    # or an opener the line-local counter cannot see (`: ; if true; then`), moves BOTH to
    # the same wrong line and the region narrows with both bounds agreeing (review B1
    # counterexamples, both pinned as mutants of their own in
    # `test_a_quoted_notice_under_a_col_0_fi_the_counter_cannot_see_fails_the_frozen_
    # block`). A frozen region cannot be narrowed at all.
    #
    # So editing the refusal block is a coordinated change: this constant moves with it, in
    # the same commit, exactly as `test_every_fragment_the_wrapper_quotes_is_a_line_the_gate_
    # really_emits` already requires of the wrapper's wording. The fragment equality below
    # stays, because the constant alone would also pass a wrapper that had stopped quoting
    # the gate's lines at all.
    WRAPPER_REFUSAL_BLOCK_FROZEN = r"""if (( rc != 0 )); then
  tell "REFUSING BOOT — pre-flight failed (exit $rc)."
  has_finding=0
  has_payload_failure=0
  has_operational_error=0
  has_policy_missing=0
  # The markers can only be read when there is a capture to read. With no capture every
  # flag stays 0 and routing falls to the rc-only rules below — which is the honest
  # degradation: the wrapper can still tell a verdict (rc 1) from an environment failure,
  # it just cannot name which environment failure it saw.
  if [[ -n "$GATE_DIR" ]]; then
    if gate_printed_a_finding "$GATE_OUT" "$GATE_ERR"; then
      has_finding=1
    fi
    # Single-quoted, and load-bearing as quotes: the refusal block is pinned to quote
    # these two fragments verbatim because the operator-facing sentence below quotes them
    # too (PRO-382 §1), so the matcher and the promise have to be the same string.
    if gate_printed 'could not write the --json payload' "$GATE_OUT" "$GATE_ERR"; then
      has_payload_failure=1
    fi
    if gate_printed 'preflight: operational error:' "$GATE_OUT" "$GATE_ERR"; then
      has_operational_error=1
    fi
    if gate_printed_a_policy_notice "$GATE_OUT" "$GATE_ERR"; then
      has_policy_missing=1
    fi
  fi

  # rc 1 IS a verdict even if the capture came back empty — a terminal may have refused the
  # bytes on the way here — so the SQL routing keys on rc as well as on the marker. The
  # marker is what adds: a run that carried real findings AND lost its payload exits 2, and
  # still has to reach Scuff (SQL).
  if (( has_finding == 1 || rc == 1 )); then
    tell "ac-worldserver was not started. Fix the named file(s) or"
    tell "                  escalate to Scuff (SQL)."
  fi
  if (( has_payload_failure == 1 )); then
    # The first half is always safe: it names the payload problem and points at the path,
    # which is true whether or not the run also carried SQL findings.
    tell "the run asked for a --json evidence payload and the gate could not write it."
    tell "                  Its own line above names the path; the directory has to exist" >&2
    tell "                  and be writable by this user. Fix the path and re-run."
    if (( has_finding == 0 )); then
      # The closing disclaimer — "not a finding about your SQL, the SQL lane is not where
      # it belongs" — is only honest when nothing on screen contradicts it. When a `!!`
      # finding is also present the escalation above just sent the operator to Scuff (SQL);
      # printing "the SQL lane is not where it belongs" two lines later is the PRO-370
      # contradiction one level up, so this arm ends at `Fix the path and re-run.` instead
      # (PRO-381 review NB).
      tell "                  This is an evidence-write failure, not a finding about your"
      tell "                  SQL, so the SQL lane is not where it belongs."
    fi
  fi
  if (( has_operational_error == 1 )); then
    tell "a 'preflight: operational error:' line is the gate failing to reach or set up"
    tell "                  its environment — usually ac-database. Nothing was started,"
    tell "                  and this is not a finding about your SQL."
  fi
  if (( has_policy_missing == 1 )); then
    # Routed like the other environment arms and never to the SQL lane (PRO-381 review
    # NB); the notice it matches lives with `gate_printed_a_policy_notice`.
    tell "the gate could not find its policy file, so the checkout it was called from is"
    tell "                  incomplete or mis-deployed. Nothing was started, and this is"
    tell "                  not a finding about your SQL."
  fi
  if (( has_finding == 0 && has_payload_failure == 0 && has_operational_error == 0 \
        && has_policy_missing == 0 && rc != 1 )); then
    # Worded so it cannot misroute in either direction: the wrapper saw no finding line, but
    # it also cannot prove there was one it failed to capture.
    tell "ac-worldserver was not started, and the gate printed neither a finding line nor"
    tell "                  one of its own failure notices, so the wrapper cannot say"
    tell "                  whether this is about your SQL. A '!!' line in the output above"
    tell "                  is a finding about your SQL; a line about the environment of"
    tell "                  the gate itself is not."
  fi
  tell "                  Do not bypass this gate."
  exit "$rc"
fi
"""
    # Compared as lines, so the diff in the failure message points at a line of the
    # block and not at an offset in a string.
    WRAPPER_REFUSAL_BLOCK_FROZEN_LINES = WRAPPER_REFUSAL_BLOCK_FROZEN.splitlines()

    # Structural `if`/`fi` tokens, matched only against a comment-stripped line and only
    # where the keyword IS the statement. Both halves of that matter: `# rc 1 IS a verdict
    # even if the capture came back empty` is a real line of the refusal block, and a
    # `\bif\b` anywhere-in-line count books it as an opener, which then needs a `fi` that
    # never comes and hides the tail of the block from the guard.
    #
    # The price is a premise, and PRO-413 review B1 is what named it: the counter sees a
    # keyword only when it starts its own stripped line and only when the line is read on
    # its own, so `: ; if true; then` has no opener and a `fi` inside a heredoc body looks
    # like a real one. Those two shapes are what the bound below walks into, and they are
    # why `WRAPPER_REFUSAL_BLOCK_FROZEN` — not this counter — is what the three-fragment
    # claim rests on.
    SHELL_IF_TOKEN = re.compile(r"if(\s|$)")
    SHELL_FI_TOKEN = re.compile(r"fi(\s|$)")

    @staticmethod
    def _without_shell_comment(line: str) -> str:
        """`line` up to the first `#` that can actually start a shell comment.

        Quote-aware, because `tell "… # …"` is operator prose and not a comment. It is
        line-local though: it carries no quote opened on an earlier line and knows nothing
        of heredoc bodies, which is exactly why the nesting bound built on it is a
        diagnostic and the frozen region is the check that matters (PRO-413 review B1).
        """
        out = []
        quote = ""
        i = 0
        while i < len(line):
            ch = line[i]
            if quote:
                if quote == '"' and ch == "\\":
                    out.append(ch)
                    i += 1
                    if i < len(line):
                        out.append(line[i])
                        i += 1
                    continue
                if ch == quote:
                    quote = ""
            elif ch == "\\":
                out.append(ch)
                i += 1
                if i < len(line):
                    out.append(line[i])
                    i += 1
                continue
            elif ch in "'\"":
                quote = ch
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                break
            out.append(ch)
            i += 1
        return "".join(out)

    def _shell_if_fi(self, line: str) -> tuple:
        """(opens, closes) for one line — a structural `if` and/or `fi`, 0 or 1 each.

        One token of each kind per line, which is the shape `worldserver-up.sh` is written
        in and the shape a reviewer can read at a glance. Two edits do not fit it: a
        `if …; then …; fi` one-liner reads as unbalanced, and so does a `tell` whose string
        is split across lines. Both then make the two bounds disagree, which is the loud
        direction and the one wanted — the alternative is a guard quietly scanning less
        than the block. Whoever hits the multi-line `tell` failure is looking at an `if`/`fi`
        problem even though the edit only touched prose.
        """
        stripped = self._without_shell_comment(line).strip()
        return (1 if self.SHELL_IF_TOKEN.match(stripped) else 0,
                1 if self.SHELL_FI_TOKEN.match(stripped) else 0)

    def _refusal_block_from_lines(self, lines: list, name: str) -> str:
        """The lines the wrapper prints when the gate exits non-zero — that block only.

        The exemption is one sentence inside one `if`; the no-widening guard and the
        wrapper-read test both need exactly that scope. Scanning the whole file is what
        let PRO-359's guard miss a fragment for reasons unrelated to the exemption
        (PRO-359 review B1).

        The check that carries the property is the last one: the region has to be
        byte-identical to `WRAPPER_REFUSAL_BLOCK_FROZEN`, so it cannot be narrowed,
        widened, re-indented or reworded without this suite saying so. The region is the
        whole executed block — the column-0 `if (( rc != 0 )); then` through the `fi` that
        closes it — and the top edge is pinned too: `start` is found from the anchor, so the
        line above the anchor is asserted to BE that opener before anything is scanned,
        which is what stops the golden from describing a block that starts one line below
        the one bash runs (PRO-413 re-review B1). The history is why it is a frozen region
        and not a rule: PRO-407 found "the first `fi` in column 0" fail-open, PRO-413
        replaced that with a nesting balance plus the column-0 `fi` as a
        cross-check, and review B1 produced two `bash -n`-clean wrappers that move BOTH
        bounds onto the same wrong line — a `fi` sitting in a heredoc body, and a
        `: ; if true; then` opener the line-local counter cannot see. A frozen region is
        not a bound, so there is nothing left for two of them to agree about.

        The bounds are kept because each names its own failure far better than a diff does,
        and the nesting balance is what refuses a block that never closes. They are not two
        independent tests: both fire on "a line whose stripped content begins with `fi`",
        so agreement is evidence about an indentation-only move of a real `fi` — the case
        PRO-407 actually found, and the one the column filter exists for — and about
        nothing else.
        """
        anchors = [i for i, ln in enumerate(lines) if self.REFUSAL_BLOCK_ANCHOR in ln]
        if not anchors:
            self.fail(f"{name} no longer prints {self.REFUSAL_BLOCK_ANCHOR!r}. "
                      "The gate's exit-code contract and this suite's view of the refusal "
                      "wording both key on that line, so a rename here is not cosmetic.")
        if len(anchors) > 1:
            self.fail(f"{name} prints {self.REFUSAL_BLOCK_ANCHOR!r} on "
                      f"{len(anchors)} lines, {[i + 1 for i in anchors]}. The region is read "
                      "from the first one, so a second refusal block would be scanned by "
                      "nothing and the three-fragment claim would cover part of what the "
                      "operator is actually told (PRO-413 review N3).")
        start = anchors[0]
        # The top edge, checked before anything downstream of it. The anchor is how the
        # block is *found*; it is not evidence that the block starts there. Without this,
        # everything above the anchor is inside the block bash runs and outside the region
        # compared to the golden, and PRO-413 re-review B1 is exactly that gap: a fourth
        # single-quoted `tell` dropped between the opener and the anchor reaches the
        # operator on every refused boot while the scan reads the frozen three.
        if start == 0 or lines[start - 1] != self.REFUSAL_BLOCK_OPENER:
            self.fail(
                f"the refusal block in {name} is not the block this suite pins: the line "
                f"above {self.REFUSAL_BLOCK_ANCHOR!r} is {None if start == 0 else lines[start - 1]!r}, "
                f"not the column-0 opener {self.REFUSAL_BLOCK_OPENER!r}. The golden region "
                "starts on that opener, so whatever sits between it and the anchor is part "
                "of what the operator is handed on a refused boot and has to be reviewed "
                "with the block (PRO-413 re-review B1).")
        anchor_opens, anchor_closes = self._shell_if_fi(lines[start])
        if anchor_opens or anchor_closes:
            self.fail(f"the {name} line holding {self.REFUSAL_BLOCK_ANCHOR!r} is itself a "
                      f"structural `if`/`fi` ({lines[start]!r}), so the depth the refusal "
                      "block sits at is ambiguous and the region cannot be bounded")
        depth = 1  # the anchor sits in the body of the `if` this block is bounded by
        end = None
        for i in range(start + 1, len(lines)):
            opens, closes = self._shell_if_fi(lines[i])
            depth += opens - closes
            if depth == 0:
                end = i
                break
        if end is None:
            self.fail(f"no `fi` closes the refusal block opened at {self.REFUSAL_BLOCK_ANCHOR!r} "
                      f"in {name}; the block cannot be bounded, so neither can "
                      "the claim about what it quotes")
        col0 = next((i for i in range(start, len(lines)) if lines[i] == "fi"), None)
        if col0 != end:
            where = ("there is no `fi` in column 0 at or below the anchor at all"
                     if col0 is None else
                     f"the first `fi` in column 0 is line {col0 + 1}: {lines[col0]!r}")
            self.fail(f"the refusal block in {name} cannot be bounded by a single rule: "
                      f"`if`/`fi` nesting balance closes it at line {end + 1}, but "
                      f"{where}. Both bounds have to land on the same `fi`; until they do, "
                      "one of them is cutting the region short and the guard would be "
                      "scanning a block that has already ended (PRO-413). Either re-indent "
                      "the arm that closes in column 0, or fix the nesting — do not relax "
                      "this check.")
        # Pinned on BOTH edges: inclusive of the opener one line above the anchor and of the
        # closing `fi`, so "the block starts here and ends here" is a frozen fact too and not
        # something a rule — or the anchor search — has to keep finding correctly.
        block = lines[start - 1:end + 1]
        if block != self.WRAPPER_REFUSAL_BLOCK_FROZEN_LINES:
            diff = "\n".join(difflib.unified_diff(
                self.WRAPPER_REFUSAL_BLOCK_FROZEN_LINES, block,
                fromfile="the pinned refusal block",
                tofile=f"{name} lines {start}-{end + 1}", lineterm="", n=1))
            self.fail("the refusal block this suite scans is not the block it pins. The "
                      f"scan reads {len(block)} line(s) of {name} "
                      f"({start}-{end + 1}); the pinned block has "
                      f"{len(self.WRAPPER_REFUSAL_BLOCK_FROZEN_LINES)}:\n{diff}\n\n"
                      "The pinned region is what the claim that the refusal block quotes "
                      "exactly the two exempted gate lines plus the finding marker rests on "
                      "(PRO-413 review B1): a rule that finds the end of the block can be "
                      "moved by text the counter cannot see, and `bash -n` does not notice "
                      "it move. If the block really did change, move "
                      "`WRAPPER_REFUSAL_BLOCK_FROZEN` in the same commit as the wrapper and "
                      "expect "
                      "`test_every_fragment_the_wrapper_quotes_is_a_line_the_gate_really_"
                      "emits` to hold the new wording against what the gate really emits.")
        # The scan still reads the body only: the closing `fi` is pinned above,
        # and a bare `fi` carries no fragment for the quote scan to find.
        return "\n".join(block[:-1])

    def _wrapper_refusal_block(self) -> str:
        return self._refusal_block_from_lines(
            self.WRAPPER.read_text(encoding="utf-8").splitlines(), self.WRAPPER.name)

    def _quoted_fragments_from_region(self, region: str) -> list:
        """Every single-quoted fragment in an already-bounded region, deduplicated.

        Comment-stripped line by line first (PRO-413 review N2): the block carries a
        comment per arm, and `# the gate's own words, quoted: 'like this'` is prose about
        the wrapper rather than a fragment the operator is handed, so it must not be booked
        as one. The strip is the same quote-aware one the token counter uses, and it is
        just as line-local — a fragment on a continuation line of a string opened earlier
        can be read as a comment. That is the direction to be worried about, and it is the
        frozen region above that refuses the edit, not this scan.
        """
        return sorted(set(re.findall(
            r"'([^']*)'",
            "\n".join(self._without_shell_comment(ln) for ln in region.splitlines()))))

    def _quoted_fragments_from_lines(self, lines: list, name: str) -> list:
        """Every single-quoted fragment inside the refusal block, deduplicated."""
        return self._quoted_fragments_from_region(
            self._refusal_block_from_lines(lines, name))

    def _wrapper_quoted_fragments(self) -> list:
        """Every single-quoted fragment inside the refusal block, deduplicated."""
        return self._quoted_fragments_from_region(self._wrapper_refusal_block())

    def _line_the_operator_matches(self, err: str, *, quoted: str, prefix: str) -> str:
        """The stderr line the wrapper's exemption is about, or a failure that says so.

        The wrapper does not grep: the exemption is prose inside an `echo`, and the
        operator is the matcher. So the relation pinned here is the one the operator
        performs — a quoted fragment has to turn up inside a line the gate really wrote
        to stderr — with the stricter half on top of it, because the line *starts with*
        the prefix and that is what a rename breaks first.
        """
        self.assertIn(quoted, prefix,
                      f"the fixture drifted: {quoted} is not a fragment of {prefix!r}, "
                      "so the pairing above no longer describes gate and wrapper")
        lines = [ln for ln in err.splitlines() if ln.startswith(prefix)]
        if not lines:
            self.fail(f"the gate wrote no line starting with {prefix!r} to stderr. The "
                      f"refusal block exempts '{quoted}' from the 'escalate to Scuff (SQL)' "
                      "routing by quoting that line, so an operator who hits this shape "
                      "is now being routed by a string the gate does not emit. stderr "
                      f"held: {err!r}")
        self.assertIn(quoted, lines[0],
                      f"the line opens with {prefix!r} but no longer carries the "
                      f"fragment '{quoted}' the wrapper quotes")
        return lines[0]

    def test_the_operational_error_line_carries_the_prefix_the_wrapper_exempts(self):
        """PRO-359: the environment half of a refused boot has a name, and it is pinned.

        `main()` catches `OperationalError` around the whole run — a docker that is not
        there, a query that timed out, a scratch schema it dare not touch — and answers
        with `EXIT_ERROR` and one line on stderr. That line is the only thing standing
        between an environment failure and a finding about someone's SQL, which is the
        exact distinction the wrapper is drawing to whoever is locked out.
        """
        import preflight_worlddb as P
        quoted, prefix = self.WRAPPER_EXEMPTED_GATE_LINES[0]
        reason = "cannot execute docker: [Errno 2] No such file or directory"
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               op_error=reason)
        self.assertEqual(P.EXIT_ERROR, res.rc,
                         "an operational failure has to be the wrapper's non-zero exit; "
                         "a check-derived rc here means this is not that path")
        line = self._line_the_operator_matches(res.err.decode("ascii"),
                                               quoted=quoted, prefix=prefix)
        self.assertIn("cannot execute docker", line,
                      "the prefix arrived without the reason, which is PRO-308 finding B "
                      "in the wrapper's clothing")

    def test_the_payload_write_line_carries_the_prefix_the_wrapper_exempts(self):
        """PRO-359: the evidence half, pinned the same way.

        `test_a_payload_we_cannot_write_costs_the_boot_but_not_the_verdict` above holds
        the two halves apart and greps part of the line; what it cannot do is notice a
        rename, because its needle is a phrase from the sentence rather than the prefix
        the wrapper quotes. This is the emit-side assertion PRO-348 review N2 asked for,
        on the shape PRO-349 measured on the deploy host.
        """
        import preflight_worlddb as P
        quoted, prefix = self.WRAPPER_EXEMPTED_GATE_LINES[1]
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               json_target="missing-dir")
        self.assertEqual(P.EXIT_ERROR, res.rc,
                         "no evidence and no refusal is the hole this pair exists to keep")
        self.assertIsNone(res.payload)
        line = self._line_the_operator_matches(res.err.decode("ascii"),
                                               quoted=quoted, prefix=prefix)
        self.assertIn("no-such-dir", line,
                      "the line no longer names the path the operator has to go and fix")
        self.assertRegex(line, r": \w+Error: \S",
                         "an untyped write failure leaves the operator unable to tell a "
                         "missing directory from a full disk")

    def test_the_wrapper_still_quotes_both_gate_lines_the_gate_emits(self):
        """The pairing is two-sided, so the other half is pinned too (PRO-359).

        This suite does not own the wrapper's wording — `worldserver-up.sh` is frozen as
        merged and its exemption is prose — but it does own the claim that these are the
        two fragments an operator locked out is handed. Read it as the seam: whenever the
        wrapper is reworded, its exemption and the two emitter lines move together, or a
        one-sided rename routes an environment failure to Scuff (SQL) and a real finding
        to nobody.
        """
        block = self._wrapper_refusal_block()
        for frag in self.WRAPPER_QUOTED_FRAGMENTS:
            self.assertIn(f"'{frag}'", block,
                          f"'{frag}' is no longer quoted in the refusal block of "
                          f"{self.WRAPPER.name}, yet the gate still emits that line and "
                          "the block still routes everything else to Scuff (SQL). Rename "
                          "both sides or neither (PRO-348 review N2).")
        # Widening is the other direction this seam can fail, and PRO-346 already
        # answered it: no taxonomy an operator has to parse while locked out, so no third
        # exempted gate line may join the sentence. The scan is scoped to the refusal
        # block and compares the WHOLE quoted set, not the `preflight:`-prefixed subset —
        # PRO-359 review B1: the earlier guard could not see `'could not write the --json
        # payload'` (no `preflight:` prefix), could not see widening through a non-
        # `preflight:` quote, and fired on quoted prose anywhere else in the file.
        self.assertEqual(sorted(self.WRAPPER_QUOTED_FRAGMENTS),
                         self._wrapper_quoted_fragments(),
                         "the refusal block quotes something other than the two exempted "
                         "gate lines plus the finding marker; it is three quoted fragments "
                         "and PRO-346 rejected adding a fourth")

    # Every shape `main()` can leave the operator in, so the wrapper-read test below has
    # the complete set of lines the gate really writes to compare against: the printed
    # verdict, the degraded verdict, the operational error, and the refused payload.
    # `render_raises` and `op_error` are the two failure paths; `missing-dir` is the
    # evidence-write path; the plain drive is a normal refused boot with findings.
    OPERATOR_FACING_SHAPES = (
        ("verdict", {}),
        ("degraded verdict", {"render_raises": True}),
        ("operational error", {"op_error": "cannot execute docker: [Errno 2] "
                                           "No such file or directory"}),
        ("refused payload", {"json_target": "missing-dir"}),
    )
    # Each fragment the refusal block quotes, and the one shape that has to show it.
    # Naming the shape is not decoration: pooled into one corpus, `'!!'` is satisfied by
    # the render-failure apology alone and the findings listing could stop marking
    # findings without anyone noticing, which is the half of the wrapper's sentence that
    # tells the operator what still counts as their problem.
    WRAPPER_QUOTED_EVIDENCE = (
        ("preflight: operational error:", "operational error"),
        ("could not write the --json payload", "refused payload"),
        ("!!", "verdict"),
    )

    def _lines_the_gate_really_emits(self) -> dict:
        """shape name -> every line `main()` writes to either stream in that shape.

        Driven, not read from source: the point of the test that uses this is that the
        wrapper quotes a string the gate *actually puts on the terminal*, so the corpus
        has to come from running the gate.
        """
        emitted = {}
        for name, kwargs in self.OPERATOR_FACING_SHAPES:
            res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                                   **kwargs)
            lines = []
            for blob in (res.out, res.err):
                lines.extend(blob.decode("ascii", "replace").splitlines())
            emitted[name] = lines
        return emitted

    def test_every_fragment_the_wrapper_quotes_is_a_line_the_gate_really_emits(self):
        """PRO-382 §1 (PRO-370 finding F2): the wrapper's quoted prefixes have no pin.

        `worldserver-up.sh` hands the operator quoted fragments of gate output and says
        which ones are not that operator's problem. `grep -rn "escalate to Scuff"`
        reaches only the wrapper, so before this test nothing tied those quotes to what
        the gate writes: reword the emitter and the wrapper keeps quoting a sentence no
        terminal will ever show, and the operator locked out of a boot is routed by a
        string that no longer exists. The gate owns the wording; this is the half that
        makes the wrapper's promise an assertion instead of a transcription.

        Deliberately derived from the wrapper rather than from the fixture table above:
        reading the file is what catches a fragment being *added* to the exemption, and
        driving `main()` rather than grepping its source is what makes a rename in the
        emitter — not an edit to this file — the thing that fails here.
        """
        quoted = self._wrapper_quoted_fragments()
        self.assertEqual(sorted(self.WRAPPER_QUOTED_FRAGMENTS), quoted,
                         "the refusal block and this suite's view of it have drifted; "
                         "fix the wrapper and the pins together, in one change")
        self.assertEqual({f for f, _ in self.WRAPPER_QUOTED_EVIDENCE}, set(quoted),
                         "a quoted fragment has no witness shape, so nothing checks that "
                         "the gate still emits it")
        emitted = self._lines_the_gate_really_emits()
        for name, lines in emitted.items():
            self.assertTrue(lines, f"the gate produced nothing at all in the {name!r} "
                                   "shape; an empty corpus passes any substring test")
        for frag, shape in self.WRAPPER_QUOTED_EVIDENCE:
            lines = emitted[shape]
            self.assertTrue(
                any(frag in ln for ln in lines),
                f"{self.WRAPPER.name} quotes '{frag}' at the operator as though the gate "
                f"wrote it, but no line the gate produced in the {shape!r} shape contains "
                "it. The refusal routing turns on that string: an operator locked out is "
                "told to escalate to Scuff (SQL) unless the gate said something like it, "
                "and the gate no longer says it. Lines seen were:\n"
                + "\n".join(sorted({ln for ln in lines if ln.strip()})))

    # A fourth notice, worded like the three the block really quotes and inserted below a
    # `fi` so it is the last thing in the block. The fragment is single-quoted because that
    # is the quoting the scan reads; the apostrophes are balanced so the mutant cannot pass
    # or fail on a parity accident.
    WRAPPER_MUTANT_NOTICE = ("  tell \"a fourth notice: 'extra notice' "
                             "is not one of the gate lines\"")
    WRAPPER_MUTANT_FRAGMENT = "extra notice"

    def _arm_closing_fis(self, lines: list, start: int) -> list:
        """Every structural `fi` below the anchor that is NOT in column 0 — the arms."""
        return [i for i in range(start + 1, len(lines))
                if self._shell_if_fi(lines[i])[1] and lines[i] != "fi"]

    def _region_and_fragments_as_before_PRO_413(self, lines: list, start: int) -> list:
        """The pre-PRO-413 bound, reproduced rather than restated: first `fi` in column 0.

        This is deliberately a second, weaker implementation of the same scan. It exists so
        the negative control below can show its own teeth: if the old rule already caught a
        mutant, asserting the new rule fails on it would prove nothing.

        The scan reads the raw lines where the live one comment-strips (PRO-413 review N2).
        That asymmetry is the point — this has to reproduce the weaker behaviour the suite
        actually shipped, not be "helpfully" unified with the current scan.
        """
        end = next((i for i in range(start, len(lines)) if lines[i] == "fi"), None)
        return sorted(set(re.findall(
            r"'([^']*)'", "\n".join(lines[start:end]) if end is not None else "")))

    def test_a_column_zero_arm_fi_fails_the_bound_instead_of_the_pipeline(self):
        """PRO-413: the three-fragment property must not rest on an indentation convention.

        PRO-407 reproduced this on `9ac7a80fd`: de-indent one arm's `  fi` to column 0 and
        put a fourth single-quoted notice under it, and the refusal block is still balanced
        bash, the column-0 bound still cuts the region above the new notice, the scan still
        reports exactly the three frozen fragments, and both seam tests stay green. A
        fail-open with no backstop — the `worlddb-preflight` job does not run shellcheck.

        So the mutants here are the finding, and the assertions are about failing. The head
        is checked first only so each mutant means what it claims, and the old bound is run
        over the same mutants to prove the new one is what bites.
        """
        import subprocess

        lines = self.WRAPPER.read_text(encoding="utf-8").splitlines()
        start = next((i for i, ln in enumerate(lines)
                      if self.REFUSAL_BLOCK_ANCHOR in ln), None)
        self.assertIsNotNone(start, "the mutants have nothing to mutate without the anchor")
        arms = self._arm_closing_fis(lines, start)
        self.assertTrue(arms, "no indented arm-closing `fi` below the anchor, so there is "
                              "no arm left to mis-indent and this test is vacuous")
        # The last arm is the one that still leaves all three frozen fragments above it, so
        # truncating there is the mutation that hides rather than the one that shouts.
        last_arm = arms[-1]

        misindented = list(lines)
        misindented[last_arm] = "fi"
        misindented_with_notice = list(misindented)
        misindented_with_notice.insert(last_arm + 1, self.WRAPPER_MUTANT_NOTICE)
        notice_only = list(lines)
        notice_only.insert(arms[0] + 1, self.WRAPPER_MUTANT_NOTICE)

        # The head first: three fragments, one bound, and the mutant sites where this test
        # thinks they are. Without this the failures below could be a fixture accident.
        self.assertEqual(sorted(self.WRAPPER_QUOTED_FRAGMENTS),
                         self._wrapper_quoted_fragments())
        self.assertEqual("fi", lines[last_arm].strip())
        self.assertNotEqual("fi", lines[last_arm])

        # `bash -n` is indifferent to the de-indent. That is the whole reason this needed a
        # pin of its own: no tool already in the pipeline sees the edit.
        with tempfile.TemporaryDirectory() as td:
            for label, mutant in (("mis-indented arm", misindented),
                                  ("mis-indented arm with a fourth notice",
                                   misindented_with_notice)):
                path = Path(td) / f"{label.replace(' ', '-')}.sh"
                path.write_text("\n".join(mutant) + "\n", encoding="utf-8")
                res = subprocess.run(["bash", "-n", str(path)],
                                     capture_output=True, text=True)
                self.assertEqual(0, res.returncode,
                                 f"{label}: the mutant is not even valid bash, so it does "
                                 f"not reproduce the fail-open — it is just a broken script. "
                                 f"bash said: {res.stderr}")

        # The negative control, and the honest half of this test: the bound this suite used
        # until PRO-413 still reports the frozen three on both mutants. Green before the
        # fix, red after it — which is the only thing that makes the assertions below worth
        # having.
        for label, mutant in (("mis-indented arm", misindented),
                              ("mis-indented arm with a fourth notice",
                               misindented_with_notice)):
            self.assertEqual(
                sorted(self.WRAPPER_QUOTED_FRAGMENTS),
                self._region_and_fragments_as_before_PRO_413(mutant, start),
                f"the negative control has gone soft: the column-0 bound alone already "
                f"catches the {label!r} mutant, so asserting a failure here would not show "
                "that the nesting bound is doing any work")

        # The pin. Either arm closing in column 0 is a failure on its own, with or without
        # the notice below it — the notice is what makes it dangerous, not what makes it
        # detectable.
        for label, mutant in (("mis-indented arm", misindented),
                              ("mis-indented arm with a fourth notice",
                               misindented_with_notice)):
            with self.assertRaises(AssertionError) as cm:
                self._refusal_block_from_lines(mutant, name=f"{self.WRAPPER.name} "
                                                            f"({label})")
            msg = str(cm.exception)
            self.assertIn("column 0", msg,
                          f"{label}: the bound failed, but not for the indentation reason "
                          f"this test is about: {msg}")
            self.assertIn("nesting balance", msg,
                          f"{label}: the bound has stopped comparing the column-0 `fi` "
                          f"against the nesting balance: {msg}")

        # And the widening half still bites on its own. A fourth quote below the FIRST arm
        # is inside the block, so the two bounds agree there and nothing about the boundary
        # is wrong: it is the region content that has to refuse it. The old bound sees all
        # four fragments at that site, which is the same thing the seam tests would have
        # seen, so the widening guard keeps its teeth and the pinned region refuses the edit
        # on top of that.
        four = sorted(list(self.WRAPPER_QUOTED_FRAGMENTS) + [self.WRAPPER_MUTANT_FRAGMENT])
        self.assertEqual(four, self._region_and_fragments_as_before_PRO_413(notice_only, start),
                         "a fourth single-quoted notice below the first arm is invisible to "
                         "the old bound too, so the widening guard has lost its teeth")
        with self.assertRaises(AssertionError) as cm:
            self._quoted_fragments_from_lines(notice_only, f"{self.WRAPPER.name} "
                                                           "(fourth notice only)")
        self.assertIn("not the block it pins", str(cm.exception),
                      "a fourth single-quoted notice inside the refusal block has to be "
                      "refused by the pinned region, not waved through on the strength of a "
                      "correct-looking boundary")

    # PRO-413 review B1. Both of these go below the last arm-closing `fi`, so all three
    # frozen fragments stay above the truncation point: the region narrows, the scan still
    # reports exactly the frozen three, and the fourth notice still reaches the terminal.
    # A heredoc whose body carries a column-0 `fi` is the realistic one — it is what
    # pasting a usage block into the refusal arms looks like — and `: ; if true; then` is
    # the structural one, an opener the line-local counter cannot see closed by a real
    # column-0 `fi`.
    WRAPPER_MUTANT_TAILS = (
        ("heredoc body carrying a column-0 `fi`", ("  cat <<EOS", "fi", "EOS")),
        ("unseen opener closed by a column-0 `fi`", (": ; if true; then", "  :", "fi")),
    )

    def test_a_quoted_notice_under_a_col_0_fi_the_counter_cannot_see_fails_the_frozen_block(
            self):
        """PRO-413 review B1: the two bounds are one test, and the frozen region is the net.

        Both bounds fire on the same textual event — a line whose comment-stripped content
        begins with `fi`. A `fi` sitting in a heredoc body is such a line and is not
        structural; a `: ; if true; then` opener is structural and is not such a line.
        Either way both bounds move together to the same wrong `fi` at the depth-1 point
        between arms, so the agreement check that PRO-413 added passes, the region narrows,
        and the scan reports the frozen three.

        That is the fail-open, and it is green: on `2aab4970e` each of these mutants leaves
        `python3 ops/preflight/test_preflight_worlddb.py` at `Ran 383 tests ... OK` with
        `bash -n` happy and the wrapper printing a fourth single-quoted notice on every
        refused boot. So the assertions here are, in order: the old bound is blind to the
        mutant, the bounds agree (which is what proves the disagreement check cannot be the
        safety net), and the frozen region refuses it.
        """
        import subprocess

        lines = self.WRAPPER.read_text(encoding="utf-8").splitlines()
        start = next((i for i, ln in enumerate(lines)
                      if self.REFUSAL_BLOCK_ANCHOR in ln), None)
        self.assertIsNotNone(start, "the mutants have nothing to mutate without the anchor")
        arms = self._arm_closing_fis(lines, start)
        self.assertTrue(arms, "no indented arm-closing `fi` below the anchor, so there is "
                              "no between-arms point to insert at and this test is vacuous")
        last_arm = arms[-1]

        # The head first, so a failure below means the mutant and not the fixture.
        self.assertEqual(sorted(self.WRAPPER_QUOTED_FRAGMENTS),
                         self._wrapper_quoted_fragments())
        self.assertEqual("fi", lines[last_arm].strip())
        tail = lines[last_arm + 1:]
        self.assertTrue(any("Do not bypass this gate." in ln for ln in tail),
                        "the last arm is no longer above the closing notices, so a mutant "
                        "inserted there would not be inside the block")

        for label, extra in self.WRAPPER_MUTANT_TAILS:
            mutant = list(lines)
            mutant[last_arm + 1:last_arm + 1] = list(extra) + [self.WRAPPER_MUTANT_NOTICE]
            name = f"{self.WRAPPER.name} ({label})"

            # The mutant has to be a script bash accepts and an operator would run: a
            # broken mutant proves nothing about a guard that never parses shell.
            with tempfile.TemporaryDirectory() as td:
                path = Path(td) / "mutant.sh"
                path.write_text("\n".join(mutant) + "\n", encoding="utf-8")
                res = subprocess.run(["bash", "-n", str(path)],
                                     capture_output=True, text=True)
                self.assertEqual(0, res.returncode,
                                 f"{label}: not valid bash, so this is a broken script and "
                                 f"not the fail-open under test. bash said: {res.stderr}")

            # The notice is live, not decoration: it sits above the two lines that close
            # the block — the `Do not bypass this gate.` notice and `exit "$rc"` — so
            # every refused boot prints it, and it sits below the line both bounds chose.
            below = mutant[last_arm + 1:]
            self.assertEqual(self.WRAPPER_MUTANT_NOTICE, below[len(extra)])
            self.assertLess(below.index(self.WRAPPER_MUTANT_NOTICE),
                            next(i for i, ln in enumerate(below)
                                 if "Do not bypass this gate." in ln),
                            f"{label}: the fourth notice is no longer inside the block, so "
                            "the mutant would not be handing the operator a fourth fragment")

            # Negative control: the bound this suite shipped before PRO-413 is blind.
            self.assertEqual(
                sorted(self.WRAPPER_QUOTED_FRAGMENTS),
                self._region_and_fragments_as_before_PRO_413(mutant, start),
                f"the negative control has gone soft: the column-0 bound alone already "
                f"catches the {label!r} mutant, so the assertions below would not show "
                "anything about the nesting bound")

            # The bounds agree. Had they disagreed, the failure would name both of them,
            # and this test would be a duplicate of the one above rather than the reason
            # the frozen region exists.
            with self.assertRaises(AssertionError) as cm:
                self._refusal_block_from_lines(mutant, name=name)
            msg = str(cm.exception)
            self.assertIn("not the block it pins", msg,
                          f"{label}: the frozen region did not refuse the mutant: {msg}")
            self.assertNotIn("cannot be bounded by a single rule", msg,
                             f"{label}: the two bounds disagreed, so this mutant no longer "
                             f"shows that their agreement is the hole — it belongs with "
                             f"`test_a_column_zero_arm_fi_fails_the_bound_instead_of_the_"
                             f"pipeline` instead: {msg}")
            self.assertLess(msg.count("\n+  tell \"a fourth notice"), 2,
                            f"{label}: the diff should name the added notice once")

        # And the pinned region is the thing doing the work at every one of these sites:
        # the frozen three are still exactly what a correct wrapper scans to.
        self.assertEqual(sorted(self.WRAPPER_QUOTED_FRAGMENTS),
                         self._quoted_fragments_from_region("\n".join(
                             self.WRAPPER_REFUSAL_BLOCK_FROZEN_LINES)))

    def _refusal_region_as_pinned_before_PRO_413_rereview(self, lines: list) -> list:
        """The golden exactly as PRO-413 shipped it: cut anchor-down, and byte-compared.

        A second, weaker copy of the pin on purpose (the same role
        `_region_and_fragments_as_before_PRO_413()` plays for the bound). The guard that
        shipped in `4a25bf0d7` cut the region from the ANCHOR to the first `fi` in column
        0 and compared those 77 lines byte-for-byte against `WRAPPER_REFUSAL_BLOCK_FROZEN_LINES`
        minus its opener line, so this reproduces both halves — the cut AND the comparison,
        which an earlier draft of this helper only claimed. The lines are raw, as they were
        at the head that shipped: byte identity against the golden is the whole check, and
        comment-stripping it would copy the wrong behaviour.

        The weakness is the cut, and it is the reason re-review B1 was possible: the region
        starts one line BELOW the block bash runs, so what sits between the opener and the
        anchor reaches neither the cut nor the comparison. A green return from here on a
        mutant is therefore the negative control — the shipped pin accepted it.
        """
        start = next(i for i, ln in enumerate(lines) if self.REFUSAL_BLOCK_ANCHOR in ln)
        end = next((i for i in range(start, len(lines)) if lines[i] == "fi"), None)
        if end is None:
            self.fail("no column-0 `fi` closes the region, so the old pin had "
                      "nothing to compare either")
        region = lines[start:end + 1]
        self.assertEqual(
            self.WRAPPER_REFUSAL_BLOCK_FROZEN_LINES[1:], region,
            "the anchor-down golden has moved, and this copy has no business moving it: "
            "it exists to show what the shipped pin could not see, byte-for-byte, so it "
            "has to stay the bytes the shipped pin compared")
        return region

    # PRO-413 re-review B1. The gap the anchor-down golden could not see is one line wide in
    # the wrapper as merged, but it is a gap and not a constant, so these mutants fill it.
    # The two-line shape is the one that matters, not the one-line shape: `gate_printed` is
    # how the block already matches a gate line, so a fourth single-quoted fragment introduced
    # that way — and echoed to the operator by the `tell` under it — keeps the scan's fragment
    # SET exactly equal to the three the two seam tests compare. A count or set assertion is
    # therefore blind to it; only a pin that covers the top of the block can see it.
    WRAPPER_MUTANT_TOP_EDGE = (
        "  gate_printed 'could not write the --json payload' \"$GATE_OUT\" \"$GATE_ERR\"",
        "  tell \"a fourth notice: 'could not write the --json payload' reaches the "
        "operator\"",
    )

    def test_a_quoted_notice_above_the_anchor_fails_the_frozen_block(self):
        """PRO-413 re-review B1: the top of the pinned region has to be pinned too.

        `_refusal_block_from_lines()` locates the block by its anchor, so before this commit
        the golden covered anchor → closing `fi` while the executed block opened one line
        higher. Re-review reproduced the hole exactly: a fourth single-quoted `tell` inserted
        between `if (( rc != 0 )); then` and the anchor is valid bash, is printed on every
        refused boot, and leaves the scanned region byte-identical to the golden — so the pin
        PRO-413 added as the safety net never looks at it.

        The assertions are in the order that makes the mutant mean something: `bash -n` is
        happy, the notice is live code inside the executed block above the arms that print
        the other notices, the fragment set an anchor-down scan reports is STILL exactly the
        three the seam tests compare (which is why a count-based check was never going to
        cover this edge), and the opener-down golden refuses it.
        """
        import subprocess

        lines = self.WRAPPER.read_text(encoding="utf-8").splitlines()
        start = next((i for i, ln in enumerate(lines)
                      if self.REFUSAL_BLOCK_ANCHOR in ln), None)
        self.assertIsNotNone(start, "the mutants have nothing to mutate without the anchor")
        # The head is what this commit asserts of every wrapper, so it is checked before
        # anything is claimed about a mutant of it.
        self.assertEqual(self.REFUSAL_BLOCK_OPENER, lines[start - 1])

        top = self.WRAPPER_REFUSAL_BLOCK_FROZEN_LINES
        self.assertEqual(self.REFUSAL_BLOCK_OPENER, top[0],
                         "the golden no longer starts on the block's opener, which is the "
                         "one property that closes the top edge PRO-413 re-review B1 found "
                         "unguarded")
        self.assertIn(self.REFUSAL_BLOCK_ANCHOR, top[1],
                      "the golden no longer opens opener-then-anchor, so the guard's view "
                      "of the block and the pinned region have drifted apart")

        mutant = list(lines)
        mutant[start:start] = list(self.WRAPPER_MUTANT_TOP_EDGE)

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "mutant.sh"
            path.write_text("\n".join(mutant) + "\n", encoding="utf-8")
            res = subprocess.run(["bash", "-n", str(path)],
                                 capture_output=True, text=True)
            self.assertEqual(0, res.returncode,
                             "the mutant is not valid bash, so this is a broken script and "
                             f"not the fail-open under test. bash said: {res.stderr}")

        # Live code, not a comment: the two lines sit between the opener and the anchor, so
        # they run before any refusal arm and before `exit "$rc"`, and the notice is one of
        # the block's own `tell` calls.
        self.assertEqual(list(self.WRAPPER_MUTANT_TOP_EDGE), mutant[start:start + 2])
        self.assertEqual(self.REFUSAL_BLOCK_OPENER, mutant[start - 1])
        self.assertTrue(any("a fourth notice" in ln for ln in mutant
                            if ln.strip().startswith("tell ")),
                        "the fourth notice is no longer a `tell`, so the mutant would not be "
                        "handing the operator a fragment at all")
        notice_pos = next(i for i, ln in enumerate(mutant) if "a fourth notice" in ln)
        bypass_pos = next(i for i, ln in enumerate(mutant) if "Do not bypass this gate." in ln)
        self.assertLess(notice_pos, bypass_pos,
                        "the fourth notice is no longer above the closing notices, so it is "
                        "no longer something every refused boot prints")

        # The negative control, and the load-bearing one: the golden as PRO-413 shipped it —
        # anchored on the anchor and byte-compared against the golden-minus-opener, both
        # halves reproduced by the helper — cannot see the inserted lines, and the fragments
        # it does see are still EXACTLY the three the seam tests compare. A green call here
        # therefore IS the shipped guard accepting this mutant. So neither
        # `test_the_wrapper_still_quotes_both_gate_lines_the_gate_emits`'s set equality nor
        # `test_every_fragment_the_wrapper_quotes_is_a_line_the_gate_really_emits`'s would
        # have said a word, while the operator was handed a fourth single-quoted notice.
        blind = self._refusal_region_as_pinned_before_PRO_413_rereview(mutant)
        for inserted in self.WRAPPER_MUTANT_TOP_EDGE:
            self.assertNotIn(inserted, blind,
                             "the region mutated is no longer above the anchor, so this "
                             "mutant no longer exercises the top edge")
        self.assertEqual(sorted(self.WRAPPER_QUOTED_FRAGMENTS),
                         self._quoted_fragments_from_region("\n".join(blind)),
                         "the anchor-down region no longer reports exactly the three frozen "
                         "fragments for a mutant that hands the operator a fourth notice; the "
                         "seam tests would catch this mutant on their own and it stops "
                         "showing that the pin's top edge is the net")

        # The pin, now that the top edge is part of it. The failure has to come from the
        # top-edge check and name the line that moved, not from a downstream bound.
        with self.assertRaises(AssertionError) as cm:
            self._refusal_block_from_lines(
                mutant, name=f"{self.WRAPPER.name} (fourth notice above the anchor)")
        msg = str(cm.exception)
        self.assertIn(self.REFUSAL_BLOCK_OPENER, msg,
                      "the guard did not refuse the wrapper on its top edge: " + msg)
        self.assertIn("the line above", msg,
                      "the refusal did not come from the opener check, so it is not the "
                      "check that closes re-review B1: " + msg)
        self.assertIn("a fourth notice", msg,
                      "the failure does not name the notice the operator is handed above "
                      "the anchor: " + msg)

    def test_the_frozen_refusal_block_is_a_balanced_block_and_not_a_hollow_copy(self):
        """A golden can be quietly gutted, so the golden is checked against itself.

        The pinned region is now the safety net for the three-fragment claim, which makes
        the constant itself an attack surface: an emptied or truncated copy would keep
        every other test green while guarding nothing. These are the properties that make
        it the block and not a placeholder — it opens on the block's own column-0 `if` with
        the anchor directly below it, it is exactly one balanced `if` body closed by the
        `fi` that is part of it, and it still quotes the three gate lines. The first two
        are the top edge PRO-413 re-review B1 found unpinned; a golden that opened on the
        anchor would keep every assertion below passing while leaving the line above the
        anchor free.
        """
        frozen = self.WRAPPER_REFUSAL_BLOCK_FROZEN_LINES
        self.assertTrue(frozen, "the pinned refusal block is empty")
        self.assertEqual(self.REFUSAL_BLOCK_OPENER, frozen[0],
                         "the pinned region no longer starts on the statement that opens "
                         "the refusal block, so the line above the anchor is outside the "
                         "pin again")
        self.assertEqual((1, 0), self._shell_if_fi(frozen[0]),
                         "the pinned region does not open on a structural `if`")
        self.assertIn(self.REFUSAL_BLOCK_ANCHOR, frozen[1],
                      "the pinned region no longer has the anchor directly under its "
                      "opener, so the scan and the pin would be describing different blocks")
        self.assertEqual((0, 1), self._shell_if_fi(frozen[-1]),
                         "the pinned region does not end on its closing `fi`")
        depth = 0
        for ln in frozen[:-1]:
            opens, closes = self._shell_if_fi(ln)
            depth += opens - closes
            self.assertGreater(depth, 0,
                               f"the pinned region closes its `if` early at {ln!r}, so the "
                               "last line is not the block's closing `fi`")
        self.assertEqual(1, depth,
                         f"the pinned region is not one balanced `if` body: depth {depth} "
                         "before the closing `fi`")
        self.assertEqual(sorted(self.WRAPPER_QUOTED_FRAGMENTS),
                         self._quoted_fragments_from_region("\n".join(frozen)),
                         "the pinned region no longer quotes exactly the two exempted gate "
                         "lines and the finding marker, so the seam it pins has moved")

    def test_a_quoted_apostrophe_pair_in_a_block_comment_is_not_a_fragment(self):
        """PRO-413 review N2: the fragment scan reads what the operator is handed.

        The block carries a comment per arm, and the scan used to read raw text while
        the token counter next to it stripped comments. A comment carrying a balanced
        pair of apostrophes was therefore booked as a fourth quoted fragment and
        failed the seam — loud, so never dangerous, but an honest comment looked like
        a widening, and this guard has a real widening job to stay credible on. The
        strip is shared now, and the raw scan below shows it is the strip doing it.
        """
        comment = "  # an apostrophe pair in prose: 'like this'"
        self.assertEqual(["like this"],
                         sorted(set(re.findall(r"'([^']*)'", comment))),
                         "the fixture comment no longer carries a balanced quoted pair, "
                         "so this test cannot show that the strip removes it")
        widened = list(self.WRAPPER_REFUSAL_BLOCK_FROZEN_LINES)
        widened.insert(1, comment)
        self.assertEqual(sorted(self.WRAPPER_QUOTED_FRAGMENTS),
                         self._quoted_fragments_from_region("\n".join(widened)),
                         "a single-quoted phrase inside a shell comment is being counted "
                         "as a fragment the wrapper quotes at the operator")

    def test_the_wrapper_is_valid_bash(self):
        """PRO-413 review N1: the premise under every mutant test in this class.

        Every mutant here is defended by `bash -n` on the mutant, which only means
        something because `bash -n` would have failed on a real mistake — and nothing in
        the suite had ever run it on the real wrapper. The `worlddb-preflight` job runs no
        shellcheck either, so this one subprocess call is the whole syntactic backstop for
        a shell script whose wording a Python suite pins byte for byte.
        """
        import subprocess
        res = subprocess.run(["bash", "-n", str(self.WRAPPER)],
                             capture_output=True, text=True)
        self.assertEqual(0, res.returncode,
                         f"the real {self.WRAPPER.name} does not parse: {res.stderr}")

    def test_a_render_failure_still_names_the_failing_check(self):
        """PRO-313 review B1 state S3: a `render()` crash is not a licence for silence."""
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               render_raises=True)
        text = res.out.decode("ascii")
        self.assertIn("RESULT: FAIL", text)
        self.assertIn("render() raised RuntimeError", text)
        self.assertIn("dirty-worktree", text,
                      "the degraded path has to name the finding, not just apologise")
        self.assertIsNotNone(res.payload, "the evidence half is independent of the echo")
        self.assertEqual(P.EXIT_FAIL, res.rc, "a presentation fallback must not change the verdict")

    # ---- PRO-324 re-review, non-blocking NB1/NB3/NB4 ------------------------ #
    #
    # NB2 is not represented here. While this branch was in review, Playerbot closed the
    # same finding on a better design: `main()` states the payload outcome and hands it to
    # the renderer as the required keyword-only `payload_note`, so no wording is derived
    # inside the presentation half at all (PRO-335 §2, PRO-341 review N1/B1). The four
    # tests in `test_the_degraded_verdict_*` below pin its three shapes plus the blank
    # `--json ""` case; the `_payload_fortune()` helper that branch carried was dropped
    # in the PRO-331 merge rather than kept as a second, competing source of the same
    # claim.

    def test_checks_that_pass_still_refuse_the_boot_when_the_payload_cannot_be_written(self):
        """PRO-324 NB3: the case that actually changes what boots.

        Every earlier payload-failure case ran on a stub that always returned
        `EXIT_FAIL`, so the refusal was invisible — the boot was already being refused
        for another reason. Here the checks pass, and only the missing evidence refuses
        the boot; without the `EXIT_ERROR` this run would boot on rc 0.
        """
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               stub_rc=P.EXIT_OK, stub_findings=False,
                               json_target="missing-dir")
        self.assertIn("RESULT: PASS", res.out.decode("ascii"),
                      "the fixture stopped being a passing run")
        self.assertIsNone(res.payload)
        self.assertEqual(P.EXIT_ERROR, res.rc,
                         "checks passed and the operator asked for evidence that never "
                         "landed; that is a refusal, not a boot")
        self.assertNotEqual(P.EXIT_OK, res.rc)
        err = res.err.decode("ascii")
        self.assertIn("could not write the --json payload", err)
        self.assertIn("boot is refused", err)
        self.assertIn("not a finding about your SQL", err,
                      "PRO-324 NB4: a filesystem failure must not be routed into the "
                      "SQL lane by the message that follows it")

    def test_a_report_that_raises_while_being_read_still_prints_the_verdict(self):
        """PRO-324 NB1: the degraded listing is a guard, so its own body is guarded too.

        Before the fix the loop and the join sat outside any `try`, so this report —
        unreadable where only the *listing* needs it — escaped `_render_or_say_why` into
        `main()` and the run died at rc 1 with no verdict on stdout at all.
        """
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               hostile_report=True)
        self.assertIsNone(res.raised)
        text = res.out.decode("ascii")
        self.assertIn("PRO-179 world-DB updater pre-flight", text)
        self.assertIn("RESULT: FAIL", text,
                      "the verdict was lost with the listing that failed to print it")
        self.assertIn("boot refused", text)
        self.assertNotIn("unformatted below", text,
                         "the listing did not survive, so this is the last resort and "
                         "not the degraded path — the two must not be conflated")
        self.assertIsNotNone(res.payload, "the evidence half is independent of the echo")
        self.assertEqual(P.EXIT_FAIL, res.rc)

    def test_the_double_fault_still_prints_the_verdict_and_the_payload_notice(self):
        """PRO-324 NB1, the reviewer's measured shape: nothing at all was said.

        An unreadable report *and* an unwritable payload. The unguarded fallback used to
        raise back into `main()`, which cost both the printed verdict and the
        `payload_error` notice that `main()` prints after this call.
        """
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               hostile_report=True, json_target="missing-dir")
        self.assertIsNone(res.raised)
        text = res.out.decode("ascii")
        self.assertIn("PRO-179 world-DB updater pre-flight", text)
        self.assertIn("RESULT:", text)
        err = res.err.decode("ascii")
        self.assertIn("could not write the --json payload", err,
                      "the notice that only prints after `_render_or_say_why` returns "
                      "never printed, which was the other half of the finding")
        self.assertIn("boot is refused", err)
        self.assertEqual(P.EXIT_ERROR, res.rc)

    def test_the_payload_is_on_disk_before_the_presentation_half_runs(self):
        """PRO-324 NB1 corollary, strengthened per PRO-339 review NB-Rev-1.

        Hoisting `_render_or_say_why()` above the payload write survived the old suite,
        because the ordering was only ever guaranteed by that function never raising. It
        cannot raise now, so the ordering needs its own pin: ask the presentation half,
        at the moment it runs, whether the evidence half already landed.

        The first version of that pin asked `is_file()`, which the reviewer defeated with
        a write that touched the path empty before presentation and put the real bytes
        there afterwards — every ordering test stayed green while the audit log would
        have held a zero-byte "evidence" file had presentation then thrown. That is the
        PRO-313 B1 failure mode again, one layer down. So the pin now compares the bytes
        that were on disk at presentation time against the bytes that ended up there, and
        requires them to be a parseable verdict. Complete-before-presentation, not
        merely-present-before-presentation.
        """
        for shape, kwargs in (("primary", {}), ("degraded", {"render_raises": True})):
            with self.subTest(shape=shape):
                res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                                       **kwargs)
                self.assertTrue(res.payload_existed_at_render,
                                f"{shape} path presented the verdict before the evidence "
                                f"was on disk; the halves have to be written in that order")
                self.assertIsNone(res.payload_read_error,
                                  f"{shape} path: the payload could not even be read at "
                                  f"presentation time ({res.payload_read_error})")
                self.assertIsNotNone(res.payload)
                at_render = res.payload_bytes_at_render
                self.assertIsNotNone(at_render,
                                     f"{shape} path: nothing readable was on disk when the "
                                     f"verdict was presented")
                self.assertGreater(len(at_render), 0,
                                   f"{shape} path: the evidence file was still empty when "
                                   f"the verdict was presented")
                self.assertEqual(
                    at_render, res.payload,
                    f"{shape} path: the payload on disk at presentation time is not the "
                    f"payload that ended up there — a file that is merely present is not "
                    f"evidence, which is the whole claim this pin exists to make")
                doc = json.loads(at_render.decode("ascii"))
                self.assertIn("findings", doc,
                              f"{shape} path: the bytes handed to the operator before "
                              f"presentation were not a verdict")
                self.assertEqual("fail", doc["result"])

    def test_a_payload_that_cannot_be_built_refuses_the_boot_instead_of_tracing_back(self):
        """PRO-339 review NB-Rev-2: the build is on the verdict path too, so it is guarded.

        The payload dictionary is read off the report — `report.data`, `report.sections`,
        `f.__dict__` — and that reading used to sit outside the `try` that covers the
        write. Nothing in `fail()/warn()/info()` can produce a report that breaks it, so
        this stays a fixture-only shape; but the same was said about the degraded listing
        before a fixture killed it, and a raise there escaped `main()` as a traceback with
        no stated reason. The build now lives with the write, so the outcome is the one
        this path already knows how to report: a named evidence-write failure and a
        refused boot, with the printed verdict intact.
        """
        import preflight_worlddb as P
        # PRO-339 review NB-Rev-4: the second shape is the build failure *combined* with
        # a broken presentation. It is the interesting one — the payload cannot be built
        # and the listing has to fall back, so the only thing left telling the operator
        # what happened is the degraded line, and that line is exactly the one that must
        # not claim a payload this run could not even assemble.
        for shape, kwargs in (
                ("build-only", {"hostile_payload": True}),
                ("build-and-degraded", {"hostile_payload": True, "render_raises": True}),
        ):
            with self.subTest(shape=shape):
                res = self._drive_main(self._ReportingStream(),
                                       self._ReportingStream(), **kwargs)
                self.assertIsNone(
                    res.raised,
                    "the payload build raised back into main() — a traceback where the "
                    "reason should be, which is PRO-308 finding B one half earlier")
                self.assertEqual(
                    P.EXIT_ERROR, res.rc,
                    "an unbuilt payload is a missing payload: the boot has to be refused, "
                    "not whatever rc the checks happened to reach")
                err = res.err.decode("ascii")
                self.assertIn("could not write the --json payload", err)
                self.assertIn("evidence-write failure, not a finding about your SQL", err)
                out = res.out.decode("ascii")
                self.assertIn("PRO-179 world-DB updater pre-flight", out,
                              "the evidence half failing must not cost the printed verdict")
                self.assertIn("RESULT:", out)
                self.assertNotIn("unaffected", out,
                                 "the degraded claim about the payload is back")
                self.assertFalse(res.payload_existed_at_render,
                                 "a payload whose build raised must not exist at "
                                 "presentation time")
                self.assertIsNone(res.payload, "a half-built payload reached the operator")

    def test_a_finding_with_no_rule_is_not_printed_as_none(self):
        """PRO-324 NB5 (taken): both render sites guard `f.rule` like they guard `f.file`."""
        import preflight_worlddb as P
        from preflight_worlddb import _render_or_say_why, render
        args = build_arg_parser().parse_args(["--compose-root", "/nonexistent"])
        r = Report()
        r.fail("4", "unbounded DELETE without a WHERE", file="data/sql/x.sql")
        r.warn("4", "OR in WHERE")
        text = render(r, args)
        self.assertIn("unbounded DELETE without a WHERE", text)
        self.assertIn("OR in WHERE", text)
        self.assertNotIn("None:", text, "the primary path printed the word None:")
        original = P.render
        P.render = lambda rep, a: (_ for _ in ()).throw(RuntimeError("fixture"))
        try:
            degraded = _render_or_say_why(r, args, payload_note=NO_JSON_PAYLOAD_NOTE)
        finally:
            P.render = original
        self.assertIn("unbounded DELETE without a WHERE", degraded)
        self.assertIn("[4] FAIL data/sql/x.sql: unbounded DELETE without a WHERE", degraded)
        self.assertIn("[4] WARN OR in WHERE", degraded)
        self.assertNotIn("None:", degraded, "the degraded path drifted from the primary one")

    def test_a_report_whose_verdict_cannot_be_read_still_gets_a_header(self):
        """PRO-324 NB1, the last resort itself: even the `RESULT:` line may not raise.

        The header is not free — `report.failed` reads the findings — so a report that
        is unreadable all the way down has to be answered with a literal, or the guard
        has the same hole it exists to close. Driven directly because `main()` cannot
        build its payload from a report that hostile either; the property under test is
        this function's contract, which is that it returns a string.
        """
        import preflight_worlddb as P
        from preflight_worlddb import _render_or_say_why

        class Unreadable:
            @property
            def failed(self):
                raise RuntimeError("fixture: .failed is unreadable")

            @property
            def findings(self):
                raise RuntimeError("fixture: .findings is unreadable")

        args = build_arg_parser().parse_args(["--compose-root", "/nonexistent"])
        original = P.render
        P.render = lambda rep, a: (_ for _ in ()).throw(RuntimeError("fixture"))
        try:
            text = _render_or_say_why(
                Unreadable(), args,
                payload_note="the --json payload at /nonexistent/verdict.json was NOT "
                             "written")
        finally:
            P.render = original
        self.assertIn("PRO-179 world-DB updater pre-flight", text)
        self.assertIn("RESULT: unreadable", text)
        self.assertNotIn("unformatted below", text)

    def test_the_refusal_comment_does_not_claim_the_boot_path_asks_for_the_payload(self):
        """PRO-324 NB4: the comment is what the next reader trusts, so pin its claim.

        It used to say the boot path asks for the payload and trusts it as the evidence.
        `ops/deploy/worldserver-up.sh` does not pass `--json` at all — it is an operator
        opt-in — so that sentence sends the next reader looking for a flag that is not
        there. Behaviour cannot catch a comment, which is the one case where a source
        text pin earns its keep alongside the `print()` pin below.
        """
        import inspect
        from preflight_worlddb import main
        src = inspect.getsource(main)
        self.assertNotIn("The boot path asks for the payload", src)
        self.assertIn("opt-in", src)
        self.assertIn("an operator who does ask for the payload", src)

    def test_main_reports_through_the_surviving_paths(self):
        """The one source-text pin that earns its keep: no bare `print()` in `main()`."""
        import inspect
        from preflight_worlddb import main
        src = inspect.getsource(main)
        self.assertNotIn("print(", src,
                         "a bare print() in main() puts the verdict back at the mercy "
                         "of the operator's stdout error handler (PRO-308 QA finding B)")

    def test_the_payload_write_does_not_inherit_the_host_default_encoding(self):
        """PRO-313 review M5: `encoding` is unobservable while `ensure_ascii` holds.

        With `ensure_ascii=True` the serialised text is already pure ASCII, so no bytes
        on disk can distinguish `encoding="utf-8"` from nothing — M5 survived the suite
        for exactly that reason. The property worth pinning is the call contract: the
        payload must never fall back to the host's default locale, which is what would
        bite whoever relaxes `ensure_ascii`. So pin the call, and say plainly that this
        is the weaker half.
        """
        import preflight_worlddb as P
        calls = {}
        original = P.Path.write_text

        def spy(self, data, encoding=None, errors=None, newline=None):
            calls["encoding"] = encoding
            calls["ascii"] = data.isascii()
            return len(data)

        P.Path.write_text = spy
        try:
            P._write_json_payload(Path("never-written.json"), self._payload(self._report()))
        finally:
            P.Path.write_text = original
        self.assertEqual("utf-8", calls.get("encoding"),
                         "the payload followed the host's default encoding, so the file "
                         "is no longer the same bytes on every host")
        self.assertTrue(calls.get("ascii"),
                        "ensure_ascii is the load-bearing half and it came off")

    def test_the_payload_is_readable_on_a_c_locale_host(self):
        """The environment-real version of the payload pin: a host with no usable locale."""
        import os
        import subprocess
        import sys
        code = (
            "import json, pathlib, sys;"
            "sys.path.insert(0, sys.argv[1]);"
            "from preflight_worlddb import _write_json_payload;"
            "raw = 'data/sql/qa_raw\\udcff.sql';"
            "out = pathlib.Path(sys.argv[2]);"
            "_write_json_payload(out, {'result': 'fail',"
            " 'findings': [{'file': raw, 'message': 'caf' + chr(233)}]});"
            "data = out.read_bytes();"
            "assert data.isascii(), 'non-ASCII bytes: %r' % data[:80];"
            "back = json.loads(data.decode('ascii'));"
            "assert back['findings'][0]['file'] == raw, back;"
            "assert back['findings'][0]['message'] == 'caf' + chr(233), back;"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        env.update({"LC_ALL": "C", "LANG": "C",
                    "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"})
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "verdict.json"
            proc = subprocess.run([sys.executable, "-c", code, str(HERE), str(out)],
                                  env=env, capture_output=True, text=True)
            self.assertEqual(0, proc.returncode,
                             f"a C-locale host could not write the payload: {proc.stderr}")
            self.assertTrue(out.read_bytes().isascii())


    # -- what the degraded line claims about the payload (PRO-335 §2) ---------- #

    def test_the_degraded_verdict_says_no_json_payload_was_requested(self):
        """PRO-325 NB2's shape, one layer up: an absent thing must be said, not `None`d.

        The degraded line used to end `...and the --json payload is unaffected` on every
        path, including the one where nobody asked for a payload — a claim about a file
        that does not exist and was never requested. The operator reading a failed boot
        is the audience; `NO_JSON_PAYLOAD_NOTE` is the gate's single wording for the
        never-requested case — "absent" is only the umbrella, and *not written* and
        *blank* are the other two arms with their own wordings — and the assertion below
        quotes what that wording says rather than the constant, because an assertion
        written against the same constant the emitter reads is self-consistent under a
        reword (PRO-369 review N1; the enumeration is the one the README carries after
        PRO-392 NB-A).

        The `leftovers` assertion is the same claim measured off the filesystem instead of
        off the string. `res.payload` is `None` by construction here — there is no path
        handed to the drive to look at — so on its own this pin would not notice a
        regression that wrote a payload with no `--json` flag at all. The drive runs in an
        empty scratch cwd, so the listing is what actually witnesses "no file, wherever it
        would land" (PRO-378 NB-B).
        """
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               render_raises=True, json_target="none")
        text = res.out.decode("ascii")
        self.assertIn("render() raised RuntimeError", text)
        self.assertIn("no --json payload requested", text)
        self.assertNotIn("unaffected", text,
                         "the degraded line still promises a payload nobody asked for")
        self.assertNotIn("None", text, "the absent payload is rendered as a word, not None")
        self.assertEqual([], res.leftovers, "no --json means no file, wherever it would land")

    def test_the_degraded_verdict_does_not_call_an_unwritten_payload_unaffected(self):
        """The same sentence on the path that asked for a payload and did not get one."""
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               render_raises=True, json_target="missing-dir")
        self.assertIsNone(res.payload, "fixture: the payload was supposed to fail")
        text = res.out.decode("ascii")
        self.assertIn("was NOT written", text)
        self.assertNotIn("unaffected", text,
                         "the line that explains the failure also denies the evidence exists")

    def test_the_degraded_verdict_still_points_at_the_payload_it_wrote(self):
        """Positive control: the truthful claim survives when the payload really is there."""
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               render_raises=True)
        self.assertIsNotNone(res.payload)
        text = res.out.decode("ascii")
        self.assertIn("is on disk", text)
        self.assertIn("--json payload", text)

    def test_the_degraded_verdict_does_not_call_a_blank_json_target_a_payload(self):
        """PRO-341 review B1 + PRO-352: `--json ""` is "evidence asked for, none produced".

        `main()` once gated the write on truthiness and this note on `is None`. The wrapper
        forwards unrecognised operator arguments verbatim, so an operator's
        `--json "$TARGET"` with a blank `$TARGET` arrives here as an empty path, and that
        run printed `the --json payload at  is on disk` while nothing was written —
        PRO-325 NB2's shape (an absent thing asserted as present) on the line an operator
        reads while the boot is being refused.

        PRO-352 (Core's PRO-355 contract A) closes the other half: the refusal is chained
        behind the write gate rather than sharing its read — it tests `payload_error`, which
        the blank-target arm sets without any write being attempted — so a blank target
        counts as requested, the refusal fires, and the rc is `EXIT_ERROR` — no longer a
        value this pin declines to name. Blank is *not* "not requested", so the degraded
        line must say the target is blank, never borrow `NO_JSON_PAYLOAD_NOTE`, and never
        claim the payload is on disk.
        """
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               render_raises=True, json_target="empty")
        self.assertIsNone(res.payload, "fixture: a blank --json must write nothing")
        text = res.out.decode("ascii")
        self.assertIn("render() raised RuntimeError", text,
                      "the fixture stopped exercising the degraded line")
        self.assertIn(BLANK_JSON_TARGET_NOTE, text,
                      "a blank target must be named on the line that explains the refusal")
        # Literal, not the constant (PRO-369 review N1): PRO-352 made this the negative
        # half of the same operator-facing pair — blank means evidence WAS requested, so
        # the not-requested wording must never appear here. Asserting the literal is what
        # keeps a reword of `NO_JSON_PAYLOAD_NOTE` from surviving the suite.
        self.assertNotIn("no --json payload requested", text,
                         "blank means evidence was requested; saying 'none requested' here "
                         "contradicts the refusal stderr raises two lines later")
        self.assertNotIn("on disk", text,
                         "the line still asserts evidence that was never written")
        self.assertNotIn("unaffected", text)
        self.assertNotIn("None", text, "a blank path must not leak as the word None")
        err = res.err.decode("ascii")
        self.assertIn(BLANK_JSON_TARGET_NOTE, err)
        self.assertIn("boot is refused", err)
        self.assertEqual(P.EXIT_ERROR, res.rc,
                         "evidence was asked for and none exists — the boot must not go "
                         "out on the checks' own rc")

    def test_a_blank_json_target_refuses_the_boot(self):
        """PRO-352: `--json ""` is "evidence asked for, none produced" — refuse at EXIT_ERROR.

        This is the half the PRO-335 pin above deliberately left unpinned. The blank-target
        arm of the payload block sets `payload_error` without touching the filesystem, so a
        blank target reaches the same evidence-missing refusal an unwritable path already
        took and the boot cannot slip out on the checks' rc. The operator's stderr names
        `--json` rather than an empty path or the cwd (`.`), and no filesystem write is
        attempted for the blank family.
        """
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               json_target="empty")
        self.assertEqual([], res.leftovers,
                         "a blank --json must not touch the filesystem at all")
        self.assertIsNone(res.payload, "a blank --json must write nothing")
        err = res.err.decode("ascii")
        self.assertIn(BLANK_JSON_TARGET_NOTE, err)
        self.assertIn("boot is refused", err)
        self.assertNotIn("could not write the --json payload to", err,
                         "a blank target must be refused by the predicate, not by a failed "
                         "write against the cwd")
        self.assertEqual(P.EXIT_ERROR, res.rc)

    def test_a_whitespace_json_target_refuses_the_boot(self):
        """PRO-355 §2: the blank family is tested with `strip()`, not `== ""`.

        `--json "   "` is the worse shape of the same bug: `Path("   ").write_text`
        *succeeds*, so a truthiness gate boots and hands the operator a file literally
        named `   ` that nobody can find. The predicate treats any whitespace-only target
        as blank and refuses it exactly like the empty string.
        """
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               json_target="whitespace")
        # The headline assertion has to be able to fail. `res.payload` is None by
        # construction for this case, so it proves nothing on its own; the scratch cwd
        # the drive runs in is what actually witnesses "no junk file" (PRO-361 NB1).
        self.assertEqual([], res.leftovers,
                         "a whitespace-only target must write no junk file — a `   ` in the "
                         "scratch cwd means the write was attempted and succeeded")
        self.assertIsNone(res.payload, "a whitespace-only target must write no junk file")
        err = res.err.decode("ascii")
        self.assertIn(BLANK_JSON_TARGET_NOTE, err)
        self.assertIn("boot is refused", err)
        self.assertNotIn("could not write the --json payload to", err)
        self.assertEqual(P.EXIT_ERROR, res.rc)

    def test_the_scratch_cwd_witness_can_report_a_file_it_found(self):
        """PRO-378 NB-C: the `leftovers` witness needs a positive control.

        Every other pin on `leftovers` asserts `[]`, including the two that hold the
        "a blank/whitespace `--json` must touch no filesystem" claims and the one that holds
        "no `--json`, no file". A witness that had silently degraded to always returning
        `[]` — a listing pointed at the wrong directory, an early `break`, a refactor of the
        scratch cwd the drive chdirs into — keeps every one of them green, and with them the
        PRO-361 NB1 "no junk file" claim it exists to hold. That is the same
        witness-that-cannot-fail shape as PRO-378 NB1, one layer down.

        So drive the one run that legitimately writes *into* the scratch cwd: `--json
        verdict.json` while chdir-ed into it. The payload must land there, the listing must
        name it, and the bytes the listing names must be the evidence — otherwise the
        positive control would pass on an unrelated file.

        The `rc` pin (PRO-404 N3) closes the other way this control can go soft: it holds
        that the very run which produced its own evidence still refused the boot. A drive
        that drifted to a passing rc while the payload kept landing would leave every
        listing/bytes assertion above green while the thing under test stopped being a
        refusal, which is the whole job of the gate.
        """
        import preflight_worlddb as P
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               json_target="relative")
        self.assertEqual(
            ["verdict.json"], res.leftovers,
            "a relative --json target wrote into the scratch cwd and the witness did not "
            "see it; a bare [] here means every emptiness pin in this class has stopped "
            "witnessing anything")
        self.assertIsNotNone(res.payload,
                             "the listing named a file the drive could not read back")
        back = json.loads(res.payload.decode("ascii"))
        self.assertEqual("fail", back["result"],
                         "the positive control must point at the run's own evidence")
        self.assertEqual(self.RAW, back["findings"][0]["file"])
        self.assertEqual(
            P.EXIT_FAIL, res.rc,
            "the positive-control run wrote its own evidence and still refused the boot; an "
            "rc of 0 here means the witness is pointing at a run that booted, and a gate "
            "that boots is not the gate this class pins")


class TestTheGateHoldsItsOwnRc(unittest.TestCase):
    """PRO-325 NB1: `_emit` can guard its own flush; nothing guards the interpreter's.

    PRO-325's live re-measure of the PRO-301 fix captured the gate refusing a boot and
    exiting **120** instead of its own code. The captured bytes for the early-closed
    pipe, read from `ops/deploy/worldserver-up.sh` (`extraD.stderr`):

        preflight: could not write the report to <stdout>: BrokenPipeError: [Errno 32] Broken pipe
        Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>
        BrokenPipeError: [Errno 32] Broken pipe
        worldserver-up: REFUSING BOOT — pre-flight failed (exit 120).
        worldserver-up: ac-worldserver was not started. Fix the named file(s) or escalate
        to Scuff (SQL); do not bypass this gate.

    and the `/dev/full` shape (`extraB.stderr`):

        preflight: could not write the report to <stdout>: OSError: [Errno 28] No space left on device
        Exception ignored in: <_io.TextIOWrapper name='<stdout>' mode='w' encoding='utf-8'>
        OSError: [Errno 28] No space left on device

    `_emit` printed the first line of each and then returned normally, exactly as
    designed. The second and third lines are CPython: the bytes a refusing device would
    not take are still in the buffer, `flush_std_files()` tries again during shutdown,
    and `Py_Exit()` turns a failed final flush into status 120. The wrapper still fails
    closed — every non-zero rc means refused — so nothing safety-relevant broke; what
    broke is that the operator is told to go look at an interpreter accident instead of
    the finding that refused the boot. These run in subprocesses because the status is
    set by interpreter teardown; no fixture can observe it from inside.
    """

    @staticmethod
    def _env():
        import os
        return {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}

    def _run(self, body, **kwargs):
        import subprocess
        import sys
        code = (
            "import sys;"
            "sys.path.insert(0, sys.argv[1]);"
            "import preflight_worlddb as P;"
            + body
        )
        return subprocess.run([sys.executable, "-c", code, str(HERE)], env=self._env(),
                              capture_output=True, text=True, **kwargs)

    # The real `main()` under a terminal that will not have it. `{stdout_setup}` and
    # `{argv}` are the only two things the end-to-end pins differ on, so the stub exists
    # once and the shapes cannot drift apart the way PRO-354's two fixtures did.
    _MAIN_DRIVER = '''
        import pathlib
        import sys
        sys.path.insert(0, sys.argv[1])
        import preflight_worlddb as P


        class StubPreflight:
            def __init__(self, args, policy, report):
                self.args, self.policy, self.report = args, policy, report
                self.sql_root = pathlib.Path(args.compose_root) / "data" / "sql"
                self.git_root = pathlib.Path(args.compose_root)

            def run(self):
                self.report.fail("3", "fixture: refused on purpose",
                                 file="x.sql", rule="dirty-worktree")
                return P.EXIT_FAIL


        P.Preflight = StubPreflight
        td = pathlib.Path(sys.argv[2])
        (td / "tree" / "data" / "sql").mkdir(parents=True)
        (td / "policy.json").write_text("{{}}")
        {stdout_setup}
        sys.exit(P.main({argv}))
    '''

    # `--policy` defaults to the file that ships next to the gate, so the `EXIT_ERROR`
    # route is reached by naming a path that is not there — the operator's typo, which is
    # how it happens in the field. The driver still builds `policy.json` and the tree;
    # `main()` returns before either is reached, which is the point: the shape under test
    # is the earliest `_emit`-backed exit `main()` has, and one of the two whose report goes
    # to `sys.stderr` — the other is the operational-error return at
    # `preflight_worlddb.py:4716-4717`, which this family does not drive.
    # `build_arg_parser().parse_args(argv)` at `preflight_worlddb.py:4692` is earlier still
    # and writes its usage error to `sys.stderr` directly — never through `_emit`, so nothing
    # retires it — which is a separate gap (PRO-389 review N-a) and not what these pins drive.
    _MAIN_ARGV = ('["--compose-root", str(td / "tree"), "--policy", str(td / "policy.json"), '
                  '"--json", str(td / "verdict.json")]')
    _MAIN_ARGV_MISSING_POLICY = ('["--compose-root", str(td / "tree"), "--policy", '
                                 'str(td / "absent.json"), "--json", str(td / "verdict.json")]')
    # The third route, for the `EXIT_ERROR` return whose report does *not* go to
    # `sys.stderr`: `--json ""` is "evidence asked for, produced none" (PRO-352), so the
    # report is emitted on `sys.stdout` (`preflight_worlddb.py:4795-4796`) and the two notice
    # lines on `sys.stderr` (`:4805-4808`). There primary and fallback are two objects,
    # which is the pairing the missing-policy return cannot show.
    _MAIN_ARGV_BLANK_JSON = ('["--compose-root", str(td / "tree"), "--policy", '
                             'str(td / "policy.json"), "--json", ""]')

    def _driver(self, stdout_setup, *, argv=None):
        """One driver template, the argv shapes `main()` can be driven through.

        Everything the pins share lives in `_MAIN_DRIVER`; a pin that needed its own copy
        of the stub would be a second fixture to keep honest, which is the PRO-354 bug.
        """
        return self._MAIN_DRIVER.format(
            stdout_setup=stdout_setup, argv=self._MAIN_ARGV if argv is None else argv)

    def _drive_real_main(self, driver, td):
        import subprocess
        import sys
        import textwrap
        script = Path(td) / "driver.py"
        script.write_text(textwrap.dedent(driver))
        return subprocess.run([sys.executable, str(script), str(HERE), str(td)],
                              env=self._env(), capture_output=True, text=True)

    def _drive_real_main_through_shell(self, driver, td, redirect):
        """Run the real `main()` with fd 1 set up outside the child, not by the child.

        PRO-354 is the reason this exists rather than another `sys.stdout = open(...)`
        fixture. Rebinding inside the child hands `main()` an object whose `name` really is
        `/dev/full`, so it exercises the object-naming half of `_emit` and nothing else; an
        operator reaches the state by redirecting descriptor 1 before the interpreter is
        built, which leaves `sys.stdout.name` as the literal `<stdout>`. Two fixtures, two
        code paths, and only the one that never happens in the field was ever pinned.

        Three destination shapes are pinned that way (PRO-373 F2, naming them rather than
        saying "both"): a shell `1>&-` in
        `test_the_real_main_names_the_closed_fd_1_it_has_to_report_through`, a shell
        `> /dev/full` in `test_a_shell_redirected_full_device_is_named_from_the_descriptor`,
        and an early-closed pipe in `test_a_shell_redirected_broken_pipe_stays_unnamed`. Only
        the two shell shapes come through here. The pipe cannot, because there is no shell
        redirect that hands a child a descriptor whose reader is already gone; that test sets
        fd 1 from the parent with `Popen(stdout=write_end)` instead. What all three pin is the
        substantive half the shell was only ever a means to: the descriptor is set outside the
        child, never by an `open()` rebinding in it.
        """
        import shlex
        import subprocess
        import sys
        import textwrap
        script = Path(td) / "driver.py"
        script.write_text(textwrap.dedent(driver))
        shell = (f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "
                 f"{shlex.quote(str(HERE))} {shlex.quote(str(td))} {redirect}")
        return subprocess.run(["/bin/sh", "-c", shell], env=self._env(),
                              capture_output=True, text=True)

    def test_the_reproduction_is_real_a_refusing_stdout_costs_120_without_the_fix(self):
        """Negative control, and the PRO-325 capture reproduced rather than restated.

        `_retire_broken_stream` is patched out, which is precisely the build PRO-325
        measured: the write guard still prints the notice, and the unflushed buffer is
        still there when the interpreter comes looking. Delete the retirement call from
        `_emit` and every rc pin below flips to 120. If this one ever exits 1 instead,
        the fixture has stopped reaching teardown and the pins below pin nothing.
        """
        proc = self._run(
            "sys.stdout = open('/dev/full', 'w');"
            "P._retire_broken_stream = lambda stream, label: None;"
            "P._emit(sys.stdout, 'verdict text');"
            "sys.exit(P.EXIT_FAIL);"
        )
        self.assertEqual(120, proc.returncode,
                         f"the fixture no longer reaches the interpreter's final flush, so "
                         f"the rc=120 pins in this class prove nothing ({proc.stderr})")
        self.assertIn("Exception ignored", proc.stderr)
        self.assertIn("No space left on device", proc.stderr)

    def test_a_full_device_still_exits_the_gates_own_code(self):
        """PRO-325 NB1 / `extraB`: the guard holds, so the teardown has nothing left to fail on."""
        import preflight_worlddb as P
        proc = self._run(
            "sys.stdout = open('/dev/full', 'w');"
            "P._emit(sys.stdout, 'verdict text');"
            "sys.exit(P.EXIT_FAIL);"
        )
        self.assertEqual(P.EXIT_FAIL, proc.returncode,
                         f"a refusing terminal turned the gate's verdict into an interpreter "
                         f"status: rc={proc.returncode} stderr={proc.stderr}")
        self.assertIn("could not write the report to /dev/full", proc.stderr)
        self.assertIn("No space left on device", proc.stderr,
                      "the notice still has to name what actually went wrong")
        self.assertNotIn("Exception ignored", proc.stderr,
                         "the interpreter is still holding bytes for a device that refused them")

    def test_an_early_closed_pipe_still_exits_the_gates_own_code(self):
        """PRO-325 `extraD`: the wrapper's view. rc 120 reads as an accident, not a verdict."""
        import os
        import subprocess
        import sys
        import preflight_worlddb as P
        code = (
            "import sys;"
            "sys.path.insert(0, sys.argv[1]);"
            "import preflight_worlddb as P;"
            "P._emit(sys.stdout, 'verdict text');"        # the inherited pipe is stdout
            "sys.exit(P.EXIT_FAIL);"
        )
        read_end, write_end = os.pipe()
        os.close(read_end)                 # the reader is gone before the child starts
        try:
            proc = subprocess.Popen([sys.executable, "-c", code, str(HERE)], stdout=write_end,
                                    stderr=subprocess.PIPE, text=True, env=self._env())
        finally:
            os.close(write_end)
        _, err = proc.communicate()
        self.assertEqual(P.EXIT_FAIL, proc.returncode,
                         f"the wrapper can only print REFUSING BOOT — pre-flight failed "
                         f"(exit {proc.returncode}) on a pipe that closed early: {err}")
        self.assertIn("BrokenPipeError", err)
        self.assertIn("could not write the report to", err)
        self.assertNotIn("Exception ignored", err)

    def test_the_real_main_still_lands_on_a_documented_rc_with_a_refusing_terminal(self):
        """The end-to-end pin: presentation may fail, the rc contract may not drift.

        `ops/deploy/worldserver-up.sh` treats any non-zero rc as a refusal, so 120 still
        failed closed. That is why this is diagnosability work and not safety work — but
        the gate documents three exit codes and a fourth arriving from the interpreter is
        the difference between `Fix the named file(s)` and a wild goose chase.

        The destination assertion is PRO-341 review N2: `main()` labels the channel it
        aimed at (`<stdout>`), and that is not the thing the operator has to go and fix.
        The notice has to name the device that refused, which is `/dev/full` here.
        """
        import preflight_worlddb as P
        driver = self._driver('sys.stdout = open("/dev/full", "w")   # terminal refuses us')
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main(driver, td)
            self.assertEqual(P.EXIT_FAIL, proc.returncode,
                             f"the gate exited a code it does not document: "
                             f"rc={proc.returncode} stderr={proc.stderr}")
            self.assertNotIn("Exception ignored", proc.stderr)
            self.assertIn("could not write the report to <stdout> (/dev/full)", proc.stderr,
                          "the notice names a channel, not the destination that refused; "
                          "an operator cannot act on `<stdout>`")
            payload = Path(td) / "verdict.json"
            self.assertTrue(payload.is_file(),
                            "the evidence still has to survive a terminal that will not take it")
            self.assertEqual("fail", json.loads(payload.read_bytes().decode("ascii"))["result"])

    def test_the_real_main_names_the_closed_fd_1_it_has_to_report_through(self):
        """PRO-341 review N4: the production pairing — `main()`'s label plus a `None` stream.

        `1>&-` is the PRO-325 `extraA` shape, and until now the only test of it called
        `_emit` directly with no label, which is not how `main()` ever calls it. With the
        label in play the channel is `<stdout>` and the stream object is `None`, so the
        sentence has to carry both: what was aimed at, and that nothing is attached.
        """
        import preflight_worlddb as P
        driver = self._driver(
            "pass   # fd 1 was closed by the shell before the interpreter ran")
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main_through_shell(driver, td, "1>&-")
            self.assertEqual(P.EXIT_FAIL, proc.returncode,
                             f"a closed fd 1 changed the verdict too: rc={proc.returncode} "
                             f"stderr={proc.stderr}")
            self.assertNotIn("Exception ignored", proc.stderr)
            self.assertNotIn("report to None", proc.stderr,
                             "the operator is reading the word None where a destination belongs")
            self.assertIn("could not write the report to <stdout> (nothing is attached to it)",
                          proc.stderr,
                          "the production line must say both what was aimed at and what is "
                          "missing; fd 2 is how the notice still gets out")
            self.assertTrue((Path(td) / "verdict.json").is_file(),
                            "the evidence still has to survive a terminal that is not there")

    def test_a_shell_redirected_full_device_is_named_from_the_descriptor(self):
        """PRO-354: the operator's real `> /dev/full`, not an `open()` stand-in.

        The PRO-341 pin above reaches `/dev/full` by rebinding `sys.stdout` inside the child,
        and that object's `name` is `/dev/full`, so it pins only the object-naming half of
        `_emit`. Under a shell redirect the interpreter inherited an already-redirected
        descriptor and still calls the stream `<stdout>`, so `_emit` compared `<stdout>` with
        `<stdout>`, concluded the parenthetical "repeats the label", and dropped the one word
        the sentence exists to carry:

            preflight: could not write the report to <stdout>: OSError: [Errno 28] ...

        The rc contract and the payload were untouched, which is what makes this survive
        review — an operator reading `<stdout>` goes looking at their terminal instead of at
        the full device. Ask the descriptor when the object cannot help.
        """
        import preflight_worlddb as P
        driver = self._driver(
            "pass   # fd 1 was redirected by the shell, so the object cannot "
            "help and the descriptor has to answer")
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main_through_shell(driver, td, "> /dev/full")
            self.assertEqual(P.EXIT_FAIL, proc.returncode,
                             f"the gate exited a code it does not document: "
                             f"rc={proc.returncode} stderr={proc.stderr}")
            self.assertNotIn("Exception ignored", proc.stderr,
                             "the interpreter is still holding bytes for a device that refused them")
            self.assertIn("could not write the report to <stdout> (/dev/full)", proc.stderr,
                          "the device that refused the boot is never named, so the sentence "
                          "reads like any other stdout failure")
            self.assertNotIn("report to None", proc.stderr)
            payload = Path(td) / "verdict.json"
            self.assertTrue(payload.is_file(),
                            "the evidence still has to survive a terminal that will not take it")
            self.assertEqual("fail", json.loads(payload.read_bytes().decode("ascii"))["result"])

    def test_the_real_main_holds_its_rc_when_fd_2_dups_the_refusing_device(self):
        """PRO-372 F1: the operator's `> /dev/full 2>&1` — both descriptors, one full device.

        PRO-325 made the gate hold its own rc against a refusing stdout while fd 2 stayed
        live, and PRO-360's rewritten `_retire_broken_stream` docstring then made a prose
        claim about the shape where fd 2 *is* fd 1. Measured at `5942df102`, `cc9d8bfda`
        and `c93866486`, that shape still exited 120: `_emit` retired only the stream that
        refused it, the notice line it then wrote to `sys.__stderr__` was refused too
        (at startup `sys.stderr is sys.__stderr__`), those bytes stayed buffered, and
        `flush_std_files()` at teardown tripped on the same full descriptor from the other
        side — `Py_Exit()` turned the failed final flush into 120. The wrapper still fails
        closed, but `REFUSING BOOT — pre-flight failed (exit 120)` is the PRO-325 `extraD`
        symptom from the operator's most natural redirect, and no pin had ever put fd 2 on
        the refusing descriptor. The refusing device keeps the notice in this shape; rc is
        the one number that still reaches the operator, and the payload is the evidence.

        There is deliberately no stderr assertion here (PRO-389 N1). Under `2>&1` fd 2 is a
        dup of the same full file description as fd 1, so nothing the interpreter writes to
        fd 2 can reach the parent: an `assertNotIn("Exception ignored", proc.stderr)` on this
        shape was unsatisfiable by construction, passed whether or not the regression it
        appeared to guard against had come back, and read like coverage while pinning
        nothing. The load-bearing claims are the rc and the on-disk payload below. The
        sibling pins that *do* assert on stderr all leave fd 2 live (`> /dev/full`, `1>&-`,
        the early-closed pipe) — that is the precondition that makes those able to fail, and
        it does not hold here. What shows the two claims that remain can still move is
        `test_the_devfull_pair_costs_120_without_the_fallback_retirement`, and the same
        argument for the error route is
        `test_the_error_route_costs_120_when_nothing_is_retired`.
        """
        import preflight_worlddb as P
        driver = self._driver(
            "pass   # the shell put fd 2 on the same full description as fd 1")
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main_through_shell(driver, td, "> /dev/full 2>&1")
            self.assertEqual(P.EXIT_FAIL, proc.returncode,
                             f"the operator's `> /dev/full 2>&1` still costs the gate its rc: "
                             f"rc={proc.returncode} "
                             f"(stderr is empty by construction under 2>&1)")
            payload = Path(td) / "verdict.json"
            self.assertTrue(payload.is_file(),
                            "the evidence has to survive both descriptors being full")
            self.assertEqual("fail", json.loads(payload.read_bytes().decode("ascii"))["result"])

    def test_the_devfull_pair_costs_120_without_the_fallback_retirement(self):
        """Negative control for the pin above — the pre-PRO-372 build, reproduced not restated.

        `_retire_broken_stream` is patched back to retiring only the refusing stream,
        which is precisely what `_emit` did before PRO-372: the notice line to
        `sys.__stderr__` strands its bytes for the shutdown flush and 120 is back. That
        equation holds here because this route presents once — one `_emit`, so one pair of
        retirement calls and no later call that could retire the fallback by itself. On the
        two-`_emit` payload-error route further down the same predicate is *not* the pre-372
        build, and its control says so; do not copy this sentence across. If this
        pin ever exits 1, the fixture has stopped reaching the interpreter's final flush
        and the rc assertion above proves nothing.
        """
        driver = self._driver(
            "_orig = P._retire_broken_stream; "
            "P._retire_broken_stream = "
            "(lambda s, l: _orig(s, l) if s is sys.stdout else None)")
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main_through_shell(driver, td, "> /dev/full 2>&1")
            self.assertEqual(120, proc.returncode,
                             f"the fixture no longer reaches the interpreter's final flush, so "
                             f"the rc=120 story in the pair test proves nothing "
                             f"(stderr is empty by construction under 2>&1)")
            self.assertTrue((Path(td) / "verdict.json").is_file(),
                            "even the pre-fix build wrote the payload before presenting")

    def test_the_real_main_holds_its_rc_on_the_error_route_when_fd_2_is_the_refusing_device(
            self):
        """PRO-389 N2: the `EXIT_ERROR` route under `> /dev/full 2>&1` — the unpinned half.

        `_emit` has a second primary. `main()` returns `EXIT_ERROR` from three places, and
        the one this pin drives — the missing `--policy` at `preflight_worlddb.py:4696-4697`
        — aims the report at `sys.stderr`; at startup `sys.stderr is sys.__stderr__`, so
        inside `_emit` the primary and the fallback are the *same object*. The report line
        and the notice line land in one buffer on one refusing descriptor, and there is no
        second stream to retire. Every end-to-end subprocess rc pin above drives the
        `EXIT_FAIL` route, where the primary is `sys.stdout` and the fallback is a
        different stream, so none of them reaches that identity — which is why this route
        could sit unpinned while looking covered. (The three in-process `EXIT_ERROR` rc
        pins in the blank/whitespace `--json` family — `test_a_blank_json_target_refuses_the_boot`
        and its two siblings in `TestTheVerdictSurvivesTheOperatorsCodec`, roughly 440-510
        lines and a class boundary above this one — are not end-to-end: they run `main()`
        under `_drive_main`, which swaps the streams inside the child and so cannot see the
        interpreter's teardown flush that costs 120.)

        Measured on 3.12.3 at `d97932345` this exits **2**, not 120. On this return the two
        `_retire_broken_stream` calls in `_emit` act on one object, so either one alone
        suffices and only removing both costs 120 — a block-the-second-call measurement does
        not by itself show which half of the fix saves the route, and this pin credits
        neither. The controls carry the asymmetry instead: on the `EXIT_FAIL` route either
        call removed on its own costs 120, while on the other `EXIT_ERROR` return — the
        payload write failure / blank `--json` at `preflight_worlddb.py:4797-4809`, whose
        report goes to `sys.stdout` — leaving only the `sys.stdout` retirement is no longer
        enough, which is what that route's own control below pins. "Passes today" is not a pin
        either way, and the failure class is the one `flush_std_files()` owns: bytes a device
        already refused, retried at teardown, and answered by `Py_Exit()` with 120. The
        companion below removes both retirements and lands on 120, so the assertion here is
        able to fail.

        No stderr assertion, for the PRO-389 N1 reason stated on the `EXIT_FAIL` pin above:
        under `2>&1` fd 2 cannot deliver anything to the parent. This route also returns
        before the payload write, so the assertion below says so rather than leaving it
        implied — on this shape the rc is the whole claim the gate gets to make.
        """
        import preflight_worlddb as P
        driver = self._driver(
            "pass   # the shell put fd 2 on the same full description as fd 1",
            argv=self._MAIN_ARGV_MISSING_POLICY)
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main_through_shell(driver, td, "> /dev/full 2>&1")
            self.assertEqual(P.EXIT_ERROR, proc.returncode,
                             f"the operator's `> /dev/full 2>&1` changed the gate's verdict on "
                             f"the error route too: rc={proc.returncode} "
                             f"(stderr is empty by construction under 2>&1)")
            self.assertFalse((Path(td) / "verdict.json").is_file(),
                             "the missing-policy return happens before the evidence write, so "
                             "the rc is the only thing this route can report")

    def test_the_error_route_costs_120_when_nothing_is_retired(self):
        """Negative control for the `EXIT_ERROR` pin — proof it is not another vacuous pass.

        `_retire_broken_stream` is patched out entirely, which is the pre-PRO-325 build: the
        report line and the notice line both strand their bytes in the one wrapper CPython
        built on the full descriptor, `flush_std_files()` retries them at teardown and
        `Py_Exit()` returns 120. PRO-389 N1 is about pins that cannot fail; this is the
        version of the error-route pin that can. If it ever exits 2, the fixture has stopped
        reaching the interpreter's final flush and the rc=2 above proves nothing.
        """
        driver = self._driver(
            "P._retire_broken_stream = lambda stream, label: None   # pre-PRO-325 build",
            argv=self._MAIN_ARGV_MISSING_POLICY)
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main_through_shell(driver, td, "> /dev/full 2>&1")
            self.assertEqual(120, proc.returncode,
                             f"the fixture no longer reaches the interpreter's final flush, so "
                             f"the rc=2 pin above proves nothing "
                             f"(stderr is empty by construction under 2>&1)")
            self.assertFalse((Path(td) / "verdict.json").is_file(),
                             "the pre-fix build returned before the payload too; if this flips "
                             "the two tests are no longer driving the same route")

    def test_the_real_main_holds_its_rc_on_the_payload_error_route_when_fd_2_is_refused(
            self):
        """PRO-389 review B1/N-b: the `EXIT_ERROR` return whose primary is *not* `sys.stderr`.

        `main()`'s third `EXIT_ERROR` return (`preflight_worlddb.py:4797-4809`) is reached
        with `--json ""` — evidence asked for and none produced (PRO-352) — and it presents
        the other way round: the report goes to `sys.stdout` (`:4795-4796`) and the two notice
        lines to `sys.stderr` (`:4805-4808`). Primary and fallback are two different
        objects there, so the one-object identity that lets the missing-policy return above
        survive either retirement call alone does not exist. Measured on 3.12.3 under
        `> /dev/full 2>&1`: unpatched it exits **2**. The control below patches
        `_retire_broken_stream` to suppress every retirement except the one on the refusing
        `sys.stdout`, and that exits **120** — and so does removing every retirement, so on
        this route those two patches are indistinguishable (both measured, 3.12.3). What
        does *not* cost it 120 is blocking only the second call, nor the genuine pre-PRO-372
        `_emit` with nothing but the `if fallback_refused:` retirement deleted: both exit
        **2**, because this route runs more than one `_emit` and the pair at
        `preflight_worlddb.py:4805-4808` carries its own *primary* retirement, which in the
        pre-372 build lands on the same `sys.__stderr__` the skipped fallback call would have
        covered. That is also why the control's predicate is strictly stronger than pre-372
        rather than equal to it — it drops that second primary call too.

        So the claim this shape earns is narrow: an `EXIT_ERROR` route whose report goes to
        `sys.stdout` needs the refused `sys.__stderr__` retired by *something* before
        teardown, and if the only surviving retirement is the stdout one, `Py_Exit()` answers
        120. It says nothing about which half of the fix owns the route, and it is not the
        "either call alone suffices" reading the missing-policy return above would suggest —
        blocking the first call here costs 120, where the same patch there leaves 2. Where
        the PRO-372 fallback call is genuinely load-bearing is the single-`_emit` report
        route, and `test_the_devfull_pair_costs_120_without_the_fallback_retirement` is the
        pin that says so.

        No stderr assertion, for the PRO-389 N1 reason stated on the `EXIT_FAIL` pin: under
        `2>&1` fd 2 cannot deliver anything to the parent. This route never writes the
        payload either, so the rc is the whole claim the gate gets to make here.
        """
        import preflight_worlddb as P
        driver = self._driver(
            "pass   # the shell put fd 2 on the same full description as fd 1",
            argv=self._MAIN_ARGV_BLANK_JSON)
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main_through_shell(driver, td, "> /dev/full 2>&1")
            self.assertEqual(P.EXIT_ERROR, proc.returncode,
                             f"the operator's `> /dev/full 2>&1` changed the gate's verdict on "
                             f"the evidence-write route too: rc={proc.returncode} "
                             f"(stderr is empty by construction under 2>&1)")
            self.assertFalse((Path(td) / "verdict.json").is_file(),
                             "a blank `--json` target must never touch the filesystem, so the "
                             "rc is the only thing this route can report")

    def test_the_payload_error_route_costs_120_when_the_refused_stderr_is_never_retired(self):
        """Negative control for the pin above — proof its rc=2 is able to fail.

        `_retire_broken_stream` is patched to suppress every retirement except the one on
        the refusing `sys.stdout`. The refused fallback — `sys.__stderr__`, still holding
        the notice line and the two payload-error lines — is therefore never retired, stays
        buffered for `flush_std_files()`, and `Py_Exit()` answers 120 on a route that should
        exit 2.

        Do not read this patch as the pre-PRO-372 build. Measured on 3.12.3, deleting only
        the `if fallback_refused:` retirement from `_emit` — what `_emit` really looked like
        before PRO-372 — leaves this route at **2**, because this route runs two `_emit`s and
        the second one retires `sys.stderr` as its own *primary*, which is the object holding
        the stranded bytes. The predicate here additionally drops that call, so it is
        strictly stronger than pre-372; and the full removal measures 120 here as well, so
        the two are indistinguishable. What this control can carry is "the refused
        `sys.__stderr__` has to be retired by something before teardown", and nothing about
        which half of the fix owns the route — which is why the pin above does not say. If
        this ever exits 2, the fixture has stopped reaching the interpreter's final flush and
        the rc=2 above proves nothing.
        """
        driver = self._driver(
            "_orig = P._retire_broken_stream; "
            "P._retire_broken_stream = "
            "(lambda s, l: _orig(s, l) if s is sys.stdout else None)",
            argv=self._MAIN_ARGV_BLANK_JSON)
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main_through_shell(driver, td, "> /dev/full 2>&1")
            self.assertEqual(120, proc.returncode,
                             f"the fixture no longer reaches the interpreter's final flush, so "
                             f"the rc=2 pin above proves nothing "
                             f"(stderr is empty by construction under 2>&1)")
            self.assertFalse((Path(td) / "verdict.json").is_file(),
                             "the suppressed-retirement build never wrote the payload either; "
                             "if this flips the two tests are no longer driving the same route")

    def test_the_payload_error_route_names_its_notices_when_only_stdout_is_full(self):
        """PRO-389 review NB-2: the same route with fd 2 live, so the notices are the proof.

        The two pins above can only claim their rc — under `2>&1` fd 2 delivers nothing to
        the parent, and an `argparse` usage error exits 2 as well
        (`preflight_worlddb.py:4692`, the gap PRO-389 review N-a routed to PRO-406), so rc
        alone does not say which `EXIT_ERROR` return ran. Here only fd 1 sits on the refusing
        device, so fd 2 is live and the route can name itself: the `_emit` notice about the
        report, the PRO-352 blank-target line and the evidence-write refusal all have to
        arrive verbatim. That is the stderr coverage the `EXIT_FAIL` side of this family has
        had since PRO-341 and this route has never had.
        """
        import preflight_worlddb as P
        driver = self._driver(
            "pass   # fd 1 is on the full device; fd 2 is still the operator's terminal",
            argv=self._MAIN_ARGV_BLANK_JSON)
        with tempfile.TemporaryDirectory() as td:
            proc = self._drive_real_main_through_shell(driver, td, "> /dev/full")
            self.assertEqual(P.EXIT_ERROR, proc.returncode,
                             f"the evidence-write refusal changed code the moment fd 2 stayed "
                             f"live: rc={proc.returncode} stderr={proc.stderr}")
            self.assertIn("preflight: the --json target is blank; pass a path or omit the flag",
                          proc.stderr,
                          "without the blank-target line a 2 is not distinguishable from an "
                          "argparse usage error, which is the ambiguity this pin exists to kill")
            self.assertIn("preflight: the requested evidence payload is missing, so the boot is "
                          "refused", proc.stderr,
                          "the sentence an operator reads has to name an evidence-write failure, "
                          "not a finding about their SQL")
            self.assertIn("could not write the report to <stdout> (/dev/full)", proc.stderr,
                          "the device that refused the report has to be named here too, the way "
                          "the `EXIT_FAIL` pins have always required")
            self.assertFalse((Path(td) / "verdict.json").is_file(),
                             "a blank `--json` target must never touch the filesystem")

    def test_a_shell_redirected_broken_pipe_stays_unnamed(self):
        """PRO-341 review N2 has to survive the descriptor path: an inode is not a destination.

        `/proc/self/fd/1` answers `pipe:[123456]` for a pipe, and PRO-354's fix asks the
        descriptor precisely when the object is silent — so without the anonymous-inode
        filter (the absolute-path test in `_descriptor_destination`) every early-closed pipe
        would start printing `(<stdout> (pipe:[123456]))`, an inode number nobody can go and
        fix, and the `extraD` wording the deploy path's operator recognises would change under
        them. `main()`'s label is in play here (the PRO-325 pin calls `_emit` without one),
        which is the pairing that could regress.
        """
        import os
        import subprocess
        import sys
        import textwrap
        import preflight_worlddb as P
        driver = self._driver(
            "pass   # fd 1 is an inherited pipe whose reader went away")
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "driver.py"
            script.write_text(textwrap.dedent(driver))
            read_end, write_end = os.pipe()
            os.close(read_end)                # the reader is gone before the child starts
            try:
                proc = subprocess.Popen([sys.executable, str(script), str(HERE), str(td)],
                                        stdout=write_end, stderr=subprocess.PIPE, text=True,
                                        env=self._env())
            finally:
                os.close(write_end)
            _, err = proc.communicate()
            self.assertEqual(P.EXIT_FAIL, proc.returncode,
                             f"a pipe that closed early changed the verdict too: "
                             f"rc={proc.returncode} stderr={err}")
            # PRO-373 F2: this ordering is load-bearing. The prefix assertion below also
            # breaks when an unwanted parenthetical turns up, so if it ran first it would
            # swallow the regression and the engineer would read a complaint about the
            # wrapper's wording instead of the one that explains the rule.
            self.assertNotIn("pipe:[", err,
                             "the descriptor path leaked an inode number into a sentence that "
                             "is supposed to name somewhere fixable")
            self.assertIn("could not write the report to <stdout>: BrokenPipeError", err,
                          "the wrapper's wording for an early-closed pipe has to stay put")
            self.assertNotIn("Exception ignored", err)
            self.assertTrue((Path(td) / "verdict.json").is_file())

    def test_a_closed_fd_1_is_named_instead_of_rendered_as_none(self):
        """PRO-325 NB2 (`extraA`): `1>&-` leaves `sys.stdout` None, and `repr(None)` is 'None'.

        The captured line was
        `preflight: could not write the report to None: AttributeError: 'NoneType' object
        has no attribute 'write'` — the one sentence an operator reads under a failed boot
        describing its own destination as `None`. The guard that was supposed to name the
        stream never fired, because `repr()` always returns something truthy.
        """
        import shlex
        import subprocess
        import sys
        body = ("P._emit(sys.stdout, 'verdict text');"
                "sys.exit(P.EXIT_FAIL);")
        code = ("import sys;"
                "sys.path.insert(0, sys.argv[1]);"
                "import preflight_worlddb as P;" + body)
        shell = (f"exec {shlex.quote(sys.executable)} -c {shlex.quote(code)} "
                 f"{shlex.quote(str(HERE))} 1>&-")
        proc = subprocess.run(["/bin/sh", "-c", shell], env=self._env(),
                              capture_output=True, text=True)
        self.assertEqual(1, proc.returncode,
                         f"a closed fd 1 changed the exit code too: {proc.stderr}")
        self.assertIn("could not write the report to", proc.stderr)
        self.assertNotIn("report to None", proc.stderr,
                         "the operator is reading the word None where a destination belongs")
        self.assertIn("nothing is attached to it", proc.stderr)

    # -- the mechanism itself -------------------------------------------------- #

    def test_a_retired_stream_cannot_raise(self):
        """Whatever is left holding the interpreter's attention must be inert."""
        import preflight_worlddb as P
        retired = P._RetiredStream("<stdout>")
        self.assertEqual(3, retired.write("abc"))
        self.assertIsNone(retired.flush())
        self.assertIsNone(retired.close())
        self.assertIsNone(retired.writelines(["a", "b"]))
        self.assertFalse(retired.isatty())
        self.assertTrue(getattr(retired, "closed", False),
                        "flush_std_files() skips a stream that reports itself closed; "
                        "without this the rc guarantee rests only on flush() staying inert")
        self.assertEqual("<stdout>", retired.name)
        self.assertEqual("utf-8", retired.encoding)
        for name in ("encoding", "errors"):
            self.assertIsNotNone(getattr(retired, name),
                                 f"a late third-party write reaches {name} at teardown")

    def test_retiring_rebinds_the_interpreters_stream_and_never_raises(self):
        """`closed` is a property of the stream class, so rebinding is the durable half."""
        import sys
        import preflight_worlddb as P

        class Angry:
            name = "<stdout>"
            encoding = "utf-8"

            def flush(self):
                raise OSError(28, "No space left on device")

            def close(self):
                raise OSError(28, "No space left on device")

        angry = Angry()
        original = sys.stdout
        try:
            sys.stdout = angry
            P._retire_broken_stream(angry, "<stdout>")
            self.assertIsInstance(sys.stdout, P._RetiredStream,
                                  "the interpreter still holds the stream that refused us")
            sys.stdout.write("late write")
            sys.stdout.flush()
        finally:
            sys.stdout = original

    def test_a_fallback_that_refuses_too_is_retired_from_teardown(self):
        """PRO-372 F1 mechanism: after a refused notice, nothing may stay buffered in it.

        The end-to-end pair can only show the rc; in-process it can show the guarantee the
        rc rests on. Both descriptors point at one refusing device (the `2>&1` shape), so
        the primary write and the fallback notice both fail. After `_emit` returns, the
        stream the interpreter will flush at shutdown must be the inert stand-in, and the
        object that held the stranded bytes must report itself closed — the one shape
        `flush_std_files()` skips outright.
        """
        import sys
        import preflight_worlddb as P

        class Full:
            name = "<stderr>"
            encoding = "utf-8"

            def __init__(self):
                self.closed = False
                self.buffered = []

            def write(self, s):
                self.buffered.append(s)
                return len(s)

            def flush(self):
                raise OSError(28, "No space left on device")

            def close(self):
                self.closed = True

        primary, fallback = Full(), Full()
        orig_out, orig_err, orig_stderr = sys.stdout, sys.stderr, sys.__stderr__
        try:
            sys.stdout, sys.stderr, sys.__stderr__ = primary, fallback, fallback
            P._emit(primary, "verdict text", label="<stdout>")
            self.assertIsInstance(sys.stderr, P._RetiredStream,
                                  "the notice's own bytes stayed buffered in a stream the "
                                  "shutdown flush still reaches — the PRO-372 shape")
            self.assertTrue(fallback.closed,
                            "a refusing fallback must report itself closed; that is what "
                            "takes it out of flush_std_files()")
            self.assertTrue(primary.closed and isinstance(sys.stdout, P._RetiredStream),
                            "the original retirement may not regress with the new one")
        finally:
            sys.stdout, sys.stderr, sys.__stderr__ = orig_out, orig_err, orig_stderr

    def test_a_fallback_that_took_the_notice_is_left_in_the_interpreters_hands(self):
        """The other half of PRO-372: a live fd 2 delivers the notice and is never retired.

        `> /dev/full` alone — stdout full, stderr live — is the shape whose operator-facing
        sentence PRO-341 review N2 and PRO-354 spent two rounds polishing. Retiring a
        fallback that took the line would silently silence it, and every rc pin would still
        pass. The fallback is retired when and only when it refused.
        """
        import sys
        import preflight_worlddb as P

        class Full:
            name = "<stdout>"
            encoding = "utf-8"

            def __init__(self):
                self.closed = False

            def write(self, s):
                return len(s)

            def flush(self):
                raise OSError(28, "No space left on device")

            def close(self):
                self.closed = True

        class Live:
            name = "<stderr>"
            encoding = "utf-8"

            def __init__(self):
                # `closed` is an instance attribute here for the same reason `Full` above
                # declares it in `__init__`: a class attribute would be shadowed the moment
                # anything sets it, and the fixture would invite a reader to trust the
                # declaration instead of the instance.
                self.closed = False
                self.lines = []

            def write(self, s):
                self.lines.append(s)
                return len(s)

            def flush(self):
                return None

        primary, fallback = Full(), Live()
        orig_out, orig_err, orig_stderr = sys.stdout, sys.stderr, sys.__stderr__
        try:
            sys.stdout, sys.stderr, sys.__stderr__ = primary, fallback, fallback
            P._emit(primary, "verdict text", label="<stdout>")
            self.assertIs(sys.stderr, fallback,
                          "a fallback that delivered the notice must not be retired")
            self.assertFalse(fallback.closed)
            self.assertTrue(any("could not write the report to <stdout>" in line
                                for line in fallback.lines),
                            "the notice has to reach the live fallback stream — that is "
                            "the sentence PRO-354 exists to get right")
        finally:
            sys.stdout, sys.stderr, sys.__stderr__ = orig_out, orig_err, orig_stderr

    def test_the_label_names_the_destination_and_never_says_none(self):
        """`_stream_label` is the whole of PRO-325 NB2, so pin its three shapes."""
        import preflight_worlddb as P

        class Named:
            name = "/dev/full"

        class Anonymous:
            pass

        self.assertEqual("/dev/full", P._stream_label(Named()))
        anon = Anonymous()
        self.assertIn("Anonymous", P._stream_label(anon),
                      "a stream with no name has to be identified by what it is")
        label = P._stream_label(None)
        self.assertNotEqual("None", label)
        self.assertNotIn("None", label)
        self.assertIn("not open", label)

    def test_the_descriptor_is_asked_only_when_the_object_cannot_name_the_destination(self):
        """PRO-354: the four answers `_stream_destination` may give, and when each is correct.

        `_stream_label` asks the Python object; that is right for a stream Python opened and
        wrong for a descriptor the shell redirected first. The pair of helpers is the fix, so
        the seam between them is what needs pinning — including the two shapes where the
        descriptor must *not* win, because a wrong destination is worse than a vague one.

        The must-not-win shapes are the ones that need a witness. A fixture that has no
        descriptor to offer passes whether or not the early return is there, because both
        branches end in `return label`; PRO-373 F2 measured that deleting it left the whole
        suite at baseline. So `Opened` carries a `fileno` pointing at a second regular file
        that is deliberately not `/dev/full`: the object and the descriptor now give different
        answers, and only the early return can keep the object's.
        """
        import os
        import preflight_worlddb as P

        class Opened:
            name = "/dev/full"          # what an in-process open() produces
            other_fd = None             # wired to a different file below; see the pin

            def fileno(self):
                return self.other_fd

        class Inherited:
            name = "<stdout>"           # what CPython stamps on fd 1 whatever the shell did
            fd = None

            def fileno(self):
                return self.fd

        class NoDescriptor:
            name = "<stdout>"           # a stand-in with nothing to ask

        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "report.txt"
            target.write_text("x")
            real = os.path.realpath(str(target))
            # PRO-373 F2: `Opened` has to be handed a descriptor that *disagrees* with its own
            # `name`, and it has to still be open when the pin runs. Left without one — the
            # shape this pin shipped with — deleting the early return at the top of
            # `_stream_destination` only moves the answer two lines down, from
            # `return label` to the "`fileno` is not callable" `return label`: still
            # `/dev/full`, still green, and the "asked *only* when" this test is named after
            # ships unpinned. With a live descriptor on a different regular file, dropping the
            # early return lets the descriptor answer and this assertion is what goes red.
            elsewhere = os.path.realpath(str(Path(td) / "elsewhere.txt"))
            Path(elsewhere).write_text("y")
            fd = os.open(real, os.O_RDONLY)
            Opened.other_fd = os.open(elsewhere, os.O_RDONLY)
            try:
                self.assertEqual(real, P._descriptor_destination(fd),
                                  "a redirected regular file is nameable only from the fd")
                named = Inherited()
                named.fd = fd
                self.assertEqual(real, P._stream_destination(named),
                                  "the operator's `> file` has to be named, not `<stdout>`")
                self.assertEqual("/dev/full", P._stream_destination(Opened()),
                                 "a stream that already names itself must not be "
                                 "second-guessed — and the only way to know whether it is "
                                 "being second-guessed is to hand it a descriptor worth "
                                 "asking, which is `elsewhere.txt` here, not `/dev/full`")
            finally:
                os.close(fd)
                os.close(Opened.other_fd)

            self.assertEqual("<stdout>", P._stream_destination(NoDescriptor()),
                             "a stream with no descriptor is back to what its object can say")

            read_end, write_end = os.pipe()
            try:
                self.assertIsNone(P._descriptor_destination(write_end),
                                  "`pipe:[123]` is an inode number, not somewhere to go fix")
                anonymous = Inherited()
                anonymous.fd = write_end
                self.assertEqual("<stdout>", P._stream_destination(anonymous),
                                  "an anonymous descriptor leaves the wording exactly as "
                                  "PRO-341 review N2 settled it")
            finally:
                os.close(read_end)
                os.close(write_end)

        self.assertIsNone(P._descriptor_destination(1 << 20),
                          "a descriptor that is not open is a 'cannot help' answer, and must "
                          "not raise out of a failure handler")

    def test_a_channel_named_stream_with_no_descriptor_gets_no_repeated_parenthetical(self):
        r"""PRO-350 review NB1: the `real != name` half of `_emit`'s guard, pinned in-process.

        `if real and real != name:` has two halves and only one of them had a test behind
        it. PRO-350 review deleted `real != name` and the suite stayed at baseline, which is
        how a documented behaviour shipped with nothing defending it: `_emit`'s own docstring
        says "Say it only when it adds something", and `ops/preflight/README.md` says the
        parenthetical "has to earn its place" by naming somewhere the operator can go fix.

        Which half this pins, precisely. `_emit` can learn the destination two ways, and
        only one of them can produce `real == name`:

        - the descriptor answers, so `real` is a path the label is not -> the `real and`
          half decides, and the parenthetical is wanted. Pinned end-to-end through `real
          main()` by `test_a_shell_redirected_full_device_is_named_from_the_descriptor`
          (a real `> /dev/full`) and, for the anonymous-inode answer, by
          `test_a_shell_redirected_broken_pipe_stays_unnamed`.
        - the object's answer is a channel name and there is no descriptor to ask, so
          `_stream_destination` falls back to that same string -> `real == name`, and
          `real != name` is the only thing standing between the operator and
          `<stdout> (<stdout>)`. That is the case with no sentence after it: a stand-in
          carrying the literal name CPython stamps on fd 1 and no callable `fileno`, which
          is what `_emit` holds when the destination is genuinely unknown.

        So the fixture is deliberately *not* given a `fileno`. Measured under exactly that
        mutation (`if real and real != name:` -> `if real:`) this pin goes red first: since
        PRO-373 F2 the `<stdout> (<stdout>)` guard runs ahead of the wording prefix, so the
        message an engineer reads is the one that explains the rule. The end-to-end pipe pin
        goes red under the same mutation as well, but on its prefix assertion — a suppressed
        parenthetical leaks no inode number, so its `pipe:[` guard has nothing to see. That
        asymmetry is why the suppression decision is pinned here, in-process, with the
        descriptor path deliberately unavailable so nothing else can be responsible for the
        answer.
        """
        import sys
        import preflight_worlddb as P

        class InheritedChannelName:
            """`sys.stdout`'s shape when the destination cannot be recovered.

            `name` is the literal CPython stamps on the std streams regardless of what the
            shell did to fd 1 (PRO-354), and the absence of `fileno` is the point: it is the
            shape that forces `_stream_destination` to answer with the object's own words,
            which is the only shape where the two sides of the guard can be equal.
            """

            name = "<stdout>"
            encoding = "utf-8"

            def write(self, s):
                raise OSError(28, "No space left on device")

            def flush(self):
                return None

        class Capture:
            """Stand-in for `sys.__stderr__`, which is where `_emit` names the failure."""

            def __init__(self):
                self.lines = []

            def write(self, s):
                self.lines.append(s)
                return len(s)

            def flush(self):
                return None

        fallback = Capture()
        original = sys.__stderr__
        try:
            sys.__stderr__ = fallback
            P._emit(InheritedChannelName(), "verdict text", label="<stdout>")
        finally:
            sys.__stderr__ = original

        self.assertEqual(1, len(fallback.lines),
                         f"`_emit` owes exactly one fallback line, got {fallback.lines}")
        note = fallback.lines[0]
        # PRO-373 F2: ordering is load-bearing here. The prefix assertion below also breaks
        # when a parenthetical turns up, so leading with it swallowed the regression and the
        # message that explains the suppression rule never reached the engineer.
        self.assertNotIn("<stdout> (<stdout>)", note,
                         "the parenthetical repeated the label, which is the noise "
                         "PRO-341 review N2's 'say it only when it adds something' rule "
                         "and the `real != name` half of the guard exist to prevent")
        self.assertIn("could not write the report to <stdout>: ", note,
                      f"the suppression half regressed, or the notice lost the channel it "
                      f"was aimed at: {note!r}")
        self.assertIn("OSError", note)
        self.assertIn("No space left on device", note,
                      "suppressing the destination must not cost the errno")

    def test_the_note_and_the_label_are_call_site_obligations(self):
        """PRO-341 review N1 + N5: neither sentence may be defaulted at the callee.

        `label` used to be `_emit`'s third positional and `payload_note` used to default
        to a claim about a payload nobody had checked. Both defaults let a caller stay
        silent and still print something authoritative about a destination. Keyword-only
        is what forces the caller to state what it means; `payload_note` is additionally
        required, because `main()` is the only caller and it always knows the truth.
        """
        import inspect
        import preflight_worlddb as P

        label_param = inspect.signature(P._emit).parameters["label"]
        self.assertIs(inspect.Parameter.KEYWORD_ONLY, label_param.kind,
                      "a positional label invites a caller to pass the stream twice "
                      "and mean something else entirely")
        with self.assertRaises(TypeError):
            P._emit(None, "verdict text", "<stdout>")

        note_param = inspect.signature(P._render_or_say_why).parameters["payload_note"]
        self.assertIs(inspect.Parameter.KEYWORD_ONLY, note_param.kind)
        self.assertIs(inspect.Parameter.empty, note_param.default,
                      "a defaulted payload note is how PRO-341 B1 got written")
        with self.assertRaises(TypeError):
            P._render_or_say_why(object(), object())
        with self.assertRaises(TypeError):
            P._render_or_say_why(object(), object(), "the --json payload at  is on disk")


class TestTheWrapperSurvivesARefusingStdout(unittest.TestCase):
    """PRO-351 QA: the boot wrapper may not die at its own status line.

    `worldserver-up.sh` runs under `set -euo pipefail`, and a write error from a shell
    builtin *is* a failing command. On a checkout whose verdict is a genuine FAIL, QA
    got `worldserver-up.sh: line 208: echo: write error: No space left on device` and
    nothing else: the wrapper aborted at the `PREFLIGHT_CHECK_ONLY=1` status line, so
    the gate never ran, the `REFUSING BOOT` sentence was never printed and the `--json`
    payload the deploy path reads back was never written (`1>&-` gave the same shape
    with `Bad file descriptor`). rc happened to come back 1 only because `echo` fails
    with 1 — a caller that checks rc alone was still safe, an operator and an audit
    trail were not.

    The verdict half of that script is the gate's exit status plus the payload the gate
    writes; its own `echo`s only narrate it. These pin that the narration is not
    load-bearing: each runs the real wrapper as a subprocess with the interpreter
    pointed at a stand-in gate, under a stdout that refuses us.
    """

    WRAPPER = HERE.parent / "deploy" / "worldserver-up.sh"

    # The stand-in answers with the caller's exit code and writes the payload the real
    # gate owns, so a pass cannot be mistaken for a refusal and the payload assertion
    # proves the `--json` argument arrived intact rather than that it was parsed.  The
    # payload says what the exit code says: an evidence file that disagrees with the
    # verdict next to it is the shape this lane keeps having to unwind (PRO-353 N4).
    # `PYTHON` is the wrapper's only interpreter handle and the lease check shares it,
    # so the stub has to hand the `python - <script>` form back to the real one.
    STUB = """#!/bin/sh
if [ "$1" = "-" ]; then exec "$PRO353_PYTHON" - "$2"; fi
printf '%s\\n' "$*" > "$PRO353_MARKER"
_json=
while [ $# -gt 0 ]; do
  if [ "$1" = "--json" ]; then _json="$2"; fi
  shift
done
if [ "$PRO353_GATE_EXIT" = 0 ]; then _result=pass; else _result=fail; fi
if [ -n "$_json" ]; then printf '{"result": "%s", "findings": []}\\n' "$_result" > "$_json"; fi
exit "$PRO353_GATE_EXIT"
"""

    @staticmethod
    def _stub_gate(td: str, marker: Path) -> str:
        stub = Path(td) / "stub-gate"
        stub.write_text(TestTheWrapperSurvivesARefusingStdout.STUB, encoding="utf-8")
        stub.chmod(0o755)
        return str(stub)

    def _run(self, *, stdout_redirect: str, gate_exit: int, check_only: bool = True,
             write_lease: bool = True):
        """Run the real wrapper once. `stdout_redirect` is the hostile fd 1 QA used."""
        import datetime
        import os
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "root"
            (root / ".stack").mkdir(parents=True)
            marker, err, payload = Path(td) / "gate-ran", Path(td) / "stderr.txt", Path(td) / "v.json"
            env = dict(os.environ)
            env.update({
                "COMPOSE_ROOT": str(root),
                "LEASE_FILE": str(root / ".stack" / "lease"),
                "PYTHON": self._stub_gate(td, marker),
                "PRO353_MARKER": str(marker),
                "PRO353_PYTHON": sys.executable,
                "PRO353_GATE_EXIT": str(gate_exit),
            })
            if check_only:
                env["PREFLIGHT_CHECK_ONLY"] = "1"
            elif write_lease:
                env.pop("PREFLIGHT_CHECK_ONLY", None)
                # Taken now, not hard-coded: a fixed stamp is a lease with a ~69-day
                # fuse, and the day it burns out this test fails as a stale lease and
                # reads like a wrapper bug (PRO-353 review N3).
                stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                (root / ".stack" / "lease").write_text(
                    json.dumps({"agentId": "core", "issueId": "PRO-353", "action": "check",
                                "acquiredAt": stamp, "expectedMinutes": 99999}),
                    encoding="utf-8")
            else:
                env.pop("PREFLIGHT_CHECK_ONLY", None)
            argv = ["bash", "-c", f'"$0" "$@" {stdout_redirect}', str(self.WRAPPER),
                    "--db-access", "command", "--mysql-command", "/bin/true -uroot",
                    "--json", str(payload)]
            with open(err, "w", encoding="utf-8") as fh:
                proc = subprocess.run(argv, env=env, cwd=td, stdout=None, stderr=fh)
            return {"rc": proc.returncode,
                    "reached_gate": marker.is_file(),
                    "gate_argv": marker.read_text(encoding="utf-8") if marker.is_file() else "",
                    "err": err.read_text(encoding="utf-8", errors="replace"),
                    # Read back inside the temporary directory: the deploy path reads
                    # this file after the wrapper exits, so a verdict that never landed
                    # on disk is no verdict at all.
                    "payload": payload.read_text(encoding="utf-8") if payload.is_file() else None}

    def test_the_bug_is_the_shape_this_class_pins(self):
        """Negative control: an unguarded builtin really does abort under `set -e`.

        Without this the class could go vacuous the day bash learns to warn instead of
        fail, and the pins below would keep passing while proving nothing.
        """
        import subprocess

        proc = subprocess.run(
            ["bash", "-c", "set -euo pipefail; echo 'worldserver-up: status line'; "
                           "echo 'gate-was-reached' >/dev/full; echo 'gate-was-reached'"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        self.assertEqual(1, proc.returncode,
                         "bash no longer fails on a builtin write error under `set -e`; "
                         "re-read this class before trusting the pins below")
        self.assertIn("write error", proc.stderr)
        self.assertNotIn("gate-was-reached", proc.stderr,
                         "the fixture stopped being the shape this class is about")

    def test_a_full_stdout_still_reaches_the_gate_and_prints_the_refusal(self):
        got = self._run(stdout_redirect=">/dev/full", gate_exit=1)
        self.assertTrue(got["reached_gate"],
                        "the wrapper died before the gate ran, which is PRO-353: " + got["err"])
        self.assertIn("--json", got["gate_argv"],
                      "the wrapper reached the gate but lost the payload argument")
        self.assertEqual(1, got["rc"], "the `exit \"$rc\"` refusal semantics changed")
        self.assertIn("worldserver-up: REFUSING BOOT — pre-flight failed (exit 1).", got["err"],
                      "the gate ran and the boot was still refused, but the operator was "
                      "never told why on the channel they read")
        self.assertIsNotNone(got["payload"], "the deploy path's evidence file is absent")
        self.assertEqual("fail", json.loads(got["payload"])["result"])
        # `tell`'s `2>/dev/null` is the point of `tell`: bash's own complaint is what
        # gets silenced, never the sentence. Drop the redirection and this fails
        # (PRO-353 review N5).
        self.assertNotIn("write error", got["err"],
                         "bash's own write-error complaint reached the operator")

    def test_a_closed_stdout_still_reaches_the_gate_and_prints_the_refusal(self):
        got = self._run(stdout_redirect="1>&-", gate_exit=1)
        self.assertTrue(got["reached_gate"],
                        "a closed fd 1 pre-empted the verdict, which is PRO-353: " + got["err"])
        self.assertEqual(1, got["rc"])
        self.assertIn("worldserver-up: REFUSING BOOT — pre-flight failed (exit 1).", got["err"])
        self.assertIsNotNone(got["payload"])
        self.assertEqual("fail", json.loads(got["payload"])["result"])
        self.assertNotIn("write error", got["err"],
                         "bash's own write-error complaint reached the operator")

    def test_a_refusing_stdout_does_not_invent_a_refusal(self):
        """The other direction: hardening the narration must not fail a passing gate."""
        got = self._run(stdout_redirect=">/dev/full", gate_exit=0)
        self.assertTrue(got["reached_gate"])
        self.assertEqual(0, got["rc"],
                         "a passing gate now reads as a refusal when stdout refuses us: "
                         + got["err"])
        self.assertNotIn("REFUSING BOOT", got["err"])

    def test_a_refusing_stdout_does_not_refuse_a_held_lease(self):
        """The normal boot path, where the lease check is the first interpreter call.

        The held-lease line used to `print()` to stdout. With a refused stdout the
        buffer could not be written, CPython's shutdown flush failed and `Py_Exit()`
        answered 120 — and `if ! "$PYTHON" …` reads any non-zero status as "the lease
        is bad", so a lease that was properly held was refused by `/dev/full`.
        """
        got = self._run(stdout_redirect=">/dev/full", gate_exit=0, check_only=False)
        self.assertTrue(got["reached_gate"],
                        "the lease check aborted on its own status line instead of "
                        "verifying the lease: " + got["err"])
        self.assertIn("lease held", got["err"],
                      "the held-lease line still aims at a stream that refuses us")
        self.assertNotIn("REFUSING BOOT — lease", got["err"],
                         "a lease that was properly held was refused by the stdout")
        self.assertNotIn("write error", got["err"],
                         "bash's own write-error complaint reached the operator")
        self.assertNotEqual(120, got["rc"],
                            "the interpreter's teardown status was read as a lease verdict")

    def test_a_refusing_stdout_still_refuses_a_missing_lease(self):
        """`die()` rides on `tell`, so the whole flag-refusal surface moved onto it.

        `--compose-root`, `-- -f x` and the missing-lease refusal all exit 2 through
        `die()`, and none of them was covered by this class. This is the cheapest of
        that family to drive, and it pins the direction the class must never soften: a
        run with no lease is refused whatever the streams are doing (PRO-353 review N6).
        """
        got = self._run(stdout_redirect=">/dev/full", gate_exit=0, check_only=False,
                        write_lease=False)
        self.assertEqual(2, got["rc"], "the `die()` exit status changed")
        self.assertIn("REFUSING BOOT — no single-writer lease", got["err"],
                      "the refusal never reached the operator's stderr")
        self.assertNotIn("write error", got["err"],
                         "bash's own write-error complaint reached the operator")
        self.assertFalse(got["reached_gate"],
                         "a run with no lease reached the gate, which is a boot on air")


class TestTheGateCompilesQuietly(unittest.TestCase):
    """PRO-308 QA finding C: a SyntaxWarning here is a warning nobody reads, forever.

    PRO-301's `dirty_paths()` docstring wrote `` `\\` `` and `\\"` in prose. Python kept
    the invalid escape but warned on it on every import, and warned *silently* on the
    valid one — `\\"` arrived as `"`, quietly rewriting the C-quoted example the docstring
    exists to show. Both are fixed by making it raw; this pins the class, not the one
    docstring, because the next piece of prose about git quoting will want a backslash
    too and preflight logs are read by eye.
    """

    def test_the_module_compiles_without_syntax_warnings(self):
        import warnings
        src = (HERE / "preflight_worlddb.py").read_text()
        with warnings.catch_warnings():
            warnings.simplefilter("error", SyntaxWarning)
            try:
                compile(src, "preflight_worlddb.py", "exec")
            except SyntaxWarning as exc:
                self.fail(f"a warning the CI log will attribute to nothing: {exc}")

    def test_the_dirty_paths_docstring_still_shows_the_quoted_form(self):
        doc = Preflight.dirty_paths.__doc__
        self.assertIsNotNone(doc)
        self.assertIn('we\\"ird.sql', doc,
                      "the raw flag changed the C-quoted example the docstring is about")
        self.assertIn("`\\`", doc)
        self.assertNotIn("\udcff", doc, "a prose docstring is not a place for a surrogate")


class TestTheWrapperRoutesItsAdvice(unittest.TestCase):
    """PRO-370 F1: an evidence-write failure may not be answered with the SQL escalation.

    `ops/deploy/worldserver-up.sh` printed one advice block for every non-zero gate rc.
    PRO-370 proved it on the deploy host: the `--json`-into-a-0555-directory case (rc 2)
    and a real SQL FAIL (rc 1) differed only in the trailing `(exit 2)` vs `(exit 1)`, so
    a gate that had just said — accurately — *"this is an evidence-write failure, not a
    finding about your SQL"* was answered two lines later with *"Fix the named file(s) or
    escalate to Scuff (SQL)"*. The gate's honesty was undone one layer up.

    Every fixture here is produced by the **real** `main()` and replayed to the wrapper by
    a stub `PYTHON`, carrying the gate's real bytes and the real rc. That indirection is
    the point: PRO-338's earlier clause keyed on a hand-written guess about what the gate
    prints ("did it name a file?") and turned out false in both directions, because nobody
    had tied the guess to the emitter. Here the marker the wrapper matches and the rc the
    wrapper reads come out of the same process run as the bytes themselves, so a change to
    `render()` or to the payload notice that would strand an operator shows up as a red
    test rather than as a wrong sentence on a refused boot.
    """

    WRAPPER = HERE.parent / "deploy" / "worldserver-up.sh"

    # Byte-for-byte what PRO-370 says a real SQL FAIL must still produce. Two echoes,
    # wrapped for a terminal; both the raw pair and the folded sentence are pinned.
    ESCALATION_RAW = ("worldserver-up: ac-worldserver was not started. Fix the named "
                      "file(s) or\nworldserver-up:                   escalate to Scuff "
                      "(SQL).")
    ESCALATION = "ac-worldserver was not started. Fix the named file(s) or escalate to " \
                 "Scuff (SQL)."

    # The one real `main()`, with only the check bodies stubbed (the shape
    # `TestTheGateHoldsItsOwnRc` already uses): no MySQL, no compose, no docker.
    _GATE_DRIVER = '''
        import pathlib
        import sys

        sys.path.insert(0, sys.argv[1])
        import preflight_worlddb as P


        class StubPreflight:
            def __init__(self, args, policy, report):
                self.args, self.policy, self.report = args, policy, report
                self.sql_root = pathlib.Path(args.compose_root) / "data" / "sql"
                self.git_root = pathlib.Path(args.compose_root)

            def run(self):
                if {operational}:
                    raise P.OperationalError("cannot reach the database (db-access=docker, "
                                             "service=ac-database). Is ac-database up?")
                if {refuse}:
                    self.report.fail("3", "UPDATE `creature_template` has no key predicate",
                                     file="creature_template.sql", rule="unbounded-write")
                    return P.EXIT_FAIL
                return P.EXIT_OK


        P.Preflight = StubPreflight
        {break_render}
        td = pathlib.Path(sys.argv[2])
        (td / "tree" / "data" / "sql").mkdir(parents=True)
        (td / "policy.json").write_text("{{}}")
        sys.exit(P.main(["--compose-root", str(td / "tree"), "--policy",
                         str(td / "policy.json"), "--json", sys.argv[3]]))
    '''

    def _real_gate(self, *, refuse=False, operational=False, break_render=False, json_target):
        """Run the real `main()`; return (rc, stdout, stderr) exactly as the operator saw them."""
        import subprocess
        import sys
        import textwrap
        broken = ("P.render = lambda report, args: (_ for _ in ())."
                  "throw(RuntimeError('fixture: render broke'))") if break_render else "pass"
        with tempfile.TemporaryDirectory() as td:
            driver = Path(td) / "gate.py"
            driver.write_text(textwrap.dedent(self._GATE_DRIVER).format(
                refuse="True" if refuse else "False",
                operational="True" if operational else "False",
                break_render=broken))
            proc = subprocess.run([sys.executable, str(driver), str(HERE), td, str(json_target)],
                                  capture_output=True, text=True, timeout=120)
        return proc.returncode, proc.stdout, proc.stderr

    def _writable_target(self):
        """A --json path the gate really can write, so the payload arm stays silent."""
        import os
        fd, name = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        target = Path(name)
        self.addCleanup(lambda: target.unlink(missing_ok=True))
        return target

    def _unwritable_dir(self):
        """A 0555 directory: the PRO-370 host case, not a missing-directory stand-in."""
        import shutil
        root = Path(tempfile.mkdtemp())
        locked = root / "evidence"
        locked.mkdir()
        locked.chmod(0o555)

        def restore():
            locked.chmod(0o755)
            shutil.rmtree(str(root), ignore_errors=True)

        self.addCleanup(restore)
        return locked

    def _replay_through_wrapper(self, gate_rc, gate_out, gate_err, json_target):
        """Run the real wrapper with `PYTHON` pointed at a shim that replays `gate_*`."""
        import os
        import shutil
        import subprocess
        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(td), True)
        (td / "stack").mkdir()
        (td / "out").write_text(gate_out, encoding="utf-8")
        (td / "err").write_text(gate_err, encoding="utf-8")
        shim = td / "replay-gate"
        # argv is the gate path and the forwarded flags; the replay is the whole point, so
        # it reads its script from the environment and ignores them.
        shim.write_text("#!/usr/bin/env bash\n"
                        'cat "$REPLAY_OUT"\n'
                        'cat "$REPLAY_ERR" >&2\n'
                        'exit "$GATE_RC"\n', encoding="utf-8")
        shim.chmod(0o755)
        env = dict(os.environ, PYTHON=str(shim), REPLAY_OUT=str(td / "out"),
                   REPLAY_ERR=str(td / "err"), GATE_RC=str(gate_rc),
                   COMPOSE_ROOT=str(td / "stack"), LEASE_FILE=str(td / "lease"),
                   PREFLIGHT_CHECK_ONLY="1")
        proc = subprocess.run(["bash", str(self.WRAPPER), "--json", str(json_target)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=env, timeout=60)
        return proc

    @staticmethod
    def _folded(text):
        """The block with its line breaks folded, so a sentence is pinned whole.

        The `worldserver-up: ` tag is dropped rather than kept: it is printed once per
        physical line, so where a sentence happens to straddle two of them the tag would
        otherwise land in the middle of the phrase under test. Exact rendering of the
        escalation is pinned separately against `ESCALATION_RAW`.
        """
        without_tag = re.sub(r"worldserver-up: *", "", text)
        return re.sub(r"\s+", " ", without_tag).strip()

    # -- the case PRO-370 raised ------------------------------------------------------ #

    def test_an_evidence_write_failure_is_not_answered_with_the_sql_escalation(self):
        import preflight_worlddb as P
        locked = self._unwritable_dir()
        target = locked / "preflight.json"
        rc, out, err = self._real_gate(json_target=target)
        self.assertEqual(P.EXIT_ERROR, rc,
                         f"fixture: a 0555 --json target must still be an evidence-write "
                         f"failure the gate refuses on its own code (rc={rc})")
        self.assertIn("could not write the --json payload to", err)
        self.assertIn("not a finding about your SQL", err,
                      "fixture: the gate has to still be the honest layer here")
        self.assertNotIn("!!", out, "fixture: this run earned no finding")

        proc = self._replay_through_wrapper(rc, out, err, target)
        self.assertEqual(P.EXIT_ERROR, proc.returncode,
                         "the wrapper stopped failing closed on an evidence-write failure")
        self.assertNotIn("Scuff (SQL)", proc.stderr,
                         "the payload failure is routed to the SQL lane again — the exact "
                         "sentence PRO-370 caught the wrapper printing")
        self.assertIn("evidence-write failure", proc.stderr)
        self.assertIn("Do not bypass this gate.", proc.stderr)
        self.assertIn(str(target), proc.stderr,
                      "the advice has to point at the payload path, as the gate's own line does")

    def test_the_gate_own_lines_still_reach_the_operator_unchanged(self):
        """Routing may read the gate's output, but it may not swallow or relocate it."""
        locked = self._unwritable_dir()
        target = locked / "preflight.json"
        rc, out, err = self._real_gate(json_target=target)
        proc = self._replay_through_wrapper(rc, out, err, target)
        for line in (out + err).splitlines():
            if line.strip():
                self.assertIn(line, proc.stdout + proc.stderr,
                              f"a line the gate printed never reached the operator: {line!r}")

    def test_the_capture_keeps_each_stream_on_the_descriptor_it_came_from(self):
        """The gate's verdict is stdout and its notices are stderr; the replay keeps that.

        Capture-then-replay is only acceptable if the wrapper's `2>/dev/null` still hides
        what `2>/dev/null` used to hide.
        """
        import preflight_worlddb as P
        target = self._writable_target()
        rc, out, err = self._real_gate(refuse=True, json_target=target)
        self.assertEqual(P.EXIT_FAIL, rc)
        proc = self._replay_through_wrapper(rc, out, err, target)
        self.assertIn("RESULT: FAIL — boot refused", proc.stdout)
        self.assertNotIn("RESULT: FAIL — boot refused", proc.stderr,
                         "the gate's verdict was moved onto stderr by the capture")
        self.assertIn("REFUSING BOOT", proc.stderr)
        self.assertNotIn("REFUSING BOOT", proc.stdout)

    # -- the case that must not regress ----------------------------------------------- #

    def test_a_real_sql_fail_still_gets_the_escalation_sentence_verbatim(self):
        import preflight_worlddb as P
        target = self._writable_target()
        rc, out, err = self._real_gate(refuse=True, json_target=target)
        self.assertEqual(P.EXIT_FAIL, rc)
        self.assertIn("    !! unbounded-write: UPDATE `creature_template` has no key predicate",
                      out, "fixture: the finding line the wrapper keys on is not on disk")

        proc = self._replay_through_wrapper(rc, out, err, target)
        self.assertEqual(P.EXIT_FAIL, proc.returncode)
        self.assertIn(self.ESCALATION_RAW, proc.stderr,
                      "the escalation block changed shape on the path that must be untouched")
        self.assertIn(self.ESCALATION, self._folded(proc.stderr))
        self.assertIn("Do not bypass this gate.", proc.stderr)
        self.assertNotIn("evidence-write", proc.stderr,
                         "a run that wrote its payload is being told about payloads")

    def test_a_run_that_carried_findings_and_lost_its_payload_routes_to_both(self):
        """rc 2 is not "not my SQL": findings and a lost payload can share a run."""
        import preflight_worlddb as P
        locked = self._unwritable_dir()
        target = locked / "preflight.json"
        rc, out, err = self._real_gate(refuse=True, json_target=target)
        self.assertEqual(P.EXIT_ERROR, rc)
        self.assertIn("    !! unbounded-write:", out)
        proc = self._replay_through_wrapper(rc, out, err, target)
        self.assertEqual(P.EXIT_ERROR, proc.returncode)
        self.assertIn(self.ESCALATION, self._folded(proc.stderr),
                      "a real finding was demoted because the rc belonged to the payload")
        folded = self._folded(proc.stderr)
        self.assertIn("Fix the path and re-run", folded,
                      "the lost payload stopped being named just because a finding shared the run")
        # PRO-390 non-blocking: with a `!!` on screen the escalation above just sent the
        # operator to Scuff (SQL); the wrapper's own payload arm must not then contradict it
        # by claiming the SQL lane is irrelevant. That wrapper sentence ("the SQL lane is not
        # where it belongs") belongs only to a findings-free run. The gate's own replayed
        # "not a finding about your SQL" notice about the payload is left verbatim — that is
        # the gate speaking, not the wrapper misrouting.
        self.assertNotIn("the SQL lane is not where it belongs", folded,
                         "the wrapper told the operator to escalate to SQL and that the SQL "
                         "lane was the wrong place in the same refusal")

    def test_a_degraded_render_still_routes_the_findings_it_listed(self):
        """The marker, not the exit code, is what decides — rc 2 with a listed FAIL routes to SQL."""
        import preflight_worlddb as P
        locked = self._unwritable_dir()
        target = locked / "preflight.json"
        rc, out, err = self._real_gate(refuse=True, break_render=True, json_target=target)
        self.assertEqual(P.EXIT_ERROR, rc)
        self.assertIn("!! render() raised RuntimeError", out)
        self.assertIn("    !! [3] FAIL", out,
                      "fixture: the degraded listing stopped listing what it refused on")
        proc = self._replay_through_wrapper(rc, out, err, target)
        self.assertIn(self.ESCALATION, self._folded(proc.stderr))

    def test_a_render_failure_alone_is_not_read_as_a_finding(self):
        """`!! render() raised ...` is the gate describing itself, not the SQL."""
        import preflight_worlddb as P
        locked = self._unwritable_dir()
        target = locked / "preflight.json"
        rc, out, err = self._real_gate(break_render=True, json_target=target)
        self.assertEqual(P.EXIT_ERROR, rc)
        self.assertIn("!! render() raised RuntimeError", out)
        self.assertNotIn("    !!", out, "fixture: no finding was listed, so this tests "
                                       "the exclusion and nothing else")
        proc = self._replay_through_wrapper(rc, out, err, target)
        self.assertNotIn("Scuff (SQL)", proc.stderr,
                         "a presentation failure sent the operator to the SQL lane")

    def test_an_operational_error_is_not_answered_with_the_sql_escalation(self):
        """The PRO-338 counterexample, now pinned: ac-database was down; nobody's SQL was."""
        import preflight_worlddb as P
        target = self._writable_target()
        rc, out, err = self._real_gate(operational=True, json_target=target)
        self.assertEqual(P.EXIT_ERROR, rc)
        self.assertIn("preflight: operational error:", err)
        proc = self._replay_through_wrapper(rc, out, err, target)
        self.assertEqual(P.EXIT_ERROR, proc.returncode)
        self.assertNotIn("Scuff (SQL)", proc.stderr)
        self.assertIn("operational error", proc.stderr)
        self.assertIn("Do not bypass this gate.", proc.stderr)

    def test_an_unrecognised_failure_fails_closed_without_naming_the_sql_lane(self):
        """No finding line, no known notice, an undocumented rc: say nothing that misroutes."""
        proc = self._replay_through_wrapper(3, "", "something the gate has never printed\n",
                                            Path("/nonexistent/preflight.json"))
        self.assertEqual(3, proc.returncode, "an undocumented rc stopped failing closed")
        self.assertNotIn("Scuff (SQL)", proc.stderr)
        self.assertIn("Do not bypass this gate.", proc.stderr)
        self.assertIn("cannot say whether this is about your SQL", self._folded(proc.stderr))
        # The other half of the unclassified arm: with no marker to route on, the only
        # thing left to hand the operator is the rule for reading the capture themselves.
        # Pinned as a sentence because PRO-382's seam proves the wrapper may not quote
        # `'!!'` anywhere in the refusal block without the gate really emitting it, and
        # this arm is the only place that quote lives — reword it into silence and the
        # seam stays green while the operator loses the rule.
        self.assertIn("A '!!' line in the output above is a finding about your SQL",
                      self._folded(proc.stderr))
        self.assertIn("a line about the environment of the gate itself is not",
                      self._folded(proc.stderr))

    # -- the capture-block regressions PRO-390 caught -------------------------------- #
    #
    # The three blocking findings all lived in the ~20 lines that capture the gate's
    # output for the markers to read. They are reproduced here on the shape the reviewer
    # used: the real wrapper, a stub `PYTHON` standing in for the gate, a stand-in `docker`
    # whose only job is to prove a boot happened. Each is checked against the fixed
    # wrapper; reverting the corresponding guard flips the assertion.

    _HOSTILE_GATE_HEAD = (
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "-" ]; then exec "$REALPY" - "$2"; fi\n'
    )

    def _run_hostile(self, gate_body, *, check_only=True, tmpdir=None, wrapper=None):
        """Run the real wrapper once against a stub gate, on PRO-390's shape.

        `gate_body` is the shell the stub runs after the lease-check passthrough. The
        stub records that the gate ran (``$GATE_MARKER``) and a fake ``docker`` on PATH
        records that a boot happened (``$DOCKER_MARKER``), so "the gate ran" and "the
        container started" are both observable rather than inferred.

        `wrapper` overrides the script that is run — for mutants, so they go through
        the same execution path the real wrapper is tested on and not a re-implementation
        of it (the PRO-413 rule for every mutant in this file).
        """
        import datetime
        import os
        import shutil
        import subprocess
        import sys

        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(td), True)
        root = td / "root"
        (root / ".stack").mkdir(parents=True)
        binp = td / "bin"
        binp.mkdir()
        docker_marker = td / "docker-ran"
        gate_marker = td / "gate-ran"

        fake = binp / "docker"
        fake.write_text(
            "#!/usr/bin/env bash\n"
            'printf "compose %s\\n" "$*" > "$DOCKER_MARKER"\n'
            "exit 0\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        gate = td / "gate"
        gate.write_text(self._HOSTILE_GATE_HEAD + gate_body, encoding="utf-8")
        gate.chmod(0o755)

        if not check_only:
            # The boot path refuses without a lease someone is holding, so hand it one.
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            (root / ".stack" / "lease").write_text(
                json.dumps(
                    {
                        "agentId": "core",
                        "issueId": "PRO-381",
                        "action": "check",
                        "acquiredAt": stamp,
                        "expectedMinutes": 99999,
                    }
                ),
                encoding="utf-8",
            )

        env = dict(os.environ)
        env.update(
            {
                "PYTHON": str(gate),
                "REALPY": sys.executable,
                "COMPOSE_ROOT": str(root),
                "LEASE_FILE": str(root / ".stack" / "lease"),
                "DOCKER_MARKER": str(docker_marker),
                "GATE_MARKER": str(gate_marker),
                "PATH": f"{binp}{os.pathsep}{env['PATH']}",
            }
        )
        if tmpdir is not None:
            env["TMPDIR"] = str(tmpdir)
        if check_only:
            env["PREFLIGHT_CHECK_ONLY"] = "1"
        else:
            env.pop("PREFLIGHT_CHECK_ONLY", None)

        err = td / "stderr.txt"
        argv = ["bash", str(wrapper or self.WRAPPER), "--json", str(td / "v.json")]
        with open(err, "w", encoding="utf-8") as fh:
            proc = subprocess.run(
                argv, env=env, cwd=str(td), stdout=subprocess.DEVNULL, stderr=fh,
                timeout=60,
            )
        return {
            "rc": proc.returncode,
            "docker_ran": docker_marker.is_file(),
            "gate_ran": gate_marker.is_file(),
            "stderr": err.read_text(encoding="utf-8", errors="replace"),
        }

    def test_a_signal_to_the_wrapper_during_the_gate_does_not_start_the_container(self):
        # PRO-390 B1 (fail-open). `docker stop`, `systemd stop` and a plain `kill -TERM`
        # signal this script and only this script — the gate child is never signalled. The
        # capture trap used to be `rm -rf "$GATE_DIR"` on INT/TERM, which cleaned up and
        # *returned*, so the unsigned gate finished rc 0 and the script walked straight into
        # `docker compose up -d ac-worldserver`. The signal traps must leave, not continue.
        got = self._run_hostile(
            'echo started > "$GATE_MARKER"\n'
            'kill -TERM "$PPID" 2>/dev/null\n'
            "exit 0\n",
            check_only=False,
        )
        self.assertTrue(got["gate_ran"], "the gate never ran, so the signal test proves nothing")
        self.assertFalse(
            got["docker_ran"],
            "a SIGTERM during the gate let the script fall through and start ac-worldserver",
        )
        self.assertEqual(143, got["rc"], "a SIGTERM during the gate did not leave with 128+15")
        self.assertNotIn("compose up", got["stderr"])

    def test_an_unwritable_scratch_dir_still_lets_the_gate_reach_its_verdict(self):
        # PRO-390 B2 (lost verdict). `GATE_DIR="$(mktemp -d)" || die` ran *before* the gate,
        # and `mktemp -d` is exactly the call that dies on `No space left on device` — the
        # PRO-351 host shape. A run that should have printed RESULT: PASS was answered with
        # rc 2, no RESULT and no payload, because a scratch dir we cannot make is not a
        # verdict. With no capture the gate speaks to the operator's own descriptors and the
        # wrapper routes on rc alone.
        td = Path(tempfile.mkdtemp())
        self.addCleanup(self._restore_tmpdir, td)
        td.chmod(0o555)
        got = self._run_hostile(
            'echo started > "$GATE_MARKER"\n'
            'printf "RESULT: PASS\\n"\n'
            "exit 0\n",
            check_only=True,
            tmpdir=td,
        )
        self.assertTrue(
            got["gate_ran"],
            "an unwritable scratch dir stopped the gate from running at all — the verdict "
            "was lost before it could be reached",
        )
        self.assertEqual(0, got["rc"], "a passing run was turned into a failure by TMPDIR")
        self.assertNotIn("REFUSING BOOT", got["stderr"])

    def _restore_tmpdir(self, td):
        td.chmod(0o755)
        import shutil

        shutil.rmtree(str(td), True)

    def test_a_capture_removed_under_the_arms_leaks_no_tool_diagnostics(self):
        # PRO-390 B3 (diagnostic leak). The cleanup trap used to delete the capture before
        # the advice arms read it, and the finding probe ran `awk` over files that were
        # already gone — so a raw `awk: cannot open ...` landed in the middle of the
        # operator's refusal (the tmpfiles-daemon-reclaims-it-underneath-a-long-run shape
        # bites the same way). The probe must treat a missing capture as "no finding here",
        # and the rc-1 verdict must still route to the SQL lane even though the `!!` line is
        # no longer readable.
        import shutil

        td = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(td), True)
        got = self._run_hostile(
            "printf '    !! unbounded-write: planted\\n'\n"
            'rm -rf "$TMPDIR"/tmp.* 2>/dev/null\n'
            "exit 1\n",
            check_only=True,
            tmpdir=td,
        )
        self.assertEqual(1, got["rc"])
        self.assertNotIn(
            "awk:", got["stderr"],
            "the finding probe printed its own file-open error into the refusal",
        )
        self.assertNotIn(
            "cat:", got["stderr"],
            "the replay printed its own file-open error into the refusal",
        )
        self.assertIn(
            "escalate to Scuff (SQL)", self._folded(got["stderr"]),
            "losing the capture must not cost a real rc-1 verdict its SQL routing",
        )

    def test_a_missing_policy_file_is_not_answered_with_the_sql_escalation(self):
        # PRO-390 non-blocking: `preflight: policy file not found: {path}` is as
        # unambiguously environmental as an operational error — a mis-deployed checkout is
        # missing its policy JSON — so it gets its own arm rather than the "cannot say
        # whether this is about your SQL" catch-all, and never routes to the SQL lane.
        proc = self._replay_through_wrapper(
            2,
            "",
            "preflight: policy file not found: /opt/world/config/preflight.json\n",
            self._writable_target(),
        )
        self.assertEqual(2, proc.returncode)
        self.assertIn("policy file", self._folded(proc.stderr))
        self.assertIn("not a finding about your SQL", self._folded(proc.stderr))
        self.assertNotIn("escalate to Scuff (SQL)", self._folded(proc.stderr))
        self.assertNotIn(
            "cannot say whether this is about your SQL", self._folded(proc.stderr)
        )

    # -- the refusal block's EMISSION, not its text ------------------------------------ #
    #
    # Every PRO-413 pin reads the refusal block as text: a golden compared line by line,
    # a scan of the single-quoted fragments inside the region. The re-review's residual
    # hole is the shape that shares a blind spot with all of them — three lines inserted
    # ABOVE the block's column-0 opener: `if (( rc != 0 )); then`, a `tell` carrying a
    # fourth single-quoted notice, `fi`. The anchor search still finds the real block
    # below, `lines[start - 1]` is still the pinned opener, the compared region is still
    # byte-identical to the golden, and `bash -n` is happy — the whole suite stayed
    # `Ran 393 tests ... OK` on that mutant (re-measured at f929e3e69) while every
    # refused boot printed a fourth quoted notice. The claim has always been about what
    # the OPERATOR is handed, so this pins what the wrapper actually PRINTS, by running
    # it: one refused boot per advice arm, compared against the exact set of
    # single-quoted fragments that route's arm is supposed to emit.
    #
    # The sets are EXACT per route, and that is load-bearing rather than tidy: the
    # mutant's fourth fragment is `'could not write the --json payload'` — itself one of
    # the pinned three — so a subset-of-pinned check accepts the mutant. Exactness also
    # refuses an arm that quietly stops emitting the fragment the operator depends on.
    REFUSAL_EMISSION_ROUTES = (
        ("gate silent, exit 2 — the unclassified arm",
         'echo started > "$GATE_MARKER"\nexit 2\n',
         2, {"!!"}),
        ("gate silent, exit 1 — the rc-only verdict",
         'echo started > "$GATE_MARKER"\nexit 1\n',
         1, set()),
        ("gate prints a `!!` finding, exit 1 — the SQL arm",
         'echo started > "$GATE_MARKER"\n'
         "printf '    !! unbounded-write: planted\\n'\nexit 1\n",
         1, set()),
        ("gate could not write the --json payload — the evidence arm",
         'echo started > "$GATE_MARKER"\n'
         "printf 'preflight: could not write the --json payload to /tmp/v.json\\n' >&2\n"
         "exit 2\n",
         2, set()),
        ("gate operational error — the environment arm",
         'echo started > "$GATE_MARKER"\n'
         "printf 'preflight: operational error: cannot reach ac-database\\n' >&2\n"
         "exit 2\n",
         2, {"preflight: operational error:"}),
    )
    # Across every refused-boot route, exactly two of the three pinned fragments ever
    # reach the terminal single-quoted. `'could not write the --json payload'` is quoted
    # in the block only as a `gate_printed` matcher argument — source text, not operator
    # prose — and the replayed gate line that proves the route works carries it
    # UNQUOTED. The union says that asymmetry out loud instead of leaving it implied.
    REFUSAL_EMISSION_UNION = {"!!", "preflight: operational error:"}

    @staticmethod
    def _quoted_fragments_emitted(stderr: str) -> set:
        """The single-quoted fragments on lines the wrapper printed, extracted per line.

        Per line, not over the whole capture: an unbalanced apostrophe on one line must
        not pair with one on another and invent a fragment. The stub gates above print no
        quote pairs (their bodies are right here to check), so anything found was written
        by the wrapper's own `tell` lines — including by code the text pins above never
        look at, which is the point of running the script.
        """
        found = set()
        for ln in stderr.splitlines():
            found.update(re.findall(r"'([^']*)'", ln))
        return found

    def test_every_refused_boot_route_emits_only_the_fragments_the_block_pins(self):
        """PRO-413 residual hole: the three-fragment claim has to hold at the terminal.

        Runs the real wrapper once per refused-boot route — every advice arm gets a
        boot refused in front of it — and compares the single-quoted fragments in the
        operator's stderr against the exact set that arm prints. No route may add one,
        nothing outside the pinned three may appear, and the union across the routes is
        exactly the two the block hands out single-quoted.
        """
        pinned = set(TestTheVerdictSurvivesTheOperatorsCodec.WRAPPER_QUOTED_FRAGMENTS)
        union = set()
        for label, body, want_rc, want_frags in self.REFUSAL_EMISSION_ROUTES:
            got = self._run_hostile(body, check_only=True)
            self.assertTrue(got["gate_ran"], f"{label}: the stub gate never ran")
            self.assertFalse(got["docker_ran"],
                             f"{label}: a refused boot started ac-worldserver")
            self.assertEqual(want_rc, got["rc"], f"{label}: wrong exit code")
            self.assertIn("REFUSING BOOT", got["stderr"],
                          f"{label}: the wrapper did not refuse this boot, so the route "
                          "is vacuous and its fragment set proves nothing")
            emitted = self._quoted_fragments_emitted(got["stderr"])
            self.assertEqual(sorted(want_frags), sorted(emitted),
                             f"{label}: the operator was handed a different set of "
                             "single-quoted fragments than the arm prints. stderr was:\n"
                             + got["stderr"])
            self.assertEqual(set(), emitted - pinned,
                             f"{label}: the refusal quoted a fragment outside the three "
                             "the block pins")
            union |= emitted
        self.assertEqual(sorted(self.REFUSAL_EMISSION_UNION), sorted(union),
                         "the union of single-quoted fragments a refused boot can hand "
                         "the operator has moved; it is the half of the PRO-382 promise "
                         "that actually reaches the terminal, and the arms above are "
                         "what defines it")

    def test_a_fourth_notice_above_the_opener_fails_the_emission_pin_though_the_text_pin_accepts(
            self):
        """PRO-413 residual hole, made a test: three lines above the column-0 opener.

        `if (( rc != 0 )); then` / `tell "… 'could not write the --json payload' …"` /
        `fi` inserted directly above the refusal block's opener is valid bash, runs on
        every refused boot, and is invisible to every text pin: the anchor search, the
        opener check and the byte-comparison all look at the block below it. Before the
        emission pin the whole suite stayed `Ran 393 tests ... OK` on this mutant. So
        the assertions are, in order: `bash -n` accepts the mutant; the text pin accepts
        it and still reports the frozen three (the negative control — without it the
        failure below would not show that EMISSION is what closes the hole); a
        subset-of-pinned check would have accepted the fragment too (why the pin above
        is exact per route); and the emission pin refuses every route.
        """
        import shutil
        import subprocess

        cls = TestTheVerdictSurvivesTheOperatorsCodec
        lines = self.WRAPPER.read_text(encoding="utf-8").splitlines()
        openers = [i for i, ln in enumerate(lines) if ln == cls.REFUSAL_BLOCK_OPENER]
        self.assertEqual(1, len(openers),
                         "the wrapper no longer has exactly one refusal opener, so the "
                         "insertion point above it is not defined")
        at = openers[0]
        mutant = (lines[:at]
                  + [cls.REFUSAL_BLOCK_OPENER,
                     "  tell \"fourth notice: 'could not write the --json payload' "
                     "hits the operator\"",
                     "fi"]
                  + lines[at:])
        # The original block is still intact below the insert: opener, anchor under it.
        self.assertEqual(cls.REFUSAL_BLOCK_OPENER, mutant[at + 3])
        self.assertIn(cls.REFUSAL_BLOCK_ANCHOR, mutant[at + 4])

        with tempfile.TemporaryDirectory() as td:
            # The wrapper locates the gate at `$SCRIPT_DIR/../preflight/
            # preflight_worlddb.py` and dies before running if it is not there, so the
            # mutant needs its `ops/` shape — it is still executed with the stub `PYTHON`,
            # the copy only has to exist.
            gate_src = self.WRAPPER.parent.parent / "preflight" / "preflight_worlddb.py"
            (Path(td) / "preflight").mkdir()
            (Path(td) / "deploy").mkdir()
            shutil.copyfile(gate_src, Path(td) / "preflight" / "preflight_worlddb.py")
            path = Path(td) / "deploy" / "worldserver-up.sh"
            path.write_text("\n".join(mutant) + "\n", encoding="utf-8")
            path.chmod(0o755)

            res = subprocess.run(["bash", "-n", str(path)],
                                 capture_output=True, text=True)
            self.assertEqual(0, res.returncode,
                             "the mutant is not valid bash, so this is a broken script "
                             "and not the fail-open under test. bash said: " + res.stderr)

            # Negative control: the text pin — anchor search, opener check, byte-identical
            # golden — accepts the mutant and reports the frozen three. If this ever
            # raises, the hole has acquired its own text pin and the emission pin below
            # becomes belt, not braces: say so here rather than let it rot.
            ref = cls("test_the_operational_error_line_carries_the_prefix_the_wrapper_"
                      "exempts")
            region = ref._refusal_block_from_lines(
                mutant, f"{self.WRAPPER.name} (three lines above the opener)")
            self.assertEqual(
                sorted(cls.WRAPPER_QUOTED_FRAGMENTS),
                ref._quoted_fragments_from_region(region),
                "the negative control has gone soft: the text pin no longer reports the "
                "frozen three on a wrapper whose real block it accepted, so it would "
                "already catch this mutant and the assertions below prove nothing")

            pinned = set(cls.WRAPPER_QUOTED_FRAGMENTS)
            for label, body, _want_rc, want_frags in self.REFUSAL_EMISSION_ROUTES:
                got = self._run_hostile(body, check_only=True, wrapper=path)
                self.assertIn("REFUSING BOOT", got["stderr"],
                              f"{label}: the mutant did not refuse the boot, so there is "
                              "no fourth notice in flight and this route proves nothing")
                emitted = self._quoted_fragments_emitted(got["stderr"])
                self.assertIn("could not write the --json payload", emitted,
                              f"{label}: the mutant stopped printing its fourth notice, "
                              "so it no longer reproduces the residual hole")
                self.assertEqual(set(), emitted - pinned,
                                 f"{label}: the fourth notice now uses a fragment outside "
                                 "the pinned three, so the exact-set design note above is "
                                 "stale — a subset check would catch this mutant too")
                self.assertNotEqual(sorted(want_frags), sorted(emitted),
                                    f"{label}: the emission pin no longer refuses a "
                                    "refused boot that hands the operator a fourth "
                                    "single-quoted notice above the block it pins")


if __name__ == "__main__":
    unittest.main(verbosity=2)
