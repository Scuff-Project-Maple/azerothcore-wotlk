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

Exit status: 0 always (this is a measurement tool), 2 on a usage/operational error.
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

import preflight_worlddb as pf  # noqa: E402


class Args:
    """Only the attributes Preflight/MySQL read at construction time."""

    def __init__(self, root: Path, policy: Path, strict_all: bool, policy_all: bool):
        self.compose_root = str(root)
        self.sql_root = None
        self.git_root = None
        self.policy = str(policy)
        self.live_db = "acore_world"
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
    engine.check_policy(files)
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
    return {
        "files": len(files),
        "filesUnderPolicy": engine.policy_stats.get("files", 0),
        "statements": engine.policy_stats.get("statements", 0),
        "findings": sum(by_rule.values()),
        "byRule": dict(sorted(by_rule.items())),
        "byRuleOrigin": {r: dict(c) for r, c in sorted(by_rule_origin.items())},
        "byDir": dict(sorted(by_dir.items())),
        "perFile": dict(Counter(v["file"] for v in engine.violations).most_common(25)),
    }


def print_report(result: dict, top: int, rule_filter: str | None,
                 engine: pf.Preflight) -> None:
    print(f"files={result['files']} under-write-policy={result['filesUnderPolicy']} "
          f"statements={result['statements']} findings={result['findings']}")
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


def print_diff(current: dict, baseline_path: Path) -> None:
    base = json.loads(baseline_path.read_text())
    rules = sorted(set(current["byRule"]) | set(base.get("byRule", {})))
    print(f"diff vs {baseline_path} (baseline head {base.get('head', '?')})")
    print("rule                               before    after     delta")
    moved = 0
    for rule in rules:
        before = base.get("byRule", {}).get(rule, 0)
        after = current["byRule"].get(rule, 0)
        if before == after:
            continue
        moved += 1
        print(f"{rule:<32} {before:>8} {after:>7} {after - before:>+8}")
    if not moved:
        print("(no rule changed)")
    for key in ("files", "filesUnderPolicy", "statements", "findings"):
        print(f"{key:<12} {base.get(key, 0)} -> {current[key]}")


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

    engine = pf.Preflight(Args(root, policy_path, args.strict_all, args.policy_all),
                          policy, pf.Report())
    files = build_files(engine, policy, args.workers)
    result = tally(engine, files)
    result.update({"head": git_head(root), "composeRoot": str(root),
                   "policy": str(policy_path),
                   "strictAll": bool(args.strict_all)})

    print_report(result, args.top, args.rule, engine)
    if args.diff:
        print()
        print_diff(result, Path(args.diff))
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
        print(f"\nwrote {args.out}")
    if args.json_out:
        print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
