#!/usr/bin/env bash
#
# PRO-320: refuse to publish an integration whose target is a stale or
# diverged LOCAL branch.
#
# The authoritative integration line is `origin/<branch>` on the remote, never
# a local ref of the same name. A local branch is a snapshot: it drifts, and a
# `git push origin <branch>` that looks like a fast-forward can either drop
# commits the line has moved on without or publish local-only commits nobody
# reviewed. That is exactly what happened in PRO-319, where the local
# `Playerbot` sat 10 ahead / 94 behind `origin/Playerbot` while looking like
# "the Playerbot branch".
#
# The pattern this guard enforces (see docs/content-playbook.md §5.4):
#
#   git fetch origin
#   git worktree add <scratch> --detach origin/<branch>
#   # merge the feature branch in that detached worktree, then:
#   scripts/merge-guard.sh --publish <merge-sha> <branch>
#   git push origin <merge-sha>:refs/heads/<branch>
#
# Read-only and idempotent: the guard only reads refs. It never fetches, never
# writes, never creates a worktree. Because it cannot fetch, a stale
# `<remote>/<branch>` remote-tracking ref would make the answer meaningless, so
# it always prints the exact refs and SHAs it compared — fetch first.
#
# usage: scripts/merge-guard.sh [options] TARGET
#
#   TARGET            the integration line: `Playerbot` or `origin/Playerbot`
#   --repo PATH       repo or worktree whose refs to inspect (default: cwd)
#   --remote NAME     remote that owns the authoritative line (default: origin)
#   --publish SHA     also require SHA to be a fast-forward over <remote>/<branch>
#   --allow-ahead     downgrade "local branch has local-only commits" to a warning
#   -h | --help       this help
#
# exit: 0 = publishing as prescribed is safe; 1 = refused; 2 = usage / not a repo

set -euo pipefail

repo=.
remote=origin
publish=
allow_ahead=0
target=

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) [[ $# -ge 2 ]] || { echo "merge-guard: --repo needs a value" >&2; exit 2; }
      repo=$2; shift 2 ;;
    --remote) [[ $# -ge 2 ]] || { echo "merge-guard: --remote needs a value" >&2; exit 2; }
      remote=$2; shift 2 ;;
    --publish) [[ $# -ge 2 ]] || { echo "merge-guard: --publish needs a value" >&2; exit 2; }
      publish=$2; shift 2 ;;
    --allow-ahead) allow_ahead=1; shift ;;
    -h|--help)
      awk 'NR>1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
      exit 0 ;;
    -*) echo "merge-guard: unknown option: $1" >&2; exit 2 ;;
    *) [[ -z $target ]] || { echo "merge-guard: one TARGET at a time" >&2; exit 2; }
      target=$1; shift ;;
  esac
done

[[ -n $target ]] || { echo "merge-guard: missing TARGET (the integration line)" >&2; exit 2; }
git -C "$repo" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || { echo "merge-guard: not a git worktree: $repo" >&2; exit 2; }

case "$target" in
  refs/heads/*) line=${target#refs/heads/} ;;
  "$remote"/*) line=${target#"$remote"/} ;;
  *) line=$target ;;
esac
[[ -n $line && $line != */ && $line != HEAD && $line != *@* ]] \
  || { echo "merge-guard: not an integration line name: $target" >&2; exit 2; }

remote_ref=refs/remotes/$remote/$line
local_ref=refs/heads/$line

say() { echo "merge-guard: $*"; }
warn() { echo "merge-guard: WARN $*"; }

# The one command block every failure ends with, so a refusal is actionable.
safe_pattern() {
  local sha=${1:-<merge-sha>}
  echo
  say "Safe pattern (never a local branch by name):"
  say "  git fetch origin"
  say "  git worktree add <scratch> --detach $remote/$line"
  say "  # merge there, then publish the resulting commit by explicit SHA:"
  say "  scripts/merge-guard.sh --publish $sha $line"
  say "  git push origin $sha:refs/heads/$line"
  say "Publish that SHA only. Never --all / --mirror / --tags: the archive/*"
  say "tags kept for PRO-319's abandoned local line stay local."
}

