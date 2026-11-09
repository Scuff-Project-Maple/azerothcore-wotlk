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

# Status lines are presentation, never verdict. A builtin write error is a failing
# command under `set -e`, so an unguarded `echo` aimed at `/dev/full`, a closed fd 1
# or a dead pager aborted this script where it stood — before `"$PYTHON" "$GATE"`,
# so the gate never ran, no `REFUSING BOOT` sentence was printed and no `--json`
# payload was written (PRO-353). The verdict half of this script is the gate's exit
# status and the payload the gate writes; these lines only narrate it, so a stream
# that refuses them is allowed to swallow them and nothing else.
say() {
  printf '%s\n' "worldserver-up: $*" 2>/dev/null || :
}

# The same guarantee on fd 2, for the lines an operator reads when a boot is
# refused. `>&2` duplicates fd 1 from the descriptor fd 2 has *at that moment*, then
# `2>/dev/null` moves fd 2 away, so bash's own `write error` complaint is what gets
# silenced and the sentence still reaches the terminal's stderr.
tell() {
  { printf '%s\n' "worldserver-up: $*"; } >&2 2>/dev/null || :
}

die() {
  tell "$*"
  exit 2
}

# Did the gate print a verdict about the SQL, as opposed to a notice about its own
# plumbing? `render()` prints every FAIL as four spaces then `!! `, and the degraded
# listing keeps that indent (`    !! [check] ...`); the one `!!` the operator must NOT be
# sent to Scuff (SQL) for is `!! render() raised ...`, which is the gate describing its
# own presentation failure rather than a finding about the SQL. Leading whitespace is
# tolerated so a reindent inside the gate cannot quietly switch the escalation off; the
# pin that renders a real verdict through the real `main()` is what catches drift in the
# marker itself.
gate_printed_a_finding() {
  # Each file is probed on its own and only when it is non-empty, so a capture that is
  # simply gone — the tmpfiles-daemon-reclaimed-it-underneath-a-long-run shape, or the
  # dir removed under the arms — reads as "no finding here" instead of printing a raw
  # `awk: cannot open ...` in the middle of the refusal (PRO-381 B3; the PRO-308/PRO-313
  # "keep the tool's own diagnostics out of the operator's refusal" rule).
  local f
  for f in "$@"; do
    [[ -s "$f" ]] || continue
    if awk '
      /^[[:space:]]*!![[:space:]]+render\(\)[[:space:]]+raised / { next }
      /^[[:space:]]*!![[:space:]]/                                { found = 1 }
      END                                                         { exit !found }
    ' "$f" 2>/dev/null; then
      return 0
    fi
  done
  return 1
}

