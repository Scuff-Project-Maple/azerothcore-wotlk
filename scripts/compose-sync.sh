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
#   scripts/compose-sync.sh          install canonical copies over the root ones
#   scripts/compose-sync.sh --check  pre-boot assertion; non-zero exit on drift
#
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

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
    sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

  if [[ -f "$root/$installed" ]] && cmp -s "$root/$canonical" "$root/$installed"; then
    echo "compose-sync: ok    $installed"
    continue
  fi

  if [[ $mode == check ]]; then
    echo "compose-sync: DRIFT $installed does not match $canonical" >&2
    if [[ -f "$root/$installed" ]]; then
      diff -u "$root/$canonical" "$root/$installed" >&2 || true
    else
      echo "compose-sync:       $installed is missing (run scripts/compose-sync.sh)" >&2
    fi
    status=1
  else
    if ! cp -p "$root/$canonical" "$root/$installed" 2>/dev/null; then
      echo "compose-sync: cannot write $installed — run as the tree owner" >&2
      echo "compose-sync: (on the build host: sudo -u pira scripts/compose-sync.sh)" >&2
      exit 1
    fi
    echo "compose-sync: wrote $installed from $canonical"
  fi
done

if [[ $mode == check && $status -ne 0 ]]; then
  echo "compose-sync: fix with 'scripts/compose-sync.sh' in $root" >&2
  echo "compose-sync: (on the build host the tree is owned by pira: sudo -u pira scripts/compose-sync.sh)" >&2
fi

exit $status
