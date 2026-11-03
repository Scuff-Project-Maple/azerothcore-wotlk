#!/usr/bin/env bash
# Boot the worldserver only when the PRO-179 pre-flight gate says the updater
# will not LOG_FATAL, silently re-apply a file, violate the write policy, or
# error out on a scratch clone of acore_world.
#
#   usage: ops/deploy/worldserver-up.sh [extra preflight flags]
#
# Set PREFLIGHT_CHECK_ONLY=1 to run the gate without touching the stack (CI,
# merge-gate rehearsal). Extra arguments are forwarded to the checker, so
# `--json /tmp/preflight.json` and `--strict-all` work from here.
#
# Lease policy: this script does NOT take the single-writer lease. Restarting
# ac-worldserver requires it — see docs/dev-environment.md#single-writer-lease.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_ROOT="${COMPOSE_ROOT:-$REPO_ROOT}"
GATE="$SCRIPT_DIR/../preflight/preflight_worlddb.py"
PYTHON="${PYTHON:-python3}"

if [[ ! -f "$GATE" ]]; then
  echo "worldserver-up: pre-flight gate not found at $GATE" >&2
  exit 2
fi

echo "worldserver-up: pre-flight gate (compose root $COMPOSE_ROOT)"
rc=0
"$PYTHON" "$GATE" --compose-root "$COMPOSE_ROOT" "$@" || rc=$?
if (( rc != 0 )); then
  echo "worldserver-up: REFUSING BOOT — pre-flight failed (exit $rc)." >&2
  echo "worldserver-up: ac-worldserver was not started. Fix the named file(s) or" >&2
  echo "                  escalate to Scuff (SQL); do not bypass this gate." >&2
  exit "$rc"
fi

if [[ "${PREFLIGHT_CHECK_ONLY:-0}" == "1" ]]; then
  echo "worldserver-up: gate passed; PREFLIGHT_CHECK_ONLY=1 so the stack was not touched."
  exit 0
fi

cd "$COMPOSE_ROOT"
echo "worldserver-up: gate passed; starting ac-worldserver"
exec docker compose up -d ac-worldserver
