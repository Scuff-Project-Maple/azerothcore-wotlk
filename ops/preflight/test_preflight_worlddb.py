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
import re
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

    def _clone(self, single_branch=False):
        """A real `git clone` — the repository shape PRO-291 review B1 got wrong.

        `git clone` populates `refs/remotes/<remote>/*` itself and writes no reflog for
        those refs, which is precisely what the sibling probe must not read as a fetch.
        The seed holds only `master`, so the policy's `refs/remotes/origin/Playerbot`
        has never existed anywhere, which is the honest case that has to boot.
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
                         namespace=(0, "", "")):
        """Hand git each shape on its own, and record every argv it was asked (review N4).

        `_stub_git` above pins the remote-tracking question. This pins the *existence*
        question, whose whole point is that `git rev-parse --verify <ref>^{commit}`
        collapses "no such ref" and "the ref names a tree" into one exit code, and that
        a real repository cannot produce both shapes separately.

        `reflog_paths` answers `--git-path logs/<refname>` per ref name, so the ref the
        policy trusts and a sibling can be given different answers — which is the whole
        content of the sibling probe (PRO-291 review B1). Anything not listed falls
        back to `reflog_path`.
        """
        calls = []

        def fake(argv, timeout=60, errors="replace"):
            calls.append(list(argv))
            argv = self._git_argv(argv)
            if argv[:2] == ["rev-parse", "--symbolic-full-name"]:
                return full_name
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
        (d / ".git" / "FETCH_HEAD").write_text("abc123\t\tbranch 'Playerbot' of origin\n")
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        up = pf.upstream_ref()
        self.assertEqual("missing", up["state"])
        self.assertTrue(up["wasTracked"], up["wasTrackedDetail"])
        self.assertIn("FETCH_HEAD", up["wasTrackedDetail"])
        self.assertEqual(pf.REF_INDETERMINATE, pf.upstream_ref_state(p)[0])

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
            "abc123\t\tbranch 'Playerbot' of origin\n")
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

    def test_an_empty_fetch_head_fails_closed_without_claiming_a_fetch(self):
        # PRO-301 review N2: the same presence/observation conflation one level up. A
        # 0-byte FETCH_HEAD is a file `exists()` says yes to, and `exists()` alone was
        # buying the word "fetched". It stays a trace — so the boot is still refused —
        # but it earns neither the fetch claim nor the deletion claim.
        d = self._repo()
        p = self._commit(d, f"{self.UP_REL}/2026_11_04_00.sql", self.IN_BAND)
        pf = self._pf(d, verified_ref="refs/remotes/origin/Absent")
        self._stub_git_shapes(
            pf, commit_peel=(1, "", ""), obj=(1, "", ""),
            reflog_path=(0, ".git/logs/ABSENT", ""),
            fetch_head_path=(0, ".git/FETCH_HEAD", ""), namespace=(0, "", ""))
        (d / ".git" / "FETCH_HEAD").write_bytes(b"")
        up = pf.upstream_ref()
        self.assertTrue(up["wasTracked"],
                        "an empty FETCH_HEAD was read as a clone that never fetched")
        self.assertEqual("unobserved", up["wasTrackedCertainty"],
                         "an empty FETCH_HEAD was allowed to claim a fetch")
        self.assertEqual("ref-deleted", pf.upstream_ref_state(p)[2])
        self._run(pf, self._file(d, p))
        fails, _ = self._outcomes(pf)
        self.assertTrue(pf.r.failed, "an empty FETCH_HEAD let the exemption through")
        msg = next(m for r, m in fails if r == "upstream-ref-deleted")
        self.assertNotIn("this clone has fetched", msg)
        self.assertIn("records nothing", msg)

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
                    [], [c for c in seen if self._git_verb(c) not in allowed],
                    f"the gate asked git for a verb this allowlist does not cover "
                    f"while deciding {label}")
                self.assertTrue(any(c[:1] == ["rev-parse"] for c in seen),
                                "the probes never ran, so this checked nothing")
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


class TestBootPathPRO306(unittest.TestCase):
    """PRO-306 B1/B2: what the wrapper may claim about a boot.

    B1 — a gated boot must not silently re-run the `depends_on` one-offs, and
    must name the mode it ran in. B2 — a boot claim must be tied to a container
    that actually restarted, so the wrapper cannot end in `exec docker compose`
    (nothing can report afterwards) and cannot accept a `--force-recreate` run
    whose `StartedAt` never moved.

    Argument-level only: `COMPOSE_ROOT` points at an empty temp dir with no
    `.stack/lease`, so a run that clears parsing dies at the lease check and can
    never reach `docker compose`, even on the deploy host.
    """

    WRAPPER = HERE.parent / "deploy" / "worldserver-up.sh"

    def _run(self, argv, env_extra=None):
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, COMPOSE_ROOT=tmp, LEASE_FILE=str(Path(tmp) / "lease"))
            env.update(env_extra or {})
            return subprocess.run(["bash", str(self.WRAPPER), *argv],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=60, env=env)

    def _source(self):
        return self.WRAPPER.read_text(encoding="utf-8")

    # --- B1: the dependency mode is never something the log does not name --- #
    def test_wrapper_refuses_no_deps_from_the_caller(self):
        for argv in (["--no-deps"], ["--no-deps=true"], ["--", "--no-deps"]):
            proc = self._run(argv)
            self.assertEqual(proc.returncode, 2, f"{argv}: {proc.stdout[:200]}")
            self.assertIn("the boot path owns dependency handling", proc.stdout, f"{argv}")

    def test_wrapper_accepts_the_explicit_one_off_mode(self):
        proc = self._run(["--with-one-offs"])
        self.assertNotIn("refusing to forward", proc.stdout)
        self.assertIn("lease", proc.stdout, "should reach the lease check, not parsing")

    def test_boot_mode_is_validated(self):
        proc = self._run([], env_extra={"BOOT_MODE": "whatever"})
        self.assertEqual(proc.returncode, 2)
        self.assertIn("BOOT_MODE must be", proc.stdout)

    def test_help_names_the_boot_mode_and_the_dependency_gate(self):
        proc = self._run(["--help"])
        self.assertEqual(proc.returncode, 0)
        for claim in ("BOOT MODE", "--with-one-offs", "read-only"):
            self.assertIn(claim, proc.stdout)

    # --- B2: a boot claim has to be tied to a container that restarted ------- #
    def test_wrapper_does_not_end_in_exec_docker_compose(self):
        src = self._source()
        self.assertNotIn('exec "${COMPOSE_CMD[@]}"', src,
                         "the boot must be reportable: exec leaves nothing to print "
                         "the StartedAt / since-boot FATAL verdict from (PRO-306 B2)")
        for marker in ("BOOT VERDICT", "BOOT_T0", "StartedAt", "since=", "lifetime"):
            self.assertIn(marker, src, f"boot-identity reporting lost {marker!r}")

    def test_wrapper_rejects_recreate_and_no_recreate_together(self):
        proc = self._run(["--force-recreate", "--no-recreate"])
        self.assertEqual(proc.returncode, 2)
        self.assertIn("cancel each other", proc.stdout)

    def test_recreate_env_and_flag_are_the_same_request_after_double_dash(self):
        # PRO-179 NB-1 keeps benign post-`--` flags forwarded; PRO-306 B2 adds
        # that they must also be *seen*, so the two spellings cannot disagree.
        proc = self._run(["--", "--force-recreate"])
        self.assertNotIn("refusing to forward", proc.stdout)
        self.assertIn("lease", proc.stdout)
        src = self._source()
        self.assertIn("FORCE_RECREATE=1", src)
        self.assertIn('RECREATE" == "1"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
