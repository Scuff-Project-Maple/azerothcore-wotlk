#!/usr/bin/env bash
#
# PRO-269: keep the gitignored ALE script directory identical to its tracked
# snapshot under wrath-unbound/env/dist/etc/modules/lua_scripts/.
#
# ALE loads every *.lua it finds in `ALE.ScriptPath`
# (env/dist/etc/modules/lua_scripts, gitignored via `/env/dist/*`), so a script
# that only ever reached that directory by hand — which is how
# `ranger_vex_gossip.lua` was live in the stack with nothing in git behind it
# (PRO-163 red flag R6, filed as PRO-269) — can be dropped by any tree-driven
# sync or `--force-recreate` with no trace, and then reads as a content
# regression in whatever change is under test.
#
#   scripts/lua-sync.sh           install every tracked script into the
#                                 deployed dir — this WRITES, so on the build
#                                 host run it as the tree owner:
#                                 `sudo -u pira scripts/lua-sync.sh`
#   scripts/lua-sync.sh --check   pre-boot assertion; non-zero exit on drift
#                                 and on a hand-placed script the snapshot does
#                                 not have. Read-only: safe as any user.
#
# The snapshot directory is the source of truth. There is deliberately no
# allowlist: each tracked script must be byte-identical (`cmp`), and the
# deployed dir must not hold a *.lua that the snapshot does not. An orphan is
# reported, never deleted — silently removing a file the live worldserver is
# relying on is the exact failure this installer exists to prevent, so it is
# handed back to a human decision: commit it to the snapshot or remove it.
#
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
canonical_dir="wrath-unbound/env/dist/etc/modules/lua_scripts"
installed_dir="env/dist/etc/modules/lua_scripts"

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

if [[ ! -d "$root/$canonical_dir" ]]; then
  echo "lua-sync: missing snapshot directory: $canonical_dir" >&2
  exit 1
fi

md5_of() {
  local file=$1
  if command -v md5sum >/dev/null 2>&1; then
    md5sum "$file" | cut -d' ' -f1
  fi
}

tracked=()
while IFS= read -r -d '' f; do
  tracked+=("$(basename "$f")")
done < <(find "$root/$canonical_dir" -maxdepth 1 -type f -name '*.lua' -print0 | sort -z)

if [[ ${#tracked[@]} -eq 0 ]]; then
  # An empty snapshot has always meant "the installer is new and nobody has
  # staged anything yet"; on an existing tree it means someone deleted the
  # tracked scripts. Either way refuse rather than call it clean.
  echo "lua-sync: no tracked scripts in $canonical_dir — refusing to call an empty snapshot clean" >&2
  exit 1
fi

status=0
installed_count=0

for name in "${tracked[@]}"; do
  canonical="$root/$canonical_dir/$name"
  installed="$root/$installed_dir/$name"

  if [[ -f "$installed" ]] && cmp -s "$canonical" "$installed"; then
    echo "lua-sync: ok      $installed_dir/$name (md5 $(md5_of "$installed"))"
    installed_count=$((installed_count + 1))
    continue
  fi

  if [[ $mode == check ]]; then
    if [[ -e "$installed" && ! -f "$installed" ]]; then
      echo "lua-sync: DRIFT   $installed_dir/$name is not a regular file" >&2
      echo "lua-sync:         remove it, then run: sudo -u pira scripts/lua-sync.sh" >&2
    elif [[ ! -e "$installed" ]]; then
      echo "lua-sync: DRIFT   $installed_dir/$name is missing (run: sudo -u pira scripts/lua-sync.sh)" >&2
    else
      echo "lua-sync: DRIFT   $installed_dir/$name does not match $canonical_dir/$name" >&2
      echo "lua-sync:           snapshot md5 $(md5_of "$canonical")  deployed md5 $(md5_of "$installed")" >&2
      diff -u "$canonical" "$installed" >&2 || true
    fi
    status=1
    continue
  fi

  if [[ -d "$installed" ]]; then
    # cp would silently copy *into* the directory and report success.
    echo "lua-sync: $installed_dir/$name is a directory, expected a regular file" >&2
    exit 1
  fi
  mkdir -p "$root/$installed_dir"
  # Keep cp's own message: ENOSPC/EROFS must not be reported as a permissions
  # problem just because the tree is pira-owned (PRO-180 review lesson).
  if ! cp_err=$(cp -p "$canonical" "$installed" 2>&1); then
    echo "lua-sync: cannot write $installed: ${cp_err:-cp failed}" >&2
    echo "lua-sync: (if that is a permissions error, the tree is owned by" >&2
    echo "lua-sync:  pira: sudo -u pira scripts/lua-sync.sh)" >&2
    exit 1
  fi
  echo "lua-sync: wrote   $installed_dir/$name from $canonical_dir/$name (md5 $(md5_of "$installed"))"
  installed_count=$((installed_count + 1))
done

# Orphans: something in the deployed dir that the snapshot cannot account for.
orphans=()
if [[ -d "$root/$installed_dir" ]]; then
  while IFS= read -r -d '' f; do
    name=$(basename "$f")
    known=0
    for t in "${tracked[@]}"; do
      if [[ "$t" == "$name" ]]; then
        known=1
        break
      fi
    done
    if [[ $known -eq 0 ]]; then
      orphans+=("$name (md5 $(md5_of "$f"))")
    fi
  done < <(find "$root/$installed_dir" -maxdepth 1 -type f -name '*.lua' -print0 | sort -z)
fi

if [[ ${#orphans[@]} -gt 0 ]]; then
  echo "lua-sync: ${#orphans[@]} deployed script(s) have no tracked source in $canonical_dir:" >&2
  for o in "${orphans[@]}"; do
    echo "lua-sync:   ORPHAN  $o" >&2
  done
  echo "lua-sync: commit each one to $canonical_dir/ or remove it — this script" >&2
  echo "lua-sync: never deletes a live script, because that is how PRO-269 hid." >&2
  status=1
fi

if [[ $status -ne 0 ]]; then
  echo "lua-sync: $installed_count/${#tracked[@]} tracked script(s) in place" >&2
  echo "lua-sync: fix with 'scripts/lua-sync.sh' in $root" >&2
  echo "lua-sync: (on the build host the tree is owned by pira, so that install" >&2
  echo "lua-sync:  must run as: sudo -u pira scripts/lua-sync.sh)" >&2
else
  echo "lua-sync: ${#tracked[@]} tracked script(s) in place, no orphans in $installed_dir"
fi

exit $status
