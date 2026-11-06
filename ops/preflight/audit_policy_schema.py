#!/usr/bin/env python3
# Audit the table/column claims in preflight-policy.json against the real DDL.
#
# The write policy is only as good as its column names: a key column that does
# not exist in the real schema silently stops bounding anything, so a policy
# typo is a hole, not a bug. This checks every `keyColumns`, `keySets`, `bands`
# and `refs` name against the `CREATE TABLE` bodies in this tree, and every
# `upstreamProvenance` claim against the invariants the upstream exemption needs
# (PRO-248 review N3).
#
#   python3 ops/preflight/audit_policy_schema.py [--repo PATH] [--no-modules]
#
# Exit 0 = claims match, 1 = drift, 2 = usage/environment error.
#
# `customTables` are created by MODULE SQL, and in this repository module SQL is
# tracked under `wrath-unbound/modules/mod-*/data/sql/db-world/` as well as the
# upstream `modules/*/data/sql/` layout. Both roots are parsed by default
# (PRO-179 review B-3): pointing only at `modules/` meant a clean clone — where
# nothing has been copied into `modules/` — reported "nothing in this tree
# creates it" for every custom table and exited 1, while passing on any host that
# happened to have a copied-out module tree. In other words the audit's verdict
# depended on the accident of the working tree instead of the tracked content.
#
# A claim that cannot be checked is named, never skipped: `--no-modules` still
# reports each custom table it did not verify as a WARN.
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

