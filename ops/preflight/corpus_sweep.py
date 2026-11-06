#!/usr/bin/env python3
"""Run the PRO-179 write policy over every .sql in the four included directories.

The boot gate only policy-checks files the updater has not applied yet. This tool
runs the *same* engine (ops/preflight/preflight_worlddb.py, check_policy) over the
whole included corpus and tallishes findings per rule, so a change to the policy
engine can be measured instead of asserted:

    python3 ops/preflight/corpus_sweep.py --out /tmp/before.json
    # ...edit the engine...
    python3 ops/preflight/corpus_sweep.py --diff /tmp/before.json

Counts are reported per rule overall, per provenance class (company / upstream /
unknown), and per included directory. `--rule RULE` lists the offending
file:line for one rule. Nothing here touches MySQL: provenance comes from git and
the policy from preflight-policy.json, exactly as the gate reads them.

The invariant the README names is the `company` column: those are the findings that
turn into `REFUSING BOOT`. It is therefore also the exit status — a non-zero company
total exits 1, and `.github/workflows/worlddb-preflight.yml` runs it that way on every
pull request that touches `ops/preflight/**` or `data/sql/**`. `--diff` prints
the per-rule company delta alongside the total, because a finding that migrates
`upstream -> company` leaves the totals untouched while creating a new boot refusal
(PRO-179 review NB-C). Use `--measure-only` for a baseline capture, where a non-zero
company total is expected and must not fail the run.

The `--out` tally is also the input to `ci_provenance_guard.py`, which reads
`provenanceFiles`/`byRuleOrigin` and refuses a run whose provenance could not be
attributed — a green company column on a checkout that could not attribute anything is
a check that measured nothing (PRO-285).

Exit status: 0 clean, 1 findings in the company column, 2 on a usage/operational error.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# A company-authored file is supposed to have zero findings, so the list is normally
# short; the cap only exists so a policy change that suddenly refuses company content
# cannot bury the rest of the CI log.
COMPANY_DETAIL_LIMIT = 100

import preflight_worlddb as pf  # noqa: E402


class Args:
    """Only the attributes Preflight/MySQL read at construction time."""

    def __init__(self, root: Path, policy: Path, strict_all: bool, policy_all: bool,
                 live_db: str):
        self.compose_root = str(root)
        self.sql_root = None
        self.git_root = None
        self.policy = str(policy)
        # The gate reads the live schema from the policy file; hard-coding it here meant
        # `cross-schema-write` could be measured against a schema the gate never uses
        # (PRO-179 review NB-C).
        self.live_db = live_db
        self.scratch_db = None
        self.db_access = "command"
        self.db_service = "ac-database"
        self.db_container = None
        self.db_user = "nobody"
        self.db_password = ""
        self.mysql_command = "true"
        self.db_timeout = 1
        self.compose_project = None
        self.compose_file = None
        self.strict_all = strict_all
        self.policy_all = policy_all


def git_head(root: Path) -> str:
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    return proc.stdout.decode().strip() or "unknown"


def build_files(engine: pf.Preflight, policy: dict, workers: int) -> list[dict]:
    dirs = []
    for entry in policy["includedDirs"]:
        local = engine.git_root / entry["path"][1:].lstrip("/")
        dirs.append({"db_path": entry["path"], "state": entry["state"],
                     "dir": local, "origin": entry["origin"]})
    files = engine.collect_files(dirs)
    paths = [f["path"] for f in files]
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        authors = dict(zip(paths, ex.map(engine.git_author_origin, paths)))
    for f in files:
        origin, provenance = engine.classify_origin(f, author=authors[f["path"]])
        f["origin"] = origin
        f["provenance"] = provenance
    return files


def tally(engine: pf.Preflight, files: list[dict]) -> dict:
    # PRO-179 review rank 3, as a corpus property rather than a handful of literals:
    # `_region_tables()` exists only to name the tables a *refused* multi-table write was
    # reaching for, so over the whole corpus it must be consulted exactly once per
    # `multi-table-write` finding. A consultation with no refusal means the scan is
    # reachable on a write the gate accepts — a policy change wearing a report fix — and
    # a refusal with no consultation means the report lost the table it should name.
    consultations: list[str] = []
    real_scan = pf._region_tables

    def spy(masked, lo, hi, from_end=None, known_names=None):
        consultations.append(masked[lo:hi])
        return real_scan(masked, lo, hi, from_end, known_names)

    pf._region_tables = spy
    try:
        engine.check_policy(files)
    finally:
        pf._region_tables = real_scan
    by_rel = {f["rel"]: f for f in files}
    by_rule: Counter = Counter()
    by_rule_origin: dict[str, Counter] = {}
    by_dir: Counter = Counter()
    for v in engine.violations:
        by_rule[v["rule"]] += 1
        f = by_rel.get(v["file"])
        origin = f["origin"] if f else "unknown"
        dirn = f["dir"]["db_path"] if f else "?"
        by_rule_origin.setdefault(v["rule"], Counter())[origin] += 1
        by_dir[dirn] += 1
    # Report fidelity only — no judgement in here changes which statements the engine
    # accepts. The `company` column is the one the CI check gates on, and "COMPANY
    # findings: N" is not an actionable failure: the reader has to learn which file and
    # which rule, and `--rule` re-runs the whole sweep to answer it (PRO-285). So the
    # offending rows are recorded while the tallies are being counted.
    company_list = [
        {"file": v["file"], "line": v.get("line"), "rule": v["rule"],
         "detail": (v.get("detail") or "")[:200]}
        for v in engine.violations
        if (by_rel.get(v["file"]) or {}).get("origin") == "company"
    ]
    provenance_files = dict(Counter(f.get("origin", "unknown") for f in files))
    return {
        "files": len(files),
        "filesUnderPolicy": engine.policy_stats.get("files", 0),
        "statements": engine.policy_stats.get("statements", 0),
        "findings": sum(by_rule.values()),
        "regionScanConsultations": len(consultations),
        "scanConsultationsWithoutRefusal": len(consultations) - by_rule.get("multi-table-write", 0),
        "byRule": dict(sorted(by_rule.items())),
        "byRuleOrigin": {r: dict(c) for r, c in sorted(by_rule_origin.items())},
        "byDir": dict(sorted(by_dir.items())),
        "perFile": dict(Counter(v["file"] for v in engine.violations).most_common(25)),
        "companyFindingList": sorted(
            company_list, key=lambda v: (v["file"], v["line"] or 0, v["rule"])),
        "provenanceFiles": {k: v for k, v in sorted(provenance_files.items())},
    }


def print_report(result: dict, top: int, rule_filter: str | None,
                 engine: pf.Preflight) -> None:
    print(f"files={result['files']} under-write-policy={result['filesUnderPolicy']} "
          f"statements={result['statements']} findings={result['findings']}")
    print(f"region-scan consultations={result.get('regionScanConsultations', '?')} "
          f"multi-table-write={result['byRule'].get('multi-table-write', 0)} "
          f"unmatched={result.get('scanConsultationsWithoutRefusal', '?')}")
    print("\nrule                              total   company  upstream  unknown")
    for rule, count in sorted(result["byRule"].items(), key=lambda kv: -kv[1])[:top]:
        o = result["byRuleOrigin"].get(rule, {})
        print(f"{rule:<32} {count:>6} {o.get('company', 0):>8} "
              f"{o.get('upstream', 0):>9} {o.get('unknown', 0):>8}")
    if rule_filter:
        print(f"\n--- {rule_filter} ---")
        for v in engine.violations:
            if v["rule"] == rule_filter:
                print(f"{v['file']}:{v.get('line')}: {v.get('detail', '')[:200]}")


def company_by_rule(result: dict) -> dict[str, int]:
    """Per-rule count of findings authored by the company.

    This is the column that matters: these are the findings the boot gate turns into
    `REFUSING BOOT`, so a non-zero total is a falsifiable invariant rather than
    decoration (PRO-179 review NB-C).
    """
    out: dict[str, int] = {}
    for rule, origins in (result.get("byRuleOrigin") or {}).items():
        n = int(origins.get("company", 0))
        if n:
            out[rule] = n
    return out


def company_total(result: dict) -> int:
    return sum(company_by_rule(result).values())


def print_diff(current: dict, baseline_path: Path) -> list[str]:
    """Print per-rule deltas and return the rules whose company column grew.

    Diffing totals alone was the defect: a finding that migrates `upstream -> company`
    leaves every total untouched while creating a brand-new boot refusal, and the tool
    printed "(no rule changed)" (PRO-179 review NB-C). The company column is therefore
    diffed in its own right, and a rule that gains company findings is reported even
    when its total is unchanged.
    """
    base = json.loads(baseline_path.read_text())
    rules = sorted(set(current.get("byRule", {})) | set(base.get("byRule", {})))
    base_company = company_by_rule(base)
    cur_company = company_by_rule(current)
    print(f"diff vs {baseline_path} (baseline head {base.get('head', '?')})")
    print("rule                               before    after     delta   company  c-delta")
    moved = 0
    grown: list[str] = []
    for rule in rules:
        before = base.get("byRule", {}).get(rule, 0)
        after = current.get("byRule", {}).get(rule, 0)
        cb, ca = base_company.get(rule, 0), cur_company.get(rule, 0)
        if before == after and cb == ca:
            continue
        moved += 1
        flag = ""
        if ca > cb:
            grown.append(rule)
            flag = "   <-- new company finding (boot refusal)"
        print(f"{rule:<32} {before:>8} {after:>7} {after - before:>+8} {ca:>8} {ca - cb:>+7}{flag}")
    if not moved:
        print("(no rule changed)")
    for key in ("files", "filesUnderPolicy", "statements", "findings"):
        print(f"{key:<12} {base.get(key, 0)} -> {current.get(key, 0)}")
    print(f"{'company':<12} {sum(base_company.values())} -> {sum(cur_company.values())}")
    return grown


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    default_root = Path(__file__).resolve().parents[2]
    ap.add_argument("--compose-root", default=None,
                    help="stack root holding data/sql (default: this repo checkout)")
    ap.add_argument("--sql-root", default=None)
    ap.add_argument("--git-root", default=None)
    ap.add_argument("--policy", default=str(HERE / "preflight-policy.json"))
    ap.add_argument("--workers", type=int, default=8, help="parallel git author lookups")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--rule", default=None, help="list file:line for one rule")
    ap.add_argument("--strict-all", action="store_true",
                    help="no provenance exemption, exactly like the gate's --strict-all")
    ap.add_argument("--policy-all", action="store_true")
    ap.add_argument("--out", default=None, help="write the tally as JSON")
    ap.add_argument("--diff", default=None, help="compare against a saved JSON tally")
    ap.add_argument("--measure-only", action="store_true",
                    help="exit 0 even when the company column is non-zero or the diff shows "
                         "an upstream->company migration; for baseline capture")
    ap.add_argument("--json", dest="json_out", default=None, help="also dump JSON to stdout")
    args = ap.parse_args(argv)

    root = Path(args.compose_root) if args.compose_root else default_root
    policy_path = Path(args.policy)
    if not policy_path.is_file():
        print(f"policy not found: {policy_path}", file=sys.stderr)
        return 2
    policy = json.loads(policy_path.read_text())
    if not (root / "data" / "sql").is_dir():
        print(f"{root} has no data/sql — pass --compose-root", file=sys.stderr)
        return 2

    engine = pf.Preflight(
        Args(root, policy_path, args.strict_all, args.policy_all,
             str(policy.get("liveDatabase", "acore_world"))),
        policy, pf.Report())
    files = build_files(engine, policy, args.workers)
    result = tally(engine, files)
    result.update({"head": git_head(root), "composeRoot": str(root),
                   "policy": str(policy_path),
                   "liveDatabase": engine.live_db,
                   "strictAll": bool(args.strict_all),
                   "companyFindings": company_total(result)})

    print_report(result, args.top, args.rule, engine)
    exit_code = 0
    if args.diff:
        print()
        grown = print_diff(result, Path(args.diff))
        if grown:
            print(f"\nCOMPANY findings appeared in: {', '.join(grown)} — a parser change "
                  f"must not move findings into the set the gate refuses boot for")
            if not args.measure_only:
                exit_code = 1
    if result["companyFindings"]:
        print(f"\nCOMPANY findings: {result['companyFindings']} — every one of these is a "
              f"boot refusal today")
        listed = result.get("companyFindingList") or []
        for v in listed[:COMPANY_DETAIL_LIMIT]:
            print(f"  {v['file']}:{v.get('line')}: {v['rule']}: {v['detail']}")
        if len(listed) > COMPANY_DETAIL_LIMIT:
            print(f"  ... and {len(listed) - COMPANY_DETAIL_LIMIT} more; "
                  f"--rule RULE lists one rule in full")
        if not args.measure_only:
            exit_code = 1
    unmatched = result.get("scanConsultationsWithoutRefusal")
    if unmatched:
        print(f"\nREGION SCAN: {unmatched} consultation(s) of the write-region name scan are "
              f"not matched by a `multi-table-write` finding. The scan is report text for a "
              f"refusal, not a second way to pass: either a multi-table write is now being "
              f"accepted while still consulting it, or a refusal stopped naming the tables "
              f"it rejected.")
        if not args.measure_only:
            exit_code = 1
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {args.out}")
    if args.json_out:
        print(json.dumps(result, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