# The gate's own two stderr prefixes for "something about my environment went wrong".
# Matched as fixed strings because the gate emits them verbatim, not formatted:
# `preflight: operational error: {exc}` and `preflight: could not write the --json
# payload to {path}: ...`.
gate_printed() {
  local needle="$1"
  shift
  grep -qF -- "$needle" "$@" 2>/dev/null
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
      tell "WARNING — $arg skips check 5 (the updater dry run on a scratch clone)."
      tell "That is the documented restart fast path; do not use it after a SQL change."
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
  say "PREFLIGHT_CHECK_ONLY=1 — read-only check, lease not required."
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
# Every other line this heredoc prints already aims at fd 2; the held-lease line
# used to go to stdout, where a stream that refuses us is fatal in the wrong
# direction (PRO-353). The buffer cannot be written, CPython retries it during
# shutdown, `Py_Exit()` turns the failed final flush into status 120, and the
# `if ! "$PYTHON" ...` above reads any non-zero status as "the lease is bad" — so a
# *held* lease was refused by a `/dev/full` rather than by its own expiry. This is the
# wrapper's twin of `_retire_broken_stream()`/`_RetiredStream` in
# ops/preflight/preflight_worlddb.py (PRO-325/PRO-335), and it keeps both halves: a
# guarded `flush()`/`close()` *and* a rebind. The close is what drops the teardown
# status back — `flush_std_files()` skips a stream that reports itself closed — and the
# rebind is what makes the guarantee structural rather than ordering-dependent, so a
# line added here later finds a sink that absorbs it instead of a `lost sys.stderr`
# that the lease check would answer with rc 1. Closing is only safe because CPython
# builds the std streams with `closefd=False`; if a future interpreter builds them with
# `closefd=True`, retiring must stop closing (same note in the gate's docstring).
# Narration must never decide the verdict, so nothing here raises.
class _RetiredStream:
    """Absorb anything written to a stream the operator's shell already refused."""

    errors = "backslashreplace"
    closed = True

    def __init__(self, name, encoding="utf-8"):
        self.name = name
        self.encoding = encoding

    def write(self, s):
        return len(s)

    def writelines(self, lines):
        return None

    def flush(self):
        return None

    def isatty(self):
        return False

    def close(self):
        return None


def say(text):
    stream = sys.stderr
    try:
        print(text, file=stream, flush=True)
    except Exception:  # noqa: BLE001 - a refused stream is not a lease verdict
        pass
    for method in ("flush", "close"):
        try:
            getattr(stream, method)()
        except Exception:  # noqa: BLE001
            pass
    if sys.stderr is stream:
        sys.stderr = _RetiredStream("<stderr> (retired: it refused the lease line)")


say(f"worldserver-up: lease held ({holder}), {minutes - age_min:.0f} min of the expected "
    f"window left.")
PY
then
  exit 2
fi
fi

say "pre-flight gate (compose root $COMPOSE_ROOT)"

# The advice under a refusal has to answer the one question the operator is actually
# asking — is this my SQL? — and the only honest source for that answer is what the gate
# printed, not the number it exited with. rc 2 covers both "the evidence payload could not
# be written" and "the environment is broken"; rc 1 is a verdict. PRO-338/PRO-331 printed
# one unconditional block for every non-zero rc, so a `--json` aimed at an unwritable
# directory — a run whose own last line says it is not a finding about anyone's SQL — was
# answered two lines later with "escalate to Scuff (SQL)" about the payload (PRO-370 F1).
# So capture both streams, read them, then replay each to the fd it came from. Deferring
# the bytes costs nothing live: the gate assembles its whole verdict before it prints any
# of it, and every child process it runs has its pipes captured.
# Capture is presentation, not the verdict (the PRO-325/PRO-353 rule): the gate run *is*
# the verdict, so a scratch dir we cannot make must not cost the operator a run that would
# have passed. `mktemp -d` is exactly the call that dies on `No space left on device` — the
# PRO-351 host shape — so it is best-effort: on success we read the bytes and route on them;
# on failure the gate speaks straight to the operator's own descriptors and we route on rc
# alone, rather than dying before the gate ever ran (PRO-381 B2: an unwritable/full TMPDIR
# used to answer a run that should have printed RESULT: PASS with rc 2 and no payload).
if GATE_DIR="$(mktemp -d 2>/dev/null)"; then
  GATE_OUT="$GATE_DIR/stdout"
  GATE_ERR="$GATE_DIR/stderr"
  # EXIT owns the removal, and it runs *after* the advice arms have read the capture.
  # INT/TERM only leave — they must not clean up and then fall through, or a signal that
  # used to abort the boot starts letting it happen: the trap-replaced handler returns,
  # the gate child (never signalled) finishes rc 0, and the script walks straight into
  # `docker compose up -d ac-worldserver`. That is the fail-open PRO-381 B1 caught under
  # `docker stop` / `systemd stop` / a plain `kill -TERM`, all of which signal only this
  # script. Forwarding the signal to the gate's pid is a bonus, not required for that.
  trap 'rm -rf "$GATE_DIR"' EXIT
else
  GATE_DIR=""
  GATE_OUT=/dev/null
  GATE_ERR=/dev/null
fi
trap 'exit 130' INT
trap 'exit 143' TERM

rc=0
# `if` rather than `|| rc=$?` so errexit stays out of the way without a `set +e` window.
if [[ -n "$GATE_DIR" ]]; then
  if "$PYTHON" "$GATE" --compose-root "$COMPOSE_ROOT" ${GATE_ARGS[@]+"${GATE_ARGS[@]}"} \
       >"$GATE_OUT" 2>"$GATE_ERR"; then
    rc=0
  else
    rc=$?
  fi
else
  # No capture: the gate writes to our own stdout/stderr, exactly as it did before the
  # capture existed, so the operator still gets RESULT:, the `!!` lines, and any notice.
  if "$PYTHON" "$GATE" --compose-root "$COMPOSE_ROOT" ${GATE_ARGS[@]+"${GATE_ARGS[@]}"}; then
    rc=0
  else
    rc=$?
  fi
fi
# The operator still sees everything the gate said, on the stream it said it on — but only
# when we actually captured it. With no capture the gate already wrote straight to those
# descriptors, and replaying /dev/null would only add noise. The guard on the replay is the
# PRO-353 one reused: `>&2` dups fd 1 from what fd 2 is at that moment, then
# `2>/dev/null` moves fd 2, so it is `cat`'s own "write error" complaint that gets
# silenced and the gate's bytes that still reach the terminal. `|| true` for the same
# reason — a terminal that refuses the replay must not replace the gate's rc with the
# status of `cat`; the PRO-325/PRO-353 contract is that presentation may fail, the verdict
# may not.
if [[ -n "$GATE_DIR" ]]; then
  cat "$GATE_OUT" 2>/dev/null || true
  cat "$GATE_ERR" >&2 2>/dev/null || true
fi

if (( rc != 0 )); then
  tell "REFUSING BOOT — pre-flight failed (exit $rc)."
  has_finding=0
  has_payload_failure=0
  has_operational_error=0
  has_policy_missing=0
  # The markers can only be read when there is a capture to read. With no capture every
  # flag stays 0 and routing falls to the rc-only rules below — which is the honest
  # degradation: the wrapper can still tell a verdict (rc 1) from an environment failure,
  # it just cannot name which environment failure it saw.
  if [[ -n "$GATE_DIR" ]]; then
    if gate_printed_a_finding "$GATE_OUT" "$GATE_ERR"; then
      has_finding=1
    fi
    if gate_printed "could not write the --json payload" "$GATE_OUT" "$GATE_ERR"; then
      has_payload_failure=1
    fi
    if gate_printed "preflight: operational error:" "$GATE_OUT" "$GATE_ERR"; then
      has_operational_error=1
    fi
    if gate_printed "preflight: policy file not found:" "$GATE_OUT" "$GATE_ERR"; then
      has_policy_missing=1
    fi
  fi

  # rc 1 IS a verdict even if the capture came back empty — a terminal may have refused the
  # bytes on the way here — so the SQL routing keys on rc as well as on the marker. The
  # marker is what adds: a run that carried real findings AND lost its payload exits 2, and
  # still has to reach Scuff (SQL).
  if (( has_finding == 1 || rc == 1 )); then
    tell "ac-worldserver was not started. Fix the named file(s) or"
    tell "                  escalate to Scuff (SQL)."
  fi
  if (( has_payload_failure == 1 )); then
    # The first half is always safe: it names the payload problem and points at the path,
    # which is true whether or not the run also carried SQL findings.
    tell "the run asked for a --json evidence payload and the gate could not write it."
    tell "                  Its own line above names the path; the directory has to exist" >&2
    tell "                  and be writable by this user. Fix the path and re-run."
    if (( has_finding == 0 )); then
      # The closing disclaimer — "not a finding about your SQL, the SQL lane is not where
      # it belongs" — is only honest when nothing on screen contradicts it. When a `!!`
      # finding is also present the escalation above just sent the operator to Scuff (SQL);
      # printing "the SQL lane is not where it belongs" two lines later is the PRO-370
      # contradiction one level up, so this arm ends at `Fix the path and re-run.` instead
      # (PRO-381 review NB).
      tell "                  This is an evidence-write failure, not a finding about your"
      tell "                  SQL, so the SQL lane is not where it belongs."
    fi
  fi
  if (( has_operational_error == 1 )); then
    tell "a 'preflight: operational error:' line is the gate failing to reach or set up"
    tell "                  its environment — usually ac-database. Nothing was started,"
    tell "                  and this is not a finding about your SQL."
  fi
  if (( has_policy_missing == 1 )); then
    # `preflight: policy file not found: {path}` (preflight_worlddb.py) is as
    # unambiguously environmental as the operational error — a mis-deployed checkout is
    # missing its policy JSON — so it gets its own arm rather than the unclassified one,
    # and like the other environment arms it never routes the operator to the SQL lane
    # (PRO-381 review NB).
    tell "the gate could not find its policy file, so the checkout it was called from is"
    tell "                  incomplete or mis-deployed. Nothing was started, and this is"
    tell "                  not a finding about your SQL."
  fi
  if (( has_finding == 0 && has_payload_failure == 0 && has_operational_error == 0 \
        && has_policy_missing == 0 && rc != 1 )); then
    # Worded so it cannot misroute in either direction: the wrapper saw no finding line, but
    # it also cannot prove there was one it failed to capture.
    tell "ac-worldserver was not started, and the gate printed neither a finding line nor"
    tell "                  one of its own failure notices, so the wrapper cannot say"
    tell "                  whether this is about your SQL. A '!!' line in the gate's output"
    tell "                  above is a finding about your SQL; a line about the gate's own"
    tell "                  environment is not."
  fi
  tell "                  Do not bypass this gate."
  exit "$rc"
fi

# Reached only on a pass. Remove the capture now rather than at EXIT so a long boot's
# scratch dir is not held for the life of the script, then stand the traps down so the
# compose path runs with default signal behaviour (a `docker stop` after this point should
# hit compose, not be swallowed here).
if [[ -n "$GATE_DIR" ]]; then
  rm -rf "$GATE_DIR"
  trap - EXIT
fi
trap - INT TERM

if [[ "${PREFLIGHT_CHECK_ONLY:-0}" == "1" ]]; then
  say "gate passed; PREFLIGHT_CHECK_ONLY=1 so the stack was not touched."
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
say "gate passed and lease held; starting ac-worldserver"
exec "${COMPOSE_CMD[@]}"