CREATE_RE = re.compile(r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+`([^`]+)`\s*\(", re.I)
COL_RE = re.compile(r"^\s*`([^`]+)`\s+[A-Za-z]", re.M)

# Relative to the repo root. Both layouts are listed because both are tracked
# here; `git ls-files` is the primary discovery mechanism and these are the
# patterns it is given (and the globs the filesystem fallback uses).
MODULE_SQL_PATTERNS = (
    "modules/*/data/sql/**/*.sql",
    "wrath-unbound/modules/*/data/sql/**/*.sql",
    "nodeps/modules/*/data/sql/**/*.sql",
)


def tracked_files(repo, patterns):
    """Tracked .sql paths matching any pattern, via `git ls-files`.

    Returns None when git cannot answer, so the caller can fall back to walking
    the filesystem. Tracked content is the point: the audit must give the same
    answer in a clean clone as on a host with a copied-out module tree.
    """
    try:
        proc = subprocess.run(["git", "ls-files", "--"] + list(patterns),
                              cwd=str(repo), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    names = proc.stdout.decode("utf-8", "replace").splitlines()
    if not names:
        # An empty answer is only trustworthy inside a git tree; `git ls-files`
        # exits 0 with no output outside one, which would silently mean "no
        # module SQL exists".
        inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                                cwd=str(repo), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=60)
        if inside.returncode != 0 or inside.stdout.strip() not in (b"true", b"1"):
            return None
    return [repo / n for n in names]


def walk_files(repo, patterns):
    out = []
    for pat in patterns:
        out.extend(repo.glob(pat))
    return sorted(p for p in out if p.is_file())


def module_sql_files(repo):
    return tracked_files(repo, MODULE_SQL_PATTERNS) or walk_files(repo, MODULE_SQL_PATTERNS)


def parse_ddl(paths):
    ddl = {}
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"audit: cannot read {path}: {exc}", file=sys.stderr)
            continue
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


def provenance_problems(policy):
    """Check the upstreamProvenance invariants the write-policy exemption rests on.

    PRO-248 review N3. `classify_origin()` decides the exemption for an
    `origin: "upstream"` directory from this block before it parses a single statement,
    so a policy that quietly loses the block — or names a `verifiedRef` that a local
    commit can move — re-hands out the exemption the PRO-248 rule exists to withhold.
    Nothing else in this repository machine-checks that; the `_comment` next to it is
    prose, and prose is what review B1 found to be untrue.

    This is a column audit's equal: the claim is `verifiedRef` names a remote-tracking
    ref that only a fetch can move, and an unverifiable claim here is a hole, not a bug.
    """
    cfg = policy.get("upstreamProvenance")
    if not isinstance(cfg, dict):
        return ["upstreamProvenance: block is missing, so a new file in an "
                "origin:upstream directory would fall back to the pre-PRO-248 "
                "directory+author exemption"]
    problems = []
    ref = str(cfg.get("verifiedRef") or "").strip()
    if not ref:
        problems.append("upstreamProvenance.verifiedRef: empty, so the upstream "
                        "exemption is decided by directory+author alone")
    elif not ref.startswith("refs/remotes/"):
        problems.append(
            f"upstreamProvenance.verifiedRef: {ref!r} is not under refs/remotes/, so "
            f"the person whose forged `git commit --author=...` this rule exists to "
            f"catch can move it (name it fully qualified, e.g. "
            f"refs/remotes/origin/Playerbot)")
    if not str(cfg.get("refreshCommand") or "").strip():
        problems.append("upstreamProvenance.refreshCommand: empty, so a boot refusal "
                        "could not name the command that clears it")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Audit preflight-policy.json table/column claims against the real DDL.")
    ap.add_argument("--repo", default=None, type=Path,
                    help="repo root containing data/sql/base/db_world "
                         "(default: this file's repo, else the current directory)")
    ap.add_argument("--policy", default=None, type=Path)
    ap.add_argument("--include-modules", action="store_true",
                    help="kept for compatibility: module SQL is parsed by default")
    ap.add_argument("--no-modules", action="store_true",
                    help="do not parse module SQL; customTables claims are then WARNed "
                         "as unchecked rather than silently skipped")
    args = ap.parse_args()

    here = Path(__file__).resolve()
    repo = args.repo
    if repo is None:
        guess = here.parents[2]
        repo = guess if (guess / "data/sql/base/db_world").is_dir() else Path.cwd()
    policy_path = args.policy or (here.parent / "preflight-policy.json")
    try:
        policy = json.loads(policy_path.read_text())
    except (OSError, ValueError) as exc:
        print(f"audit: cannot read policy {policy_path}: {exc}", file=sys.stderr)
        return 2

    base = repo / "data/sql/base/db_world"
    if not base.is_dir():
        print(f"audit: no DDL directory at {base}", file=sys.stderr)
        return 2
    ddl = parse_ddl(sorted(base.glob("*.sql")))
    module_ddl = {}
    mods = []
    if not args.no_modules:
        mods = module_sql_files(repo)
        module_ddl = parse_ddl(mods)
        roots = sorted({str(p.parent) for p in mods})
        print(f"parsed {len(ddl)} base table(s) from {base}")
        print(f"parsed {len(module_ddl)} module table(s) from {len(mods)} file(s) in "
              f"{len(roots)} module dir(s)")
        for root in roots[:12]:
            print(f"  {root}")
        if len(roots) > 12:
            print(f"  ... {len(roots) - 12} more")
    else:
        print(f"parsed {len(ddl)} base table(s) from {base}; module SQL skipped by --no-modules")

    problems = []
    unchecked = []
    for group in ("baseTables", "customTables"):
        for table, entry in policy.get(group, {}).items():
            if table.startswith("_") or not isinstance(entry, dict):
                continue
            cols = ddl.get(table) or module_ddl.get(table)
            if cols is None:
                if group == "customTables" and args.no_modules:
                    unchecked.append(f"{group}.{table}")
                    continue
                where = "data/sql/base/db_world or module SQL" if not args.no_modules \
                    else "data/sql/base/db_world"
                problems.append(f"{group}.{table}: no CREATE TABLE in {where}")
                continue
            for col in named_columns(entry):
                if col not in cols:
                    problems.append(f"{group}.{table}: column {col!r} is not in the DDL "
                                    f"(first columns: {', '.join(cols[:8])})")

    for line in unchecked:
        print(f"WARN {line}: not verified — module SQL was skipped with --no-modules")
    # The upstream exemption is a policy claim like any other column name: if it does
    # not hold the failure is silent, so it is audited here rather than in a review
    # comment nobody re-reads (PRO-248 review N3).
    problems += provenance_problems(policy)
    for line in problems:
        print(f"FAIL {line}")
    if unchecked:
        print(f"{len(unchecked)} claim(s) unchecked, {len(problems)} problem(s)")
    else:
        print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
