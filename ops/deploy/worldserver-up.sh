#!/usr/bin/env bash
# Boot the worldserver only when the PRO-179 pre-flight gate says the updater
# will not LOG_FATAL, silently re-apply a file, violate the write policy, or
# error out on a scratch clone of acore_world.
#
#   usage: ops/deploy/worldserver-up.sh [preflight flags] [compose flags]
#
# Recognised compose flags are forwarded to `docker compose up -d` and, where
# they change what the gate would read, to the gate as well — otherwise every
# `--force-recreate` or `-f other.yml` would be a silent bypass of the gate
# (PRO-179 review, Scuff (Core) in PRO-192). Anything else is forwarded to the
# checker, so `--json /tmp/preflight.json` and `--strict-all` work from here.
#
#   -f, --file FILE        compose file (repeatable; also sent as --compose-file)
#   -p, --project-name NAME compose project (also sent as --compose-project)
#   --force-recreate, --no-recreate, --no-build, --wait, --no-start,
#   --no-aliases, --no-anon, --remove-orphans, --no-color, --quiet,
#   --ignore-buildable, --profile NAME, --scale SVC=N, -d   compose-only
#
# Refused outright (PRO-179 review N-2), because each one would let the boot
# path read something the gate did not audit:
#   -f, --file, --env-file, -p and their --x=y forms AFTER a bare `--`
#                          (everything after `--` is raw-forwarded to compose,
#                          so these would pick a compose file or project the
#                          gate never saw)
#   --compose-root / --gate-path
#                          (the gate would run against one root while the boot
#                          `cd`s into another; set COMPOSE_ROOT instead)
#   --no-boot-path-check   (disables check 0b; only allowed with
#                          PREFLIGHT_CHECK_ONLY=1, where nothing boots)
# --no-dry-run and --no-copy-data are allowed — check 5 is the restart fast path
# docs/dev-environment.md points at — but they print a loud warning.
#
# Environment:
#   COMPOSE_ROOT          stack root, default the repo root of this script
#   COMPOSE_FILE          honoured, same meaning as for `docker compose`
#   PREFLIGHT_CHECK_ONLY=1  run the gate only: no lease requirement, no stack
#                          touch — CI and merge-gate rehearsal
#   LEASE_FILE            lease path override, default $COMPOSE_ROOT/.stack/lease
#   PYTHON                python interpreter for the gate
#
# Order of operations is deliberate: the single-writer lease is verified BEFORE
# the gate, because the gate opens a scratch schema on the database and a
# boot-path writer must be the only one touching that server. The script
# verifies the lease, it does not take it — taking it is the boot path's job,
# see docs/dev-environment.md#single-writer-lease.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
COMPOSE_ROOT="${COMPOSE_ROOT:-$REPO_ROOT}"
GATE="$SCRIPT_DIR/../preflight/preflight_worlddb.py"
PYTHON="${PYTHON:-python3}"
# Resolved after argument parsing, so that a --compose-root given on the
# command line cannot leave the lease check pointing at the default root.
LEASE_FILE="${LEASE_FILE:-}"

usage() {
  # Print the header block up to `set -euo pipefail` rather than a fixed line
  # range: a hard-coded range silently leaks shell into --help whenever the
  # header grows (PRO-179 review N-2).
  sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" \
    | sed -e '/^set -euo pipefail$/d' -e 's/^# \{0,1\}//'
}

die() {
  echo "worldserver-up: $*" >&2
  exit 2
}

if [[ ! -f "$GATE" ]]; then
  die "pre-flight gate not found at $GATE"
fi

GATE_ARGS=()
COMPOSE_ARGS=()
COMPOSE_FILES=()
COMPOSE_PROJECT=""