if ! remote_sha=$(git -C "$repo" rev-parse --verify --quiet "$remote_ref^{commit}"); then
  say "REFUSED — the authoritative line $remote/$line is not visible in $repo"
  say "  ($remote_ref does not exist)"
  say "Nothing has been fetched, so there is nothing safe to merge into. If the"
  say "line really is new on the remote, fetch first — this script never does:"
  say "  git -C $repo fetch $remote"
  safe_pattern
  exit 1
fi

if ! local_sha=$(git -C "$repo" rev-parse --verify --quiet "$local_ref^{commit}"); then
  say "ok — no local '$line' branch in $repo, so nothing can be published by name"
  say "  authoritative line: $remote/$line @ ${remote_sha:0:9}"
  if [[ -n $publish ]]; then
    publish_sha=$(git -C "$repo" rev-parse --verify --quiet "$publish^{commit}" \
      || { echo "merge-guard: --publish: not a commit: $publish" >&2; exit 2; })
    if ! git -C "$repo" merge-base --is-ancestor "$remote_sha" "$publish_sha"; then
      say "REFUSED — $publish is not a fast-forward over $remote/$line"
      say "  line @ ${remote_sha:0:9}, to publish @ ${publish_sha:0:9}"
      say "The line moved under you. Re-merge in a fresh detached worktree:"
      safe_pattern "$publish"
      exit 1
    fi
    if [[ $publish_sha == "$remote_sha" ]]; then
      say "  $publish is already the tip of $remote/$line — nothing to publish."
    else
      say "  $publish is a fast-forward over $remote/$line — safe to publish:"
      say "  git push origin ${publish_sha}:refs/heads/$line"
    fi
  else
    say "  publish by explicit SHA, never by branch name:"
    say "  git push origin <merge-sha>:refs/heads/$line"
  fi
  exit 0
fi

# Config surprises: `git push` by name follows branch.<name>.remote/merge, so a
# branch whose upstream name differs from itself is not the branch it claims.
if cfg_remote=$(git -C "$repo" config --get "branch.$line.remote") && [[ $cfg_remote != "$remote" ]]; then
  warn "branch.$line.remote is '$cfg_remote', not '$remote' — a push by name leaves this repo's line"
fi
if cfg_merge=$(git -C "$repo" config --get "branch.$line.merge"); then
  if [[ $cfg_merge != "refs/heads/$line" ]]; then
    warn "branch.$line.merge is '$cfg_merge', not 'refs/heads/$line' — a push by name targets another line"
  fi
fi
if head_ref=$(git -C "$repo" symbolic-ref -q HEAD 2>/dev/null) && [[ $head_ref == "$local_ref" ]]; then
  warn "you are standing on $local_ref in $repo — integrate in a detached worktree from $remote/$line instead"
fi

