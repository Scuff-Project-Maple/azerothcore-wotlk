#!/usr/bin/env python3
"""Ask whether the pre-flight check a merge is about to rely on actually ran.

Everything else in this directory answers "is the corpus clean". This answers the
question underneath it, the one a green that never existed cannot answer: **did the
check run at all?**

PRO-441 is why it exists. Between 2026-11-06T22:41:32Z (``8f960d0ba``) and the day this
script was written, GitHub Actions started **nothing** for this repository — no
``World DB pre-flight``, no ``ubuntu-build``, nothing on any branch or PR. Five review
rounds of PRO-285/295/298/423/430/434/438 each cited "CI is green" for the sweep, and
each citation was true of the last run that existed and false of the merge it
accompanied. Nothing in the repository could notice. A workflow cannot report its own
non-execution: the reporter is the thing that stopped running. So the check for a
missing run has to live where merges happen, outside Actions, and be run by the hand
that merges. That is this script.

It is deliberately not a required status check. Both triggers on
``.github/workflows/worlddb-preflight.yml`` are ``paths:``-filtered, so a PR outside
``ops/preflight/**`` and ``data/sql/**`` starts no run, and a required check that never
starts sits at "Expected — waiting" forever — every ordinary PR would become
unmergeable (PRO-295 review N2). Required-ness would also be the wrong instrument here:
a required check reports "Expected" for a run that was never scheduled, which is
exactly the silence this script exists to break.

Three refusals, all exit 2, all distinct, because they have different owners:

``NOT RUNNING``
    No run of this workflow for the commit, and no run of **any** workflow in the
    repository since that commit was pushed. Actions itself is dead. This is the
    PRO-441 shape. It is not a filter bug and no edit to the YAML can fix it.

``NOT SCHEDULED``
    No run of this workflow, but other workflows ran for the same commit — so Actions
    is alive and this specific trigger did not fire. The suspects are the ``paths:``
    filter, a workflow file renamed or deleted at that commit, and a trigger type the
    event was not.

``NOT EXECUTED``
    A run exists but has not produced a verdict: ``queued``, ``waiting``,
    ``action_required``, ``pending``, or a still-running job. A check that has not
    finished is not a pass either. (A completed **red** run is also exit 2, but it is
    reported separately — that is the gate working, and the worst outcome in this file
    is a green nobody can see.)

And one answer that is not a refusal: ``OUT OF SCOPE`` when the commit's changed files
touch none of the ``paths:`` the workflow filters on. Absence is then correct, and
saying so is the reason the path mirror is pinned to the YAML by a test rather than
copied by hand (`test_preflight_worlddb.TestCiLivenessGuard`).

A verdict needs evidence, and evidence needs the API. With no token and no reachable
API the script says ``UNPROVEN`` and exits 2 rather than exiting 0: a check you cannot
look at is not a check that passed. ``--allow-unproven`` exists for offline use and
shouts when it is used.

``--probe-ref`` is how a suspicion becomes a named cause. Read-only history can show
that nothing ran; it cannot show *why*, and PRO-441's review standard is that the cause
is named by the response that shows it, not inferred. The probe POSTs a
``workflow_dispatch`` for this workflow at a ref you name and records the HTTP status
and message GitHub returns **verbatim**. It was that call, pointed at ``Playerbot``,
that answered PRO-441 with::

    HTTP 422
    {"message": "Actions has been disabled for this repository.",
     "documentation_url": ".../rest/actions/workflows#create-a-workflow-dispatch-event",
     "status": "422"}

    (https://api.github.com/repos/Scuff-Project-Maple/azerothcore-wotlk/actions/permissions)
    {"enabled": true, "allowed_actions": "all", "sha_pinning_required": false}

The second body is the part that makes this script worth having: the repository's
Actions **settings** said enabled, and Actions was disabled. A guard that trusts the
permissions flag reads a dead CI as a healthy one. So the probe result outranks the
flag, and when they disagree the refusal says so in those words. The probe has a side
effect — a real run of this workflow at that ref, ~6 minutes of a free Linux runner on
a public repo — so it is opt-in, and the script prints what it caused.

No MySQL, no Docker, no deploy host. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

WORKFLOW_FILE = ".github/workflows/worlddb-preflight.yml"
WORKFLOW_NAME = "World DB pre-flight"

# The `paths:` set the real workflow filters on, mirrored. This is the only place a
# second copy of that list is allowed to live, and TestCiLivenessGuard fails if the two
# ever differ — a mirror nobody pins becomes a guard that waves through an out-of-scope
# verdict for paths the workflow started filtering on yesterday.
GATE_PATHS = [
    "ops/preflight/**",
    "data/sql/**",
    WORKFLOW_FILE,
]

# Run states that mean "a run object exists and has told us nothing yet". Kept as data
# with the reason each is suspicious, so the refusal can name the state it saw.
NOT_YET = ("queued", "waiting", "pending", "requested", "action_required", "in_progress")

SHA40 = re.compile(r"^[0-9a-f]{40}$")


def _when(value: str | None):
    """Parse an ISO-8601 timestamp from either side of the comparison.

    Actions answers with `...Z`, `git show --format=%cI` answers with `...+00:00`, and a
    contributor in another zone answers with `...+08:00`. Comparing those as strings
    ranks `+08:00` timestamps against `Z` ones by the sign character, which decides the
    single most consequential branch in this file — NOT RUNNING versus NOT SCHEDULED —
    on the offset spelling. So both sides are parsed, and a timestamp that will not
    parse is returned as `None` and makes the comparison refuse rather than guess.
    """
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


# --------------------------------------------------------------------------- paths ---

def _glob_to_re(pattern: str) -> re.Pattern:
    """Translate a Actions `paths:` glob to a regex.

    Actions treats ``**`` as any-depth and ``*`` as single-segment. ``fnmatch`` alone
    gets that wrong — it lets ``*`` cross ``/`` — so ``ops/preflight/**`` would match
    nothing in a way that silently widens the out-of-scope verdict. ``data/sql/**``
    matching ``data/sql/x.sql`` (yes) and ``docs/x.md`` (no) is the whole contract.
    """
    out = []
    i = 0
    while i < len(pattern):
        ch = pattern[i]
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            if pattern.startswith("/", i):
                i += 1
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def in_gate_scope(changed: list[str], gate: list[str] | None = None) -> list[str]:
    """The changed files that the workflow's `paths:` filter would have matched."""
    pats = [(p, _glob_to_re(p)) for p in (gate if gate is not None else GATE_PATHS)]
    return [f for f in changed if any(rx.match(f) for _, rx in pats)]


# ------------------------------------------------------------------------ verdicts ---

def _fmt_run(run: dict) -> str:
    return (f"{run.get('name')}[{run.get('event')}/{run.get('status')}"
            f"/{run.get('conclusion')}] run={run.get('id')}")


def classify(head: str, changed: list[str], runs: list[dict],
             repo_recent: list[dict], pushed_at: str | None,
             api_error: str | None = None, probe: dict | None = None,
             permissions: dict | None = None,
             lookback_days: int = 30, recent_known: bool = True) -> dict:
    """Decide what the absence or presence of a run means for one commit.

    Pure: everything it reads was fetched by the caller, so every branch below is
    testable without a network and the recorded PRO-441 bodies can be replayed verbatim.
    """
    gate_hits = in_gate_scope(changed)
    ours = [r for r in runs if (r.get("name") or "").strip() == WORKFLOW_NAME]

    verdict = {
        "head": head,
        "inScope": bool(gate_hits),
        "gateHits": gate_hits,
        "runs": [_fmt_run(r) for r in ours],
        "otherRuns": [_fmt_run(r) for r in runs if r not in ours],
        "verdict": None,
        "why": None,
        "fixOwner": None,
    }

    # A refused dispatch is direct evidence and outranks a broken connection: GitHub has
    # already told us it will not start a run. An API error with no such answer is not
    # evidence of absence — it is an unanswered question, and an unanswered question that
    # exits 0 is how PRO-441 got believed for four days.
    if api_error and not (probe and probe.get("disabled")):
        verdict.update(
            verdict=None,
            why=f"cannot read the API: {api_error}",
            fixOwner="whoever merged: re-run with a token, or with --allow-unproven "
                     "and a shouted warning")
        verdict["verdict"] = "UNPROVEN"
        return verdict


    if probe and probe.get("status") not in (None, 204) and probe.get("disabled"):
        # The probe answered. It outranks everything else, including a permissions flag
        # that says otherwise, because it is the response GitHub gives when it refuses
        # to create a run rather than a setting that describes an intention.
        verdict.update(
            verdict="ACTIONS DISABLED",
            why=(f"GitHub refused to create a run for {WORKFLOW_NAME}: "
                 f"HTTP {probe.get('status')} {probe.get('message')!r}. The repository "
                 f"Actions setting says enabled={ (permissions or {}).get('enabled') }, "
                 f"so the setting and the behaviour disagree and the behaviour wins."),
            fixOwner="whoever controls the organisation's Actions settings — this is "
                     "above the repository, and no edit to the YAML can reach it")
        return verdict

    if ours:
        r = ours[0]
        status = (r.get("status") or "").lower()
        conclusion = (r.get("conclusion") or "").lower()
        if status in NOT_YET or (status == "completed" and conclusion == "cancelled"):
            verdict.update(
                verdict="NOT EXECUTED",
                why=(f"a run exists ({_fmt_run(r)}) but has not produced a verdict; "
                     f"`{status or conclusion or '?'}` is not a pass"),
                fixOwner="whoever merged: wait for the run, or re-run it — do not "
                         "merge on a check that has not finished")
        elif status == "completed" and conclusion == "success":
            verdict.update(verdict="PRESENT", why="the gate ran and is green",
                           fixOwner=None)
        elif status == "completed" and conclusion:
            verdict.update(
                verdict="RED",
                why=(f"the gate ran and is red ({_fmt_run(r)}). This is the loud "
                     f"outcome working as designed"),
                fixOwner="whoever changed the engine or the corpus: the gate is "
                         "telling you something")
        else:
            verdict.update(verdict="NOT EXECUTED",
                           why=f"unreadable run state {_fmt_run(r)}",
                           fixOwner="whoever merged: read the run before trusting it")
        return verdict

    # A run of this workflow is owed only if one was possible at all, and whether the
    # repository has been starting anything is a question the API has to have answered.
    # `[]` from a query that was never made — no token, a 401, a dropped connection — and
    # `[]` from a repository that genuinely has no runs are the same value and opposite
    # claims, and the second is the claim that lets a dead Actions read as a healthy one.
    # An in-scope commit over an unread history therefore has no verdict rather than the
    # convenient one.
    if not recent_known and gate_hits:
        verdict.update(
            verdict="UNPROVEN",
            why=(f"in scope ({', '.join(gate_hits[:3])}) with no run found, but the "
                 f"repository's run history was never read, so 'nothing ran' would be a "
                 f"claim with no evidence behind it"),
            fixOwner="whoever merged: re-run with a token, or with --allow-unproven and "
                     "a shouted warning")
        return verdict

    # No run of this workflow. Is one owed, and is the repository quiet?
    #
    # `quiet` is one decision made in one place: the newest run of any workflow in the
    # repository is missing, or predates this commit's push. Both shapes mean Actions
    # started nothing for a commit that should have produced one, so both are NOT
    # RUNNING — a repository with zero runs in the window and a repository frozen four
    # days ago are the same failure wearing different clothes. NOT SCHEDULED is what
    # remains, and only that branch is allowed to claim Actions is alive, because only
    # that branch has a run at or after this commit to point at.
    newest_run = repo_recent[0] if repo_recent else None
    newest_at = str((newest_run or {}).get("created_at") or "")
    newest_dt, pushed_dt = _when(newest_at), _when(pushed_at)
    if newest_run is None:
        quiet, quiet_said = True, (f"no workflow has run in this repository in the "
                                   f"last {lookback_days} days")
    elif newest_dt is None or pushed_dt is None:
        # An unparseable timestamp on either side is not "not quiet". Deciding the
        # severe-versus-mundane branch by falling through to the mundane one is how a
        # dead Actions quietly reads as a filter bug, so this refuses instead.
        quiet, quiet_said = None, (f"cannot compare the newest run ({newest_at!r}) "
                                   f"with this commit's date ({pushed_at!r})")
    elif newest_dt < pushed_dt:
        quiet, quiet_said = True, (f"the newest run of any workflow in this repository "
                                   f"is {newest_at}")
    else:
        quiet, quiet_said = False, (f"the newest run of any workflow in this repository "
                                    f"is {newest_at}, at or after this commit's push")

    if quiet is None and gate_hits:
        verdict.update(verdict="UNPROVEN",
                       why=(f"in scope ({', '.join(gate_hits[:3])}) with no run, and "
                            f"{quiet_said} — cannot tell a dead Actions from a dead "
                            f"trigger, and the two have different owners"),
                       fixOwner="whoever merged: pass --pushed-after with the ISO "
                                "instant this commit reached the remote")
        return verdict

    if not gate_hits:
        # ...unless the whole repository is quiet, in which case the out-of-scope answer
        # would be a lie by omission: the next in-scope merge is unguarded too, and the
        # person merging is the only one who can see it. `quiet` only counts when the
        # history was actually read — an unread history is not a quiet one.
        if quiet and recent_known:
            verdict.update(
                verdict="NOT RUNNING",
                why=(f"this commit is out of scope so no run was owed, but {quiet_said} "
                     f"— older than this commit ({pushed_at or 'date unknown'}). "
                     f"Actions itself is down, so the next in-scope merge will be "
                     f"silently unguarded too"),
                fixOwner="whoever controls the organisation's Actions settings")
        else:
            caveat = ("" if recent_known else
                      " NOTE: the repository's run history could not be read, so this "
                      "verdict cannot say whether Actions is running at all — the "
                      "in-scope merge after this one may be unguarded")
            verdict.update(verdict="OUT OF SCOPE",
                           why="touches none of the workflow's `paths:`, so no run is "
                               "owed and this merge was never gated by this check"
                               + caveat,
                           fixOwner=None)
        return verdict

    if quiet:
        verdict.update(
            verdict="NOT RUNNING",
            why=(f"in scope ({', '.join(gate_hits[:3])}) but no run, and {quiet_said} "
                 f"— before this commit was pushed at {pushed_at or '??? '}. Actions "
                 f"has started nothing since. Nothing in the repository can detect this "
                 f"from inside itself: the thing that would report it is the thing that "
                 f"stopped running"),
            fixOwner="whoever controls the organisation's Actions settings — the "
                     "repository-level Actions setting still reads enabled, so look "
                     "above the repository")
    else:
        others = verdict["otherRuns"]
        said = (f"and {len(others)} other workflow(s) ran for this commit "
                f"({', '.join(others[:3])})" if others else
                f"while {quiet_said}, at or after this commit")
        verdict.update(
            verdict="NOT SCHEDULED",
            why=(f"in scope ({', '.join(gate_hits[:3])}) but no `{WORKFLOW_NAME}` run, "
                 f"{said}. Actions is executing and this trigger did not fire: check "
                 f"the `paths:` filter, whether {WORKFLOW_FILE} exists at this commit, "
                 f"and whether the event is one of the three triggers"),
            fixOwner="the owner of ops/preflight and the CI workflow")
    return verdict


EXIT_FOR = {
    "PRESENT": 0,
    "OUT OF SCOPE": 0,
    "RED": 2,
    "NOT EXECUTED": 2,
    "NOT SCHEDULED": 2,
    "NOT RUNNING": 2,
    "ACTIONS DISABLED": 2,
    "UNPROVEN": 2,
}


# ---------------------------------------------------------------------- api plumbing ---

class Github:
    """The four read endpoints and one write endpoint this guard needs.

    Isolated so `classify()` stays pure and the tests can replay recorded bodies.
    """

    def __init__(self, token: str | None, repo: str, api: str = "https://api.github.com"):
        self.token = token
        self.repo = repo
        self.api = api.rstrip("/")
        self.error: str | None = None
        # False until a newest-run query actually returned 200. `[]` from a query that
        # was never made (no token) and `[]` from a repository that really has no runs
        # are the same value and completely different claims, so the guard carries the
        # distinction instead of inferring it from emptiness.
        self.newest_known = False

    def _req(self, method: str, path: str, params: dict | None = None,
             body: dict | None = None) -> tuple[int, object]:
        url = f"{self.api}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/vnd.github+json",
                   "User-Agent": "acore-worlddb-preflight-liveness"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if data:
            headers["Content-Type"] = "application/json"
        try:
            with urllib.request.urlopen(urllib.request.Request(
                    url, data=data, headers=headers, method=method)) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                parsed = {"message": raw[:200]}
            return exc.code, parsed
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            return 0, {}

    def runs_for(self, sha: str) -> list[dict]:
        code, obj = self._req("GET", f"/repos/{self.repo}/actions/runs",
                              {"head_sha": sha, "per_page": 100})
        if code != 200:
            self.error = self.error or f"GET runs?head_sha={sha[:10]} -> HTTP {code} " \
                                       f"{str(obj.get('message'))[:120]}"
            return []
        return obj.get("workflow_runs", [])

    def newest_run(self, since: str | None = None) -> list[dict]:
        # No `status` filter: `all` is not one of its values, and asking for it returns
        # 422 — which used to surface as "newest run = NONE" and turn a repository whose
        # Actions had been dead for four days into a NOT SCHEDULED verdict blaming the
        # `paths:` filter. The absence of a filter is what lists everything.
        params = {"per_page": 1}
        if since:
            params["created"] = f">={since}"
        code, obj = self._req("GET", f"/repos/{self.repo}/actions/runs", params)
        if code != 200:
            self.error = self.error or f"GET runs (newest) -> HTTP {code} " \
                                       f"{str(obj.get('message'))[:120]}"
            return []
        self.newest_known = True
        return obj.get("workflow_runs", [])

    def permissions(self) -> dict:
        code, obj = self._req("GET", f"/repos/{self.repo}/actions/permissions")
        if code != 200:
            return {}
        return obj

    def workflow_id(self, path: str) -> int | None:
        code, obj = self._req("GET", f"/repos/{self.repo}/actions/workflows",
                              {"per_page": 100})
        if code != 200:
            return None
        for w in obj.get("workflows", []):
            if w.get("path") == path:
                return w.get("id")
        return None

    def probe_dispatch(self, wf_id: int, ref: str) -> dict:
        """Ask GitHub, in the only language that answers, to start a run.

        A 204 means it accepted — which also means a real run is now queued at `ref`.
        Anything else is the cause, in GitHub's own words, recorded verbatim.
        """
        code, obj = self._req("POST",
                              f"/repos/{self.repo}/actions/workflows/{wf_id}/dispatches",
                              body={"ref": ref})
        msg = obj.get("message") if isinstance(obj, dict) else None
        return {"status": code, "message": msg, "ref": ref,
                "disabled": code == 422 and "disabled" in str(msg).lower(),
                "started": code == 204}


# -------------------------------------------------------------------------- git glue ---

def git_changed(repo_dir: Path, base: str, head: str) -> list[str]:
    out = subprocess.run(["git", "-C", str(repo_dir), "diff", "--name-only",
                          f"{base}...{head}"],
                         capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        raise SystemExit(f"liveness: git diff {base}...{head} failed: {out.stderr.strip()}")
    return [l for l in out.stdout.splitlines() if l.strip()]


def git_field(repo_dir: Path, head: str, fmt: str) -> str:
    out = subprocess.run(["git", "-C", str(repo_dir), "show", "-s", f"--format={fmt}", head],
                         capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    if out.returncode != 0:
        return ""
    return out.stdout.strip()


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ci_liveness_guard.py",
        description="Fail a merge whose pre-flight check never ran. Read-only unless "
                    "--probe-ref is given.")
    ap.add_argument("--sha", action="append", default=[], dest="shas",
                    help="commit to check for a run; repeatable. Defaults to the "
                         "current HEAD of this checkout.")
    ap.add_argument("--base",
                    help="ref the changed-file set is diffed against. Default is each "
                         "commit's own first parent, which for a merge already landed "
                         "on Playerbot is exactly the change set the merge brought in.")
    ap.add_argument("--pushed-after",
                    help="ISO-8601 instant this commit reached the remote. Only needed "
                         "when the committer date cannot answer the quiet question — "
                         "a rebase or a locally created merge carries a creation date, "
                         "not a push date.")
    ap.add_argument("--repo", default="Scuff-Project-Maple/azerothcore-wotlk")
    ap.add_argument("--api", default="https://api.github.com")
    ap.add_argument("--token-env", default="GH_TOKEN",
                    help="environment variable holding a token with actions:read "
                         "(default: %(default)s; GITHUB_TOKEN is used if it is unset)")
    ap.add_argument("--repo-dir", default=str(HERE.parents[1]),
                    help="checkout to read changed files and commit dates from")
    ap.add_argument("--lookback-days", type=int, default=30,
                    help="window for the newest-run query that decides NOT RUNNING "
                         "vs NOT SCHEDULED (default: %(default)s)")
    ap.add_argument("--probe-ref",
                    help="POST a workflow_dispatch for this workflow at REF and record "
                         "GitHub's answer verbatim. This is how a suspicion becomes a "
                         "named cause. Side effect: a real run starts if Actions is "
                         "alive.")
    ap.add_argument("--allow-unproven", action="store_true",
                    help="exit 0 when the API could not be read. Shouts when used.")
    ap.add_argument("--json", dest="json_out",
                    help="also write the per-commit verdicts here")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    repo_dir = Path(args.repo_dir).resolve()

    shas = list(args.shas)
    if not shas:
        head = git_field(repo_dir, "HEAD", "%H")
        if not SHA40.match(head or ""):
            print("liveness: cannot resolve HEAD; pass --sha explicitly", file=sys.stderr)
            return 1
        shas = [head]

    token = os.environ.get(args.token_env) or os.environ.get("GITHUB_TOKEN")
    gh = Github(token, args.repo, args.api)

    permissions = gh.permissions() if token else {}
    wf_id = gh.workflow_id(WORKFLOW_FILE) if token else None

    print(f"liveness: repo={args.repo} workflow={WORKFLOW_FILE!r} "
          f"token={'yes' if token else 'NO'} "
          f"actions.enabled={permissions.get('enabled', '?')} "
          f"allowed_actions={permissions.get('allowed_actions', '?')}")

    probe: dict | None = None
    if args.probe_ref:
        if not token:
            print("liveness: --probe-ref needs a token", file=sys.stderr)
            return 1
        if wf_id is None:
            print(f"liveness: --probe-ref refused: {WORKFLOW_FILE} is not registered as "
                  f"an active workflow in this repository. A workflow that is not "
                  f"registered cannot run, which is its own answer — see NOT SCHEDULED "
                  f"below.", file=sys.stderr)
        else:
            probe = gh.probe_dispatch(wf_id, args.probe_ref)
            if probe["started"]:
                print(f"liveness: PROBE started a real run of {WORKFLOW_NAME} at "
                      f"{args.probe_ref} (HTTP 204). That is the side effect this flag "
                      f"buys; the answer it cost you ~6 minutes of a runner.")
            else:
                print(f"liveness: PROBE refused: HTTP {probe['status']} "
                      f"{probe['message']!r} — recorded verbatim as the cause.")

    since = (datetime.now(timezone.utc)
             - timedelta(days=args.lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    newest = gh.newest_run(since) if token else []
    pushed_hint = str(newest[0].get("created_at") or "") if newest else ""
    print(f"liveness: newest run in repo of any workflow = {pushed_hint or 'NONE'} "
          f"(lookback {args.lookback_days}d, i.e. since {since})")

    results = []
    for sha in shas:
        if not SHA40.match(sha):
            full = git_field(repo_dir, sha, "%H")
            sha = full if SHA40.match(full or "") else sha
        changed = git_changed(repo_dir, args.base or f"{sha}^", sha)
        pushed_at = args.pushed_after or git_field(repo_dir, sha, "%cI")
        runs = gh.runs_for(sha) if token else []
        v = classify(sha, changed, runs, newest, pushed_at,
                     api_error=gh.error, probe=probe, permissions=permissions,
                     lookback_days=args.lookback_days,
                     recent_known=gh.newest_known)
        results.append(v)
        code = EXIT_FOR.get(v["verdict"], 2)
        mark = "ok  " if code == 0 else "FAIL"
        print(f"liveness: {mark} {sha[:12]} {v['verdict'] or '?'}: {v['why']}")
        if v["verdict"] not in ("PRESENT", "OUT OF SCOPE") and v["fixOwner"]:
            print(f"        next: {v['fixOwner']}")
        if v["inScope"] and v["verdict"] != "PRESENT":
            print(f"        in-scope files: {', '.join(v['gateHits'][:6])}"
                  f"{' ...' if len(v['gateHits']) > 6 else ''}")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            {"repo": args.repo, "probe": probe, "permissions": permissions,
             "newestRun": newest[:1], "commits": results}, indent=2, default=str))

    failing = [r for r in results if EXIT_FOR.get(r["verdict"], 2) != 0]
    unproven = [r for r in results if r["verdict"] == "UNPROVEN"]
    demoted = 0

    if unproven and args.allow_unproven:
        print("\n" + "=" * 72, file=sys.stderr)
        print("liveness: WARNING --allow-unproven used. The API was not read, so NO "
              "verdict about the pre-flight check exists for "
              f"{len(unproven)} commit(s). This merge is NOT shown to be gated."
              "\n" + "=" * 72, file=sys.stderr)
        failing = [r for r in failing if r["verdict"] != "UNPROVEN"]
        demoted = len(unproven)

    if failing:
        kinds = sorted({r["verdict"] for r in failing})
        print(f"\n{len(failing)} commit(s) are not gated: {', '.join(kinds)}.",
              file=sys.stderr)
        if "NOT RUNNING" in kinds or "ACTIONS DISABLED" in kinds:
            print("Actions is not executing for this repository. The check that the "
                  "last five review rounds cited does not exist for these commits, and "
                  "nothing inside the repository can tell you that — which is why this "
                  "script is run by the hand that merges. Do not merge on the strength "
                  "of a check you did not observe running.", file=sys.stderr)
        return 2
    if demoted:
        print(f"liveness: {demoted} commit(s) exited 0 on --allow-unproven alone. That "
              f"is a decision to merge without reading the check, not a pass.")
    else:
        print("liveness: every commit checked has a pre-flight verdict that can be read.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
