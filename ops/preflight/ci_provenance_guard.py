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
reason, which means three questions, not one: was the corpus attributable (a shallow
checkout, a one-commit history, one author across the archive, `unknown` rows); was it
measured at all (no files, no statements, no findings, no rule that fired, no company
file in a corpus whose gated column is supposed to be about company files); and do its
own counts agree (a `findings` total the `byRule` counts contradict, a rule in `byRule`
with no `byRuleOrigin` split — and it is `byRuleOrigin` the gated column is counted
from). A tally that fails any question reports zero, and zero from a check that never
ran is not a pass.
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

    # A tally can also measure its corpus and report nothing about it, which looks
    # identical to a clean tree from the outside. Every number the gate reads is
    # downstream of these three, so a dead engine — an emptied `forbiddenStatements`, a
    # rule registry that failed to load, a parser that stopped matching a whole
    # statement class — has to be refused here rather than mistaken for a pass
    # (PRO-295 B2). This tree's upstream archive has measured a large, stable finding
    # total at every recording ever checked in — `ops/preflight/README.md` carries the
    # figure with the head it was measured at — so `findings=0` is not a clean corpus,
    # it is a non-measurement. The number is deliberately not repeated in the refusal
    # below: a frozen measurement inside an error message becomes a false statement
    # printed by a red run the first time the corpus legitimately changes (PRO-298 N7).
    statements = int(tally.get("statements", 0) or 0)
    if files and statements <= 0:
        problems.append(
            f"statements={statements} over files={files}: the parser read no statement out "
            f"of anything it was handed, so `findings`, every per-rule count and the "
            f"`company` column the check gates on are all counts over nothing. That is the "
            f"same defect as files=0 one level down; it is refused for the same reason.")

    findings = int(tally.get("findings", 0) or 0)
    if files and findings <= 0:
        problems.append(
            f"findings={findings} over files={files} / statements={statements}: the "
            f"upstream archive in this tree has produced a large finding total at every "
            f"recorded run — ops/preflight/README.md records the measured baseline with "
            f"the head it was taken at — so a total of zero is not a clean corpus but an "
            f"engine that stopped matching rules: an emptied `forbiddenStatements`, a rule "
            f"registry that failed to load, a parser that no longer recognises a statement "
            f"class. A red sweep whose tally cannot be attributed and a green sweep that "
            f"measured nothing must not be the same answer.")

    if files and not (tally.get("byRule") or {}):
        problems.append(
            f"byRule is empty while files={files}: no rule is recorded as having fired, "
            f"which is the per-rule form of findings=0 and fails for the same reason.")

    # The three counts above each stand alone; this one asks them to agree with each
    # other. `corpus_sweep.py` takes `findings` as `sum(by_rule.values())` and builds
    # every per-rule origin split from the same violation list, so a tally the sweep
    # wrote cannot disagree with itself. When it does, this guard is reading a document
    # rather than a measurement — and one of those documents is the gated column itself:
    # `company` findings are counted out of `byRuleOrigin`, so a rule that is present in
    # `byRule` and missing from `byRuleOrigin` contributes nothing to the exit status
    # while the totals say it fired (PRO-298 review N8).
    rule_counts = tally.get("byRule") or {}
    if files and findings > 0 and rule_counts:
        total_of_rules = sum(int(n or 0) for n in rule_counts.values())
        if total_of_rules != findings:
            problems.append(
                f"findings={findings} but the {len(rule_counts)} rule(s) in byRule total "
                f"{total_of_rules}: the tally does not add up against itself, so neither "
                f"the headline count nor the per-rule counts can be read. "
                f"`corpus_sweep.py` derives one from the other, so a sweep that wrote "
                f"this tally did not count what it says it counted.")
        breakdowns = tally.get("byRuleOrigin") or {}
        for rule, count in sorted(rule_counts.items()):
            breakdown = breakdowns.get(rule)
            if breakdown is None:
                problems.append(
                    f"byRule records {count} finding(s) for `{rule}` but byRuleOrigin has "
                    f"no breakdown for it: the `company` column this check gates on is "
                    f"counted out of byRuleOrigin, so those findings are invisible to the "
                    f"exit status while the totals say they fired.")
                continue
            parts = sum(int(n or 0) for n in breakdown.values())
            if parts != int(count or 0):
                problems.append(
                    f"byRule has {count} for `{rule}` but byRuleOrigin sums to {parts}: "
                    f"the gated `company` column and the reported total are counted from "
                    f"different sets of rows, so one of them is wrong about the corpus.")

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

    # The same question about the column that is actually gated (PRO-295 B1). The
    # symmetric clause above only asks about `upstream`, so a corpus that lost every
    # company file — `data/sql/updates/pending_db_world` deleted from `includedDirs`,
    # or simply not present in the checkout — passed: provenanceFiles.company=0 is a
    # `company` column with nothing behind it, and zero is what it reports whether the
    # rule is satisfied or the rule was never run. Note the shape the sweep really
    # writes: `provenanceFiles` is a `dict(Counter(...))`, so a class with no members is
    # an ABSENT key, not a zero — `{"upstream": 3053}` is what dropping
    # `pending_db_world` from `includedDirs` puts in the tally file. Both readings have
    # to refuse, which is what `.get(..., 0) or 0` is for, and both are pinned by a test
    # (PRO-298 review N5).
    if provenance_files is not None and files and \
            int(provenance_files.get("company", 0) or 0) == 0:
        problems.append(
            f"no scanned file was attributed to `company` even though {files} file(s) "
            f"were scanned: `data/sql/updates/pending_db_world` and the other company "
            f"directories contribute nothing, so the `company` column this check gates on "
            f"is empty rather than clean. Either the checkout is missing them or "
            f"`includedDirs` lost them — in both cases a zero there is not a policy "
            f"verdict. ops/preflight/README.md records what a healthy tally for this "
            f"tree looks like; compare it before reading a green.")

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
    if listed is None:
        # The one tally key the gate reads that used to be optional: a sweep that
        # recorded no row list at all could not be checked against its own count, which
        # made an absent list the quietest way to lose the cross-check (PRO-295 B2). It
        # is refused on absence like provenanceFiles, shallow/commits and authors.
        problems.append(
            "the tally has no `companyFindingList`, so the guard cannot check that the "
            "rows back the `companyFindings` total the check gates on. It is never "
            "skipped silently: re-run the sweep with the corpus_sweep.py that ships with "
            "this guard.")
    else:
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
