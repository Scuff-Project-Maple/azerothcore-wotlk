#!/usr/bin/env python3
"""Read, classify, take and release the single-writer stack lease.

    python3 ops/deploy/lease_check.py check                       # who holds it, if anyone
    python3 ops/deploy/lease_check.py acquire --agent A --issue PRO-99 --action e2e-suite
    python3 ops/deploy/lease_check.py release --agent A --issue PRO-99
    python3 ops/deploy/lease_check.py verify [PATH] [--label worldserver-up]   # the boot wrapper

This is the **only** lease parser. It exists because there were two, and the weaker one
was the one agents were told to trust: `docs/dev-environment.md#single-writer-lease`
documented its staleness test as a `jq` one-liner,

    jq -er "(.acquiredAt|fromdate)+(.expectedMinutes*60) > (now|floor)" .stack/lease

and `jq`'s `fromdate` accepts only `%Y-%m-%dT%H:%M:%SZ`. A lease writer that emitted a
fractional-second stamp — `datetime.now(timezone.utc).isoformat()` in the ACQUIRE path
that [PRO-356](/PRO/issues/PRO-356) caught on the deploy host — made that predicate raise
a *jq error*, and the `|| { echo LEASE_STALE_BREAKABLE; }` branch cannot tell an error from
an expiry. A live lease 28 min into a 180 min window therefore printed
`LEASE_STALE_BREAKABLE`, which is the doc's one and only guard before an agent `rm -f`s
someone else's lease. The wrapper next to this file parsed the same file with
`datetime.fromisoformat` and reached the right answer, so the two readers disagreed about
whether two writers could land on the one `ac-worldserver` at once.

Two rules follow, and both are enforced here rather than in prose:

  * **One parser.** The doc's CHECK, the boot wrapper and this file all call `assess()`.
    `ops/deploy/test_lease_check.py` fails if the doc or the wrapper grows its own again.
  * **Fail closed.** A timestamp you cannot parse is `LEASE_UNREADABLE`, never
    `LEASE_STALE_BREAKABLE`. Only a lease you can *prove* is past its window may be broken
    by someone who does not hold it; an unreadable one names the holder and stops.

Verdicts and exit codes for `check`:

    0 LEASE_HELD_LIVE        someone is in there — do not proceed
    1 LEASE_FREE             no lease file
    2 LEASE_STALE_BREAKABLE  provably past `acquiredAt + expectedMinutes`
    3 LEASE_UNREADABLE       cannot be judged; not yours to break — see the `action:` line

`acquire` pins the writer side to the whole-second UTC form `date -u +%FT%TZ` produces, so
a hand-written lease and a tool-written one are the same shape; the reader still tolerates
fractional seconds, a `+00:00` offset and a naive stamp, because leases written before this
file existed are already on the host and a stamp the reader cannot parse must not become a
licence to break a live holder.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

LEASE_FREE = "LEASE_FREE"
LEASE_HELD_LIVE = "LEASE_HELD_LIVE"
LEASE_STALE_BREAKABLE = "LEASE_STALE_BREAKABLE"
LEASE_UNREADABLE = "LEASE_UNREADABLE"

#: What `check` exits with, per verdict. A caller that branches on the exit status must be
#: able to tell "expired" from "I could not read it", so they are different codes.
EXIT_FOR_VERDICT = {
    LEASE_HELD_LIVE: 0,
    LEASE_FREE: 1,
    LEASE_STALE_BREAKABLE: 2,
    LEASE_UNREADABLE: 3,
}

#: The one timestamp shape the writer emits (PRO-356 ask 3). Identical to `date -u +%FT%TZ`.
CANONICAL_TIMESTAMP = "%Y-%m-%dT%H:%M:%SZ"

#: The actions docs/dev-environment.md lists. Not enforced — the doc's own list has been
#: exceeded in practice (`pro344-preflight-record-qa`) — but an action outside it is worth
#: saying out loud, because the lease doubles as the audit record of who touched the stack.
DOCUMENTED_ACTIONS = ("worldserver-restart", "e2e-suite", "db-migration", "compose-up")

#: A lease older than this without a release is worth calling out even when it is live:
#: 24 h is longer than any documented `expectedMinutes` by an order of magnitude.
SUSPICIOUSLY_LONG_MINUTES = 24 * 60

_FRACTION = re.compile(r"\.(\d+)")

DEFAULT_LEASE = Path(os.environ.get("LEASE_FILE")
                     or Path(__file__).resolve().parents[2] / ".stack" / "lease")


@dataclass(frozen=True)
class Reading:
    """What `assess()` knows about one lease file."""

    verdict: str
    notes: list = field(default_factory=list)
    raw: str = None
    holder: str = None
    age_min: float = None
    minutes: float = None

    @property
    def minutes_left(self):
        if self.age_min is None or self.minutes is None:
            return None
        return self.minutes - self.age_min


def parse_acquired_at(raw):
    """Tolerant ISO-8601 -> aware UTC datetime. Raises ValueError when it cannot be proven.

    Accepted, because all of them have appeared in a real lease or in a plausible writer:
    `...:14Z`, `...:14.086945Z` (the PRO-356 shape), `...:14.086945+00:00` (what
    `isoformat()` emits for an aware datetime), a 1- to 9-digit fraction, and a naive stamp,
    which is read as UTC because UTC is the only clock the lease has ever used.
    """
    if not isinstance(raw, str):
        raise ValueError(f"acquiredAt is {type(raw).__name__}, not a string")
    text = raw.strip()
    if not text:
        raise ValueError("acquiredAt is empty")
    if text[-1] in "Zz":
        text = text[:-1] + "+00:00"
    match = _FRACTION.search(text)
    if match:
        # Python < 3.11 accepts only a 3- or 6-digit fraction, and this runs on the CI
        # runner's 3.10 as well as the host's 3.12. Normalise to 6 rather than reject.
        text = text[:match.start()] + "." + (match.group(1) + "000000")[:6] + text[match.end():]
    stamp = dt.datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=dt.timezone.utc)
    return stamp


def parse_window(raw):
    """`expectedMinutes` -> float minutes. Raises ValueError when it is not a window."""
    if raw is None or isinstance(raw, bool):
        raise ValueError("expectedMinutes is missing")
    try:
        minutes = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"expectedMinutes={raw!r} is not a number")
    if not math.isfinite(minutes) or minutes <= 0:
        raise ValueError(f"expectedMinutes={raw!r} is not a positive window")
    return minutes


def classify(raw):
    """Classify the text of a lease file. `raw is None` means there is no lease file.

    Every branch that cannot *prove* expiry answers LEASE_UNREADABLE. That is the whole
    point of this function: the old CHECK routed anything it could not parse into the
    breakable bucket, so the way to make a live lease look breakable was to write it with
    an unusual-but-valid timestamp.
    """
    if raw is None:
        return Reading(LEASE_FREE, ["no lease file"])

    try:
        lease = json.loads(raw)
    except ValueError as exc:
        return Reading(LEASE_UNREADABLE,
                       [f"it is not JSON ({exc})",
                        "a half-written file is what a writer that died mid-`noclobber` "
                        "redirect leaves behind, and that writer may still be running"],
                       raw)
    if not isinstance(lease, dict):
        return Reading(LEASE_UNREADABLE,
                       [f"it is a {type(lease).__name__}, not an object"], raw)

    agent = lease.get("agentId")
    if not isinstance(agent, str) or not agent.strip():
        return Reading(LEASE_UNREADABLE,
                       ["it names no holder (`agentId` is missing or empty), so there is "
                        "nobody to ask before breaking it"], raw)
    holder = f"agent {agent} issue {lease.get('issueId')} action {lease.get('action')}"

    raw_at = lease.get("acquiredAt")
    raw_minutes = lease.get("expectedMinutes")
    try:
        acquired = parse_acquired_at(raw_at)
        minutes = parse_window(raw_minutes)
    except ValueError as exc:
        return Reading(LEASE_UNREADABLE,
                       [f"{exc} — the window cannot be computed",
                        f"acquiredAt={raw_at!r} expectedMinutes={raw_minutes!r}"],
                       raw, holder)

    age_min = (dt.datetime.now(dt.timezone.utc) - acquired).total_seconds() / 60.0
    notes = [f"holder: {holder}",
             f"window: {minutes - age_min:.0f} min of the expected {minutes:.0f} min left "
             f"(acquiredAt {raw_at!r}, age {age_min:.1f} min)"]
    if age_min < -1:
        notes.append("note: acquiredAt is in the future — one of the two clocks is skewed, "
                     "which is another reason not to trust an expiry computed from it")
    if minutes > SUSPICIOUSLY_LONG_MINUTES:
        notes.append(f"note: {minutes:.0f} min is far past any documented window; if this is "
                     "yours, consider whether it should have been released")

    if age_min > minutes:
        notes.append(f"expired: held {age_min:.0f} min > expected {minutes:.0f} min")
        return Reading(LEASE_STALE_BREAKABLE, notes, raw, holder, age_min, minutes)
    return Reading(LEASE_HELD_LIVE, notes, raw, holder, age_min, minutes)


def assess(path):
    """Read and classify `path`. An unreadable file is never reported as free or breakable."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return classify(None)
    except IsADirectoryError:
        return Reading(LEASE_UNREADABLE, [f"{path} is a directory, not a lease"])
    except OSError as exc:
        return Reading(LEASE_UNREADABLE, [f"it cannot be read ({exc})"])
    except UnicodeDecodeError as exc:
        return Reading(LEASE_UNREADABLE, [f"it is not UTF-8 text ({exc})"])
    return classify(raw)


