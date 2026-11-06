#!/usr/bin/env bash
#
# PRO-269: keep the gitignored ALE script directory identical to its tracked
# snapshot under wrath-unbound/env/dist/etc/modules/lua_scripts/.
#
# ALE loads every script it finds *under* `ALE.ScriptPath`
# (env/dist/etc/modules/lua_scripts, gitignored via `/env/dist/*`):
# `ALE::GetScripts` (mod-ale `LuaEngine.cpp:617`) walks that directory
# recursively (`:653`) and `ALE::AddScriptPath` (`:366`) accepts every
# extension in `ale_exts` below, not just `.lua`. So a script that only ever
# reached that directory by hand — which is how `ranger_vex_gossip.lua` was
# live in the stack with nothing in git behind it (PRO-163 red flag R6, filed
# as PRO-269) — can be dropped by any tree-driven sync or `--force-recreate`
# with no trace, and then reads as a content regression in whatever change is
# under test. The orphan pass below mirrors what ALE actually loads, so a
# nested `sub/sneaky.lua` or a live `payload.moon` fails `--check` too
# (PRO-273 finding b).
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
# deployed tree must not hold a loadable script the snapshot does not. An
# orphan is reported, never deleted — silently removing a file the live
# worldserver is relying on is the exact failure this installer exists to
# prevent, so it is handed back to a human decision: commit it to the snapshot
# or remove it.
#
# A deployed path may only ever be ABSENT or a REGULAR FILE, and that is
# checked before anything reads or writes it, in both modes (PRO-273 finding
# a). The two ways that mattered are asymmetric, which is how the old
# directory-only guard survived review: a FIFO made `--check` refuse but made
# sync mode block forever inside `cmp` (rc 124, no output); a symlink made
# `cp -p` write *through* the link onto a file outside the deployed dir, still
# print `wrote`, and still pass `--check` when the target happened to match
# the snapshot. This script is documented to run as the tree owner, so
# "silently overwrote a file outside the deployed directory" is the failure to
# refuse outright, not to detect afterwards.
#
# The gate is on the WHOLE deployed path, not just its last component:
# nested tracked paths are legal here (ALE recurses), so `…/lua_scripts/sub`
# being a symlink is the same clobber at depth 2, and `mkdir -p` will happily
# create through it (PRO-293 blocking 1).
#
# Tested by ops/preflight/test_sync_scripts.sh — run it after any change here.
#
set -euo pipefail

# `pwd -P`, not `pwd`: invoking this script through a symlinked checkout
# (`link -> real`) would otherwise make `root` name the link while every inode
# it writes lives somewhere else — the tree-root shape of exactly the failure
# the ancestor gates below exist to prevent (PRO-293 N6).
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
canonical_dir="wrath-unbound/env/dist/etc/modules/lua_scripts"
installed_dir="env/dist/etc/modules/lua_scripts"

# What ALE::AddScriptPath accepts (mod-ale LuaEngine.cpp:378). A file with any
# other extension is not something ALE loads, so it is not an orphan this
# script can assert anything about. Case-sensitive on purpose: ALE compares
# `ext != ".lua"` verbatim, so `README.LUA` is never loaded and must not be
# reported as if it were.
ale_exts=(lua moon ext dll so out)

# Hidden entries are skipped by ALE before it ever looks at the type or the
# extension (`name[0] == '.'` -> continue, LuaEngine.cpp:646), including whole
# hidden subfolders — so `find` has to prune them, not just omit them.
hidden_pred=(-name '.*' -prune -o)

name_pred=()
for i in "${!ale_exts[@]}"; do
  if [[ $i -gt 0 ]]; then
    name_pred+=("-o")
  fi
  name_pred+=("-name" "*.${ale_exts[$i]}")
done

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
  else
    printf 'unavailable'
  fi
}

# Name the kind of thing sitting at a deployed path, so the refusal is
# legible enough to act on without a second `ls`. Never call this on a path
# that has not been gated: `readlink`/`stat` are cheap, but the caller must
# not have tried to read the file itself yet.
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