while [[ $# -gt 0 ]]; do
  arg="$1"
  case "$arg" in
    -h|--help)
      usage
      exit 0
      ;;
    -f|--file|--compose-file)
      [[ $# -ge 2 ]] || die "$arg needs a value"
      COMPOSE_FILES+=("$2")
      GATE_ARGS+=(--compose-file "$2")
      shift 2
      ;;
    --file=*|--compose-file=*)
      val="${arg#*=}"
      COMPOSE_FILES+=("$val")
      GATE_ARGS+=(--compose-file "$val")
      shift
      ;;
    -p|--project-name|--compose-project)
      [[ $# -ge 2 ]] || die "$arg needs a value"
      COMPOSE_PROJECT="$2"
      GATE_ARGS+=(--compose-project "$2")
      shift 2
      ;;
    --project-name=*|--compose-project=*)
      val="${arg#*=}"
      COMPOSE_PROJECT="$val"
      GATE_ARGS+=(--compose-project "$val")
      shift
      ;;
    --scale)
      [[ $# -ge 2 ]] || die "--scale needs SERVICE=NUM"
      COMPOSE_ARGS+=("$arg" "$2")
      shift 2
      ;;
    --scale=*)
      COMPOSE_ARGS+=("$arg")
      shift
      ;;
    --force-recreate|--no-recreate|--no-build|--wait|--no-start|--no-aliases|\
--no-anon|--remove-orphans|--no-color|--quiet|--ignore-buildable|-d)
      COMPOSE_ARGS+=("$arg")
      shift
      ;;
    --profile)
      [[ $# -ge 2 ]] || die "--profile needs a value"
      COMPOSE_ARGS+=("$arg" "$2")
      shift 2
      ;;
    --profile=*)
      COMPOSE_ARGS+=("$arg")
      shift
      ;;
    --compose-root|--compose-root=*|--gate|--gate=*)
      # The gate is invoked with `--compose-root "$COMPOSE_ROOT"` below and the
      # boot happens after `cd "$COMPOSE_ROOT"`. argparse lets the LAST
      # --compose-root win, so a second one here would audit one tree and boot
      # another (PRO-179 review N-2). One source of truth: the variable.
      die "--compose-root/--gate cannot be passed here; set COMPOSE_ROOT (and PYTHON) in the environment so the gate and the boot use the same root"
      ;;
    --no-boot-path-check|--no-boot*|--disable-boot*)
      # Prefix-matched on purpose: argparse accepts unambiguous abbreviations,
      # so `--no-boot` would otherwise slip through this case and still disable
      # check 0b in the gate.
      if [[ "${PREFLIGHT_CHECK_ONLY:-0}" != "1" ]]; then
        die "refusing to boot with the boot-path check disabled: check 0b is what proves the running container reads the audited SQL tree read-only. Run with PREFLIGHT_CHECK_ONLY=1 to check without booting."
      fi
      GATE_ARGS+=("$arg")
      shift
      ;;
    --no-dry-run|--no-dry*|--no-copy-data|--no-copy*)
      echo "worldserver-up: WARNING — $arg skips check 5 (the updater dry run on a scratch clone)." >&2
      echo "                  That is the documented restart fast path; do not use it after a SQL change." >&2
      GATE_ARGS+=("$arg")
      shift
      ;;
    --)
      shift
      while [[ $# -gt 0 ]]; do
        case "$1" in
          -[fp]*|--file*|--env*|--project*|--compose-file*|--compose-project*)
            # Everything after `--` goes to `docker compose` verbatim, so a
            # compose file, env file or project named here would boot without the
            # gate having read it (PRO-179 review N-2). Matched by *shape*, not by
            # exact token: rev-3 refused `-- -f x` but let `-- -f=/tmp/evil.yml`,
            # `-- -f/tmp/evil.yml` and `-- -p=proj` sail through to the boot
            # (PRO-179 review NB-1). Pass them before `--`, where they are also
            # forwarded to the gate.
            die "refusing to forward $1 to docker compose: it selects what boots, and the gate must read exactly that. Pass it before -- instead."
            ;;
        esac
        COMPOSE_ARGS+=("$1")
        shift
      done
      ;;
    *)
      GATE_ARGS+=("$arg")
      shift
      ;;
  esac
done

# Resolved here rather than at the top of the script so an explicit COMPOSE_ROOT
# from the environment and a defaulted one behave identically: the lease always
# lives under the root the gate is about to audit.
if [[ -z "$LEASE_FILE" ]]; then
  LEASE_FILE="$COMPOSE_ROOT/.stack/lease"
fi

