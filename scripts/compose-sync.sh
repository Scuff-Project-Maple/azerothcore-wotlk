#!/usr/bin/env bash
#
# PRO-180: keep the gitignored root compose overrides identical to their
# tracked source-of-truth copies under wrath-unbound/.
#
# `docker compose` only ever loads the repo-root files, but `.gitignore`
# (`/*.override.yml`) keeps them out of the index, so they used to be
# hand-edited and silently drifted from the tracked copies (PRO-33, PRO-65,
# PRO-178). There is deliberately no allowlist: each pair must be
# byte-identical.
#
#   scripts/compose-sync.sh           install the snapshots over the root
#                                     copies — this WRITES, so on the build
#                                     host run it as the tree owner:
#                                     `sudo -u pira scripts/compose-sync.sh`
#   scripts/compose-sync.sh --check   pre-boot assertion; non-zero exit on
#                                     drift. Read-only: safe as any user.
#
# A root copy may only ever be ABSENT or a REGULAR FILE, and that is checked
# before anything reads or writes it, in both modes (PRO-273 finding (a),
# inherited from the same house pattern as scripts/lua-sync.sh). Anything else
# is refused rather than touched: `cmp`/`diff` on a FIFO blocks forever, and
# `cp -p` follows a symlink, so a link at a root copy would let the installer
# overwrite a file elsewhere in — or outside — the tree while still printing
# `wrote`.
#
# Tested by ops/preflight/test_sync_scripts.sh — run it after any change here.
#
set -euo pipefail

# `pwd -P`, not `pwd`: run through a symlinked checkout (`link -> real`)
# and `root` would name the link while the inodes it writes live elsewhere
# (PRO-293 N6).
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"

# Name the kind of thing sitting at a root path, so the refusal is legible
# enough to act on without a second `ls`.
path_kind() {
  local p=$1
  if [[ -L "$p" ]]; then
    printf 'symlink -> %s' "$(readlink "$p" 2>/dev/null || echo 'unreadable')"
  elif [[ -p "$p" ]]; then
    printf 'FIFO'
  elif [[ -S "$p" ]]; then
    printf 'socket'
  elif [[ -d "$p" ]]; then
    printf 'directory'
  elif [[ -b "$p" ]]; then
    printf 'block device'
  elif [[ -c "$p" ]]; then
    printf 'character device'
  else
    printf 'non-regular file'
  fi
}

# canonical snapshot path : installed (gitignored, compose-visible) path
pairs=(
  "wrath-unbound/docker-compose.override.yml:docker-compose.override.yml"
  "wrath-unbound/docker-compose.loadtest.override.yml:docker-compose.loadtest.override.yml"
)

mode=sync
case "${1:-}" in
  "") ;;
  --check) mode=check ;;
  -h|--help)
    awk 'NR>1 { if (!/^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
    exit 0
    ;;
  *)
    echo "usage: ${0##*/} [--check]" >&2
    exit 2
    ;;
esac

status=0
for pair in "${pairs[@]}"; do
  canonical=${pair%%:*}
  installed=${pair##*:}

  if [[ ! -f "$root/$canonical" ]]; then
    echo "compose-sync: missing source of truth: $canonical" >&2
    status=1
    continue
  fi

  # Gate every read and write on this, with one message and one verdict in
  # both modes. Before this, a non-regular root copy reached the final `else`
  # and was reported as "missing", and a symlink to matching content was
  # reported as `ok`.
  if [[ -L "$root/$installed" || ( -e "$root/$installed" && ! -f "$root/$installed" ) ]]; then
    kind="$(path_kind "$root/$installed")"
    echo "compose-sync: $installed is a $kind, not a regular file — refusing to overwrite" >&2
    echo "compose-sync: remove it, then install as the tree owner:" >&2
    echo "compose-sync:  sudo -u pira scripts/compose-sync.sh" >&2
    if [[ $mode == check ]]; then
      status=1
      continue
    fi
    exit 1
  fi

  if [[ -f "$root/$installed" ]] && cmp -s "$root/$canonical" "$root/$installed"; then
    echo "compose-sync: ok    $installed"
    continue
  fi

  if [[ $mode == check ]]; then
    echo "compose-sync: DRIFT $installed does not match $canonical" >&2
    if [[ -f "$root/$installed" ]]; then
      diff -u "$root/$canonical" "$root/$installed" >&2 || true
    else
      echo "compose-sync:       $installed is missing (run: sudo -u pira scripts/compose-sync.sh)" >&2
    fi
    status=1
  else
    # Keep cp's own message: ENOSPC/EROFS must not be reported as a
    # permissions problem just because the tree is pira-owned (PRO-180 review).
    if ! cp_err=$(cp -p "$root/$canonical" "$root/$installed" 2>&1); then
      echo "compose-sync: cannot write $installed: ${cp_err:-cp failed}" >&2
      echo "compose-sync: (if that is a permissions error, the tree is owned by" >&2
      echo "compose-sync:  pira: sudo -u pira scripts/compose-sync.sh)" >&2
      exit 1
    fi
    echo "compose-sync: wrote $installed from $canonical"
  fi
done

if [[ $mode == check && $status -ne 0 ]]; then
  echo "compose-sync: fix with 'scripts/compose-sync.sh' in $root" >&2
  echo "compose-sync: (on the build host the tree is owned by pira, so that install" >&2
  echo "compose-sync:  must run as: sudo -u pira scripts/compose-sync.sh)" >&2
fi

exit $status
