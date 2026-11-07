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
#   --no-deps / --no-deps=*  anywhere: the boot path owns dependency handling,
#                          so the mode can never be something the log does not
#                          name (PRO-306 B1)
#
# `--force-recreate`, `--no-recreate` and `--no-start` are still forwarded when
# they appear after a bare `--` (PRO-179 NB-1 keeps benign flags working), but
# they are also *recorded* there, because they decide what this run may claim
# about the boot (PRO-306 B2).
#   --compose-root / --gate-path
#                          (the gate would run against one root while the boot
#                          `cd`s into another; set COMPOSE_ROOT instead)
#   --no-boot-path-check   (disables check 0b; only allowed with
#                          PREFLIGHT_CHECK_ONLY=1, where nothing boots)
#   --no-dry-run and --no-copy-data are allowed — check 5 is the restart fast path
# docs/dev-environment.md points at — but they print a loud warning.
#
# Boot-path flags (PRO-306 B1/B2) — these are NOT plain compose forwards,
# because each one changes what this script is willing to claim about the boot:
#
#   (default)              compose is started as `up -d --no-deps
#                          ac-worldserver`. `ac-db-import` and
#                          `ac-client-data-init` are service_completed_successfully
#                          dependencies of ac-worldserver, so a plain
#                          `up -d ac-worldserver` re-creates both one-offs and
#                          re-runs the DB updater against the LIVE acore_world on
#                          every boot — including the boots the runbook describes
#                          as "gate passed, read-only checks" (PRO-306 B1; the
#                          same shape PRO-292 recorded). Defaulting to --no-deps
#                          makes that claim true, and the dependency gate below
#                          asserts by hand what compose would otherwise have
#                          done implicitly. The boot log prints `BOOT MODE:`
#                          with the mode it actually ran in.
#   --with-one-offs        the old behaviour: let compose create the one-offs.
#                          Loudly legitimate when you *want* the updater to run
#                          (after a SQL change, or a fresh stack) — the boot log
#                          then says so, and the ledger row count printed
#                          before/after shows whether anything was imported.
#   --force-recreate       already forwarded to compose, but PRO-306 B2 makes it
#                          load-bearing rather than cosmetic: `up -d` on a
#                          running container is a no-op, so "boot and check
#                          LOG_FATAL" without a recreate reads a *lifetime*
#                          counter and reports it as a fresh boot (that is
#                          exactly what PRO-303 caught). The wrapper now records
#                          its own start timestamp, prints the container's
#                          StartedAt next to it, and FAILS a --force-recreate run
#                          whose StartedAt did not actually move.
#   --wait-ready           after the start, wait for the worldserver ready
#                          banner (bounded by READY_TIMEOUT) and report it
#   --require-ready        as --wait-ready, but a missing banner exits non-zero
#   --strict-boot          exit non-zero if any FATAL line appears *since this
#                          boot*. Report-only by default; both the since-boot
#                          and the lifetime FATAL counts are always printed.
#
# Refused outright, in the same spirit as the PRO-179 refusals above:
#   --no-deps / --no-deps=*  the boot path owns dependency handling, so the mode
#                            can never be something the log does not name
#                            (use --with-one-offs for the compose-driven path)
#
# `--no-start` stays allowed (it is a real compose workflow) but the run is then
# labelled NOT A BOOT: no ready-banner wait, no fresh-boot verdict, and the
# FATAL counts are lifetime ones.
#
# Environment:
#   COMPOSE_ROOT          stack root, default the repo root of this script
#   COMPOSE_FILE          honoured, same meaning as for `docker compose`
#   PREFLIGHT_CHECK_ONLY=1  run the gate only: no lease requirement, no stack
#                          touch — CI and merge-gate rehearsal
#   LEASE_FILE            lease path override, default $COMPOSE_ROOT/.stack/lease
#   PYTHON                python interpreter for the gate
#   BOOT_MODE             `no-deps` (default) or `with-one-offs`; same meaning as
#                          the flags. no-deps is what makes the runbook's
#                          "read-only boot" claim true (PRO-306 B1).
#   DEPENDENCY_CHECK_ONLY=1  run the gate and the dependency gate, then stop:
#                          nothing is created or started. The lease is still
#                          required, because the gate reads the live database.
#   RECREATE=1            shorthand for --force-recreate (PRO-306 B2)
#   REQUIRE_READY=1       wait for the ready banner and fail if it never appears
#   STRICT_BOOT=1         fail if any FATAL line appears since this boot
#   READY_TIMEOUT         seconds to wait for the ready banner, default 900
#   DB_SERVICE / DB_USER / DOCKER_DB_ROOT_PASSWORD
#                          how the dependency gate and the ledger counter reach
#                          acore_world — same defaults as the gate itself
#                          (ac-database / root / password)
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