counts=$(git -C "$repo" rev-list --left-right --count "$local_sha...$remote_sha")
ahead=${counts%%[[:space:]]*}
behind=${counts##*[[:space:]]}
[[ $ahead =~ ^[0-9]+$ && $behind =~ ^[0-9]+$ ]] \
  || { echo "merge-guard: cannot parse ahead/behind from: $counts" >&2; exit 2; }

if [[ $ahead -eq 0 && $behind -eq 0 ]]; then
  say "ok — local '$line' is exactly at $remote/$line @ ${remote_sha:0:9} (0 ahead / 0 behind)"
  say "  there is nothing to publish by name; a branch-name push here is a no-op."
  if [[ -n $publish ]]; then
    publish_sha=$(git -C "$repo" rev-parse --verify --quiet "$publish^{commit}" \
      || { echo "merge-guard: --publish: not a commit: $publish" >&2; exit 2; })
    if ! git -C "$repo" merge-base --is-ancestor "$remote_sha" "$publish_sha"; then
      say "REFUSED — $publish is not a fast-forward over $remote/$line"
      say "  line @ ${remote_sha:0:9}, to publish @ ${publish_sha:0:9}"
      safe_pattern "$publish"
      exit 1
    fi
    if [[ $publish_sha == "$remote_sha" ]]; then
      say "  $publish is already the tip of $remote/$line — nothing to publish."
    else
      say "  $publish is a fast-forward over $remote/$line — safe to publish:"
      say "  git push origin ${publish_sha}:refs/heads/$line"
    fi
  else
    say "  publish the merge commit by SHA from the detached worktree:"
    say "  git push origin <merge-sha>:refs/heads/$line"
  fi
  exit 0
fi

verdict=refuse
reason=
if [[ $behind -gt 0 && $ahead -gt 0 ]]; then
  reason="has DIVERGED from $remote/$line ($ahead ahead / $behind behind)"
elif [[ $behind -gt 0 ]]; then
  reason="is BEHIND $remote/$line (0 ahead / $behind behind)"
else
  reason="carries $ahead local-only commit(s) that $remote/$line has never seen"
  if [[ $allow_ahead -eq 1 ]]; then
    verdict=warn
  elif [[ -n $publish ]]; then
    # A SHA publish cannot move the line to a stale local tip; the fast-forward
    # check below is the load-bearing one. Downgrade, but say why.
    verdict=warn
  fi
fi

if [[ $verdict == refuse ]]; then
  say "REFUSED — the proposed merge target is the local branch '$line', which $reason"
  say "  local   $local_ref @ ${local_sha:0:9}"
  say "  line    $remote_ref @ ${remote_sha:0:9}"
  if [[ $behind -gt 0 ]]; then
    say "  commits on $remote/$line that the local branch does not have ($behind):"
    git -C "$repo" log --no-decorate -n 5 --pretty='    %h %s' "$local_sha..$remote_sha"
  fi
  if [[ $ahead -gt 0 ]]; then
    say "  commits only on the local branch ($ahead) — never reviewed on the line:"
    git -C "$repo" log --no-decorate -n 5 --pretty='    %h %s' "$remote_sha..$local_sha"
  fi
  say "Why: a local branch is a snapshot. Publishing it by name can drop commits"
  say "the line already has, or publish unreviewed work — and the 'Fast-forward'"
  say "git prints makes that look like a clean merge (PRO-319)."
  safe_pattern "${publish:-<merge-sha>}"
  exit 1
fi

warn "local '$line' $reason — it is not what gets published by a SHA push, but"
warn "  it is the habit that produced PRO-319. Realign it: git branch -f $line $remote/$line"
if [[ -n $publish ]]; then
  publish_sha=$(git -C "$repo" rev-parse --verify --quiet "$publish^{commit}" \
    || { echo "merge-guard: --publish: not a commit: $publish" >&2; exit 2; })
  if ! git -C "$repo" merge-base --is-ancestor "$remote_sha" "$publish_sha"; then
    say "REFUSED — $publish is not a fast-forward over $remote/$line"
    say "  line @ ${remote_sha:0:9}, to publish @ ${publish_sha:0:9}"
    say "The line moved under you. Re-merge in a fresh detached worktree:"
    safe_pattern "$publish"
    exit 1
  fi
  say "ok — $publish is a fast-forward over $remote/$line @ ${remote_sha:0:9}"
  say "  publish it, and only it:"
  say "  git push origin ${publish_sha}:refs/heads/$line"
  exit 0
fi
say "ok — nothing refused for a SHA publish, but no --publish SHA was given"
say "  publish by explicit SHA, never by branch name:"
say "  git push origin <merge-sha>:refs/heads/$line"
exit 0
