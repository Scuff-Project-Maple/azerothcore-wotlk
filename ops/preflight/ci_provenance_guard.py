#!/usr/bin/env python3
"""Ask whether a corpus sweep tally is trustworthy enough to gate a merge on.

`corpus_sweep.py` gates on the `company` provenance column: zero company findings
exits 0. That column is only evidence if git could say who wrote every file it scanned.
`git_author_origin()` asks `git log -1 -- <path>`, and on a shallow checkout — or for a
file git does not know — it returns nothing, the file is classified `unknown`, and the
company total goes to zero all by itself. The policy verdict the CI check is supposed
to report would then be produced by nothing at all, and the check would be **green**
(PRO-285).

So the workflow runs the sweep, then runs this guard over the tally the sweep wrote
with `--out`. This script re-runs nothing and needs no MySQL, no Docker and no deploy
host: it reads one JSON file and answers one question — did the sweep measure the
corpus it claims to have measured?

Exit status: 0 the tally is trustworthy, 2 it is not (an operational claim, the same
meaning 2 has in `corpus_sweep.py`), 1 on a usage error. The guard deliberately does
not re-litigate the policy verdict: company findings are `corpus_sweep.py`'s to fail
on, and it does. This script only fails a run that would otherwise pass for the wrong
reason.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SHA = re.compile(r"^[0-9a-f]{40}$")


def _unknown_findings(tally: dict) -> dict[str, int]:
    """Per-rule counts of findings whose provenance could not be attributed."""
    out: dict[str, int] = {}
    for rule, origins in (tally.get("byRuleOrigin") or {}).items():
        n = int(origins.get("unknown", 0) or 0)
        if n:
            out[rule] = n
    return out


def check(tally: dict) -> list[str]:
    """Return the reasons this tally must not be read as a policy pass."""
    problems: list[str] = []

    files = int(tally.get("files", 0) or 0)
    if files <= 0:
        problems.append(
            f"files={files}: the sweep scanned no .sql at all, so the zero company "
            f"findings it reports is the absence of a measurement, not a pass. In CI "
            f"that means data/sql is not in the checkout; locally it means --compose-root "
            f"points somewhere without it.")

    under = int(tally.get("filesUnderPolicy", 0) or 0)
    if files and under < files:
        problems.append(
            f"filesUnderPolicy={under} of files={files}: {files - under} scanned "
            f"file(s) were exempt from the write policy, so this tally does not speak "
            f"for the whole corpus. Run the sweep with --strict-all.")

    head = str(tally.get("head") or "")
    if not SHA.match(head):
        problems.append(
            f"head={head!r} is not a commit sha: git could not resolve HEAD, so the "
            f"sweep did not run on a usable git checkout and no provenance in this "
            f"tally was read from history.")

    if "provenanceFiles" not in tally:
        problems.append(
            "the tally has no `provenanceFiles`, so the guard cannot ask how many "
            "files git attributed. It is never skipped silently: re-run the sweep with "
            "the corpus_sweep.py that ships with this guard.")
    else:
        unknown_files = int((tally.get("provenanceFiles") or {}).get("unknown", 0) or 0)
        if unknown_files:
            problems.append(
                f"{unknown_files} scanned file(s) have provenance `unknown`: git could "
                f"not name a last-commit author for them, which is what a shallow "
                f"checkout (or an untracked file) looks like. Those files were policy-"
                f"checked but their findings are not counted in the `company` column "
                f"the check gates on. In CI: fetch with `fetch-depth: 0`.")

    unknown_findings = _unknown_findings(tally)
    if unknown_findings:
        detail = ", ".join(f"{r}={n}" for r, n in sorted(unknown_findings.items()))
        problems.append(
            f"{sum(unknown_findings.values())} finding(s) landed in the `unknown` "
            f"provenance column ({detail}); they were neither attributed to the company "
            f"nor exempted as upstream, so they are invisible to the exit status.")

    company = int(tally.get("companyFindings", 0) or 0)
    listed = tally.get("companyFindingList")
    if listed is not None and len(listed) != company:
        problems.append(
            f"companyFindings={company} but companyFindingList has {len(listed)} "
            f"entries: the tally contradicts itself, so neither number can be trusted.")

    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("tally", help="the JSON written by corpus_sweep.py --out")
    args = ap.parse_args(argv)

    path = Path(args.tally)
    try:
        tally = json.loads(path.read_text())
    except FileNotFoundError:
        print(f"GUARD FAIL {path}: no tally there — did the sweep step run and reach "
              f"--out?", file=sys.stderr)
        return 1
    except (ValueError, OSError) as exc:
        print(f"GUARD FAIL {path}: unreadable tally ({exc})", file=sys.stderr)
        return 1

    pf = tally.get("provenanceFiles") or {}
    print(f"guard: head={str(tally.get('head') or '?')[:12]} files={tally.get('files')} "
          f"under-policy={tally.get('filesUnderPolicy')} statements={tally.get('statements')} "
          f"findings={tally.get('findings')} "
          f"provenance[company={pf.get('company', '?')} upstream={pf.get('upstream', '?')} "
          f"unknown={pf.get('unknown', '?')}] companyFindings={tally.get('companyFindings')}")

    problems = check(tally)
    for p in problems:
        print(f"GUARD FAIL: {p}", file=sys.stderr)
    if problems:
        print(f"\nThe corpus sweep tally is not trustworthy: {len(problems)} reason(s) "
              f"above. A green `company` column here is the absence of a measurement, "
              f"not a policy pass.", file=sys.stderr)
        return 2
    print("guard: the tally is attributable; the sweep's company column may be read as "
          "a policy verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
