#!/usr/bin/env python3
# Audit the table/column claims in preflight-policy.json against the real DDL.
#
# The write policy is only as good as its column names: a key column that does
# not exist in the real schema silently stops bounding anything, so a policy
# typo is a hole, not a bug. This checks every `keyColumns`, `keySets`, `bands`
# and `refs` name against the `CREATE TABLE` bodies under data/sql/base/db_world.
#
#   python3 ops/preflight/audit_policy_schema.py [--repo PATH] [--include-modules]
#
# Exit 0 = claims match, 1 = drift, 2 = usage/environment error.
#
# `customTables` are skipped by default: those tables are created by module SQL
# under modules/, which the core updater does not apply. `--include-modules`
# parses those directories too, and a custom table still missing afterwards is
# reported, because then nothing in this tree creates it at all.
import argparse
import json
import re
import sys
from pathlib import Path

CREATE_RE = re.compile(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+`([^`]+)`\s*\(", re.I)
COL_RE = re.compile(r"^\s*`([^`]+)`\s+[A-Za-z]", re.M)


def parse_ddl(paths):
    ddl = {}
    for path in paths:
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in CREATE_RE.finditer(text):
            table = m.group(1).lower()
            depth, i = 0, m.end() - 1
            while i < len(text):
                if text[i] == "(":
                    depth += 1
                elif text[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            ddl.setdefault(table, [c.lower() for c in COL_RE.findall(text[m.end():i])])
    return ddl


def named_columns(entry):
    cols = [c.lower() for c in entry.get("keyColumns", [])]
    for group in entry.get("keySets", []):
        cols += [c.lower() for c in group]
    for item in list(entry.get("bands", [])) + list(entry.get("refs", [])):
        cols.append(item["column"].lower())
    return cols


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit preflight-policy.json table/column claims against the real DDL.")
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--policy", default=None, type=Path)
    ap.add_argument("--include-modules", action="store_true",
                    help="also parse modules/*/data/sql/**/*.sql for custom tables")
    args = ap.parse_args()

    policy_path = args.policy or (args.repo / "ops/preflight/preflight-policy.json")
    try:
        policy = json.loads(policy_path.read_text())
    except (OSError, ValueError) as exc:
        print(f"audit: cannot read policy {policy_path}: {exc}", file=sys.stderr)
        return 2

    base = args.repo / "data/sql/base/db_world"
    if not base.is_dir():
        print(f"audit: no DDL directory at {base}", file=sys.stderr)
        return 2
    ddl = parse_ddl(sorted(base.glob("*.sql")))
    module_ddl = {}
    if args.include_modules:
        mods = sorted((args.repo / "modules").glob("*/data/sql/**/*.sql"))
        module_ddl = parse_ddl(mods)
        print(f"parsed {len(ddl)} base table(s), {len(module_ddl)} module table(s)")
    else:
        print(f"parsed {len(ddl)} base table(s) from {base}")

    problems = []
    for group in ("baseTables", "customTables"):
        for table, entry in policy.get(group, {}).items():
            if table.startswith("_") or not isinstance(entry, dict):
                continue
            cols = ddl.get(table)
            if cols is None and group == "customTables":
                cols = module_ddl.get(table)
                if cols is None:
                    if args.include_modules:
                        problems.append(f"{group}.{table}: nothing in this tree creates it")
                    continue
            if cols is None:
                problems.append(f"{group}.{table}: no CREATE TABLE in data/sql/base/db_world")
                continue
            for col in named_columns(entry):
                if col not in cols:
                    problems.append(f"{group}.{table}: column {col!r} is not in the DDL "
                                    f"(first columns: {', '.join(cols[:8])})")

    for line in problems:
        print(f"FAIL {line}")
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
