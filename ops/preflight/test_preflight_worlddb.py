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
    NO_JSON_PAYLOAD_NOTE,
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
        pf.attestation_record = lambda ref: (pf.ATTEST_UNTRUSTED,
                                             "a cause-less refusal", "", "", "")
        up = pf.upstream_ref()
        self.assertTrue(up.get("treeSkipped"),
                        "an untrusted record with no cause was treated as no record")
        self.assertTrue(up.get("attestationRefused"))
        self.assertEqual([], [c for c in seen if self._git_verb(c) == "ls-tree"],
                         "the tree of a ref the gate does not trust was read anyway")
        state, detail, cause = pf.upstream_ref_state(p)
        self.assertEqual((pf.REF_INDETERMINATE, "attestation-untrusted"),
                         (state, cause), detail)


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

    def _drive_main(self, stdout, stderr, *, render_raises=False, json_target="default",
                    expect_raises=False):
        """Run the real `main()` with the gate stubbed and the operator's streams swapped.

        `main()` had no test before this class, which is why the first version of these
        pins reached for `inspect.getsource`. A source-text ordering assertion cannot
        tell you which half loses when the other one throws, and it pins the shape that
        caused finding B in the first place (PRO-313 review B1). This drives the real
        function and measures both halves.
        """
        import sys
        import types
        import preflight_worlddb as P

        raw = self.RAW

        class StubPreflight:
            def __init__(self, args, policy, report):
                self.args, self.policy, self.report = args, policy, report
                self.sql_root = Path(args.compose_root) / "data" / "sql"

            def run(self):
                self.report.fail("3",
                                 f"{raw} is upstream-provenanced at HEAD but has "
                                 "uncommitted changes, so it is checked against the "
                                 "write policy",
                                 file=raw, rule="dirty-worktree")
                return P.EXIT_FAIL

        def boom(report, args):
            raise RuntimeError("fixture: render() blew up")

        original = (P.Preflight, P.render, sys.stdout, sys.stderr, sys.__stderr__)
        P.Preflight = StubPreflight
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
                else:
                    out_path = None
                    json_arg = None
                argv = ["--compose-root", str(tree), "--policy", str(policy)]
                if json_arg is not None:
                    argv += ["--json", json_arg]
                rc = None
                raised = None
                try:
                    rc = P.main(argv)
                except BaseException as exc:      # a fixture may raise one on purpose
                    if not expect_raises:
                        raise
                    raised = type(exc).__name__
                payload = None
                if out_path is not None and out_path.is_file():
                    payload = out_path.read_bytes()
        finally:
            P.Preflight, P.render, sys.stdout, sys.stderr, sys.__stderr__ = original
        return types.SimpleNamespace(rc=rc, raised=raised, payload=payload,
                                      out=stdout.value(), err=stderr.value(),
                                      fallback=fallback.value())

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
        is the audience; `NO_JSON_PAYLOAD_NOTE` is the single wording for the absent case.
        """
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               render_raises=True, json_target="none")
        text = res.out.decode("ascii")
        self.assertIn("render() raised RuntimeError", text)
        self.assertIn(NO_JSON_PAYLOAD_NOTE, text)
        self.assertNotIn("unaffected", text,
                         "the degraded line still promises a payload nobody asked for")
        self.assertNotIn("None", text, "the absent payload is rendered as a word, not None")

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
        """PRO-341 review B1: `--json ""` skips the write, so it may not claim the disk.

        `main()` gated the write on truthiness and this note on `is None`. The wrapper
        forwards unrecognised operator arguments verbatim, so an operator's
        `--json "$TARGET"` with a blank `$TARGET` arrives here as an empty path, and
        that run printed `the --json payload at  is on disk` while nothing was written.
        Same class as PRO-325 NB2 (an absent thing asserted as present), one layer up,
        and on the line an operator reads while the boot is being refused.

        The rc is deliberately not pinned here: a blank `--json` still leaves
        `payload_error` None, so it does not reach the evidence-missing refusal either.
        That half predates this branch and is tracked separately; pinning `EXIT_FAIL`
        now would make the follow-up look like a regression when it lands.
        """
        res = self._drive_main(self._ReportingStream(), self._ReportingStream(),
                               render_raises=True, json_target="empty")
        self.assertIsNone(res.payload, "fixture: a blank --json must write nothing")
        text = res.out.decode("ascii")
        self.assertIn("render() raised RuntimeError", text,
                      "the fixture stopped exercising the degraded line")
        self.assertIn(NO_JSON_PAYLOAD_NOTE, text)
        self.assertNotIn("on disk", text,
                         "the line still asserts evidence that was never written")
        self.assertNotIn("unaffected", text)
        self.assertNotIn("None", text, "a blank path must not leak as the word None")


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

    # The real `main()` under a terminal that will not have it. `{stdout_setup}` is the
    # one thing the two end-to-end pins differ on, so the stub exists once.
    _MAIN_DRIVER = '''
        import pathlib
        import sys
        sys.path.insert(0, sys.argv[1])
        import preflight_worlddb as P


        class StubPreflight:
            def __init__(self, args, policy, report):
                self.args, self.policy, self.report = args, policy, report
                self.sql_root = pathlib.Path(args.compose_root) / "data" / "sql"

            def run(self):
                self.report.fail("3", "fixture: refused on purpose",
                                 file="x.sql", rule="dirty-worktree")
                return P.EXIT_FAIL


        P.Preflight = StubPreflight
        td = pathlib.Path(sys.argv[2])
        (td / "tree" / "data" / "sql").mkdir(parents=True)
        (td / "policy.json").write_text("{{}}")
        {stdout_setup}
        sys.exit(P.main(["--compose-root", str(td / "tree"), "--policy",
                         str(td / "policy.json"), "--json", str(td / "verdict.json")]))
    '''

    def _drive_real_main(self, driver, td):
        import subprocess
        import sys
        import textwrap
        script = Path(td) / "driver.py"
        script.write_text(textwrap.dedent(driver))
        return subprocess.run([sys.executable, str(script), str(HERE), str(td)],
                              env=self._env(), capture_output=True, text=True)

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
        driver = self._MAIN_DRIVER.format(
            stdout_setup='sys.stdout = open("/dev/full", "w")   # terminal refuses us')
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
        import shlex
        import subprocess
        import sys
        import textwrap
        import preflight_worlddb as P
        driver = self._MAIN_DRIVER.format(
            stdout_setup="pass   # fd 1 was closed by the shell before the interpreter ran")
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "driver.py"
            script.write_text(textwrap.dedent(driver))
            shell = (f"exec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "
                     f"{shlex.quote(str(HERE))} {shlex.quote(str(td))} 1>&-")
            proc = subprocess.run(["/bin/sh", "-c", shell], env=self._env(),
                                  capture_output=True, text=True)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
