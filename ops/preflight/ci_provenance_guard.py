#!/usr/bin/env python3
"""Ask whether a corpus sweep tally is trustworthy enough to gate a merge on.

`corpus_sweep.py` gates on the `company` provenance column: zero company findings
exits 0. That column is only evidence if git could say who wrote every file it scanned,
and `git_author_origin()` asks `git log -1 -- <path>` — so the answer depends on the
checkout as much as on the corpus. A shallow clone has a **grafted root**: `git log`
stops at that one commit for *every* path, so each file is attributed to the tip author
and nothing comes back `unknown` to warn anyone. Measured on a `--depth 1` clone of this
repo, all 3 060 scanned files were classified `company` — with a company-authored tip
that is 444 726 company findings and a red check with the whole upstream corpus in the
dock; with the upstream-authored tip `Playerbot` normally has, the same checkout puts all
444 726 in `upstream` and the `company` column this check gates on reads 0. A green
produced by nothing is worse than a red, so the workflow runs the sweep, then runs this
guard over the tally the sweep wrote with `--out`. This script re-runs nothing and needs no MySQL, no Docker and no deploy
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

    provenance_files = tally.get("provenanceFiles")
    if provenance_files is not None and files and \
            int(provenance_files.get("upstream", 0) or 0) == 0:
        problems.append(
            f"no scanned file was attributed to `upstream` even though the included "
            f"directories are mostly upstream archive and updates: every file was "
            f"given the same origin, which is what a checkout with no real history "
            f"looks like. The `company` column in that tally is not a policy verdict.")

    unknown_findings = _unknown_findings(tally)
    if unknown_findings:
        detail = ", ".join(f"{r}={n}" for r, n in sorted(unknown_findings.items()))
        problems.append(
            f"{sum(unknown_findings.values())} finding(s) landed in the `unknown` "
            f"provenance column ({detail}); they were neither attributed to the company "
            f"nor exempted as upstream, so they are invisible to the exit status.")

    if "shallow" not in tally or "commits" not in tally:
        problems.append(
            "the tally says nothing about the git history provenance was read out of "
            "(`shallow`/`commits` are absent), so the guard cannot rule out a grafted "
            "checkout. It is never skipped silently: re-run the sweep with the "
            "corpus_sweep.py that ships with this guard.")
    else:
        if tally.get("shallow") is True:
            problems.append(
                "the sweep ran in a shallow repository (`git rev-parse "
                "--is-shallow-repository` = true). A shallow checkout has a grafted root, "
                "so `git log -1 -- <path>` returns that one commit for every file: all "
                "findings land in whichever column the tip author belongs to, and the "
                "`unknown` column stays empty so nothing else here complains. In CI: "
                "`fetch-depth: 0` on actions/checkout.")
        commits = tally.get("commits")
        if isinstance(commits, int) and commits <= 1:
            problems.append(
                f"the branch has commits={commits}: there is no history for `git log -- "
                f"<path>` to answer from, so every provenance in this tally is the tip "
                f"commit's author wearing every file's hat. In CI: `fetch-depth: 0`.")

    if "authors" not in tally:
        problems.append(
            "the tally has no `authors` count, so the guard cannot ask how many distinct "
            "commit authors the attribution drew on. It is never skipped silently: "
            "re-run the sweep with the corpus_sweep.py that ships with this guard.")
    elif isinstance(tally.get("authors"), int) and files and tally["authors"] <= 1:
        problems.append(
            f"authors={tally['authors']}: {files} scanned files, all attributed to a "
            f"single commit author. The archive spans years of upstream contributors, so "
            f"one address means the checkout has no usable history rather than that one "
            f"person wrote the corpus.")

    company = int(tally.get("companyFindings", 0) or 0)
    listed = tally.get("companyFindingList")
    if listed is not None:
        # The sweep caps the list it writes (COMPANY_JSON_LIMIT) so a run that attributes
        # the whole corpus to the company cannot write a 120 MB tally; the flag is what
        # keeps that cap from becoming a way to hide rows.
        if tally.get("companyFindingListTruncated"):
            if company <= len(listed):
                problems.append(
                    f"companyFindingListTruncated is set but companyFindings={company} "
                    f"fits in the {len(listed)} rows listed: the tally says it dropped "
                    f"rows it did not have, so neither number can be trusted.")
        elif len(listed) != company:
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

    pf = tally.get("provenanceFiles")
    if pf is None:
        files_by_provenance = "provenance[?]"
    else:
        files_by_provenance = " ".join(
            f"{k}={pf.get(k, 0)}" for k in ("company", "upstream", "unknown"))
    listed = tally.get("companyFindingList")
    rows = "?" if listed is None else f"{len(listed)}{'+' if tally.get('companyFindingListTruncated') else ''}"
    print(f"guard: head={str(tally.get('head') or '?')[:12]} files={tally.get('files')} "
          f"under-policy={tally.get('filesUnderPolicy')} statements={tally.get('statements')} "
          f"findings={tally.get('findings')} "
          f"provenance[{files_by_provenance}] companyFindings={tally.get('companyFindings')} "
          f"rowsListed={rows} "
          f"history[commits={tally.get('commits', '?')} shallow={tally.get('shallow', '?')} "
          f"authors={tally.get('authors', '?')}]")

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