# `COMPOSE_FILE` is how the host root of this stack pins its override file
# (scripts/compose-sync.sh writes it). If it is set and no -f was passed, hand
# it to the gate explicitly so the gate reads the same files compose will.
if [[ ${#COMPOSE_FILES[@]} -eq 0 && -n "${COMPOSE_FILE:-}" ]]; then
  IFS=':' read -r -a _cf <<<"$COMPOSE_FILE"
  for f in "${_cf[@]}"; do
    [[ -n "$f" ]] || continue
    COMPOSE_FILES+=("$f")
    GATE_ARGS+=(--compose-file "$f")
  done
fi

# --- single-writer lease --------------------------------------------------- #
# Refuse to boot without a lease someone is actually holding. `PREFLIGHT_CHECK_ONLY=1`
# is the CI/rehearsal escape hatch and skips it, because a check-only run neither
# needs the stack nor starts it. The inner block is not indented: the `PY` heredoc
# terminator has to sit at column 0.
if [[ "${PREFLIGHT_CHECK_ONLY:-0}" == "1" ]]; then
  echo "worldserver-up: PREFLIGHT_CHECK_ONLY=1 — read-only check, lease not required."
else
if [[ ! -f "$LEASE_FILE" ]]; then
  die "REFUSING BOOT — no single-writer lease at $LEASE_FILE. Take it first (docs/dev-environment.md#single-writer-lease); this script verifies the lease, it does not take it."
fi

if ! "$PYTHON" - "$LEASE_FILE" <<'PY'
import datetime as dt
import json
import sys

path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as fh:
        lease = json.load(fh)
except Exception as exc:  # noqa: BLE001 - fail closed on anything unreadable
    print(f"worldserver-up: REFUSING BOOT — lease {path} is unreadable ({exc}).", file=sys.stderr)
    sys.exit(1)

if not isinstance(lease, dict) or not lease.get("agentId"):
    print(f"worldserver-up: REFUSING BOOT — lease {path} has no holder.", file=sys.stderr)
    sys.exit(1)


def parse_ts(raw):
    text = str(raw)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


raw = lease.get("acquiredAt") or ""
minutes = lease.get("expectedMinutes")
try:
    acquired = parse_ts(raw)
    minutes = float(minutes)
except (TypeError, ValueError):
    print(f"worldserver-up: REFUSING BOOT — lease {path} has an unusable "
          f"acquiredAt={raw!r}/expectedMinutes={minutes!r}.", file=sys.stderr)
    sys.exit(1)

age_min = (dt.datetime.now(dt.timezone.utc) - acquired).total_seconds() / 60.0
holder = f"agent {lease.get('agentId')} issue {lease.get('issueId')} action {lease.get('action')}"
if age_min > minutes:
    print(f"worldserver-up: REFUSING BOOT — lease {path} is STALE: held {age_min:.0f} min "
          f"> expected {minutes:.0f} min ({holder}). Renew or release it first.", file=sys.stderr)
    sys.exit(1)
print(f"worldserver-up: lease held ({holder}), {minutes - age_min:.0f} min of the expected "
      f"window left.")
PY
then
  exit 2
fi
fi

echo "worldserver-up: pre-flight gate (compose root $COMPOSE_ROOT)"
rc=0
"$PYTHON" "$GATE" --compose-root "$COMPOSE_ROOT" ${GATE_ARGS[@]+"${GATE_ARGS[@]}"} || rc=$?
if (( rc != 0 )); then
  echo "worldserver-up: REFUSING BOOT — pre-flight failed (exit $rc)." >&2
  echo "worldserver-up: ac-worldserver was not started. Fix the named file(s) or" >&2
  echo "                  escalate to Scuff (SQL); a 'preflight: operational error:' or" >&2
  echo "                  'could not write the --json payload' line from the gate alone is an" >&2
  echo "                  environment or evidence-write failure, not a finding about your SQL." >&2
  echo "                  Any '!!' line from the gate is still a finding about your SQL." >&2
  echo "                  Do not bypass this gate." >&2
  exit "$rc"
fi

if [[ "${PREFLIGHT_CHECK_ONLY:-0}" == "1" ]]; then
  echo "worldserver-up: gate passed; PREFLIGHT_CHECK_ONLY=1 so the stack was not touched."
  exit 0
fi

cd "$COMPOSE_ROOT"
COMPOSE_CMD=(docker compose)
i=0
while [[ ${i} -lt ${#COMPOSE_FILES[@]} ]]; do
  COMPOSE_CMD+=(-f "${COMPOSE_FILES[$i]}")
  i=$((i + 1))
done
if [[ -n "$COMPOSE_PROJECT" ]]; then
  COMPOSE_CMD+=(-p "$COMPOSE_PROJECT")
fi
COMPOSE_CMD+=(up -d)
if [[ ${#COMPOSE_ARGS[@]} -gt 0 ]]; then
  COMPOSE_CMD+=("${COMPOSE_ARGS[@]}")
fi
COMPOSE_CMD+=(ac-worldserver)
echo "worldserver-up: gate passed and lease held; starting ac-worldserver"
exec "${COMPOSE_CMD[@]}"