# Gate a deployed path and every directory component above it, down from the
# deployed directory. The leaf check alone is not enough: nested tracked paths
# are legal here (ALE recurses, `LuaEngine.cpp:653`), so with
# `…/lua_scripts/sub -> /somewhere/else` the installer would `mkdir -p` through
# the link, `cp -p` through it, print `wrote …/sub/c.lua`, and leave a file
# outside the deployed directory changed — while `--check` on the same tree
# called that outside file verified `ok` (PRO-293 blocking 1). `mkdir -p` and
# `cp` both follow intermediate links without complaint and every message still
# names `env/dist/…`, which is the "reported one thing, did another" failure.
#
# Prints the refusal and returns 1; returns 0 when the whole chain is safe to
# read from and write to. Never call it after anything has opened the path.
gate_deployed_chain() {
  local rel=$1 prefix="" comp kind
  local -a parts=()
  local i last
  IFS='/' read -r -a parts <<<"$rel"
  last=$(( ${#parts[@]} - 1 ))
  for (( i = 0; i < last; i++ )); do
    comp="${parts[$i]}"
    prefix="${prefix:+$prefix/}$comp"
    local p="$inst/$prefix"
    if [[ -L "$p" ]]; then
      kind="$(path_kind "$p")"
      echo "lua-sync: $installed_dir/$prefix is a $kind, not a directory — refusing to write through it" >&2
      echo "lua-sync: ALE walks it too, so nothing under it can be verified from this tree" >&2
      return 1
    fi
    if [[ -e "$p" && ! -d "$p" ]]; then
      kind="$(path_kind "$p")"
      echo "lua-sync: $installed_dir/$prefix is a $kind, not a directory — refusing to write" >&2
      return 1
    fi
  done
  return 0
}

# Every ancestor of the deployed directory has to be a real directory too, not
# just the leaf. `mkdir -p` and `cp` follow `env/dist/etc/modules -> /tmp/x`
# without complaint, and every message this script prints would still name
# `env/dist/...` — the "reported one thing, did another" failure in its
# directory-shaped form. `env` itself is deliberately not checked: relocating
# it is a legitimate host layout, and it is the last thing between this tree
# and the container mount.
for rel_walk in env/dist env/dist/etc env/dist/etc/modules; do
  if [[ -L "$root/$rel_walk" ]]; then
    echo "lua-sync: $rel_walk is a symlink — refusing to install through it" >&2
    echo "lua-sync: make it a real directory, or point ALE.ScriptPath at a real path" >&2
    exit 1
  fi
done

# `mkdir -p` is a no-op on a symlink to a directory, and the per-file `cp`
# would then land wherever that link points while every message still named
# the deployed directory. Refuse the whole target rather than install
# somewhere else. A missing directory is fine — the installer creates it.
inst="$root/$installed_dir"
if [[ -L "$inst" || ( -e "$inst" && ! -d "$inst" ) ]]; then
  kind="$(path_kind "$inst")"
  echo "lua-sync: $installed_dir is a $kind, not a directory — refusing to write" >&2
  exit 1
fi

# Relative paths, recursively, over the extension set ALE accepts. `-type f`
# on the source side is deliberate: a symlink committed into the snapshot can
# never satisfy the deployed-file check, so it surfaces as `missing` rather
# than as a silent pass — there is nothing here that reads through it.
snap="$root/$canonical_dir"
tracked=()
while IFS= read -r -d '' f; do
  tracked+=("${f#"$snap"/}")
done < <(find "$snap" -mindepth 1 "${hidden_pred[@]}" \
  \( -type f -a \( "${name_pred[@]}" \) \) -print0 | sort -z)

if [[ ${#tracked[@]} -eq 0 ]]; then
  # An empty snapshot has always meant "the installer is new and nobody has
  # staged anything yet"; on an existing tree it means someone deleted the
  # tracked scripts. Either way refuse rather than call it clean.
  echo "lua-sync: no tracked scripts in $canonical_dir — refusing to call an empty snapshot clean" >&2
  exit 1
fi

status=0
installed_count=0
# Whether anything is missing or mismatched, i.e. whether the install command
# below is news to the caller (PRO-273 finding e).
per_file_drift=0

for rel in "${tracked[@]}"; do
  canonical="$root/$canonical_dir/$rel"
  installed="$root/$installed_dir/$rel"

  # Gate the directory chain first, then the leaf. Both refuse before any
  # `cmp`/`diff`/`md5sum`/`cp` touches the path, in both modes, so the
  # check/sync asymmetry PRO-273 reported cannot come back at either depth.
  if ! gate_deployed_chain "$rel"; then
    if [[ $mode == check ]]; then
      status=1
      continue
    fi
    exit 1
  fi

  # Gate every read and write on this. One message and one verdict in both
  # modes, so the check/sync asymmetry PRO-273 reported cannot come back:
  # nothing below may `cmp`, `diff`, `md5sum` or `cp` a path that is not a
  # regular file.
  if [[ -L "$installed" || ( -e "$installed" && ! -f "$installed" ) ]]; then
    kind="$(path_kind "$installed")"
    echo "lua-sync: $installed_dir/$rel is a $kind, not a regular file — refusing to overwrite" >&2
    echo "lua-sync: remove it, then install as the tree owner:" >&2
    echo "lua-sync:  sudo -u pira scripts/lua-sync.sh" >&2
    if [[ $mode == check ]]; then
      status=1
      per_file_drift=1
      continue
    fi
    exit 1
  fi

  if [[ -f "$installed" ]] && cmp -s "$canonical" "$installed" 2>/dev/null; then
    echo "lua-sync: ok      $installed_dir/$rel (md5 $(md5_of "$installed"))"
    installed_count=$((installed_count + 1))
    continue
  fi

  if [[ $mode == check ]]; then
    if [[ ! -e "$installed" ]]; then
      echo "lua-sync: DRIFT   $installed_dir/$rel is missing (run: sudo -u pira scripts/lua-sync.sh)" >&2
    else
      echo "lua-sync: DRIFT   $installed_dir/$rel does not match $canonical_dir/$rel" >&2
      echo "lua-sync:           snapshot md5 $(md5_of "$canonical")  deployed md5 $(md5_of "$installed")" >&2
      diff -u "$canonical" "$installed" >&2 || true
    fi
    status=1
    per_file_drift=1
    continue
  fi

  # Nested tracked scripts are legal (ALE recurses), so the parent directory
  # is part of the install — `mkdir -p` on the deployed dir alone is not
  # enough once the tracked list holds relative paths.
  if ! mk_err=$(mkdir -p "$(dirname "$installed")" 2>&1); then
    echo "lua-sync: cannot create $(dirname "$installed"): ${mk_err:-mkdir failed}" >&2
    exit 1
  fi
  # Keep cp's own message: ENOSPC/EROFS must not be reported as a permissions
  # problem just because the tree is pira-owned (PRO-180 review lesson).
  if ! cp_err=$(cp -p "$canonical" "$installed" 2>&1); then
    echo "lua-sync: cannot write $installed: ${cp_err:-cp failed}" >&2
    echo "lua-sync: (if that is a permissions error, the tree is owned by" >&2
    echo "lua-sync:  pira: sudo -u pira scripts/lua-sync.sh)" >&2
    exit 1
  fi
  echo "lua-sync: wrote   $installed_dir/$rel from $canonical_dir/$rel (md5 $(md5_of "$installed"))"
  installed_count=$((installed_count + 1))
done

# Orphans: something ALE would load in the deployed tree that the snapshot
# cannot account for. Recursive, compared on paths relative to the deployed
# directory, over the whole extension set ALE accepts — the PRO-269 version
# (`-maxdepth 1 -type f -name '*.lua'`) called a nested `sub/sneaky.lua` clean
# while ALE was loading it (PRO-273 finding b). Report, never delete.
#
# Symlinks are listed too, because ALE opens them like any other file: a
# symlink is never `ok` above (it fails the regular-file gate), so it must
# never be invisible here either. `find` without `-L` does not descend into a
# symlinked subdirectory, so those are reported separately as unauditable
# rather than folded into the clean case.
orphans=()
unauditable=()
if [[ -d "$inst" ]]; then
  while IFS= read -r -d '' f; do
    rel=${f#"$inst"/}
    known=0
    for t in "${tracked[@]}"; do
      if [[ "$t" == "$rel" ]]; then
        known=1
        break
      fi
    done
    if [[ $known -eq 0 ]]; then
      if [[ -L "$f" ]]; then
        orphans+=("$rel (symlink -> $(readlink "$f" 2>/dev/null || echo 'unreadable'))")
      else
        orphans+=("$rel (md5 $(md5_of "$f"))")
      fi
    fi
  done < <(find "$inst" -mindepth 1 "${hidden_pred[@]}" \
    \( \( -type f -o -type l \) -a \( "${name_pred[@]}" \) \) -print0 | sort -z)

  while IFS= read -r -d '' d; do
    unauditable+=("${d#"$inst"/} (symlink -> $(readlink "$d" 2>/dev/null || echo 'unreadable'))")
  done < <(find "$inst" -mindepth 1 "${hidden_pred[@]}" \( -type l -xtype d \) -print0 | sort -z)
fi

if [[ ${#orphans[@]} -gt 0 ]]; then
  echo "lua-sync: ${#orphans[@]} deployed script(s) ALE would load but $canonical_dir does not track:" >&2
  for o in "${orphans[@]}"; do
    echo "lua-sync:   ORPHAN  $o" >&2
  done
  echo "lua-sync: commit each one to $canonical_dir/ with 'git add', or remove it" >&2
  echo "lua-sync: by hand — this script never deletes a live script, because that" >&2
  echo "lua-sync: is how PRO-269 hid." >&2
  status=1
fi

if [[ ${#unauditable[@]} -gt 0 ]]; then
  echo "lua-sync: ${#unauditable[@]} symlinked director(ies) under $installed_dir cannot be" >&2
  echo "lua-sync: audited from here — ALE walks them, this check does not follow them:" >&2
  for u in "${unauditable[@]}"; do
    echo "lua-sync:   UNAUDITED $u" >&2
  done
  echo "lua-sync: replace each with a real directory, or move it out of $installed_dir" >&2
  status=1
fi

if [[ $status -ne 0 ]]; then
  echo "lua-sync: $installed_count/${#tracked[@]} tracked script(s) in place" >&2
  if [[ $per_file_drift -eq 1 ]]; then
    # Only name the install command when something is actually missing or
    # mismatched. On an orphan-only tree it is the command the caller just ran
    # (PRO-273 finding e); the orphan block above already names the action
    # that is not a repeat.
    echo "lua-sync: install the snapshot with: scripts/lua-sync.sh in $root" >&2
    echo "lua-sync: (on the build host the tree is owned by pira, so that install" >&2
    echo "lua-sync:  must run as: sudo -u pira scripts/lua-sync.sh)" >&2
  fi
else
  echo "lua-sync: ${#tracked[@]} tracked script(s) in place, nothing untracked under $installed_dir"
fi

exit $status