# Refusal of the *boot*, not of the command line: the gate already passed and
# the lease is held, but something about the stack is not safe to start. Kept
# distinct from die (exit 2 = bad usage) so a caller can tell "you asked
# wrongly" from "the stack said no" (PRO-306 B1).
refuse() {
  echo "worldserver-up: REFUSING BOOT — $*" >&2
  exit 3
}

note() {
  echo "worldserver-up: $*"
}

if [[ ! -f "$GATE" ]]; then
  die "pre-flight gate not found at $GATE"
fi

GATE_ARGS=()
COMPOSE_ARGS=()
COMPOSE_FILES=()
COMPOSE_PROJECT=""

# Boot-path state (PRO-306 B1/B2). Defaults are the safe side: the boot that the
# runbook calls read-only is only read-only if compose is kept away from the
# one-offs, and a "boot and check LOG_FATAL" claim is only honest if the
# container actually restarted.
BOOT_MODE="${BOOT_MODE:-no-deps}"
RECREATE="${RECREATE:-0}"
FORCE_RECREATE=0
NO_START=0
WAIT_READY=0
REQUIRE_READY="${REQUIRE_READY:-0}"
STRICT_BOOT="${STRICT_BOOT:-0}"
READY_TIMEOUT="${READY_TIMEOUT:-900}"
# A container started this many seconds before the wrapper is still "this boot";
# anything older is a container we did not start.
FRESH_SLOP_SECONDS="${FRESH_SLOP_SECONDS:-5}"
DB_SERVICE="${DB_SERVICE:-ac-database}"
DB_USER="${DB_USER:-root}"
DB_PASSWORD="${DOCKER_DB_ROOT_PASSWORD:-password}"
LEDGER_ROWS_BEFORE=""

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
    --force-recreate)
      # Forwarded to compose AND remembered: PRO-306 B2 makes the fresh-boot
      # verdict depend on whether a recreate was actually asked for.
      FORCE_RECREATE=1
      COMPOSE_ARGS+=("$arg")
      shift
      ;;
    --no-start)
      NO_START=1
      COMPOSE_ARGS+=("$arg")
      shift
      ;;
    --with-one-offs)
      BOOT_MODE="with-one-offs"
      shift
      ;;
    --no-deps|--no-deps=*)
      die "--no-deps is not accepted here: the boot path owns dependency handling, and the log must always name the mode it ran in. Leave the default (which already implies --no-deps), or pass --with-one-offs to have compose create ac-db-import and ac-client-data-init."
      ;;
    --wait-ready)
      WAIT_READY=1
      shift
      ;;
    --require-ready)
      WAIT_READY=1
      REQUIRE_READY=1
      shift
      ;;
    --strict-boot)
      STRICT_BOOT=1
      shift
      ;;
    --ready-timeout)
      [[ $# -ge 2 ]] || die "--ready-timeout needs a value in seconds"
      READY_TIMEOUT="$2"
      shift 2
      ;;
    --ready-timeout=*)
      READY_TIMEOUT="${arg#*=}"
      shift
      ;;
    --no-recreate|--no-build|--wait|--no-aliases|\
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
          --no-deps*)
            # Same reason as above, for the PRO-306 B1 state specifically: the
            # dependency mode decides what this script is allowed to *claim*
            # about the boot, so it must be a mode the log names rather than
            # something compose was handed quietly.
            die "refusing to forward $1 to docker compose: the boot path owns dependency handling, and the dependency mode has to be named for the log. Leave the default (which already implies --no-deps) or pass --with-one-offs before --."
            ;;
          --force-recreate|--no-recreate|--no-start)
            # Forwarded (PRO-179 NB-1 keeps benign post-`--` flags working), but
            # *also* recorded, because these decide what this run may claim:
            # without this the fresh-boot verdict and the NOT-A-BOOT label would
            # describe a boot that was never asked for (PRO-306 B2).
            case "$1" in
              --force-recreate*) FORCE_RECREATE=1 ;;
              --no-start*) NO_START=1 ;;
            esac
            COMPOSE_ARGS+=("$1")
            # Shift *then* `continue`: the shared tail below would append and shift
            # the same argument a second time, and once `$#` hits 0 `$1` is an
            # unbound-variable error under `set -u`. Continuing without shifting
            # instead spins the loop forever on the same argument.
            shift
            continue
            ;;
          --no-color|--no-ansi|--no-build|--no-aliases|--no-anon|--no-log-prefix)
            # Exact-match allowlist for the benign `--no-*` compose flags that do
            # not change what this run may claim. They are forwarded unrecorded
            # on purpose; anything else in this shape is refused below.
            ;;
          --no-*|--force-*)
            # Refused rather than forwarded (PRO-306 review NB-5). An unknown
            # spelling like `-- --force-rec` or `-- --no-star` used to reach
            # compose unrecorded, and the safety of that path rested entirely on
            # compose's own parser rejecting it — which is an assumption about a
            # binary this script cannot audit. Anything that *looks* like a
            # recreate / dependency / start-shaping flag has to be one this
            # wrapper recognises and can describe in the log, or it does not get
            # near compose.
            die "refusing to forward $1 to docker compose: it is shaped like a recreate/dependency/start flag but is not one this wrapper records, so the boot verdict could not honestly describe it. Use the exact compose flag name, and pass it before -- if the wrapper has a flag for it."
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