def acquire(path, agent, issue, action, minutes):
    """Take the lease with O_EXCL. Returns the payload, or None when one already exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime(CANONICAL_TIMESTAMP)
    payload = {"agentId": agent, "issueId": issue, "action": action,
               "acquiredAt": stamp, "expectedMinutes": minutes}
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o664)
    except FileExistsError:
        return None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return payload


RELEASED = "RELEASED"
NOTHING_TO_RELEASE = "NOTHING_TO_RELEASE"
NOT_YOURS = "NOT_YOURS"


def release(path, agent, issue):
    """Remove the lease, but only if it names this agent + issue.

    Returns `(outcome, reading)`. `outcome` is one of RELEASED, NOTHING_TO_RELEASE,
    NOT_YOURS, or the reading's own verdict for the fail-closed UNREADABLE case — release
    never removes a lease it cannot match, and never removes an unparseable one either,
    because "I cannot read it" is not evidence that it is mine.
    """
    reading = assess(path)
    if reading.verdict == LEASE_FREE:
        return NOTHING_TO_RELEASE, reading
    if reading.verdict == LEASE_UNREADABLE:
        return LEASE_UNREADABLE, reading
    try:
        lease = json.loads(reading.raw)
    except ValueError:  # classify already ruled, but the file may have changed under us
        return LEASE_UNREADABLE, Reading(LEASE_UNREADABLE,
                                        ["it changed under us and no longer parses"], None)
    if lease.get("agentId") != agent or lease.get("issueId") != issue:
        return NOT_YOURS, reading
    try:
        path.unlink()
    except FileNotFoundError:
        return NOTHING_TO_RELEASE, classify(None)
    return RELEASED, reading


def render(reading, path, quiet=False):
    """The agent-facing voice: the verdict token first, then what it means."""
    if quiet:
        return [reading.verdict]
    lines = [reading.verdict, f"lease: {path}"]
    if reading.raw is not None:
        body = reading.raw.rstrip("\n")
        lines.append(f"  raw: {body if body else '<empty file>'}")
    lines += [f"  {note}" for note in reading.notes]
    if reading.verdict == LEASE_UNREADABLE:
        lines.append("  action: do NOT `rm -f` this lease. Unreadable is not the same as "
                     "expired, and the doc's rule is that breaking a lease that is not yours "
                     "needs a comment on **your** issue naming it (`agentId`, `issueId`, "
                     "`acquiredAt`, `expectedMinutes`) — here, plus the holder's owner saying "
                     "it is safe. See docs/dev-environment.md#single-writer-lease.")
    if reading.verdict == LEASE_STALE_BREAKABLE:
        lines.append("  action: breakable. Still name it in a comment on your own issue "
                     "before you `rm -f` it.")
    return lines


def verify(path, label):
    """The boot-gate voice — what `ops/deploy/worldserver-up.sh` says when it refuses.

    Returns the exit status the wrapper should propagate: 0 only for a live lease.
    """
    reading = assess(path)
    prefix = f"{label}: "
    if reading.verdict == LEASE_HELD_LIVE:
        print(f"{prefix}lease held ({reading.holder}), {reading.minutes_left:.0f} min of the "
              f"expected window left.")
        return 0
    if reading.verdict == LEASE_FREE:
        print(f"{prefix}REFUSING BOOT — no single-writer lease at {path}. Take it first "
              f"(docs/dev-environment.md#single-writer-lease); this script verifies the "
              f"lease, it does not take it.", file=sys.stderr)
    elif reading.verdict == LEASE_STALE_BREAKABLE:
        print(f"{prefix}REFUSING BOOT — lease {path} is STALE: held {reading.age_min:.0f} min "
              f"> expected {reading.minutes:.0f} min ({reading.holder}). Renew or release it "
              f"first.", file=sys.stderr)
    else:
        print(f"{prefix}REFUSING BOOT — lease {path} is UNREADABLE, which is not the same as "
              f"breakable: {'; '.join(reading.notes)}. Boot refused.", file=sys.stderr)
    return 2


def build_arg_parser():
    parser = argparse.ArgumentParser(
        prog="lease_check.py",
        description=__doc__.splitlines()[0],
        epilog="Exit codes for `check`: 0 LEASE_HELD_LIVE, 1 LEASE_FREE, "
               "2 LEASE_STALE_BREAKABLE, 3 LEASE_UNREADABLE. "
               "For `release`: 0 released, 1 nothing to release, 3 unreadable (refused), "
               "4 held by someone else. `verify` exits 0 only on a live lease.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_path(sp):
        sp.add_argument("path", nargs="?", default=None,
                        help=f"lease file (default $LEASE_FILE or {DEFAULT_LEASE})")

    check = sub.add_parser("check", help="classify the lease (the doc's CHECK)")
    add_path(check)
    check.add_argument("--quiet", action="store_true", help="print the verdict token only")

    ver = sub.add_parser("verify", help="boot-gate voice (used by worldserver-up.sh)")
    add_path(ver)
    ver.add_argument("--label", default="worldserver-up", help="message prefix")

    acq = sub.add_parser("acquire", help="take the lease (O_EXCL; never overwrites)")
    add_path(acq)
    acq.add_argument("--agent", required=True)
    acq.add_argument("--issue", required=True)
    acq.add_argument("--action", required=True)
    acq.add_argument("--minutes", type=int, default=90)

    rel = sub.add_parser("release", help="release the lease, if it names you")
    add_path(rel)
    rel.add_argument("--agent", required=True)
    rel.add_argument("--issue", required=True)

    return parser


def resolve_path(args):
    return Path(args.path) if args.path else DEFAULT_LEASE


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    path = resolve_path(args)

    if args.command == "check":
        reading = assess(path)
        print("\n".join(render(reading, path, quiet=args.quiet)))
        return EXIT_FOR_VERDICT[reading.verdict]

    if args.command == "verify":
        return verify(path, args.label)

    if args.command == "acquire":
        for name, value in (("--agent", args.agent), ("--issue", args.issue),
                            ("--action", args.action)):
            if not value.strip():
                print(f"{name} is empty", file=sys.stderr)
                return 3
        if args.minutes <= 0:
            print("--minutes must be a positive number of minutes", file=sys.stderr)
            return 3
        payload = acquire(path, args.agent, args.issue, args.action, args.minutes)
        if payload is None:
            reading = assess(path)
            print("\n".join(render(reading, path)))
            print(f"  LEASE_HELD — acquire never overwrites, stale or not. Breaking a live "
                  f"or unreadable lease is a decision, not a flag; see the CHECK output above.")
            # Non-zero whatever the verdict: an acquire that did not acquire never reports
            # success, and LIVE/UNREADABLE must not collapse into the same code as FREE.
            return EXIT_FOR_VERDICT.get(reading.verdict, 1) or 1
        print("LEASE_ACQUIRED")
        print(f"  lease: {path}")
        print(f"  raw: {json.dumps(payload, separators=(',', ':'))}")
        print(f"  acquiredAt is the pinned whole-second form {CANONICAL_TIMESTAMP}; "
              f"release it when the action finishes, including on failure.")
        if args.action not in DOCUMENTED_ACTIONS:
            print(f"  note: action {args.action!r} is outside the documented set "
                  f"({', '.join(DOCUMENTED_ACTIONS)}). Fine, but say what it means in your "
                  f"issue so the holder is identifiable.")
        return 0

    if args.command == "release":
        outcome, reading = release(path, args.agent, args.issue)
        if outcome == RELEASED:
            print("LEASE_RELEASED")
            print(f"  {path} is gone; {reading.holder} was the holder.")
            return 0
        if outcome == NOTHING_TO_RELEASE:
            print("LEASE_FREE")
            print(f"  nothing to release at {path}")
            return 1
        if outcome == LEASE_UNREADABLE:
            print("LEASE_UNREADABLE")
            for line in render(reading, path)[1:]:
                print(line)
            print("  refused: release only removes a lease it can match to you, and it "
                  "cannot match one it cannot read.")
            return 3
        print("LEASE_NOT_YOURS")
        print(f"  lease: {path}")
        if reading.raw is not None:
            print(f"  raw: {reading.raw.rstrip()}")
        for note in reading.notes:
            print(f"  {note}")
        return 4

    return 3


if __name__ == "__main__":
    sys.exit(main())
