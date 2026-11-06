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
# The `upstreamProvenance` half runs at two depths and the output names which one ran.
# Without a git repo it is a text check of the name; with one it additionally peels
# `verifiedRef`, so a name whose object is a tree, or a ref that has been deleted from
# the clone, exits 1 here as well as refusing the boot — the gate and the audit must not
# disagree about the same policy (PRO-307 A2/A3). Read the mode line before "0 problem(s)".
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

# A git refname cannot contain `^`, `~`, `@{`, `?`, `*`, `[`, `\` or a space
# (`git check-ref-format --branch` refuses them), so any of these in `verifiedRef`
# means the policy is naming a *revision expression* rather than a ref — which is
# how PRO-248 review N1's `refs/tags/<annotated>^{tree}` shape reached the gate.
SELECTOR_RE = re.compile(r"\^\{[^}]*\}|\^\d+|~\d*|@\{")
OBJECT_NAME_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")

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


def _git(repo, argv):
    """(rc, stdout, stderr) for a read-only git probe, or (None, '', reason).

    stdin is DEVNULL rather than inherited: this audit is routinely driven from a
    script piped into `ssh host 'bash -s'`, and a child that inherits stdin drains
    the rest of that script (PRO-307 A4, same defect in the gate's DbRunner).
    """
    try:
        proc = subprocess.run(["git", *argv], cwd=str(repo),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              stdin=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)
    return (proc.returncode,
            proc.stdout.decode("utf-8", "replace").strip(),
            proc.stderr.decode("utf-8", "replace").strip())


def repo_is_git(repo) -> bool:
    rc, out, _err = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out in ("true", "1")


def ref_is_wellformed(ref: str) -> bool:
    """Does `ref` look like the name the shape checks are willing to pass?

    The resolution probe is only meaningful for a name that is not already reported
    as malformed, so the two checks never file the same defect twice.
    """
    return bool(ref) and not OBJECT_NAME_RE.match(ref) \
        and ref.startswith("refs/remotes/") and SELECTOR_RE.search(ref) is None