# --- compose invocation, built once so every path agrees ------------------- #
# The dependency gate below, the ledger counter and the boot itself must all
# address the *same* project and compose files the gate audited; building the
# base array here and reusing it is what keeps them from drifting (PRO-306 B1).
COMPOSE_BASE=(docker compose)
_cf_i=0
while [[ ${_cf_i} -lt ${#COMPOSE_FILES[@]} ]]; do
  COMPOSE_BASE+=(-f "${COMPOSE_FILES[$_cf_i]}")
  _cf_i=$((_cf_i + 1))
done
if [[ -n "$COMPOSE_PROJECT" ]]; then
  COMPOSE_BASE+=(-p "$COMPOSE_PROJECT")
fi

case "$BOOT_MODE" in
  no-deps|with-one-offs) ;;
  *) die "BOOT_MODE must be 'no-deps' or 'with-one-offs' (got '$BOOT_MODE')" ;;
esac

if [[ "$RECREATE" == "1" && "$FORCE_RECREATE" != "1" ]]; then
  # RECREATE=1 is the env spelling of --force-recreate (PRO-306 B2) so a
  # runbook step can pin it without touching the argument list.
  FORCE_RECREATE=1
  COMPOSE_ARGS+=(--force-recreate)
fi

if [[ "$FORCE_RECREATE" == "1" ]]; then
  for _a in ${COMPOSE_ARGS[@]+"${COMPOSE_ARGS[@]}"}; do
    [[ "$_a" == --no-recreate* ]] || continue
    die "--force-recreate and --no-recreate cancel each other, and the fresh-boot verdict depends on which one was really asked for"
  done
fi

# Contradictory requests are refused at parse time rather than silently cancelled
# later (PRO-306 review B-1). `--no-start` creates the container without starting
# it, so there is no boot to wait a ready banner on and no since-boot log window
# to count FATAL lines in; an earlier revision let exactly that combination run
# and exit 0 with the caller's hard requirement dropped on the floor. Checked
# here rather than in the flag loop so the env spellings (REQUIRE_READY=1,
# STRICT_BOOT=1) are refused by the same code path as the flags.
if [[ "$NO_START" == "1" ]]; then
  if [[ "$REQUIRE_READY" == "1" ]]; then
    die "--require-ready and --no-start contradict each other: nothing starts, so the ready banner can never appear. Drop one: --no-start to create only, or --require-ready to boot and prove readiness."
  fi
  if [[ "$STRICT_BOOT" == "1" ]]; then
    die "--strict-boot and --no-start contradict each other: with nothing started there is no since-boot log window, so a FATAL count would describe a boot that did not happen. Drop one: --no-start to create only, or --strict-boot to boot and fail on FATAL."
  fi
fi

# Numeric knobs are validated before the boot, not at first use (PRO-306 review
# NB-3): `--ready-timeout abc` used to die at `deadline=$(( ... ))` *after*
# ac-worldserver had already been started, with rc=1 — a code the runbook does
# not document (2 usage / 3 refuse / 4 not-a-boot) — and it died before the
# ready wait and the ledger before/after evidence could be printed. Arithmetic
# expansion is also a command-execution path for a value shaped like an array
# subscript, so a non-digit never reaches the arithmetic context at all.
if [[ ! "$READY_TIMEOUT" =~ ^[0-9]+$ ]]; then
  die "READY_TIMEOUT must be a non-negative integer number of seconds (got '$READY_TIMEOUT')"
fi
if [[ ! "$FRESH_SLOP_SECONDS" =~ ^[0-9]+$ ]]; then
  die "FRESH_SLOP_SECONDS must be a non-negative integer number of seconds (got '$FRESH_SLOP_SECONDS')"
fi

compose_ps_q() {
  # `docker compose ps -q <service>` — empty when the service has no container
  # in this project. stdin is closed because `compose ps` is not a reader and a
  # caller piping this script into `ssh 'bash -s'` has nothing else to lose.
  #
  # COMPOSE_BASE already *is* `docker compose [-f …] [-p …]`, so it must not be
  # prefixed with a literal `docker compose` as well: an earlier revision of this
  # helper did exactly that, compose answered with its usage text and exit 1, and
  # because the caller assigns from this function under `set -e` the script died
  # between two log lines with no explanation at all (PRO-306). Failure is
  # reported here instead of returned quietly, and the callers refuse loudly.
  local out
  if ! out="$("${COMPOSE_BASE[@]}" ps -a -q "$1" </dev/null)"; then
    echo "worldserver-up: cannot ask compose about service '$1' (see the compose output above)." >&2
    return 1
  fi
  out="${out//$'\r'/}"
  printf '%s' "${out%%$'\n'*}"
}

db_scalar() {
  # db_scalar <database> <sql> -> the single scalar, whitespace-stripped.
  # Same client argv shape as the gate's MySQL class (ops/preflight/
  # preflight_worlddb.py) so the two never disagree about how to reach the DB,
  # and </dev/null because `docker compose exec -T … mysql` otherwise eats the
  # caller's stdin (the PRO-306 host gotcha, stdin-eating variant).
  local db="$1" sql="$2" out
  # `|| true` and `2>&1` together on purpose: a failing client must not kill the
  # script through `set -e` — the caller decides what an unreadable ledger means
  # — and the client's own error text is the only clue to what went wrong, so it
  # travels back in the value rather than being dropped on the floor.
  out="$("${COMPOSE_BASE[@]}" exec -T -e "MYSQL_PWD=${DB_PASSWORD}" \
        "$DB_SERVICE" mysql --no-defaults -u "$DB_USER" --default-character-set=utf8mb4 \
        -N -B "$db" -e "$sql" </dev/null 2>&1 || true)"
  out="${out//$'\r'/}"
  out="$(printf '%s' "$out" | tr -d '[:space:]')"
  printf '%s' "${out:0:200}"
}

assert_dependencies() {
  # What compose's depends_on was doing implicitly, said out loud (PRO-306 B1).
  # In no-deps mode this is the only thing standing between the worldserver and
  # a cold or half-imported database, so it fails closed; in with-one-offs mode
  # the one-offs are still coming up and a cold stack legitimately has an empty
  # ledger, so the same numbers are reported without refusing.
  local require_ledger="$1" id status health rows inc vol init_id initrc="none" initrunning="unknown"
  note "dependency gate (asserting by hand what depends_on used to do silently)"

  id="$(compose_ps_q "$DB_SERVICE")" \
    || refuse "cannot ask compose about $DB_SERVICE. The dependency gate has to read the stack to say anything about it; an unreadable stack is not a booted one."
  if [[ -z "$id" ]]; then
    refuse "$DB_SERVICE has no container in this project. Bring the database up first (docs/dev-environment.md#build--run); with BOOT_MODE=$BOOT_MODE nothing here creates it."
  fi
  status="$(docker inspect --format '{{.State.Status}}' "$id" </dev/null 2>/dev/null || echo unknown)"
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$id" </dev/null 2>/dev/null || echo unknown)"
  if [[ "$status" != "running" ]]; then
    refuse "$DB_SERVICE is '$status', not running (container $id)."
  fi
  if [[ "$health" == "starting" ]]; then
    refuse "$DB_SERVICE is still health-checking. Wait for it to report healthy, then re-run."
  fi
  if [[ "$health" != "healthy" && "$health" != "no-healthcheck" ]]; then
    refuse "$DB_SERVICE health is '$health'."
  fi
  note "  [dep 1] $DB_SERVICE: status=$status health=$health"

  rows="$(db_scalar acore_world 'SELECT COUNT(*) FROM updates')"
  inc="$(db_scalar acore_world 'SELECT COUNT(*) FROM updates_include')"
  # Only a real number may become the "before" side of the ledger comparison;
  # a client error text in that slot would later read as a changed ledger.
  if [[ "$rows" =~ ^[0-9]+$ ]]; then
    LEDGER_ROWS_BEFORE="$rows"
  fi
  if [[ ! "$rows" =~ ^[0-9]+$ ]]; then
    if [[ "$require_ledger" == "1" ]]; then
      refuse "cannot read acore_world.updates (got '$rows'). BOOT_MODE=no-deps promises the updater will not be run by this boot, so it also has to prove the ledger is already there."
    fi
    note "  [dep 2] acore_world.updates unreadable (got '$rows'); BOOT_MODE=$BOOT_MODE leaves the import to ac-db-import"
  elif [[ "$rows" == "0" || ! "$inc" =~ ^[1-9][0-9]*$ ]]; then
    if [[ "$require_ledger" == "1" ]]; then
      refuse "the updater ledger is not imported in this database (updates=$rows, updates_include=$inc). BOOT_MODE=no-deps starts no ac-db-import, so the worldserver would come up against an unconfigured ledger — boot with --with-one-offs for the first import."
    fi
    note "  [dep 2] updater ledger not populated yet: updates=$rows updates_include=$inc (ac-db-import will do it)"
  else
    note "  [dep 2] updater ledger already imported: acore_world.updates rows=$rows updates_include rows=$inc"
  fi

  # No `| head -n 1`: under `set -euo pipefail` a `head` that closes the pipe
  # early turns the producer into a SIGPIPE failure, and an assignment that
  # fails kills the script with no output at all.
  #
  # The volume name comes from compose, not from a string in this script:
  # docker-compose.yml mounts `${DOCKER_VOL_DATA:-ac-client-data}`, so anyone who
  # sets that knob would otherwise be refused a boot for a volume that does not
  # exist under that name (PRO-306 review NB-1). The docker `name=` filter is an
  # *unanchored* regex, so `name=ac-client-data` also matches an unrelated
  # `…_ac-client-data-v2`; the filter is only a cheap pre-selection and the
  # decision is made by an exact comparison here. A compose-managed volume is
  # normally project-prefixed (`wow-server_ac-client-data`), an external one is
  # not, so both spellings are accepted and nothing else is.
  local vol_name="${DOCKER_VOL_DATA:-ac-client-data}"
  local vol_raw="" c
  if ! vol_raw="$(docker volume ls -q --filter "name=${vol_name}" </dev/null)"; then
    if [[ "$require_ledger" == "1" ]]; then
      refuse "cannot list docker volumes, so this boot cannot confirm $vol_name exists. BOOT_MODE=no-deps starts no ac-client-data-init to create it."
    fi
  fi
  vol_raw="${vol_raw//$'\r'/}"
  vol=""
  while IFS= read -r c; do
    [[ -n "$c" ]] || continue
    c="${c%%/*}"
    if [[ "$c" == "$vol_name" || "$c" == *"_${vol_name}" ]]; then
      vol="$c"
      break
    fi
  done <<<"$vol_raw"
  if [[ -z "$vol" ]]; then
    if [[ "$require_ledger" == "1" ]]; then
      refuse "no $vol_name volume exists. BOOT_MODE=no-deps starts no ac-client-data-init, and ac-worldserver mounts that volume read-only — boot with --with-one-offs to populate it first."
    fi
    note "  [dep 3] no $vol_name volume yet; ac-client-data-init will create it"
  else
    init_id=""
    if ! init_id="$(compose_ps_q ac-client-data-init)"; then
      if [[ "$require_ledger" == "1" ]]; then
        refuse "cannot ask compose about ac-client-data-init, so the last exit code of the one-off that fills $vol_name cannot be verified."
      fi
    fi
    if [[ -z "$init_id" ]]; then
      # The volume exists but the one-off that filled it is gone, so there is no
      # exit code to read. `none` used to fall through as if it satisfied the
      # check (PRO-306 review NB-2): in no-deps mode, where nothing will re-run
      # that one-off, "we cannot check" has to be a refusal, not a pass.
      if [[ "$require_ledger" == "1" ]]; then
        refuse "$vol_name exists but no ac-client-data-init container is present, so nothing can show it completed successfully. BOOT_MODE=no-deps will not re-run it — boot with --with-one-offs to run the one-off again, or remove the volume and let it recreate."
      fi
      note "  [dep 3] client data: volume=$vol ac-client-data-init container absent (exit code unverifiable; compose will re-run the one-off)"
    else
      initrc="$(docker inspect --format '{{.State.ExitCode}}' "$init_id" </dev/null 2>/dev/null || echo unknown)"
      initrunning="$(docker inspect --format '{{.State.Running}}' "$init_id" </dev/null 2>/dev/null || echo unknown)"
      if [[ "$initrunning" == "true" ]]; then
        # `.State.ExitCode` reads 0 for a *running* container, so an in-flight
        # client-data download would otherwise read as a completed one.
        if [[ "$require_ledger" == "1" ]]; then
          refuse "ac-client-data-init is still running, so $vol_name is being written right now and its last exit code is not meaningful yet. Wait for it to finish, then re-run."
        fi
        note "  [dep 3] client data: volume=$vol ac-client-data-init still running (its exit code means nothing yet; compose will wait for it)"
      elif [[ "$initrc" != "0" ]]; then
        refuse "the last ac-client-data-init exited $initrc, so $vol_name may be incomplete. Fix that one-off, or re-run it deliberately with --with-one-offs."
      else
        note "  [dep 3] client data: volume=$vol ac-client-data-init last exit=$initrc"
      fi
    fi
  fi
}

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
  echo "                  escalate to Scuff (SQL); do not bypass this gate." >&2
  exit "$rc"
fi

if [[ "${PREFLIGHT_CHECK_ONLY:-0}" == "1" ]]; then
  echo "worldserver-up: gate passed; PREFLIGHT_CHECK_ONLY=1 so the stack was not touched."
  exit 0
fi

# --- B1: say out loud what this boot does to the one-offs ------------------- #
# The runbook calls a gated boot "read-only checks". That is only true while
# nothing runs the updater against the live world DB, so the mode is printed
# before the boot and the ledger is counted on both sides of it (PRO-306 B1).
# Move into the audited root first: the dependency gate addresses the stack with
# `docker compose`, which has to resolve the same project the gate just read.
cd "$COMPOSE_ROOT"

if [[ "$BOOT_MODE" == "no-deps" ]]; then
  note "BOOT MODE: no-deps — compose will be started as 'up -d --no-deps ac-worldserver'. ac-db-import and ac-client-data-init are NOT (re-)created by this boot and the world-DB updater is NOT run by it; the [dep N] lines below are what this boot relies on instead."
  assert_dependencies 1
else
  note "BOOT MODE: with-one-offs — compose will (re-)create ac-db-import and ac-client-data-init, so the world-DB updater DOES run against the live acore_world during this boot. This boot is NOT read-only, whatever the gate says."
  assert_dependencies 0
fi

# The dependency gate is the part of a boot that reads the live stack, so it is
# also the part worth rehearsing on its own: `DEPENDENCY_CHECK_ONLY=1` answers
# "would this boot be allowed" without creating or starting anything, which is
# what a verification pass needs before it dares a --force-recreate (PRO-306).
if [[ "${DEPENDENCY_CHECK_ONLY:-0}" == "1" ]]; then
  note "DEPENDENCY_CHECK_ONLY=1 — gate and dependency gate above are the whole run. Nothing was created or started."
  exit 0
fi

COMPOSE_CMD=("${COMPOSE_BASE[@]}" up -d)
if [[ "$BOOT_MODE" == "no-deps" ]]; then
  COMPOSE_CMD+=(--no-deps)
fi
if [[ ${#COMPOSE_ARGS[@]} -gt 0 ]]; then
  COMPOSE_CMD+=("${COMPOSE_ARGS[@]}")
fi
COMPOSE_CMD+=(ac-worldserver)
note "gate passed and lease held; starting ac-worldserver"

# Recorded before the start so the fresh-boot verdict and the since-boot FATAL
# window cannot be slid backwards afterwards (PRO-306 B2).
BOOT_T0="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
note "wrapper start (BOOT_T0): $BOOT_T0"

rc=0
# Not `exec` any more (PRO-306 B2): the previous last line of this script was
# `exec docker compose up -d ac-worldserver`, which made it structurally
# impossible to report what actually booted — that is how a container that had
# been Up 50 minutes came to be reported as a verified boot with LOG_FATAL 0.
"${COMPOSE_CMD[@]}" </dev/null || rc=$?
if (( rc != 0 )); then
  echo "worldserver-up: docker compose up failed (exit $rc); the numbers below describe whatever is left running." >&2
fi

# --- B2: prove which boot these numbers belong to --------------------------- #
# `|| ws_id=""` rather than letting the helper's failure reach `set -e`: the
# boot has already happened by this point, and a report that dies halfway is
# how a stack ends up running with no recorded numbers at all.
ws_id="$(compose_ps_q ac-worldserver)" || ws_id=""
ready="not-checked"
fatal_since="not-counted"
fatal_total="not-counted"
ledger_after=""
if [[ -z "$ws_id" ]]; then
  note "ac-worldserver has no container id; nothing to report a boot from."
  # Not a boot, whatever mode asked for it. The `--no-start` exemption an earlier
  # revision carried here let `--no-start --require-ready --strict-boot` with no
  # worldserver container at all exit 0 — a missing container is the strongest
  # possible "this was not a boot", so no flag may talk the wrapper out of saying
  # so (PRO-306 review B-1).
  rc=$(( rc != 0 ? rc : 4 ))
else
  ws_status="$(docker inspect --format '{{.State.Status}}' "$ws_id" </dev/null 2>/dev/null || echo unknown)"
  ws_started="$(docker inspect --format '{{.State.StartedAt}}' "$ws_id" </dev/null 2>/dev/null || echo 0001-01-01T00:00:00Z)"
  ws_restarts="$(docker inspect --format '{{.RestartCount}}' "$ws_id" </dev/null 2>/dev/null || echo unknown)"
  note "ac-worldserver: container=$ws_id status=$ws_status restarts=$ws_restarts"
  # Docker stamps nanoseconds; GNU date does not need them and chokes on some
  # builds, so cut to whole seconds.
  ws_started_s="${ws_started%%.*}"
  [[ "$ws_started_s" == "$ws_started" ]] || ws_started_s="${ws_started_s}Z"
  t0_epoch="$(date -u -d "$BOOT_T0" +%s 2>/dev/null || echo 0)"
  st_epoch="$(date -u -d "$ws_started_s" +%s 2>/dev/null || echo 0)"
  note "boot identity: wrapper start=$BOOT_T0  container StartedAt=$ws_started  (a lifetime counter can now be told from a fresh boot by eye)"
  if [[ "$NO_START" == "1" ]]; then
    note "NOT A BOOT: --no-start was passed, so ac-worldserver was created but not started. The FATAL counts below are lifetime ones."
  elif (( st_epoch + FRESH_SLOP_SECONDS < t0_epoch )); then
    age_s=$(( t0_epoch - st_epoch ))
    note "BOOT VERDICT: NOT A FRESH BOOT — the container was already running $age_s s before this wrapper started. Any LOG_FATAL count below is a LIFETIME counter for a boot someone else started; use --force-recreate (or RECREATE=1) when the runbook asks you to boot and check LOG_FATAL."
    if [[ "$FORCE_RECREATE" == "1" ]]; then
      refuse "--force-recreate was requested and the gate passed, but ac-worldserver's StartedAt ($ws_started) did not move past BOOT_T0 ($BOOT_T0). Reporting that as a boot is what PRO-303 caught; failing instead."
    fi
  else
    note "BOOT VERDICT: fresh boot — container StartedAt is at or after this wrapper's start."
  fi

  # Capture the log stream and its exit status separately (PRO-306 review B-2).
  # Folding stderr into stdout and appending `|| true` — what this did before —
  # turns a *failing* `docker logs` into the literal string "0", which then reads
  # as "zero FATAL lines" to `--strict-boot` and to whoever is reading the run
  # log. A daemon error, a pruned log file or a non-json-file logging driver all
  # reach that path; an unreadable log stream is not evidence of a clean boot, so
  # it stays a word and the STRICT_BOOT guard below fails on any non-number.
  if fatal_since_logs="$(docker logs --since "$BOOT_T0" "$ws_id" </dev/null 2>/dev/null)"; then
    fatal_since="$(printf '%s\n' "$fatal_since_logs" | grep -c 'FATAL' || true)"
  else
    fatal_since="logs-unreadable"
  fi
  if fatal_total_logs="$(docker logs "$ws_id" </dev/null 2>/dev/null)"; then
    fatal_total="$(printf '%s\n' "$fatal_total_logs" | grep -c 'FATAL' || true)"
  else
    fatal_total="logs-unreadable"
  fi
  note "LOG_FATAL since this boot (since=$BOOT_T0): $fatal_since    lifetime (whole container, not a boot measurement): $fatal_total"

  if [[ "$WAIT_READY" == "1" && "$NO_START" != "1" ]]; then
    note "waiting up to ${READY_TIMEOUT}s for the worldserver ready banner '…(worldserver-daemon) ready...'"
    deadline=$(( $(date -u +%s) + READY_TIMEOUT ))
    while (( $(date -u +%s) < deadline )); do
      if docker logs --since "$BOOT_T0" "$ws_id" </dev/null 2>&1 | grep -qE 'worldserver-daemon\) ready\.\.\.'; then
        ready="seen"
        break
      fi
      ws_status="$(docker inspect --format '{{.State.Status}}' "$ws_id" </dev/null 2>/dev/null || echo unknown)"
      if [[ "$ws_status" == "exited" || "$ws_status" == "dead" ]]; then
        ready="container-$ws_status"
        break
      fi
      sleep 5
    done
    [[ "$ready" == "seen" ]] || ready="not-seen-within-${READY_TIMEOUT}s-$ws_status"
    note "ready banner: $ready"
  fi

  ledger_after="$(db_scalar acore_world 'SELECT COUNT(*) FROM updates')"
  if [[ -n "$LEDGER_ROWS_BEFORE" && "$ledger_after" =~ ^[0-9]+$ ]]; then
    if [[ "$LEDGER_ROWS_BEFORE" == "$ledger_after" ]]; then
      note "acore_world.updates rows: before=$LEDGER_ROWS_BEFORE after=$ledger_after (unchanged — no update was imported by this boot)"
    elif [[ "$BOOT_MODE" == "no-deps" ]]; then
      # The contract of no-deps is literally "this boot ran no updater", so a
      # ledger that moved underneath it is a violated contract, not a colour line
      # (PRO-306 review NB-4).
      echo "worldserver-up: REFUSING TO CALL THIS A CLEAN BOOT — acore_world.updates rows changed during a BOOT_MODE=no-deps boot (before=$LEDGER_ROWS_BEFORE after=$ledger_after). Something imported updates while this wrapper started ac-worldserver without them; find the writer before trusting the run." >&2
      rc=$(( rc != 0 ? rc : 4 ))
    else
      note "acore_world.updates rows: before=$LEDGER_ROWS_BEFORE after=$ledger_after — CHANGED, which is what BOOT_MODE=with-one-offs asked for: ac-db-import ran the updater against the live acore_world during this boot."
    fi
  else
    # An unreadable ledger on either side is not the same as an unchanged one.
    # In no-deps mode the whole read-only claim rests on that comparison, so it
    # fails closed; in with-one-offs mode the updater was *meant* to run and the
    # count is diagnostic only.
    echo "worldserver-up: acore_world.updates rows: before='${LEDGER_ROWS_BEFORE:-unknown}' after='${ledger_after:-unknown}' (could not compare)." >&2
    if [[ "$BOOT_MODE" == "no-deps" ]]; then
      echo "worldserver-up: REFUSING TO CALL THIS A READ-ONLY BOOT — BOOT_MODE=no-deps claims no update was imported, and it cannot prove that without a ledger count on both sides." >&2
      rc=$(( rc != 0 ? rc : 4 ))
    fi
  fi
fi

# `--strict-boot` may only pass on a number it actually counted (PRO-306 review
# B-2): `not-counted` (no container to report from) and `logs-unreadable` are both
# "I do not know", and under --strict-boot "I do not know" is a failure.
if [[ "${STRICT_BOOT}" == "1" ]] && { [[ ! "$fatal_since" =~ ^[0-9]+$ ]] || (( fatal_since > 0 )); }; then
  if [[ "$fatal_since" =~ ^[0-9]+$ ]]; then
    echo "worldserver-up: REFUSING TO CALL THIS A BOOT — $fatal_since FATAL line(s) appeared since $BOOT_T0." >&2
  else
    echo "worldserver-up: REFUSING TO CALL THIS A BOOT — --strict-boot cannot check anything: the since-boot FATAL count is '$fatal_since', not a number. A log stream this wrapper cannot read is not proof of a clean boot." >&2
  fi
  rc=$(( rc != 0 ? rc : 4 ))
fi
# No `--no-start` exemption here: the contradictory pair is refused at parse time,
# so reaching this check with --no-start would itself be a bug (PRO-306 review B-1).
if [[ "${REQUIRE_READY}" == "1" && "$ready" != "seen" ]]; then
  echo "worldserver-up: REFUSING TO CALL THIS A BOOT — ready banner '$ready'." >&2
  rc=$(( rc != 0 ? rc : 4 ))
fi

exit "$rc"
