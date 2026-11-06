#!/usr/bin/env bash
#
# Regression harness for the two snapshot installers, scripts/lua-sync.sh
# (PRO-269) and scripts/compose-sync.sh (PRO-180), including the cases PRO-273
# review measured and PRO-282 closed:
#
#   (a) a non-regular path at a deployed target — FIFO, symlink, socket,
#       directory — is refused with rc 1 in BOTH modes. The two reproductions
#       that made this a security matter rather than a cosmetic one are here:
#       a FIFO used to hang sync mode forever (`timeout` turns that into a
#       visible rc 124 failure) and a symlink used to let `cp -p` overwrite an
#       unrelated file outside the deployed directory while still printing
#       `wrote`.
#   (b) the orphan pass sees what ALE actually loads: recursively, and over
#       every extension mod-ale accepts, compared on relative paths. A nested
#       `sub/sneaky.lua` and a live `payload.moon` used to pass `--check`.
#       The negative controls matter just as much — ALE compares extensions
#       case-sensitively and skips hidden entries, so `README.LUA` and
#       `.hidden.lua` must NOT be reported as orphans.
#   (e) on an orphan-only tree the sync run must not tell the user to run the
#       command they just ran.
#
# Nothing here touches a real tree: every case builds a throwaway repo root
# under $TMPDIR and runs a copy of the installer against it. Device nodes are
# not created (mknod needs privileges) — they share the `-e && ! -f` gate that
# the FIFO and socket cases exercise.
#
#   bash ops/preflight/test_sync_scripts.sh
#
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$here/../.." && pwd)"
lua_sync="$repo_root/scripts/lua-sync.sh"
compose_sync="$repo_root/scripts/compose-sync.sh"

pass=0
fail=0

ok() {
  pass=$((pass + 1))
  printf 'ok    %s\n' "$1"
}

bad() {
  fail=$((fail + 1))
  printf 'FAIL  %s — %s\n' "$1" "$2" >&2
}