def resolve_provenance_problems(policy, repo):
    """Peel `verifiedRef` in `repo` and report what the boot gate would refuse.

    The shape checks are pure text, so a well-formed name under `refs/remotes/` with
    no revision selector passes them whatever object it happens to name. PRO-303
    measured this on the deployhost: a policy copy naming
    `refs/remotes/origin/pro303-drill-tree`, created with
    `git update-ref refs/remotes/origin/pro303-drill-tree $(git rev-parse <sha>^{tree})`,
    made the gate refuse the boot as `upstream-ref-not-a-commit` while the audit on the
    same repo and the same file printed "0 problem(s)" and exited 0 — exactly the
    divergence class f96c40313 ("audit must not pass a verifiedRef the gate refuses")
    was written to close (PRO-307 A2).

    A ref that has been deleted from the clone is the other shape text cannot see: the
    gate refuses that boot as `upstream-ref-deleted`, while a shape-only audit passes it
    (PRO-307 A3 item 1). Same two-step question the gate asks — peel to a commit, and
    only if that fails ask whether the name resolved to anything at all.
    """
    cfg = policy.get("upstreamProvenance")
    if not isinstance(cfg, dict):
        return []
    ref = str(cfg.get("verifiedRef") or "").strip()
    if not ref_is_wellformed(ref):
        return []
    rc, _out, err = _git(repo, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    if rc is None:
        return [f"upstreamProvenance.verifiedRef: cannot resolve {ref!r} in {repo}: {err}; "
                f"the gate fails closed on the same question, so an audit that cannot ask "
                f"it must not report a pass either"]
    if rc == 0:
        return []
    rc2, oid, err2 = _git(repo, ["rev-parse", "--verify", "--quiet", ref])
    if rc2 is None:
        return [f"upstreamProvenance.verifiedRef: cannot resolve {ref!r} in {repo}: {err2}"]
    if rc2 != 0 or not oid:
        refresh = str(cfg.get("refreshCommand") or "the policy refreshCommand").strip()
        return [f"upstreamProvenance.verifiedRef: {ref!r} does not exist in {repo}, so the "
                f"boot gate refuses as upstream-ref-deleted (or, once a last-verified "
                f"record names it, upstream-ref-attested-missing) while a shape-only read "
                f"of the same file would pass it; run `{refresh}` or point verifiedRef at "
                f"a ref this clone holds"]
    rc3, otype, _err3 = _git(repo, ["cat-file", "-t", oid])
    kind = otype if rc3 == 0 and otype else "non-commit object"
    return [f"upstreamProvenance.verifiedRef: {ref!r} resolves to {oid[:12]} in {repo}, "
            f"which is a {kind}, not a commit; the gate cannot list a tree of it and "
            f"refuses the boot as not-a-commit, so this policy must not audit clean"]


def provenance_problems(policy, repo=None):
    """Check the upstreamProvenance invariants the write-policy exemption rests on.

    PRO-248 review N3. `classify_origin()` decides the exemption for an
    `origin: "upstream"` directory from this block before it parses a single statement,
    so a policy that quietly loses the block — or names a `verifiedRef` that a local
    commit can move — re-hands out the exemption the PRO-248 rule exists to withhold.
    Nothing else in this repository machine-checks that; the `_comment` next to it is
    prose, and prose is what review B1 found to be untrue.

    This is a column audit's equal: the claim is `verifiedRef` names a remote-tracking
    ref that only a fetch can move, and an unverifiable claim here is a hole, not a bug.

    Two depths, and the caller reports which one ran (`provenance_mode`). Without a
    repo this is text only: block present, name non-empty, not an object name, under
    `refs/remotes/`, no revision selector. With a repo it additionally peels the name,
    which is what catches the shapes text cannot see — a `refs/remotes/...` name whose
    object is a tree, and a ref deleted from the clone (PRO-307 A2, A3 item 1).
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
    elif OBJECT_NAME_RE.match(ref):
        # PRO-291 (PRO-248 review N1): a mistyped object name resolves to *something*,
        # so the gate files it 'not-a-commit' and refuses; an audit that exits 0 on it
        # is the audit/gate divergence review N5 was about.
        problems.append(
            f"upstreamProvenance.verifiedRef: {ref!r} is an object name, not a ref — "
            f"a fetch does not move it and it cannot be shown to be a commit the way a "
            f"remote-tracking ref can (name e.g. refs/remotes/origin/Playerbot)")
    elif not ref.startswith("refs/remotes/"):
        problems.append(
            f"upstreamProvenance.verifiedRef: {ref!r} is not under refs/remotes/, so "
            f"the person whose forged `git commit --author=...` this rule exists to "
            f"catch can move it (name it fully qualified, e.g. "
            f"refs/remotes/origin/Playerbot)")
    elif SELECTOR_RE.search(ref) is not None:
        # `refs/remotes/origin/Playerbot^{tree}` passes the prefix test and names a
        # tree, which is Rev's PRO-248 N1 POC. A refname cannot contain `^`, `~` or
        # `@{`, so this is a revision expression and the gate cannot peel it to a
        # commit — it refuses the boot on exactly that.
        problems.append(
            f"upstreamProvenance.verifiedRef: {ref!r} carries the revision selector "
            f"{SELECTOR_RE.search(ref).group(0)!r}, so it names something other than a "
            f"ref and the gate cannot peel it to a commit; it refuses the boot as "
            f"not-a-commit (name the ref plainly, e.g. refs/remotes/origin/Playerbot)")
    if not str(cfg.get("refreshCommand") or "").strip():
        problems.append("upstreamProvenance.refreshCommand: empty, so a boot refusal "
                        "could not name the command that clears it")
    # Shape is necessary and not sufficient, and it is all that is available with no
    # repo to ask. Where a repo is present, resolve the name: that is the check which
    # makes the audit's answer the gate's answer (PRO-307 A2).
    if repo is not None and repo_is_git(repo):
        problems += resolve_provenance_problems(policy, repo)
    return problems


def provenance_mode(policy, repo) -> tuple[str, str]:
    """What the provenance half of this run actually checked: (mode, why).

    "0 problem(s)" has to be readable as what it means. A shape-only pass says the
    policy is well-formed and nothing more; it does not say the ref resolves, is a
    commit, or is even still in the clone (PRO-307 A2/A3).
    """
    cfg = policy.get("upstreamProvenance")
    ref = str(cfg.get("verifiedRef") or "").strip() if isinstance(cfg, dict) else ""
    if not ref_is_wellformed(ref):
        return "shape-only", ("verifiedRef is not a well-formed remote-tracking name, "
                              "which is itself reported above; nothing was resolved")
    if repo is None:
        return "shape-only", "no repo was named, so verifiedRef was never resolved"
    if not repo_is_git(repo):
        return "shape-only", f"{repo} is not a git tree, so verifiedRef was never resolved"
    return "resolved", f"verifiedRef was peeled in the git repo at {repo}"


def main(argv=None) -> int:
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
    args = ap.parse_args(argv)

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
    mode, mode_why = provenance_mode(policy, repo)
    problems += provenance_problems(policy, repo)
    print(f"provenance check: {mode.upper()} — {mode_why}")
    if mode != "resolved":
        print("provenance check: a shape-only pass does not mean verifiedRef resolves, "
              "is a commit, or is still in the clone; the boot gate checks all three")
    for line in problems:
        print(f"FAIL {line}")
    if unchecked:
        print(f"{len(unchecked)} claim(s) unchecked, {len(problems)} problem(s)")
    else:
        print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