t() {
  # t <name> <tree> <script> <want-rc> [match ...] -- [script args]
  # A match prefixed with `!` must be ABSENT from the combined output.
  local name=$1 tree=$2 script=$3 want=$4
  local -a greps=()
  shift 4
  while [[ $# -gt 0 && "$1" != "--" ]]; do
    greps+=("$1")
    shift
  done
  if [[ $# -gt 0 ]]; then
    shift
  fi

  local out rc
  out="$(cd "$tree" && timeout 10 bash "$script" "$@" 2>&1)"
  rc=$?
  if [[ $rc -ne $want ]]; then
    bad "$name" "rc=$rc want=$want"
    printf '%s\n' "$out" | sed 's/^/      | /' >&2
    return
  fi
  local g
  for g in "${greps[@]}"; do
    if [[ "$g" == '!'* ]]; then
      if [[ "$out" == *"${g#!}"* ]]; then
        bad "$name" "unexpected in output: ${g#!}"
        printf '%s\n' "$out" | sed 's/^/      | /' >&2
        return
      fi
    elif [[ "$out" != *"$g"* ]]; then
      bad "$name" "missing from output: $g"
      printf '%s\n' "$out" | sed 's/^/      | /' >&2
      return
    fi
  done
  ok "$name"
}

md5() {
  md5sum "$1" | cut -d' ' -f1
}

make_socket() {
  # An AF_UNIX socket at $1, the one non-regular path type mkfifo can't make.
  python3 -c 'import os,socket,sys; os.unlink(sys.argv[1]); s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])' "$1"
}

trees=()

new_tree() {
  # A throwaway repo root: two tracked ALE scripts already installed and
  # in-sync, plus both compose override pairs in-sync.
  local t
  t="$(mktemp -d "${TMPDIR:-/tmp}/sync-selftest.XXXXXX")"
  trees+=("$t")
  local snap="$t/wrath-unbound/env/dist/etc/modules/lua_scripts"
  local inst="$t/env/dist/etc/modules/lua_scripts"
  mkdir -p "$snap" "$inst" "$t/scripts"
  printf -- '-- tracked a\nprint("a")\n' >"$snap/a.lua"
  printf -- '-- tracked b\nprint("b")\n' >"$snap/b.lua"
  cp -p "$snap/a.lua" "$snap/b.lua" "$inst/"
  printf 'name: override\n' >"$t/wrath-unbound/docker-compose.override.yml"
  printf 'name: loadtest\n' >"$t/wrath-unbound/docker-compose.loadtest.override.yml"
  cp -p "$t/wrath-unbound/docker-compose.override.yml" "$t/docker-compose.override.yml"
  cp -p "$t/wrath-unbound/docker-compose.loadtest.override.yml" "$t/docker-compose.loadtest.override.yml"
  cp -p "$lua_sync" "$t/scripts/lua-sync.sh"
  cp -p "$compose_sync" "$t/scripts/compose-sync.sh"
  printf '%s' "$t"
}

cleanup() {
  local t
  for t in "${trees[@]}"; do
    [[ -n "$t" && -d "$t" ]] && rm -rf "$t"
  done
}
trap cleanup EXIT

L=scripts/lua-sync.sh
C=scripts/compose-sync.sh

# ── the matrix Review ran on PRO-273 ────────────────────────────────────────
tree="$(new_tree)"
t "lua: in-sync --check" "$tree" "$L" 0 "2 tracked script(s) in place"
t "lua: in-sync sync" "$tree" "$L" 0 "2 tracked script(s) in place"
t "lua: bad arg" "$tree" "$L" 2 "usage:" -- --nope
t "lua: --help" "$tree" "$L" 0 "pre-boot assertion" -- --help
t "compose: --help" "$tree" "$C" 0 "pre-boot assertion" -- --help

tree="$(new_tree)"
rm "$tree/env/dist/etc/modules/lua_scripts/b.lua"
t "lua: missing --check" "$tree" "$L" 1 "b.lua is missing" -- --check
t "lua: missing sync repairs" "$tree" "$L" 0 "wrote"
if [[ -f "$tree/env/dist/etc/modules/lua_scripts/b.lua" ]]; then
  ok "lua: missing sync created the file"
else
  bad "lua: missing sync created the file" "not written"
fi

tree="$(new_tree)"
printf -- '-- drifted\n' >"$tree/env/dist/etc/modules/lua_scripts/b.lua"
want_snap="$(md5 "$tree/wrath-unbound/env/dist/etc/modules/lua_scripts/b.lua")"
want_dep="$(md5 "$tree/env/dist/etc/modules/lua_scripts/b.lua")"
t "lua: drift --check" "$tree" "$L" 1 "DRIFT" "snapshot md5 $want_snap" "deployed md5 $want_dep" "---" "+++" -- --check
t "lua: drift sync repairs" "$tree" "$L" 0 "wrote"
if cmp -s "$tree/wrath-unbound/env/dist/etc/modules/lua_scripts/b.lua" \
  "$tree/env/dist/etc/modules/lua_scripts/b.lua"; then
  ok "lua: drift sync left bytes identical"
else
  bad "lua: drift sync repairs" "deployed copy still differs"
fi

tree="$(new_tree)"
rm "$tree/wrath-unbound/env/dist/etc/modules/lua_scripts/a.lua" \
  "$tree/wrath-unbound/env/dist/etc/modules/lua_scripts/b.lua"
t "lua: empty snapshot --check" "$tree" "$L" 1 "refusing to call an empty snapshot clean" -- --check
t "lua: empty snapshot sync" "$tree" "$L" 1 "refusing to call an empty snapshot clean"

# orphan: reported, never deleted, in both modes
tree="$(new_tree)"
printf -- '-- hand-placed\n' >"$tree/env/dist/etc/modules/lua_scripts/hand.lua"
t "lua: orphan --check" "$tree" "$L" 1 "ORPHAN" "hand.lua"
if [[ -f "$tree/env/dist/etc/modules/lua_scripts/hand.lua" ]]; then
  ok "lua: orphan --check did not delete"
else
  bad "lua: orphan --check did not delete" "the orphan was removed"
fi
t "lua: orphan sync" "$tree" "$L" 1 "ORPHAN" "hand.lua" \
  "!install the snapshot with" "!fix with"
if [[ -f "$tree/env/dist/etc/modules/lua_scripts/hand.lua" ]]; then
  ok "lua: orphan sync did not delete"
else
  bad "lua: orphan sync did not delete" "the orphan was removed"
fi

# (e) the hint is news to the caller: shown when something is also missing
tree="$(new_tree)"
printf -- '-- hand-placed\n' >"$tree/env/dist/etc/modules/lua_scripts/hand.lua"
rm "$tree/env/dist/etc/modules/lua_scripts/b.lua"
t "lua: orphan + drift --check names both actions" "$tree" "$L" 1 "ORPHAN" "install the snapshot with" -- --check

# ── (b) the orphan pass must match what ALE loads ───────────────────────────
tree="$(new_tree)"
mkdir -p "$tree/env/dist/etc/modules/lua_scripts/sub"
printf -- '-- nested\n' >"$tree/env/dist/etc/modules/lua_scripts/sub/sneaky.lua"
t "lua: nested orphan --check" "$tree" "$L" 1 "ORPHAN" "sub/sneaky.lua"
t "lua: nested orphan sync" "$tree" "$L" 1 "ORPHAN" "sub/sneaky.lua"
rm -rf "$tree/env/dist/etc/modules/lua_scripts/sub"
t "lua: nested orphan removal clears --check" "$tree" "$L" 0

tree="$(new_tree)"
mkdir -p "$tree/wrath-unbound/env/dist/etc/modules/lua_scripts/sub"
printf -- '-- tracked nested\nprint("c")\n' >"$tree/wrath-unbound/env/dist/etc/modules/lua_scripts/sub/c.lua"
t "lua: tracked subdirectory is compared on its relative path" "$tree" "$L" 1 "DRIFT" "sub/c.lua is missing" -- --check
t "lua: tracked subdirectory installs" "$tree" "$L" 0 "wrote"
t "lua: tracked subdirectory then in-sync" "$tree" "$L" 0 "3 tracked script(s) in place" -- --check

for ext in moon ext so out dll lua; do
  tree="$(new_tree)"
  printf 'x\n' >"$tree/env/dist/etc/modules/lua_scripts/payload.$ext"
  t "lua: ALE-loaded .$ext orphan --check" "$tree" "$L" 1 "ORPHAN" "payload.$ext"
done

# negative controls: ALE would not load any of these, so they are not orphans
tree="$(new_tree)"
printf 'notes\n' >"$tree/env/dist/etc/modules/lua_scripts/notes.txt"
printf 'x\n' >"$tree/env/dist/etc/modules/lua_scripts/README.LUA"
printf 'x\n' >"$tree/env/dist/etc/modules/lua_scripts/.hidden.lua"
mkdir -p "$tree/env/dist/etc/modules/lua_scripts/.cache"
printf 'x\n' >"$tree/env/dist/etc/modules/lua_scripts/.cache/loaded.lua"
printf 'x\n' >"$tree/env/dist/etc/modules/lua_scripts/noext"
t "lua: non-loaded files are not orphans" "$tree" "$L" 0 "nothing untracked under"

# a symlink a live ALE would open is never silently clean
tree="$(new_tree)"
ln -s a.lua "$tree/env/dist/etc/modules/lua_scripts/alias.lua"
t "lua: untracked symlink is an orphan" "$tree" "$L" 1 "ORPHAN" "alias.lua"

# a symlinked subdirectory cannot be audited from here, so it must not be clean
tree="$(new_tree)"
mkdir -p "$tree/elsewhere"
printf 'x\n' >"$tree/elsewhere/deep.lua"
ln -s "$tree/elsewhere" "$tree/env/dist/etc/modules/lua_scripts/linked"
t "lua: symlinked subdirectory is unaudited" "$tree" "$L" 1 "UNAUDITED" "linked"

# ── (a) non-regular paths are refused before anything reads or writes ───────
for mode in check sync; do
  arg=()
  [[ $mode == check ]] && arg=(--check)
  tree="$(new_tree)"
  rm "$tree/env/dist/etc/modules/lua_scripts/a.lua"
  mkfifo "$tree/env/dist/etc/modules/lua_scripts/a.lua"
  t "lua: FIFO at a tracked path ($mode)" "$tree" "$L" 1 \
    "not a regular file" "FIFO" "refusing to overwrite" -- "${arg[@]}"

  tree="$(new_tree)"
  rm "$tree/env/dist/etc/modules/lua_scripts/b.lua"
  mkfifo "$tree/env/dist/etc/modules/lua_scripts/b.lua"
  t "lua: FIFO at an uninstalled tracked path ($mode)" "$tree" "$L" 1 \
    "not a regular file" "FIFO" -- "${arg[@]}"

  tree="$(new_tree)"
  make_socket "$tree/env/dist/etc/modules/lua_scripts/a.lua"
  t "lua: socket at a tracked path ($mode)" "$tree" "$L" 1 \
    "not a regular file" "socket" -- "${arg[@]}"

  tree="$(new_tree)"
  rm -rf "$tree/env/dist/etc/modules/lua_scripts/a.lua"
  mkdir "$tree/env/dist/etc/modules/lua_scripts/a.lua"
  t "lua: directory at a tracked path ($mode)" "$tree" "$L" 1 \
    "not a regular file" "directory" -- "${arg[@]}"
done

# the clobber reproduction: the symlink target must survive, untouched
tree="$(new_tree)"
victim="$tree/outside-victim.txt"
printf 'do not touch me\n' >"$victim"
victim_md5="$(md5 "$victim")"
rm "$tree/env/dist/etc/modules/lua_scripts/a.lua"
ln -s "$victim" "$tree/env/dist/etc/modules/lua_scripts/a.lua"
t "lua: symlink at a tracked path (sync) is refused" "$tree" "$L" 1 \
  "not a regular file" "symlink ->" "refusing to overwrite" "!wrote"
if [[ -L "$tree/env/dist/etc/modules/lua_scripts/a.lua" ]]; then
  ok "lua: symlink still a symlink"
else
  bad "lua: symlink still a symlink" "it was replaced by a regular file"
fi
if [[ "$(md5 "$victim")" == "$victim_md5" ]]; then
  ok "lua: symlink target not clobbered"
else
  bad "lua: symlink target not clobbered" "the file outside the deployed dir was overwritten"
fi
t "lua: symlink at a tracked path (--check) is refused" "$tree" "$L" 1 \
  "not a regular file" "symlink ->"

# a symlink whose target already matches the snapshot must NOT read as ok
tree="$(new_tree)"
printf -- '-- tracked a\nprint("a")\n' >"$tree/outside-match.lua"
rm "$tree/env/dist/etc/modules/lua_scripts/a.lua"
ln -s "$tree/outside-match.lua" "$tree/env/dist/etc/modules/lua_scripts/a.lua"
t "lua: matching symlink target is not 'ok'" "$tree" "$L" 1 \
  "not a regular file" "!a.lua (md5"

# an ancestor of the deployed directory must not be walked through either
for ancestor in dist dist/etc dist/etc/modules; do
  for mode in check sync; do
    arg=()
    [[ $mode == check ]] && arg=(--check)
    tree="$(new_tree)"
    slug=${ancestor//\//_}
    victim="$tree/outside-victim.txt"
    printf 'do not touch me\n' >"$victim"
    victim_md5="$(md5 "$victim")"
    mv "$tree/env/$ancestor" "$tree/elsewhere-$slug"
    ln -s "$tree/elsewhere-$slug" "$tree/env/$ancestor"
    t "lua: symlinked ancestor env/$ancestor ($mode)" "$tree" "$L" 1 \
      "is a symlink" "refusing to install through it" -- "${arg[@]}"
    if [[ "$(md5 "$victim")" == "$victim_md5" ]]; then
      ok "lua: symlinked ancestor env/$ancestor wrote nothing ($mode)"
    else
      bad "lua: symlinked ancestor env/$ancestor wrote nothing ($mode)" \
        "the file outside the tree changed"
    fi
  done
done

tree="$(new_tree)"
victim="$tree/outside-victim.txt"
printf 'do not touch me\n' >"$victim"
victim_md5="$(md5 "$victim")"
rm -rf "$tree/env/dist/etc/modules/lua_scripts"
ln -s "$(dirname "$victim")" "$tree/env/dist/etc/modules/lua_scripts"
t "lua: deployed dir is a symlink (sync)" "$tree" "$L" 1 \
  "not a directory" "symlink ->" "!wrote"
if [[ "$(md5 "$victim")" == "$victim_md5" ]]; then
  ok "lua: deployed-dir symlink wrote nothing outside"
else
  bad "lua: deployed-dir symlink wrote nothing outside" "the file outside the tree changed"
fi
t "lua: deployed dir is a symlink (--check)" "$tree" "$L" 1 "not a directory"

# ── compose-sync.sh carries the same weakness ───────────────────────────────
tree="$(new_tree)"
t "compose: in-sync --check" "$tree" "$C" 0 "ok    docker-compose.override.yml"
t "compose: in-sync sync" "$tree" "$C" 0 "ok    docker-compose.override.yml"
t "compose: bad arg" "$tree" "$C" 2 "usage:" -- --nope

tree="$(new_tree)"
printf 'drifted\n' >>"$tree/docker-compose.override.yml"
t "compose: drift --check" "$tree" "$C" 1 "DRIFT" "---" "+++" "fix with" -- --check
t "compose: drift sync repairs" "$tree" "$C" 0 "wrote docker-compose.override.yml"

tree="$(new_tree)"
rm "$tree/docker-compose.loadtest.override.yml"
t "compose: missing --check" "$tree" "$C" 1 "is missing" -- --check
t "compose: missing sync repairs" "$tree" "$C" 0 "wrote"

for mode in check sync; do
  arg=()
  [[ $mode == check ]] && arg=(--check)
  tree="$(new_tree)"
  rm "$tree/docker-compose.override.yml"
  mkfifo "$tree/docker-compose.override.yml"
  t "compose: FIFO at a root copy ($mode)" "$tree" "$C" 1 \
    "not a regular file" "FIFO" "refusing to overwrite" -- "${arg[@]}"

  tree="$(new_tree)"
  make_socket "$tree/docker-compose.override.yml"
  t "compose: socket at a root copy ($mode)" "$tree" "$C" 1 \
    "not a regular file" "socket" -- "${arg[@]}"

  tree="$(new_tree)"
  victim="$tree/outside-victim.txt"
  printf 'do not touch me\n' >"$victim"
  victim_md5="$(md5 "$victim")"
  rm "$tree/docker-compose.override.yml"
  ln -s "$victim" "$tree/docker-compose.override.yml"
  t "compose: symlink at a root copy ($mode)" "$tree" "$C" 1 \
    "not a regular file" "symlink ->" "!wrote" -- "${arg[@]}"
  if [[ "$(md5 "$victim")" == "$victim_md5" ]]; then
    ok "compose: symlink target not clobbered ($mode)"
  else
    bad "compose: symlink target not clobbered ($mode)" "the file outside the tree was overwritten"
  fi

  tree="$(new_tree)"
  printf 'name: override\n' >"$tree/outside-match.yml"
  rm "$tree/docker-compose.override.yml"
  ln -s "$tree/outside-match.yml" "$tree/docker-compose.override.yml"
  t "compose: matching symlink target is not 'ok' ($mode)" "$tree" "$C" 1 \
    "not a regular file" "!wrote docker-compose.override.yml" -- "${arg[@]}"

  tree="$(new_tree)"
  rm "$tree/docker-compose.override.yml"
  mkdir "$tree/docker-compose.override.yml"
  t "compose: directory at a root copy ($mode)" "$tree" "$C" 1 \
    "not a regular file" "directory" -- "${arg[@]}"
done

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[[ $fail -eq 0 ]] || exit 1
