#!/usr/bin/env python3
"""PRO-179 pre-flight gate for the AzerothCore world-DB updater.

Runs BEFORE `docker compose up -d ac-worldserver` and refuses the boot when the
four updater-included directories under data/sql would make UpdateFetcher
LOG_FATAL, silently re-apply a file, violate the company write policy, or fail
against a scratch clone of acore_world.

References (this tree):
  src/server/database/Updater/UpdateFetcher.cpp:96    duplicate basename -> LOG_FATAL
  src/server/database/Updater/UpdateFetcher.cpp:216   ReadSQLUpdate() = raw file bytes
  src/server/database/Updater/UpdateFetcher.cpp:297   hash = ByteArrayToHexStr(SHA1(raw bytes))
  src/common/Utilities/Util.cpp:545                   ByteArrayToHexStr -> uppercase %02X
  src/server/database/Updater/UpdateFetcher.cpp:350   hash mismatch -> silent re-apply

The live acore_world is only ever read. Every write goes to a scratch schema.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_ERROR = 2

# Hard-coded (PRO-179 review N5): the scratch schema name is validated against this
# immediately before the run's `DROP DATABASE`, so it must not be operator-settable.
SCRATCH_PREFIX = "acore_world_preflight"
SCRATCH_NAME_RE = re.compile(r"^acore_world_preflight_[A-Za-z0-9_]{1,48}$")

IDENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")
_UNSET = object()  # "not computed yet" for cached lookups that may cache None
NAME_TOKEN = r"`?[A-Za-z0-9_$]+`?"


# --------------------------------------------------------------------------- #
# report plumbing
# --------------------------------------------------------------------------- #


@dataclass
class Finding:
    check: str
    severity: str  # FAIL | WARN | INFO
    message: str
    file: str | None = None
    line: int | None = None
    rule: str | None = None


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    sections: dict[str, list[str]] = field(default_factory=dict)
    data: dict = field(default_factory=dict)

    def fail(self, check, message, **kw):
        self.findings.append(Finding(check, "FAIL", message, **kw))

    def warn(self, check, message, **kw):
        self.findings.append(Finding(check, "WARN", message, **kw))

    def info(self, check, message, **kw):
        self.findings.append(Finding(check, "INFO", message, **kw))

    def line(self, check, text):
        self.sections.setdefault(check, []).append(text)

    @property
    def failed(self) -> bool:
        return any(f.severity == "FAIL" for f in self.findings)


class OperationalError(Exception):
    pass


# --------------------------------------------------------------------------- #
# mysql access
# --------------------------------------------------------------------------- #


class MySQL:
    """Thin shell-out wrapper: docker compose exec into the DB container, or a raw client."""

    def __init__(self, args):
        self.mode = args.db_access
        self.compose_root = Path(args.compose_root)
        self.compose_project = args.compose_project
        self.compose_files = args.compose_file or []
        self.service = args.db_service
        self.container = args.db_container
        self.user = args.db_user
        self.password = args.db_password
        self.command_template = args.mysql_command
        self.timeout = args.db_timeout

    # -- command builders ---------------------------------------------------- #

    def _mysql_argv(self, extra: list[str]) -> list[str]:
        if self.mode == "command":
            base = shlex.split(self.command_template)
            return base + extra
        argv = ["docker", "compose"]
        for f in self.compose_files:
            argv += ["-f", f]
        if self.compose_project:
            argv += ["-p", self.compose_project]
        target = self.container or self.service
        argv += ["exec", "-T", "-e", f"MYSQL_PWD={self.password}", target, "mysql"]
        argv += ["--no-defaults", "-u", self.user, "--default-character-set=utf8mb4"]
        return argv + extra

    def _dump_argv(self, database: str, no_data: bool) -> list[str]:
        if self.mode == "command":
            tpl = self.command_template.replace("mysql", "mysqldump", 1)
            base = shlex.split(tpl)
        else:
            argv = ["docker", "compose"]
            for f in self.compose_files:
                argv += ["-f", f]
            if self.compose_project:
                argv += ["-p", self.compose_project]
            target = self.container or self.service
            argv += ["exec", "-T", "-e", f"MYSQL_PWD={self.password}", target, "mysqldump"]
            argv += ["--no-defaults", "-u", self.user]
            base = argv
        base += [
            "--no-tablespaces",
            "--default-character-set=utf8mb4",
            "--skip-triggers",
            "--skip-comments",
            "--single-transaction",
        ]
        if no_data:
            base.append("--no-data")
        return base + [database]

    # -- execution ----------------------------------------------------------- #

    def _run(self, argv, stdin: bytes | None = None, timeout: int | None = None):
        try:
            proc = subprocess.run(
                argv, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout or self.timeout, cwd=str(self.compose_root),
            )
        except FileNotFoundError as exc:
            raise OperationalError(f"cannot execute {argv[0]}: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OperationalError(f"timed out after {timeout or self.timeout}s: {' '.join(argv[:6])} ...") from exc
        out = proc.stdout.decode("utf-8", "replace")
        err = proc.stderr.decode("utf-8", "replace")
        err = "\n".join(
            l for l in err.splitlines()
            if "Using a password" not in l and "mysql: [Warning]" not in l
        ).strip()
        return proc.returncode, out, err

    def query(self, sql: str, database: str | None = None) -> list[list[str]]:
        argv = self._mysql_argv(["-N", "-B"])
        if database:
            argv.append(database)
        argv += ["-e", sql]
        rc, out, err = self._run(argv)
        if rc != 0:
            raise OperationalError(f"query failed ({rc}): {err or out}\n  sql: {sql[:200]}")
        rows = []
        for raw in out.splitlines():
            if raw == "":
                continue
            rows.append(raw.split("\t"))
        return rows

    def execute(self, sql: str, database: str | None = None) -> tuple[int, str, str]:
        argv = self._mysql_argv([])
        if database:
            argv.append(database)
        return self._run(argv, stdin=(sql + "\n").encode("utf-8"))

    def execute_script(self, payload: bytes, database: str | None = None) -> tuple[int, str, str]:
        argv = self._mysql_argv([])
        if database:
            argv.append(database)
        return self._run(argv, stdin=payload)

    def execute_file(self, path: Path, database: str | None = None,
                     fk_checks: bool = True) -> tuple[int, str, str]:
        prefix = b"" if fk_checks else b"SET SESSION FOREIGN_KEY_CHECKS=0;\n"
        return self.execute_script(prefix + path.read_bytes(), database)

    def restore_pipe(self, dump_argv: list[str], database: str) -> tuple[int, str, str]:
        """mysqldump | mysql  (server-side pipe, no 400 MB in python memory)."""
        restore = self._mysql_argv([]) + [database]
        shell = "set -o pipefail; " + " ".join(
            shlex.quote(a) for a in dump_argv
        ) + " | " + " ".join(shlex.quote(a) for a in restore)
        try:
            proc = subprocess.run(["bash", "-lc", shell], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=self.timeout * 4,
                                  cwd=str(self.compose_root))
        except subprocess.TimeoutExpired as exc:
            raise OperationalError(f"clone timed out after {self.timeout * 4}s") from exc
        err = proc.stderr.decode("utf-8", "replace")
        err = "\n".join(l for l in err.splitlines() if "Using a password" not in l).strip()
        return proc.returncode, proc.stdout.decode("utf-8", "replace"), err

    def ping(self) -> bool:
        try:
            self.query("SELECT 1")
            return True
        except OperationalError:
            return False


# --------------------------------------------------------------------------- #
# SQL text scanning (comment/quote aware, offset preserving)
# --------------------------------------------------------------------------- #


def mask_sql(text: str) -> str:
    """Blank comments and string bodies, keeping every offset.

    /*! ... */ stays live (mysql executes it). String bodies are blanked too
    (PRO-179 review N-1): keyword rules such as `insert-select` and
    `or-in-where` scan the masked text, so a page_text value like
    'Please select your destination;' used to read as SQL. Backticked
    identifiers are NOT blanked — table and column names are read out of the
    masked text by the parsers below.
    """
    out = list(text)
    i, n = 0, len(text)
    state = None  # 'sq' | 'dq' | 'bt' | 'line' | 'block'
    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if state is None:
            if c == "'":
                state = "sq"
            elif c == '"':
                state = "dq"
            elif c == "`":
                state = "bt"
            elif c == "-" and nxt == "-":
                state = "line"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            elif c == "#":
                state = "line"
                out[i] = " "
            elif c == "/" and nxt == "*":
                if text[i + 2:i + 3] == "!":
                    # /*!40000 ... */ is executable SQL for the client: blank the fences only
                    out[i] = out[i + 1] = out[i + 2] = " "
                    i += 3
                    continue
                state = "block"
                out[i] = out[i + 1] = " "
                i += 2
                continue
            elif c == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                i += 2
                continue
            i += 1
            continue
        if state == "sq":
            if c == "\\":
                out[i] = " "
                if i + 1 < n:
                    out[i + 1] = " " if text[i + 1] != "\n" else "\n"
                i += 2
                continue
            if c == "'":
                if nxt == "'":
                    # escaped quote inside the literal: blank BOTH characters so
                    # the mask keeps exactly one quote per string boundary and
                    # every offset-based splitter stays balanced (PRO-179 rev-3)
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                state = None
            elif c != "\n":
                out[i] = " "
            i += 1
            continue
        if state == "dq":
            if c == "\\":
                out[i] = " "
                if i + 1 < n and text[i + 1] != "\n":
                    out[i + 1] = " "
                i += 2
                continue
            if c == '"':
                if nxt == '"':
                    out[i] = out[i + 1] = " "
                    i += 2
                    continue
                state = None
            elif c != "\n":
                out[i] = " "
            i += 1
            continue
        if state == "bt":
            if c == "`":
                state = None
            i += 1
            continue
        if state == "line":
            if c == "\n":
                state = None
            else:
                out[i] = " "
            i += 1
            continue
        if state == "block":
            if c == "*" and nxt == "/":
                out[i] = out[i + 1] = " "
                state = None
                i += 2
                continue
            if c != "\n":
                out[i] = " "
            i += 1
            continue
    return "".join(out)


def split_statements(text: str) -> list[tuple[str, int]]:
    """Return [(statement, start_offset)] using the updater-visible statement boundaries."""
    masked = mask_sql(text)
    statements: list[tuple[str, int]] = []
    delimiter = ";"
    i, n = 0, len(masked)
    start = 0
    state = None
    while i < n:
        c = masked[i]
        if state is None:
            if c == "'":
                state = "sq"
            elif c == '"':
                state = "dq"
            elif c == "`":
                state = "bt"
            elif masked[i:i + len(delimiter)] == delimiter:
                stmt = text[start:i]
                stripped = stmt.strip()
                if stripped.lower().startswith("delimiter"):
                    # mysql client directive: the real delimiter is the rest of the line
                    line_start = text.rfind("\n", 0, start) + 1
                    line_end = text.find("\n", i)
                    if line_end == -1:
                        line_end = n
                    new = text[line_start:line_end].strip()[len("delimiter"):].strip().strip("'\"")
                    if new:
                        delimiter = new
                    i = line_end + 1 if line_end < n else n
                    start = i
                    continue
                if stripped:
                    statements.append((stmt, start))
                i += len(delimiter)
                start = i
                continue
            i += 1
            continue
        if state == "sq":
            if c == "\\":
                i += 2
                continue
            if c == "'":
                if masked[i + 1:i + 2] == "'":
                    i += 2
                    continue
                state = None
            i += 1
            continue
        if state == "dq":
            if c == '"':
                if masked[i + 1:i + 2] == '"':
                    i += 2
                    continue
                state = None
            i += 1
            continue
        if state == "bt":
            if c == "`":
                state = None
            i += 1
            continue
    tail = text[start:]
    if tail.strip():
        statements.append((tail, start))
    return statements


def unquote_ident(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == "`" and token[-1] == "`":
        return token[1:-1]
    return token


def split_qualified(token: str) -> tuple[str | None, str]:
    token = token.strip()
    if token.count(".") == 1:
        db, tbl = token.split(".", 1)
        return unquote_ident(db), unquote_ident(tbl)
    return None, unquote_ident(token)


def paren_depths(masked: str) -> list[int]:
    """Bracket depth at every offset (len + 1 entries).

    For a `(` the depth recorded is the one outside it; for a `)` the depth
    recorded is the one after it closes. Clamped at 0 so unbalanced text cannot
    go negative.
    """
    depths = [0] * (len(masked) + 1)
    d = 0
    for i, c in enumerate(masked):
        if c == ")":
            d = d - 1 if d > 0 else 0
        depths[i] = d
        if c == "(":
            d += 1
    depths[len(masked)] = d
    return depths


def top_level_positions(masked: str, word: str) -> list[int]:
    """Offsets of a bare keyword at bracket depth 0, outside quotes.

    Depth matters for locating clause boundaries: in
    `UPDATE t SET a = (SELECT x FROM s WHERE k = 1) WHERE entry = 9002020`
    the first WHERE is inside the SET clause, and treating it as the statement
    WHERE would slice the predicate region out of the wrong place (PRO-179
    review B-2 family). The masked text has comments and string bodies blanked,
    so a keyword inside a literal is never seen here.
    """
    pat = re.compile(r"(?<![A-Za-z0-9_$])" + word + r"(?![A-Za-z0-9_$])", re.IGNORECASE)
    depths = paren_depths(masked)
    return [m.start() for m in pat.finditer(masked) if depths[m.start()] == 0]


def split_top_level(text: str, masked: str, sep: str) -> list[str]:
    parts, depth, last = [], 0, 0
    i, n = 0, len(text)
    inq = None
    while i < n:
        c = text[i]
        if inq:
            if inq == "`":
                if c == "`":
                    inq = None
            elif c == "\\":
                i += 2
                continue
            elif c == inq:
                inq = None
            i += 1
            continue
        if c in "'\"`":
            inq = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif depth == 0 and text[i:i + len(sep)] == sep:
            parts.append(text[last:i])
            i += len(sep)
            last = i
            continue
        i += 1
    parts.append(text[last:])
    return parts


LITERAL_RE = re.compile(r"""^\s*(?:(?P<num>-?\d+(?:\.\d+)?)|'(?P<str>(?:[^']|'')*)'|(?P<other>[^,()]*))\s*$""",
                        re.DOTALL)


def literal_value(token: str):
    """Return (kind, value). kind in {'int','float','string','unknown'}."""
    m = LITERAL_RE.match(token)
    if not m:
        return "unknown", None
    if m.group("num") is not None:
        raw = m.group("num")
        return ("float", float(raw)) if "." in raw else ("int", int(raw))
    if m.group("str") is not None:
        return "string", m.group("str")
    return "unknown", None


SUBQUERY_HEAD_RE = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
BOOL_KW_RE = re.compile(r"(?<![A-Za-z0-9_$])(AND|OR|XOR)(?![A-Za-z0-9_$])", re.IGNORECASE)
BOOL_KW_OR_RE = re.compile(r"(?<![A-Za-z0-9_$])(OR|XOR)(?![A-Za-z0-9_$])", re.IGNORECASE)
PRED_NAME = r"`?[A-Za-z0-9_$]+`?"
PRED_OPS = r"(?P<op><=>|!=|<>|>=|<=|=|<|>|\bIN\b|\bLIKE\b|\bBETWEEN\b|\bIS\b|\bREGEXP\b)"


def blank_subqueries(masked: str) -> str:
    """Blank parenthesised `(SELECT ...)` groups, offsets preserved.

    A predicate whose only bound lives inside a subquery does not bound the
    outer write (PRO-179 review B-2): `DELETE FROM creature WHERE id IN (SELECT
    id FROM t WHERE guid = 9002020)` must not read as `guid` being literal
    bound. Trimming operands without doing this first would turn that shape into
    a false pass, which is exactly the pairing the review asked to fix together.
    """
    out = list(masked)
    i, n = 0, len(masked)
    while i < n:
        if out[i] != "(":
            i += 1
            continue
        depth, j = 0, i
        while j < n:
            if out[j] == "(":
                depth += 1
            elif out[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        if SUBQUERY_HEAD_RE.match(masked[i + 1:j + 1]):
            for k in range(i, min(j + 1, n)):
                if out[k] != "\n":
                    out[k] = " "
        i = j + 1 if j < n else n
    return "".join(out)


def _read_operand(masked: str, start: int, stops: set[int]) -> str:
    """Masked text of one comparison operand, starting at `start`.

    Stops at a top-level AND/OR/XOR, or at a `)` that closes a group opened
    *before* the operand. The second rule is what makes
    `WHERE (entryorguid = 9002020 AND source_type = 0)` — the dominant shape in
    AzerothCore update files — read as two literal bounds instead of one
    operand ending in a stray `)`, which the old scanner classified as non-literal
    and refused the boot over (PRO-179 review B-2).

    The operand is read out of the MASKED text on purpose: slicing the raw text
    instead let a trailing `-- WHERE ...` comment leak into the operand and turn
    a clean `WHERE entry = 900200 -- note` into `band-not-literal`.
    """
    depth, i, n = 0, start, len(masked)
    while i < n:
        c = masked[i]
        if c == "(":
            depth += 1
        elif c == ")":
            if depth == 0:
                break
            depth -= 1
        elif depth == 0 and i in stops:
            break
        i += 1
    return masked[start:i]


def scan_predicates(where_text: str, qualifiers: set[str] | None = None
                    ) -> dict[str, list[tuple[str, str, object]]]:
    """col -> [(operator, kind, value)] for `col op literal` and `col IN (lit, ...)` shapes.

    `col` may be qualified by the table name or its alias; `qualifiers` is the
    set of qualifier spellings that belong to the statement's own table. A
    qualifier outside that set is not recorded at all, so the column simply does
    not count as bounded and the statement fails closed — an aliased write to a
    table the gate has not approved can never borrow the driving table's key.
    """
    masked = blank_subqueries(mask_sql(where_text))
    stops = {m.start() for m in BOOL_KW_RE.finditer(masked)}
    allowed = {q.lower().strip("`") for q in (qualifiers or set())}
    out: dict[str, list[tuple[str, str, object]]] = {}
    pat = re.compile(
        r"(?<![A-Za-z0-9_$.])" + r"(?:(?P<q>" + PRED_NAME + r")\s*\.\s*)?" +
        r"(?P<col>" + PRED_NAME + r")" + r"\s*" + PRED_OPS,
        re.IGNORECASE,
    )
    for m in pat.finditer(masked):
        qual = m.group("q")
        if qual is not None:
            qual = unquote_ident(qual).lower()
            if qual not in allowed:
                continue
        col = unquote_ident(m.group("col")).lower()
        op = m.group("op").upper()
        rest_start = m.end()
        if op == "IN":
            while rest_start < len(masked) and masked[rest_start] in " \n\t":
                rest_start += 1
            if rest_start >= len(masked) or masked[rest_start] != "(":
                # includes `IN (SELECT ...)` — the group was blanked above
                out.setdefault(col, []).append((op, "unknown", None))
                continue
            depth, j = 0, rest_start
            while j < len(masked):
                if masked[j] == "(":
                    depth += 1
                elif masked[j] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            inner = masked[rest_start + 1:j]
            for piece in split_top_level(inner, inner, ","):
                kind, value = literal_value(piece)
                out.setdefault(col, []).append((op, kind, value))
            continue
        operand = _read_operand(masked, rest_start, stops)
        kind, value = literal_value(operand)
        out.setdefault(col, []).append((op, kind, value))
    return out


def _bool_split(masked: str, lo: int, hi: int, depths: list[int],
                keywords: tuple[str, ...]) -> list[tuple[int, int]]:
    """Slice bounds of the operands of `keywords` at the range's own paren depth."""
    base = depths[lo]
    cuts: list[tuple[int, int]] = []
    for m in BOOL_KW_RE.finditer(masked, lo, hi):
        if depths[m.start()] == base and m.group(1).upper() in keywords:
            cuts.append((m.start(), m.end()))
    if not cuts:
        return [(lo, hi)]
    spans: list[tuple[int, int]] = []
    start = lo
    for c, e in cuts:
        spans.append((start, c))
        start = e
    spans.append((start, hi))
    return spans


def _unwrap_parens(masked: str, lo: int, hi: int) -> tuple[int, int]:
    """Peel parenthesised grouping that wraps the whole range, e.g. `((a AND b))`."""
    while lo < hi and masked[lo] == "(":
        depth, j, close = 0, lo, -1
        while j < hi:
            if masked[j] == "(":
                depth += 1
            elif masked[j] == ")":
                depth -= 1
                if depth == 0:
                    close = j
                    break
            j += 1
        if close != hi - 1:
            break
        lo, hi = lo + 1, hi - 1
        while lo < hi and masked[lo].isspace():
            lo += 1
        while hi > lo and masked[hi - 1].isspace():
            hi -= 1
    return lo, hi


NOT_RE = re.compile(r"(?<![A-Za-z0-9_$])NOT(?![A-Za-z0-9_$])", re.IGNORECASE)
NOT_OPERATOR_RE = re.compile(r"^(?:IN|BETWEEN|LIKE|REGEXP)\b", re.IGNORECASE)


def has_expression_negation(masked: str, lo: int, hi: int) -> bool:
    """True when a NOT negates an expression rather than one operator.

    `IS NOT NULL` and `NOT IN (...)` are single operators: the row set is still
    what the predicates say. `NOT (entry = 900200)` is not — it admits every row
    except the keyed one, which no key bounding can describe — so the caller
    treats the range as bounding nothing at all.
    """
    for m in NOT_RE.finditer(masked, lo, hi):
        before = masked[:m.start()].rstrip().upper()
        after = masked[m.end():hi].lstrip()
        if before.endswith("IS") or NOT_OPERATOR_RE.match(after):
            continue
        return True
    return False


def bound_columns(masked: str, text: str, lo: int, hi: int,
                  qualifiers: set[str] | None = None,
                  depths: list[int] | None = None) -> set[str]:
    """Columns that EVERY row the WHERE admits is equality/IN-bound to literals on.

    Conjunction unions its operands, disjunction intersects them, which is the
    difference between "the key bounds the write" and "one of the branches happens
    to mention the key": `key = 1 AND (a = 'x' OR a = 'y')` still bounds `key`,
    while `(source_type = 0 OR entryorguid = 9002020)` bounds neither — each branch
    lets rows through that the other does not. Subqueries are invisible here
    (scan_predicates blanks them), so a column bounded only inside a derived table
    never counts, and a `NOT` flips the whole range to unbounded rather than
    guessing (PRO-179 rev-2 B-2).
    """
    depths = paren_depths(masked) if depths is None else depths
    while lo < hi and masked[lo].isspace():
        lo += 1
    while hi > lo and masked[hi - 1].isspace():
        hi -= 1
    if lo >= hi:
        return set()
    # Grouping first: `((a = 1 OR b = 2))` has to split as a disjunction, and it
    # cannot be seen as one while the wrapping parentheses still hold the depth up.
    lo, hi = _unwrap_parens(masked, lo, hi)
    if lo >= hi:
        return set()
    if has_expression_negation(masked, lo, hi):
        return set()
    disj = _bool_split(masked, lo, hi, depths, ("OR", "XOR"))
    if len(disj) > 1:
        common: set[str] | None = None
        for a, b in disj:
            part = bound_columns(masked, text, a, b, qualifiers, depths)
            common = part if common is None else common & part
        return common or set()
    conj = _bool_split(masked, lo, hi, depths, ("AND",))
    if len(conj) > 1:
        out: set[str] = set()
        for a, b in conj:
            out |= bound_columns(masked, text, a, b, qualifiers, depths)
        return out
    out = set()
    for col, hits in scan_predicates(text[lo:hi], qualifiers).items():
        if any(op in ("=", "IN", "<=>") for op, _, _ in hits) and \
                not any(kind == "unknown" for _, kind, _ in hits):
            out.add(col)
    return out


def top_level_disjunction(where_text: str) -> bool:
    """True when the WHERE *as a whole* is a disjunction, `(...)` grouping included.

    `(a = 1 OR b = 2)` is the shape that used to slip past the depth-0 scan: the
    parentheses are pure grouping, so the statement still reaches rows that
    neither key column selects. An OR nested under an AND is a different thing
    (`key = 1 AND (x = 'a' OR x = 'b')` is still key-bounded) and stays legal —
    bound_columns() decides those. (PRO-179 rev-2 B-2.)
    """
    masked = blank_subqueries(mask_sql(where_text))
    depths = paren_depths(masked)
    lo, hi = 0, len(masked)
    while lo < hi and masked[lo].isspace():
        lo += 1
    while hi > lo and masked[hi - 1].isspace():
        hi -= 1
    lo, hi = _unwrap_parens(masked, lo, hi)
    base = depths[lo]
    for m in BOOL_KW_OR_RE.finditer(masked, lo, hi):
        if depths[m.start()] == base:
            return True
    return False


WHERE_TAIL_KW_RE = re.compile(
    r"(?<!`)(?<![A-Za-z0-9_$])(ORDER|GROUP|HAVING|LIMIT|OFFSET|INTO|PROCEDURE|LOCK|FOR)"
    r"(?![A-Za-z0-9_$])(?!`)",
    re.IGNORECASE)


def where_clause(masked: str, text: str, wpos: int) -> str:
    """The WHERE expression alone, cut before any trailing top-level clause.

    `... WHERE `guid` = 9002020 ORDER BY `guid` LIMIT 1` must scan as the guid
    predicate only; leaving `ORDER BY guid LIMIT 1` in the text makes the operand
    of the last predicate read as `9002020 ORDER BY` — not a literal, so a
    perfectly bounded write fails closed. Keywords inside a subquery survive:
    the ORDER BY of a derived table is part of that predicate.
    """
    body_masked = masked[wpos + len("WHERE"):]
    depths = paren_depths(body_masked)
    end = len(body_masked)
    for m in WHERE_TAIL_KW_RE.finditer(body_masked):
        if depths[m.start()] == 0:
            end = m.start()
            break
    return text[wpos + len("WHERE"):wpos + len("WHERE") + end]


# --------------------------------------------------------------------------- #
# statement model
# --------------------------------------------------------------------------- #


@dataclass
class Statement:
    verb: str
    table: str | None
    schema: str | None
    kind: str  # write | ddl | other
    text: str
    masked: str
    offset: int


BACKTICK_NAME_RE = re.compile(r"`([^`]+)`")
# bare identifiers, not preceded by a dot (schema/table qualifier) and not inside a
# backticked run, which mask_sql leaves intact
BARE_NAME_RE = re.compile(r"(?<![`A-Za-z0-9_$.])[A-Za-z_][A-Za-z0-9_$]{2,}(?![`A-Za-z0-9_$])")
FIRST_WORD_RE = re.compile(r"^\s*[(\s]*(?:\d+\s*)?(?P<w>[A-Za-z_]+)")
TBL = r"(?:" + NAME_TOKEN + r"(?:\s*\.\s*" + NAME_TOKEN + r")?)"
TBL_CAP = "(" + TBL + ")"


def parse_statement(text: str, masked: str, offset: int) -> Statement:
    m = FIRST_WORD_RE.match(masked)
    verb = (m.group("w") if m else "").upper()
    table = schema = None
    kind = "other"

    if verb in ("INSERT", "REPLACE"):
        kind = "write"
        mm = re.match(
            r"^\s*" + verb + r"\s+(?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY|IGNORE)\s+)*"
            r"(?:INTO\s+)?" + TBL_CAP, masked, re.IGNORECASE)
        if mm:
            schema, table = split_qualified(mm.group(1))
    elif verb == "UPDATE":
        kind = "write"
        mm = re.match(
            r"^\s*UPDATE\s+(?:(?:LOW_PRIORITY|IGNORE)\s+)*" + TBL_CAP, masked, re.IGNORECASE)
        if mm:
            schema, table = split_qualified(mm.group(1))
    elif verb == "DELETE":
        kind = "write"
        mm = re.match(
            r"^\s*DELETE\s+(?:(?:LOW_PRIORITY|QUICK|IGNORE)\s+)*FROM\s+" + TBL_CAP,
            masked, re.IGNORECASE)
        if mm:
            schema, table = split_qualified(mm.group(1))
    elif verb == "CREATE":
        kind = "ddl"
        mm = re.match(
            r"^\s*CREATE\s+(?:(?:OR\s+REPLACE\s+|TEMPORARY\s+|UNIQUE\s+|FULLTEXT\s+|SPATIAL\s+)*)"
            r"(?P<what>[A-Za-z]+)\s+", masked, re.IGNORECASE)
        if mm and mm.group("what").upper() in ("TABLE", "INDEX"):
            rest = masked[mm.end():]
            if mm.group("what").upper() == "TABLE":
                rest = re.sub(r"^\s*(?:IF\s+NOT\s+EXISTS\s+)", "", rest, flags=re.IGNORECASE)
                mm2 = re.match(TBL, rest, re.IGNORECASE)
                if mm2:
                    schema, table = split_qualified(mm2.group(0))
            else:
                mm2 = re.match(r"[A-Za-z0-9_$`]+\s+ON\s+" + TBL_CAP, rest,
                               re.IGNORECASE)
                if mm2:
                    schema, table = split_qualified(mm2.group(1))
    elif verb in ("ALTER", "DROP", "TRUNCATE", "RENAME", "GRANT", "REVOKE", "LOAD",
                  "CALL", "LOCK", "UNLOCK", "HANDLER", "PREPARE", "DEALLOCATE", "KILL",
                  "SHUTDOWN", "OPTIMIZE", "ANALYZE", "REPAIR", "CHECKSUM"):
        kind = "ddl"
    elif verb == "SET":
        kind = "set"

    return Statement(verb, table, schema, kind, text, masked, offset)


# --------------------------------------------------------------------------- #
# write-target grammar for UPDATE / DELETE (PRO-179 review B-1)
# --------------------------------------------------------------------------- #

WRITE_JOIN_RE = re.compile(
    r"(?<![A-Za-z0-9_$])(JOIN|STRAIGHT_JOIN|INNER|OUTER|CROSS|NATURAL|USING)(?![A-Za-z0-9_$])",
    re.IGNORECASE)
PARTITION_CLAUSE_RE = re.compile(r"(?<![A-Za-z0-9_$])PARTITION\s*\([^()]*\)", re.IGNORECASE)
WRITE_REGION_RE = re.compile(
    r"^\s*(?P<tbl>" + TBL + r")(?:\s+(?:(?P<as>AS)\s+)?(?P<alias>" + NAME_TOKEN + r"))?\s*$",
    re.IGNORECASE)
UPDATE_HEAD_RE = re.compile(r"^\s*UPDATE\s*(?:(?:LOW_PRIORITY|IGNORE|HIGH_PRIORITY|QUICK)\s+)*",
                            re.IGNORECASE)
DELETE_HEAD_RE = re.compile(r"^\s*DELETE\s*(?:(?:LOW_PRIORITY|QUICK|IGNORE|UNIQUE)\s+)*",
                            re.IGNORECASE)
BACKTICKED_NAME_RE = re.compile(r"`([^`]+)`")
BACKTICK_SPAN_RE = re.compile(r"`[^`]*`")
# The syntax a single-table write region must be free of. Splitting on these is how
# the rejection path decides which names in the region sit in a table position.
# FROM is in the list because a multi-table DELETE has two different name grammars on
# either side of it: the delete-target list before it, the table references after it
# (PRO-179 review NB-B).
REGION_SPLIT_RE = re.compile(
    r"(?<![A-Za-z0-9_$])(?:JOIN|STRAIGHT_JOIN|INNER|OUTER|CROSS|NATURAL|USING|FROM)"
    r"(?![A-Za-z0-9_$])|,",
    re.IGNORECASE)
# One identifier, optionally the first half of a qualified one. Reading ``a`.`b`` as a
# *unit* is the point: matching one backtick run at a time promoted the schema to table
# position and demoted the real table to a name (PRO-179 review NB-A). Bare names are
# included so an unquoted region is not read as naming nothing (PRO-179 review NB-B).
_REGION_IDENT = r"(?:`[^`]+`|[A-Za-z_][A-Za-z0-9_$]*)"
REGION_NAME_RE = re.compile(_REGION_IDENT + r"(?:\s*\.\s*" + _REGION_IDENT + r")?")
# Words that are syntax or index-hint noise where a bare name is expected. Applied to
# unquoted names only — a backticked name is unambiguously an identifier, and a table
# may legitimately be spelled like a keyword.
REGION_NAME_STOPWORDS = frozenset({
    "as", "by", "cross", "duplicate", "for", "force", "from", "group", "having", "ignore",
    "index", "inner", "join", "key", "limit", "low_priority", "natural", "on", "order",
    "outer", "partition", "primary", "quick", "select", "set", "straight_join", "unique",
    "use", "using", "where",
})
# `UPDATE`smart_scripts` is legal MySQL: the backtick terminates the verb token.
# The head patterns therefore allow the table to abut the verb; a verb that is not
# UPDATE/DELETE never reaches here (parse_statement already decided), and anything
# the region grammar does not understand fails closed.


def blank_backticked(s: str) -> str:
    """Blank the inside of every backticked run, keeping length and the backticks.

    The multi-table region tests look for JOIN, `,` and `(` as *syntax*, and a name
    is not syntax: ``UPDATE `creature` `join` SET`` and ``DELETE FROM `creature`
    `using` WHERE`` are single-table writes that rev-3 refused because the alias
    happened to be spelled like a keyword (PRO-179 review NB-3). Offsets survive so
    the same span can be sliced out of the unblanked text for the name extraction.
    """
    out = list(s)
    for m in BACKTICK_SPAN_RE.finditer(s):
        for i in range(m.start() + 1, m.end() - 1):
            out[i] = " "
    return "".join(out)


def write_targets(st: Statement) -> tuple[list[tuple[str | None, str]], set[str],
                                          str | None, str]:
    """Parse the region between the verb and the first top-level SET/WHERE.

    Returns `([(schema, table), ...], aliases, error, region)`. A well-formed
    single-table write yields one target plus any alias, which the caller needs
    so `WHERE c.guid = 9002020` counts as bounding `creature.guid`. Anything
    else yields an error string, the tables the region still names in a table
    position (the ledger among them), and the other names it mentions — see
    `_region_tables()`.

    Only one write target is accepted. MySQL's multi-table forms (`UPDATE a JOIN
    b`, `UPDATE a, b`, `DELETE a, b FROM ...`, derived tables, index hints) are
    rejected rather than parsed, because a partial reading of them is exactly how
    the rev-1 gate let `UPDATE creature c JOIN updates u SET u.hash = ...`
    through: only the first table was ever examined.
    """
    masked = st.masked
    from_end: int | None = None
    if st.verb == "UPDATE":
        head = UPDATE_HEAD_RE.match(masked)
        if not head:
            return [], set(), "could not locate the UPDATE keyword", ""
        stops = top_level_positions(masked, "SET")
        lo, hi = head.end(), (stops[0] if stops else len(masked))
        pre = ""
    elif st.verb == "DELETE":
        head = DELETE_HEAD_RE.match(masked)
        if not head:
            return [], set(), "could not locate the DELETE keyword", ""
        froms = top_level_positions(masked, "FROM")
        if not froms:
            return [], set(), "DELETE has no FROM clause", ""
        pre = masked[head.end():froms[0]]
        from_end = froms[0] + len("FROM")
        lo = from_end
        ends = [p for p in (top_level_positions(masked, "WHERE")
                            + top_level_positions(masked, "ORDER")
                            + top_level_positions(masked, "LIMIT")) if p > lo]
        hi = min(ends) if ends else len(masked)
    else:
        return [], set(), None, ""

    if pre.strip():
        region = masked[head.end():hi]
        tables, others = _region_tables(masked, head.end(), hi, from_end)
        return tables, others, \
            f"table list {pre.strip()[:60]!r} before FROM is multi-table DELETE syntax", region
    region_names = PARTITION_CLAUSE_RE.sub(" ", masked[lo:hi])
    # The three syntax tests read the region with backticked names blanked, so an
    # alias spelled `join`/`using` or a name containing a comma cannot manufacture a
    # multi-table write (PRO-179 review NB-3). Name extraction still reads the
    # unblanked text, and PARTITION clauses stay visible because their contents are
    # never quoted.
    region_scan = blank_backticked(region_names)
    region = masked[lo:hi].strip()
    err = None
    if WRITE_JOIN_RE.search(region_scan):
        err = "the write region contains a JOIN"
    elif "," in region_scan:
        err = "the write region is a comma-separated table list"
    elif "(" in region_scan:
        err = ("the write region contains a parenthesised expression (derived table or index "
               "hint), which the gate does not attempt to parse")
    m = WRITE_REGION_RE.match(region_names)
    if err is None and not m:
        err = "the write region is not a single table with an optional alias"
    if err is not None:
        tables, others = _region_tables(masked, lo, hi, from_end)
        return tables, others, err, region
    schema, table = split_qualified(m.group("tbl"))
    aliases = {unquote_ident(m.group("alias")).lower()} if m.group("alias") else set()
    return [(schema, table)], aliases, None, region


def _region_names(fragment: str) -> list[str]:
    """Identifiers in a region fragment, a qualified name counted as one name."""
    return [m.group(0) for m in REGION_NAME_RE.finditer(fragment)]


def _region_tables(masked: str, lo: int, hi: int,
                   from_end: int | None = None) -> tuple[list[tuple[str | None, str]], set[str]]:
    """(tables, other names) for a rejected write region, in order of first appearance.

    Used only on the rejection path, so the report can name the table a rejected
    statement was reaching for (the ledger among them). The region is split on the
    *syntax* keywords with backticked names blanked, and inside each fragment the
    first name is the table position while the rest are aliases and qualifiers:
    ``UPDATE `creature` `c` JOIN `updates` `u` SET`` names two tables, `creature` and
    `updates`, and `c`/`u` are names, not tables — running them through the
    allow-list check reported a phantom `table-not-allowed` for each (PRO-179
    review NB-2).

    Three refinements, all report fidelity on an already-rejected statement:

    * a qualified name is read as one unit, so ``UPDATE `acore_world`.`updates` `u``
      names `updates` in a table position instead of promoting the schema to table
      and losing the ledger (PRO-179 review NB-A);
    * unquoted names are visible too, so ``DELETE updates, creature FROM updates u``
      names `updates` rather than reporting "none identifiable" (PRO-179 review NB-B);
    * a multi-table DELETE has two name grammars, so `from_end` says where the FROM
      keyword ends. Before it every name is a delete target; after it the first name
      of each fragment is a table and the rest are aliases. A delete target that an
      alias in the FROM list defines is that alias, not a second table, which is what
      keeps ``DELETE `u`, `c` FROM `updates` `u` JOIN `creature` `c``` naming
      `updates` and `creature` instead of `u` and `c` (PRO-179 review NB-B).

    What a bare-name scan cannot tell is an index hint: `USE INDEX (idx)` contributes
    `idx` as a name. That is noise in a message about a statement that is already
    refused, never a silently-passing write — a name this function misses can only
    cost report fidelity, because `multi-table-write` is raised independently of it.
    """
    raw = masked[lo:hi]
    scan = blank_backticked(PARTITION_CLAUSE_RE.sub(" ", raw))
    bounds = [0] + [m.end() for m in REGION_SPLIT_RE.finditer(scan)] + [len(scan)]
    from_off = None if from_end is None else from_end - lo
    tables: dict[str, tuple[str | None, str]] = {}
    aliases: set[str] = set()
    targets: dict[str, tuple[str | None, str]] = {}
    seen: set[str] = set()

    def record(token: str) -> tuple[str, tuple[str | None, str]] | None:
        schema, table = split_qualified(token)
        if not table:
            return None
        if not token.startswith("`") and table.lower() in REGION_NAME_STOPWORDS:
            return None
        return table.lower(), (schema, table)

    for a, b in zip(bounds, bounds[1:]):
        in_from = from_off is None or a >= from_off
        for idx, token in enumerate(_region_names(raw[a:b])):
            hit = record(token)
            if hit is None:
                continue
            key, ident = hit
            seen.add(key)
            if in_from:
                if idx == 0:
                    tables.setdefault(key, ident)
                else:
                    aliases.add(key)
            else:
                targets.setdefault(key, ident)
    for key, ident in targets.items():
        if key not in aliases:
            tables.setdefault(key, ident)
    return list(tables.values()), {k for k in seen if k not in tables}


# --------------------------------------------------------------------------- #
# the checker
# --------------------------------------------------------------------------- #


class Preflight:
    def __init__(self, args, policy, report: Report):
        self.a = args
        # `--compose-file` is `append`, so it is None unless someone passed one.
        if self.a.compose_file is None:
            self.a.compose_file = []
        self.p = policy
        self.r = report
        self.db = MySQL(args)
        self.live_db = args.live_db
        self.sql_root = Path(args.sql_root) if args.sql_root else Path(args.compose_root, "data", "sql")
        self.git_root = Path(args.git_root) if args.git_root else Path(args.compose_root)
        self.violations: list[dict] = []
        self.policy_stats: dict = {}

    # -- helpers ------------------------------------------------------------- #

    def line_of(self, text: str, offset: int) -> int:
        tail = text[offset:]
        offset += len(tail) - len(tail.lstrip())
        return text.count("\n", 0, offset) + 1

    def git_author_origin(self, path: Path) -> tuple[str, str]:
        """Last-commit author provenance: ('company'|'upstream'|'unknown', detail).

        This is author metadata only. It is never the whole answer — see
        classify_origin(), which combines it with the directory tag from policy.
        """
        rel = os.path.relpath(path, self.git_root)
        try:
            proc = subprocess.run(
                ["git", "log", "-1", "--format=%ae\t%an\t%s", "--", rel],
                cwd=str(self.git_root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return "unknown", "git unavailable"
        if proc.returncode != 0:
            return "unknown", "not a git tree"
        line = proc.stdout.decode("utf-8", "replace").strip()
        if not line:
            return "unknown", "untracked"
        email = line.split("\t", 1)[0].lower()
        domains = [d.lower().lstrip("@") for d in self.p.get("companyEmailDomains", [])]
        if any(email.endswith("@" + d) for d in domains):
            return "company", line
        return "upstream", line

    # -- upstream ref provenance (PRO-248, PRO-179 review NB-E) --------------- #

    # Outcomes of asking "did this file arrive with the history we are tracking?".
    REF_VERIFIED = "verified"            # in the ref -> upstream history, exempt
    REF_UNVERIFIED = "unverified"        # ref resolved, file absent -> introduced locally
    REF_INDETERMINATE = "indeterminate"  # git could not answer -> fail closed
    REF_UNCHECKED = "unchecked"          # no ref / ref absent -> pre-PRO-248 behaviour

    def _git(self, argv: list[str], timeout: int = 60, errors: str = "replace"):
        """Run git in the tracked tree. (returncode, out, err), or (None, '', reason).

        `errors` is exposed because `git ls-tree -z` emits raw path bytes: decoding
        those with "replace" would silently mangle a non-ASCII filename and turn a
        genuine upstream file into an unverified one (PRO-248 review B2).
        """
        try:
            proc = subprocess.run(["git", *argv], cwd=str(self.git_root),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            return None, "", str(exc)
        return (proc.returncode,
                proc.stdout.decode("utf-8", errors),
                proc.stderr.decode("utf-8", "replace").strip())

    def upstream_ref_setting(self) -> tuple[str | None, str]:
        """(verifiedRef, refreshCommand) from policy, with a derived fetch hint."""
        cfg = self.p.get("upstreamProvenance")
        cfg = cfg if isinstance(cfg, dict) else {}
        ref = str(cfg.get("verifiedRef") or "").strip() or None
        refresh = str(cfg.get("refreshCommand") or "").strip()
        if not refresh:
            if ref and ref.startswith("refs/remotes/"):
                remote, _, branch = ref[len("refs/remotes/"):].partition("/")
                refresh = f"git fetch --prune {remote} {branch}"
            elif ref and "/" in ref and not ref.startswith("refs/"):
                remote, _, branch = ref.partition("/")
                refresh = f"git fetch --prune {remote} {branch}"
            else:
                refresh = "git fetch --prune"
        return ref, refresh

    def upstream_ref(self) -> dict:
        """Resolve the tracked upstream ref once per run.

        Returns {state, ref, refresh, paths, blobs, pathsLower, pathsByLower, detail,
        remoteTracked, remoteDetail} where state is 'ok' (`paths` is the set of tree
        paths, `blobs` maps a tree path to its blob id), 'unconfigured', 'missing' or
        'error'. A missing ref is not an error: this must never need the network, and a
        clone that has not fetched yet still has to boot.

        `remoteTracked` answers "is verifiedRef under refs/remotes/?" and it fails
        closed (PRO-248 review B1). `git rev-parse --symbolic-full-name` prints nothing
        and reports failure for a short name that is ambiguous — which is exactly what
        `git branch origin/Playerbot HEAD` creates — while every other read of that same
        name silently resolves refs/heads/origin/Playerbot, the local branch the
        `--author` forger can rewrite. An unanswered question therefore means "not a
        remote-tracking ref", never the default "yes".
        """
        cached = getattr(self, "_upstream_ref_cache", _UNSET)
        if cached is not _UNSET:
            return cached
        ref, refresh = self.upstream_ref_setting()
        out = {"state": "ok", "ref": ref, "refresh": refresh, "paths": None,
               "blobs": None, "pathsLower": None, "pathsByLower": None,
               "detail": "", "remoteTracked": True, "remoteDetail": ""}
        if not ref:
            out.update(state="unconfigured",
                       detail="policy has no upstreamProvenance.verifiedRef")
        else:
            rc, full, err = self._git(["rev-parse", "--symbolic-full-name", ref])
            if rc is None:
                out.update(state="error", detail=f"git unavailable: {err}")
            elif rc != 0 or not full.strip():
                # Ambiguous, unresolvable, or answered with nothing: `git rev-parse
                # --symbolic-full-name origin/Playerbot` prints no name when a local
                # branch shadows the remote-tracking one. Fail closed (review B1).
                first = (err.strip().splitlines() or [""])[0]
                out.update(remoteTracked=False,
                           remoteDetail=first or f"`git rev-parse --symbolic-full-name "
                                                 f"{ref}` exited {rc} without naming a ref")
            elif not full.strip().startswith("refs/remotes/"):
                out.update(remoteTracked=False,
                           remoteDetail=f"it resolves to {full.strip()}")
            if out["state"] == "ok":
                rc, _, err = self._git(["rev-parse", "--verify", "--quiet",
                                        f"{ref}^{{commit}}"])
                if rc is None:
                    out.update(state="error", detail=f"git unavailable: {err}", paths=None)
                elif rc != 0:
                    out.update(state="missing",
                               detail=f"{ref} does not exist in this clone")
            if out["state"] == "ok" and out["remoteTracked"]:
                # `-z` is not cosmetic: without it git quotes every path that is not
                # plain ASCII, so `2026_01_01_01_érfar.sql` comes back as
                # "...\\303\\251rfar.sql" and can never equal the on-disk name — a
                # genuine upstream file would be refused a boot (review B2). `-z` also
                # drops `--name-only` so the same single call carries the blob id the
                # content check needs (review N2).
                rc, body, err = self._git(["ls-tree", "-z", "-r", ref], errors="surrogateescape")
                if rc is None:
                    out.update(state="error", detail=f"git unavailable: {err}")
                elif rc != 0:
                    out.update(state="error",
                               detail=f"git ls-tree {ref} exited {rc}: {err[:200]}")
                else:
                    blobs: dict[str, str] = {}
                    for record in body.split("\0"):
                        if not record:
                            continue
                        meta, sep, name = record.partition("\t")
                        if not sep or not name:
                            continue
                        fields = meta.split(" ")
                        if len(fields) >= 3 and fields[1] == "blob":
                            blobs[name] = fields[2]
                    out["blobs"] = blobs
                    out["paths"] = set(blobs)
                    out["pathsLower"] = {n.lower() for n in blobs}
                    lower: dict[str, str] = {}
                    for n in blobs:
                        lower.setdefault(n.lower(), n)
                    out["pathsByLower"] = lower
        self._upstream_ref_cache = out
        return out

    def upstream_ref_state(self, path: Path) -> tuple[str, str, str]:
        """Did `path` arrive with the tracked upstream history? (state, detail, cause).

        `cause` is the machine-readable reason behind the state so the report can name
        the right recovery: 'verified', 'absent', 'content-diverged', 'case-diverged',
        'not-remote', 'unreadable', 'uncomparable', 'outside-tree', 'unhashable',
        'unconfigured' or 'missing'.
        """
        up = self.upstream_ref()
        ref = up["ref"] or "the configured upstreamProvenance.verifiedRef"
        if up["state"] == "unconfigured":
            return self.REF_UNCHECKED, (f"upstream ref not configured; set "
                                        f"upstreamProvenance.verifiedRef or run "
                                        f"{up['refresh']}"), "unconfigured"
        if up["state"] == "missing":
            return (self.REF_UNCHECKED,
                    f"{ref} is not present locally; run {up['refresh']}", "missing")
        if up["state"] == "error":
            return (self.REF_INDETERMINATE,
                    f"cannot read {ref}: {up['detail']}", "unreadable")
        if not up.get("remoteTracked", True):
            # The ref itself is not trustworthy, so nothing it says about this file is.
            # A ref outside refs/remotes/ is a branch, and a branch is moved by the same
            # `git commit` / `git update-ref` this rule exists to catch (review B1).
            return (self.REF_INDETERMINATE,
                    f"{ref} is not a remote-tracking ref ({up.get('remoteDetail', '')}), "
                    f"so whoever can move it can manufacture this exemption",
                    "not-remote")
        try:
            rel = Path(os.path.relpath(path, self.git_root)).as_posix()
        except ValueError:
            return (self.REF_INDETERMINATE,
                    f"{path} is not comparable to {self.git_root}", "uncomparable")
        if rel.startswith(".."):
            return (self.REF_INDETERMINATE,
                    f"{path} is outside the tracked tree {self.git_root}", "outside-tree")
        # `updates.name` is utf8mb4_unicode_ci, so the updater itself cannot tell two
        # paths apart that differ only in case (PRO-179 review N4). Matching the same
        # way here keeps a case-only rename from being accused of forgery — but a
        # case-only twin is still reported, because 'the ref has a path that matches
        # modulo case' is not the claim the exemption is built on (review N1).
        exact = rel in up["paths"]
        twin = None if exact else (up.get("pathsByLower") or {}).get(rel.lower())
        if not exact and twin is None:
            return self.REF_UNVERIFIED, f"absent from {ref}", "absent"
        ref_path = rel if exact else twin
        # A name in the ref is not the file. `git add` a rewritten body under a forged
        # `--author`, commit it, leave the tree clean, and a path-only check still says
        # 'present in the ref' while the updater runs content no fetch ever delivered
        # (review N2). Compare the blob the ref actually holds.
        want = (up.get("blobs") or {}).get(ref_path)
        rc, got, err = self._git(["hash-object", "--", rel])
        if rc is None or rc != 0 or not got.strip():
            return (self.REF_INDETERMINATE,
                    f"cannot hash {rel} to compare it with {ref}: "
                    f"{(err or f'exited {rc}')[:200]}", "unhashable")
        got = got.strip()
        if want and got != want:
            where = "in the work tree" if self.is_dirty(path) else "as committed"
            return (self.REF_UNVERIFIED,
                    f"its bytes are not the blob {ref} holds for {ref_path} "
                    f"({got[:12]} vs {want[:12]}) {where}: the name is upstream, the "
                    f"content is not", "content-diverged")
        if not exact:
            return (self.REF_VERIFIED,
                    f"present in {ref} as {ref_path}, which differs from {rel} only in "
                    f"case", "case-diverged")
        return self.REF_VERIFIED, f"present in {ref}", "verified"

    def pending_dir_for_message(self) -> str:
        """Where company content belongs, as named by the policy's own PENDING dir."""
        for entry in self.p.get("includedDirs", []):
            if str(entry.get("state", "")).upper() == "PENDING" \
                    and entry.get("origin") == "company":
                return str(entry.get("path", "")).lstrip("$").lstrip("/")
        return "data/sql/updates/pending_db_world"

    def report_upstream_provenance(self, f: dict) -> None:
        """PRO-248: an origin:upstream file that the tracked ref cannot vouch for is
        company content, not an exempt one.

        `classify_origin()` already re-claims the file for the write policy when this
        returns 'unverified'/'indeterminate'; this adds the boot-refusing finding that
        tells the author where the file actually belongs. Called for every new file,
        including one with no statements at all, so an empty file cannot slip through
        on a policy run that finds nothing to parse.
        """
        state = f.get("upstream_ref_state")
        if state is None:
            return
        counts = self.policy_stats.setdefault("upstreamRef", {})
        counts[state] = counts.get(state, 0) + 1
        up = self.upstream_ref()
        ref = up["ref"] or "upstreamProvenance.verifiedRef"
        pending = self.pending_dir_for_message()
        db_path = (f.get("dir") or {}).get("db_path", "?")
        prov = f.get("provenance", "")
        detail = f.get("upstream_ref_detail", "")
        cause = f.get("upstream_ref_cause", "")
        # A provenance FAIL is a boot refusal exactly like a policy finding, so it is
        # recorded the same way: `corpus_sweep.py` tallies `engine.violations`, which
        # only `_policy_statement()` filled, so the rule the README claims to falsify
        # was invisible to the tool doing the falsifying (PRO-248 review N4).
        def bad(rule: str, message: str) -> None:
            self.r.fail("4", message, file=f["rel"], rule=rule)
            self.violations.append({"file": f["rel"], "line": 0, "rule": rule,
                                    "detail": message, "statement": "provenance"})
        if state == self.REF_UNVERIFIED and cause == "content-diverged":
            bad("unverified-upstream-content",
                f"{f['rel']} is new in the upstream-tagged directory "
                f"{db_path} with a non-company author ({prov}), and {ref} "
                f"does hold that path, but {detail}. A `git pull` puts the "
                f"name and its bytes in the ref together; rewriting one of "
                f"them under a forged --author is not upstream history. "
                f"Company content belongs in {pending}, where it is always "
                f"policy-checked: move it there. If this is an accidental "
                f"local edit, restore it with `git checkout -- {f['rel']}`, "
                f"or run `{up['refresh']}` if the ref is simply behind, and "
                f"re-run this gate.",
                )
        elif state == self.REF_UNVERIFIED:
            bad("unverified-upstream-origin",
                f"{f['rel']} is new in the upstream-tagged directory "
                f"{db_path} with a non-company author ({prov}), but it is "
                f"not in {ref}. Only a fetch/pull puts a file in that ref — "
                f"git commit --author=<upstream author> does not, so this "
                f"reads as company content wearing an upstream provenance. "
                f"Company content belongs in {pending}, where it is always "
                f"policy-checked: move it there, or if it genuinely arrived "
                f"with upstream history run `{up['refresh']}` (or point "
                f"upstreamProvenance.verifiedRef at the ref this tree "
                f"tracks) and re-run this gate.",
                )
        elif state == self.REF_INDETERMINATE and cause == "not-remote":
            # Both the policy comment and the README say verifiedRef *must* be a
            # remote-tracking ref. A 'must' that only costs a warning is the exemption
            # with extra steps: the file stays exempt on the word of a ref the person
            # whose forged --author this rule exists to catch can move (review B1).
            bad("upstream-ref-not-remote",
                f"{f['rel']} is new in the upstream-tagged directory "
                f"{db_path} and the upstream exemption was about to be "
                f"granted on {ref}, which is not a remote-tracking ref "
                f"({detail}). A ref outside refs/remotes/ is a branch, and "
                f"a branch is rewritten by the same `git commit` / "
                f"`git update-ref` this rule constrains, so it cannot vouch "
                f"for anything. Point upstreamProvenance.verifiedRef at "
                f"refs/remotes/<remote>/<branch> and re-run; if this file is "
                f"company content it belongs in {pending}.",
                )
        elif state == self.REF_INDETERMINATE:
            bad("unverified-upstream-origin",
                f"{f['rel']} is new in the upstream-tagged directory "
                f"{db_path} and git could not decide whether it is in {ref} "
                f"({prov}): {detail}. Failing closed rather than handing out "
                f"the upstream exemption on an unanswered question. Refresh "
                f"the ref with `{up['refresh']}` and re-run; if this is "
                f"company content, it belongs in {pending}.",
                )
        elif state == self.REF_UNCHECKED:
            key = f"unchecked:{f['rel']}"
            if not getattr(self, "_upstream_warn_emitted", None):
                self._upstream_warn_emitted = set()
            if key not in self._upstream_warn_emitted:
                self._upstream_warn_emitted.add(key)
                self.r.warn("4", f"{f['rel']} sits in an upstream-tagged directory and "
                                 f"the upstream exemption could not be verified against "
                                 f"a ref ({detail}), so the pre-PRO-248 directory+author "
                                 f"exemption was used. Restore verification with "
                                 f"`{up['refresh']}`.",
                            file=f["rel"], rule="upstream-ref-unverified")
        elif state == self.REF_VERIFIED and cause == "case-diverged":
            # The exemption is kept — `updates.name` is utf8mb4_unicode_ci and a
            # case-insensitive checkout produces exactly this shape — but a path that
            # only matches the ref modulo case is a fact a reviewer should see, not a
            # detail the gate swallows while calling the file 'present in the ref'
            # (review N1).
            key = f"case:{f['rel']}"
            if not getattr(self, "_upstream_warn_emitted", None):
                self._upstream_warn_emitted = set()
            if key not in self._upstream_warn_emitted:
                self._upstream_warn_emitted.add(key)
                self.r.warn("4", f"{f['rel']} is exempt as upstream content only by a "
                                 f"case-insensitive match: {detail}. The write policy "
                                 f"still treats it as upstream (the updater cannot tell "
                                 f"the two names apart either), but a case-only twin of "
                                 f"an upstream path is worth a look before this boots.",
                            file=f["rel"], rule="upstream-ref-case-divergence")

    def classify_origin(self, f: dict, author: tuple[str, str] | None = None
                        ) -> tuple[str, str]:
        """Provenance used for write-policy exemption: ('company'|'upstream'|'unknown', detail).

        Rule (PRO-179 review B1): a file is company-authored when EITHER the
        directory it lives in is a company-authored directory (policy
        includedDirs[].origin == 'company') OR its last commit author is inside a
        company email domain. 'upstream' — the only value the write policy is
        exempt for — requires BOTH an upstream-tagged directory AND an author
        outside the company domains. Anything git cannot attribute is 'unknown'
        and gets no exemption, so a file dropped in or committed with a foreign
        --author inside a company directory can never skip checks 2-5.

        Rule (PRO-248, PRO-179 review NB-E): author metadata is a claim anyone can
        make with `git commit --author=...`, so an upstream-tagged directory plus an
        upstream-looking author is no longer enough. The file must also exist in the
        tracked upstream ref (policy upstreamProvenance.verifiedRef) with the bytes that
        ref holds for its path, which only a fetch/pull can produce, and that ref must
        live under refs/remotes/ or it is a branch the forger can move (PRO-248 review
        B1/N2). When the ref says the file is not from that history — or git cannot
        answer — the file is 'company' and is policy-checked like any
        other company file; report_upstream_provenance() turns it into a FAIL that
        names pending_db_world.
        """
        dir_origin = (f.get("dir") or {}).get("origin", "unknown")
        author_origin, detail = author if author else self.git_author_origin(f["path"])
        if author_origin == "company":
            return "company", f"author {detail}"
        if author_origin == "unknown":
            return "unknown", f"cannot attribute: {detail}"
        if dir_origin == "company":
            return "company", f"company directory {f['dir']['db_path']}, " \
                              f"foreign commit author {detail}"
        if dir_origin == "upstream":
            state, ref_detail, cause = self.upstream_ref_state(f["path"])
            f["upstream_ref_state"] = state
            f["upstream_ref_detail"] = ref_detail
            f["upstream_ref_cause"] = cause
            if state == self.REF_VERIFIED:
                return "upstream", f"upstream directory, author and ref — {detail}, " \
                                   f"{ref_detail}"
            if state == self.REF_UNVERIFIED:
                return "company", f"{detail} but {ref_detail} — a non-company author is " \
                                  f"not evidence of upstream history"
            if state == self.REF_INDETERMINATE:
                return "company", f"{detail} and {ref_detail} — failing closed"
            return "upstream", f"upstream directory and author (ref not verified — " \
                               f"{ref_detail}): {detail}"
        return "unknown", f"unrecognised directory origin {dir_origin!r} for {f['dir']['db_path']}"

    def dirty_paths(self) -> set | None:
        """Work-tree-modified paths, lower-cased and posix-normalised; None if unknown.

        Provenance is read from HEAD (`git_author_origin`), so a file that
        upstream-provenances cleanly but was hand-edited in the work tree keeps
        its exemption from checks 2-5 while the bytes the updater will run are
        nobody's upstream content (PRO-179 review N-3). One `git status` for the
        whole run, cached. Callers must treat None as dirty: a question git
        cannot answer is not a clean bill of health.
        """
        cached = getattr(self, "_dirty_cache", _UNSET)
        if cached is not _UNSET:
            return cached
        paths: set | None = set()
        try:
            proc = subprocess.run(
                ["git", "status", "--porcelain", "--ignore-submodules=none"],
                cwd=str(self.git_root), stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, timeout=60)
        except (OSError, subprocess.TimeoutExpired):
            paths = None
        else:
            if proc.returncode != 0:
                paths = None
            else:
                for entry in proc.stdout.decode("utf-8", "replace").splitlines():
                    if len(entry) < 4 or entry[:2] == "!!":
                        continue
                    # R/C entries carry "old -> new"; both sides count as touched.
                    for part in entry[3:].split(" -> "):
                        part = part.strip().strip('"')
                        if part:
                            paths.add(Path(part).as_posix().lower())
        self._dirty_cache = paths
        return paths

    def is_dirty(self, path: Path) -> bool:
        """True when `path` differs from HEAD, or when git cannot tell us."""
        dirty = self.dirty_paths()
        if dirty is None:
            return True
        rel = Path(os.path.relpath(path, self.git_root)).as_posix().lower()
        return rel in dirty

    # -- discovery ----------------------------------------------------------- #

    def included_dirs(self) -> list[dict]:
        rows = self.db.query(
            "SELECT `path`, `state` FROM `updates_include` ORDER BY `path`", self.live_db)
        if not rows:
            raise OperationalError(f"{self.live_db}.updates_include is empty or missing")
        expected = {d["path"]: d["state"] for d in self.p["includedDirs"]}
        seen = {r[0]: r[1] for r in rows}
        if seen != expected:
            extra = {k: v for k, v in seen.items() if expected.get(k) != v}
            missing = {k: v for k, v in expected.items() if seen.get(k) != v}
            # This is the check most likely to refuse a boot after someone pokes the
            # database, so the recovery is in the failure text rather than in a README
            # nobody has open at 1am (PRO-179 review N-8).
            repair = ", ".join(f"('{d['path']}', '{d['state']}')"
                               for d in self.p["includedDirs"])
            self.r.fail("0", "updates_include does not match the approved four-directory set. "
                             f"unexpected/changed={sorted(extra)} missing={sorted(missing)}. "
                             f"Recovery with the worldserver stopped and the single-writer lease "
                             f"held: DELETE FROM `updates_include`; INSERT INTO `updates_include` "
                             f"(`path`, `state`) VALUES {repair}; then re-run this gate. If the "
                             f"row came from a module, fix that module's AddDatabaseUpdates call "
                             f"instead of editing the table (PRO-177).",
                        rule="updater-wiring")
            self.r.info("0", "Module SQL paths must never be registered into the core updater "
                             "(PRO-177). An unexpected row here means someone added AddDatabaseUpdates "
                             "or edited updates_include by hand.", rule="updater-wiring")
        out = []
        for path, state in rows:
            if not path.startswith("$"):
                self.r.fail("0", f"updates_include path {path!r} is not $-relative to the source "
                                 f"directory; the container would resolve it differently",
                            rule="updater-wiring")
                continue
            # `$/x` is relative to Updates.SourceDirectory (/azerothcore in the container),
            # whose host counterpart is the compose root of this stack.
            local = self.git_root / path[1:].lstrip("/")
            out.append({"db_path": path, "state": state, "dir": local,
                        "origin": self.origin_for(path)})
        return out

    def origin_for(self, db_path: str) -> str:
        for entry in self.p["includedDirs"]:
            if entry["path"].lower() == db_path.lower():
                return entry["origin"]
        return "unknown"

    def collect_files(self, dirs: list[dict]) -> list[dict]:
        files = []
        for entry in dirs:
            root = entry["dir"]
            if not root.is_dir():
                self.r.fail("1", f"included directory does not exist on disk: {root}",
                            rule="missing-dir")
                continue
            for path in sorted(root.rglob("*.sql")):
                if not path.is_file():
                    continue
                rel = str(path.relative_to(self.sql_root.parent.parent)) if path.is_relative_to(
                    self.sql_root.parent.parent) else str(path)
                files.append({
                    "basename": path.name,
                    "key": path.name.lower(),
                    "path": path,
                    "rel": rel,
                    "dir": entry,
                    "size": path.stat().st_size,
                    "sha1": hashlib.sha1(path.read_bytes()).hexdigest().upper(),
                })
        return files

    # -- check 0: wiring ----------------------------------------------------- #

    def check_wiring(self):
        present = {r[0] for r in self.db.query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = "
            f"'{self.live_db}' AND table_name IN ('updates','updates_include')", None)}
        for needed in ("updates", "updates_include"):
            if needed not in present:
                raise OperationalError(f"{self.live_db}.{needed} is missing — this is not a "
                                       f"world database the AzerothCore updater can drive")
        version = self.db.query("SELECT VERSION()")
        src = None
        for cand in self.p.get("worldserverConfCandidates", []):
            c = Path(self.a.compose_root, cand)
            if c.is_file():
                src = c
                break
        if src:
            m = re.search(r"^Updates\.SourceDirectory\s*=\s*(\S+)", src.read_text(
                encoding="utf-8", errors="replace"), re.MULTILINE)
            if m and m.group(1).rstrip('"') != self.p.get("expectedSourceDirectory"):
                self.r.warn("0", f"Updates.SourceDirectory={m.group(1)} differs from the expected "
                                 f"{self.p.get('expectedSourceDirectory')}; $-relative paths were "
                                 f"resolved against {self.sql_root}", rule="updater-wiring")
        self.r.line("0", f"mysql {version[0][0]} on {self.live_db}; "
                         f"updater sees {len(self.p['includedDirs'])} included dirs")
        ledger_counts = {}
        for row in self.db.query(
                "SELECT `state`, COUNT(*) FROM `updates` GROUP BY `state`", self.live_db):
            ledger_counts[row[0]] = int(row[1])
        self.r.line("0", f"ledger rows: {sum(ledger_counts.values())} "
                         f"({', '.join(f'{k}={v}' for k, v in sorted(ledger_counts.items()))})")
        if ledger_counts.get("MODULE"):
            self.r.warn("0", f"{ledger_counts['MODULE']} ledger rows carry state=MODULE. Core does not "
                             f"register module SQL in this tree (PRO-177); these are dead references. "
                             f"UpdateFetcher skips MODULE rows during cleanup, so this is not fatal.",
                        rule="module-rows")

    # -- check 0b: boot path (PRO-179 review B2) ------------------------------ #

    def compose_config(self) -> dict | None:
        """Resolved compose model for the stack, or None (after reporting)."""
        argv = ["docker", "compose"]
        for f in self.a.compose_file:
            argv += ["-f", f]
        if self.a.compose_project:
            argv += ["-p", self.a.compose_project]
        argv += ["config", "--format", "json"]
        try:
            proc = subprocess.run(argv, cwd=str(Path(self.a.compose_root)),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            self.r.fail("0b", f"cannot run `docker compose config`: {exc}. Without the resolved "
                              f"compose model the gate cannot prove that the SQL it scanned is the "
                              f"SQL the boot will apply.", rule="boot-path-unreadable")
            return None
        if proc.returncode != 0:
            self.r.fail("0b", f"`docker compose config --format json` exited {proc.returncode}: "
                              f"{proc.stderr.decode('utf-8', 'replace').strip()[:300]}",
                        rule="boot-path-unreadable")
            return None
        try:
            return json.loads(proc.stdout.decode("utf-8", "replace"))
        except ValueError as exc:
            self.r.fail("0b", f"`docker compose config --format json` returned unparseable "
                              f"output ({exc}); refusing to guess the boot path.",
                        rule="boot-path-unreadable")
            return None

    @staticmethod
    def normalize_volumes(svc: dict) -> list[dict]:
        """Compose volumes in either syntax -> [{source,target,ro,type}]."""
        out = []
        for v in svc.get("volumes") or []:
            if isinstance(v, str):
                parts = v.split(":")
                if len(parts) < 2:
                    out.append({"source": None, "target": parts[0], "ro": None, "type": "unknown"})
                    continue
                ro = None
                if len(parts) > 2:
                    ro = "ro" in [p for p in parts[2].split(",") if p] and "rw" not in [
                        p for p in parts[2].split(",") if p]
                out.append({"source": parts[0], "target": parts[1], "ro": ro,
                            "type": "bind" if parts[0].startswith(("/", ".", "~")) else "volume"})
                continue
            src = v.get("source", v.get("Source"))
            tgt = v.get("target", v.get("destination", v.get("Destination")))
            ro = v.get("read_only", v.get("readonly", v.get("ReadOnly")))
            typ = str(v.get("type", v.get("Type") or "")).lower() or (
                "bind" if str(src or "").startswith(("/", ".", "~")) else "volume")
            out.append({"source": src, "target": tgt, "ro": ro, "type": typ})
        return out

    @staticmethod
    def normalize_env(svc: dict) -> dict:
        env = svc.get("environment") or {}
        if isinstance(env, dict):
            return {str(k): ("" if v is None else str(v)) for k, v in env.items()}
        out = {}
        for item in env:
            if isinstance(item, str) and "=" in item:
                k, _, v = item.partition("=")
                out[k.strip()] = v.strip()
            elif isinstance(item, str):
                out[item.strip()] = os.environ.get(item.strip(), "")
        return out

    def _sql_mount_target(self) -> str:
        src_dir = str(self.p.get("expectedSourceDirectory", "/azerothcore")).strip().strip('"')
        return src_dir.rstrip("/") + "/data/sql"

    def check_boot_path(self):
        """Prove the tree that was scanned IS the tree the updater will read.

        The updater reads `$/...` paths from inside the worldserver container, so
        the gate is only meaningful if (a) the container's /azerothcore/data/sql
        is a bind mount of exactly the directory the gate walked, (b) it is
        read-only, and (c) the world updater is the only updater the core enable
        flag turns on. Anything else and a green gate proves nothing.
        """
        bp = self.p.get("bootPath", {})
        svc_name = bp.get("worldserverService", "ac-worldserver")
        cfg = self.compose_config()
        if cfg is None:
            return
        svc = (cfg.get("services") or {}).get(svc_name)
        if svc is None:
            self.r.fail("0b", f"service {svc_name!r} is not in the resolved compose config, so the "
                              f"gate cannot verify the boot path for it", rule="boot-path-service")
            return
        target = self._sql_mount_target()
        vols = self.normalize_volumes(svc)
        norm = lambda p: (p or "").rstrip("/")
        matched = [v for v in vols if norm(v["target"]) == norm(target)]
        scanned = os.path.realpath(self.sql_root)
        if not matched:
            self.r.fail("0b", f"{svc_name} has no mount on {target}. The updater would read "
                              f"{target} out of the container image, not from {scanned}, so this "
                              f"gate has not inspected the files the boot will apply.",
                        rule="sql-mount-missing")
        for v in matched:
            if v["type"] not in ("bind",):
                self.r.fail("0b", f"{target} is mounted as {v['type']!r} (source {v['source']}), "
                                  f"not a bind mount of the working tree. A named volume keeps "
                                  f"whatever SQL an earlier boot put in it.", rule="sql-mount-type")
                continue
            source = v["source"] or ""
            if not os.path.isabs(source):
                source = os.path.join(str(Path(self.a.compose_root)), source)
            if os.path.realpath(source) != scanned:
                self.r.fail("0b", f"{target} is bound to {source}, but the gate scanned {scanned}. "
                                  f"Fix --compose-root/--sql-root or the compose file; a green "
                                  f"result here would be about the wrong files.",
                            rule="sql-mount-source")
            if bp.get("requireReadOnlySqlMount", True):
                if v["ro"] is not True:
                    self.r.fail("0b", f"the {target} mount is not read-only (ro={v['ro']}). The "
                                      f"container could rewrite a file after the gate hashed it.",
                                rule="sql-mount-writable")
        # a mount higher up the tree can shadow the one we verified
        for v in vols:
            t = norm(v["target"])
            if t and t != norm(target) and norm(target).startswith(t + "/"):
                self.r.warn("0b", f"{v['target']} is mounted over a parent of {target}; check it "
                                  f"does not shadow the SQL tree.", rule="sql-mount-shadow")
        env = self.normalize_env(svc)
        flag = bp.get("updaterEnableEnv", "AC_UPDATES_ENABLE_DATABASES")
        raw = env.get(flag)
        if raw is None:
            self.r.fail("0b", f"{flag} is not set in the resolved compose config for {svc_name}. "
                              f"The gate cannot tell which databases the core updater will touch; "
                              f"set it explicitly (4 = DATABASE_WORLD only) rather than relying on "
                              f"an env_file the gate cannot resolve.", rule="updater-env-unknown")
        else:
            try:
                bits = int(str(raw).strip(), 0)
            except ValueError:
                bits = -1
                self.r.fail("0b", f"{flag}={raw!r} is not numeric; cannot map it onto "
                                  f"DatabaseLoadFlags", rule="updater-env-unknown")
            if bits >= 0:
                want = int(bp.get("requireUpdaterBit", 4))
                if not bits & want:
                    self.r.fail("0b", f"{flag}={raw} does not include bit {want} "
                                      f"(DATABASE_WORLD), yet the world updater is what this gate "
                                      f"guards.", rule="updater-env-world-off")
                if bits & ~want:
                    self.r.fail("0b", f"{flag}={raw} enables more than DATABASE_WORLD "
                                      f"(bit {want}). PRO-65/PRO-177 fixed this at 4; auth and "
                                      f"hotfixes must not be fed by the same pull.",
                                rule="updater-env-widened")
        pb_flag = bp.get("playerbotsEnableEnv", "AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES")
        if pb_flag in env:
            self.r.line("0b", f"{pb_flag}={env[pb_flag]} belongs to the Playerbots updater "
                              f"(separate acore_playerbots ledger); this gate does not read or "
                              f"write it")
        restart = (svc.get("restart") or "").lower()
        if restart in ("unless-stopped", "always"):
            self.r.line("0b", f"restart={restart}: a bad update file crash-loops the stack, which "
                              f"is why this gate runs before every boot")
        if not matched:
            ro_label = "not mounted"
        elif all(v["ro"] is True for v in matched):
            ro_label = "read-only bind"
        elif any(v["ro"] is False for v in matched):
            ro_label = "WRITABLE bind"
        else:
            ro_label = "bind, read-only not asserted"
        self.r.line("0b", f"sql mount {scanned} -> {target} ({ro_label}) for {svc_name}; "
                          f"{flag}={env.get(flag)}")
        self._check_live_container(svc_name, target, scanned, bp)

    def _check_live_container(self, svc_name: str, target: str, scanned: str, bp: dict):
        """If a container already exists, its ACTUAL mounts win over the compose file."""
        base = ["docker", "compose"]
        for f in self.a.compose_file:
            base += ["-f", f]
        if self.a.compose_project:
            base += ["-p", self.a.compose_project]
        try:
            # `-a` matters: a stopped-but-not-removed ac-worldserver still owns its
            # mounts, and `docker compose up -d` would recreate it from the compose
            # file only if the container were gone (PRO-179 review N-4). Plain `ps -q`
            # lists running containers, so the exited container was invisible and the
            # gate asserted against the compose config alone.
            ids = subprocess.run(base + ["ps", "-a", "-q", svc_name],
                                 cwd=str(Path(self.a.compose_root)),
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return
        cid = ids.stdout.decode("utf-8", "replace").strip().splitlines()
        if ids.returncode != 0 or not cid or not cid[0].strip():
            self.r.line("0b", f"no existing {svc_name} container; asserted against the compose "
                              f"config only")
            return
        try:
            insp = subprocess.run(["docker", "inspect", cid[0].strip()],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=60)
            data = json.loads(insp.stdout.decode("utf-8", "replace"))
        except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
            self.r.warn("0b", f"could not inspect the existing {svc_name} container; compose "
                              f"config only", rule="boot-path-inspect")
            return
        if not data:
            return
        mounts = [m for m in (data[0].get("Mounts") or [])
                  if (m.get("Destination") or "").rstrip("/") == target.rstrip("/")]
        if not mounts:
            self.r.fail("0b", f"the existing {svc_name} container has no mount on {target}; "
                              f"`up -d` may leave it running against the image copy of the SQL",
                        rule="container-mount-missing")
            return
        m = mounts[0]
        if os.path.realpath(m.get("Source") or "") != scanned:
            self.r.fail("0b", f"the existing {svc_name} container binds {target} to "
                              f"{m.get('Source')}, not {scanned}. Recreate it before trusting a "
                              f"green gate.", rule="container-mount-source")
        if bp.get("requireReadOnlySqlMount", True) and m.get("RW", True):
            self.r.fail("0b", f"the existing {svc_name} container mounts {target} read-write "
                              f"(RW={m.get('RW')}). The compose file may say ro while the running "
                              f"container says otherwise.", rule="container-mount-writable")

    # -- check 1: duplicate basenames ---------------------------------------- #

    def check_duplicates(self, files: list[dict]):
        by_exact: dict[str, list[dict]] = {}
        by_lower: dict[str, list[dict]] = {}
        for f in files:
            by_exact.setdefault(f["basename"], []).append(f)
            by_lower.setdefault(f["key"], []).append(f)
        dupes = 0
        for name, group in sorted(by_exact.items()):
            if len(group) > 1:
                dupes += 1
                where = ", ".join(sorted(g["dir"]["db_path"] for g in group))
                self.r.fail("1", f"duplicate basename {name!r} appears {len(group)} times in "
                                 f"[{where}] -> LOG_FATAL at UpdateFetcher.cpp:96 aborts the boot",
                            file=group[0]["rel"], rule="duplicate-basename")
        for name, group in sorted(by_lower.items()):
            exact_names = {g["basename"] for g in group}
            if len(group) > 1 and len(exact_names) > 1:
                self.r.fail("1", f"case-only collision {name!r}: {sorted(exact_names)} are distinct "
                                 f"files to UpdateFetcher but collide in `updates`.`name` "
                                 f"(utf8mb4_unicode_ci) -> one file is silently treated as applied",
                            file=group[0]["rel"], rule="case-collision")
        self.r.line("1", f"{len(files)} .sql files across {len(self.p['includedDirs'])} included "
                         f"dirs; {dupes} duplicate basename(s)")
        if not dupes:
            self.r.info("1", "no duplicate basename", rule="duplicate-basename")

    # -- check 2 + 3: ledger comparison -------------------------------------- #

    def ledger(self) -> dict[str, dict]:
        rows = self.db.query(
            "SELECT `name`, `hash`, `state`, UNIX_TIMESTAMP(`timestamp`) FROM `updates` "
            "ORDER BY `name`", self.live_db)
        # `updates.name` is a utf8mb4_unicode_ci primary key, so the updater itself
        # compares names case-insensitively (PRO-179 review N4). Mirror that here or a
        # file renamed only in case looks "new" and gets silently re-applied.
        return {r[0].lower(): {"hash": r[1] or "", "state": r[2],
                               "ts": r[3] if len(r) > 3 else ""} for r in rows}

    def check_ledger(self, files: list[dict]) -> list[dict]:
        led = self.ledger()
        # Reverse index for rename detection (PRO-179 review N8). UpdateFetcher does not
        # treat every hash mismatch as a re-apply: if the file's current hash equals some
        # *other* ledger row's hash it renames that row instead (UpdateFetcher.cpp:335-345).
        by_hash: dict[str, list[str]] = {}
        for name, row in led.items():
            if row["hash"]:
                by_hash.setdefault(row["hash"].lower(), []).append(name)
        new_files = []
        checked = 0
        seen = set()
        for f in files:
            seen.add(f["key"])
            row = led.get(f["key"])
            if row is None:
                author = self.git_author_origin(f["path"])
                origin, prov = self.classify_origin(f, author)
                f["origin"] = origin
                f["provenance"] = prov
                if author[0] == "unknown" and author[1] == "untracked" \
                        and not self.p.get("allowUntrackedFiles"):
                    self.r.warn("2", f"{f['rel']} is not tracked by git. A file that is not in the "
                                     f"tree cannot be reviewed or reproduced; treat it as an "
                                     f"unexpected drop-in.", file=f["rel"], rule="untracked-file")
                new_files.append(f)
                continue
            checked += 1
            f["origin"] = self.origin_for(f["dir"]["db_path"])
            disk, stored = f["sha1"], row["hash"]
            if not stored:
                if self.p["hashCheck"]["emptyLedgerHashIsWarning"]:
                    self.r.warn("3", f"{f['rel']} has an empty hash in `updates`; the updater will "
                                     f"re-hash and re-insert it (MODE_REHASH).", file=f["rel"],
                                rule="empty-hash")
                continue
            if disk.lower() != stored.lower():
                archived_pair = (self.p["hashCheck"]["ignoreArchivedPairMismatch"]
                                 and f["dir"]["state"] == "ARCHIVED" and row["state"] == "ARCHIVED")
                msg = (f"{f['rel']} hash mismatch: disk {disk[:12]} != ledger {stored[:12]}")
                if archived_pair:
                    self.r.warn("3", msg + " — skipped by UpdateFetcher "
                                           "(Updates.ArchivedRedundancy=0), so it is not re-applied.",
                                file=f["rel"], rule="hash-mismatch")
                elif row["state"] == "ARCHIVED" and f["dir"]["state"] == "ARCHIVED":
                    self.r.warn("3", msg, file=f["rel"], rule="hash-mismatch")
                else:
                    # A mismatch is only a silent re-apply when the content is genuinely
                    # new. If the bytes match another ledger row this is a rename, which
                    # UpdateFetcher answers by renaming that row (UpdateFetcher.cpp:335-345),
                    # not by re-running the file (PRO-179 review N8).
                    same = [n for n in by_hash.get(disk.lower(), []) if n != f["key"]]
                    if same:
                        self.r.fail("3", msg + f" — the bytes match ledger row(s) "
                                               f"{', '.join(same[:3])}, so this reads as a rename: "
                                               f"UpdateFetcher renames that entry "
                                               f"(UpdateFetcher.cpp:335-345) rather than re-applying. "
                                               f"Confirm the rename is the intended change.",
                                    file=f["rel"], rule="hash-rename")
                    else:
                        self.r.fail("3", msg + " — ApplyUpdateFile() re-applies the changed file "
                                               "silently (UpdateFetcher.cpp:350).",
                                    file=f["rel"], rule="hash-mismatch")
            elif row["state"] != f["dir"]["state"]:
                self.r.info("3", f"{f['rel']} state will move {row['state']} -> "
                                 f"{f['dir']['state']} (harmless UpdateState).", file=f["rel"],
                            rule="state-move")
        dead = sorted(set(led) - seen)
        # UpdateFetcher::CleanOrphanUpdates compares the whole ledger against the whole
        # file list (UpdateFetcher.cpp:409-416), so the CleanDeadRefMaxCount threshold has
        # to be measured the same way; the listing stays non-MODULE for signal.
        dead_non_module = [n for n in dead if led[n]["state"] != "MODULE"]
        self.r.line("2", f"{len(files)} file(s) on disk, {checked} already in the ledger, "
                         f"{len(new_files)} will be applied on next boot")
        for f in new_files:
            ref_state = f.get("upstream_ref_state") or "-"
            self.r.line("2", f"  NEW {f['rel']}  sha1={f['sha1'][:12]} size={f['size']}B "
                             f"state={f['dir']['state']} origin={f['origin']} "
                             f"upstreamRef={ref_state}")
        if not new_files:
            self.r.line("2", "  (nothing new — the next boot applies no update files)")
        if dead:
            self.r.line("2", f"  {len(dead)} ledger row(s) have no file on disk "
                             f"({len(dead_non_module)} non-MODULE)")
            max_clean = self.a.clean_dead_ref_max_count
            if dead and max_clean >= 0 and len(dead) > max_clean:
                self.r.warn("2", f"{len(dead)} ledger row(s) are orphaned "
                                 f"({len(dead_non_module)} non-MODULE) but "
                                 f"Updates.CleanDeadRefMaxCount={max_clean}: cleanup is skipped with "
                                 f"LOG_ERROR (not fatal). If a git pull deleted those files, confirm "
                                 f"that is intentional.", rule="dead-reference")
            for name in dead_non_module[: self.a.max_dead_list]:
                self.r.line("2", f"  DEAD {name} (state={led[name]['state']})")
        return new_files

    # -- check 4: write policy ------------------------------------------------ #

    def check_policy(self, new_files: list[dict]):
        exempt = set(self.p.get("writePolicyExemptOrigins", []))
        if self.a.strict_all:
            exempt = set()
        base = self.p["baseTables"]
        custom = {k: v for k, v in self.p["customTables"].items() if not k.startswith("_")}
        checked_files = 0
        statements = 0
        for f in new_files:
            # PRO-248: the upstream exemption is a claim about history, so it has to be
            # vouched for by the tracked ref, not by --author alone. This both reports
            # the finding and counts the outcome, and runs before the exemption branch
            # so a file with no parsable statements is still refused.
            self.report_upstream_provenance(f)
            if f.get("origin") in exempt and not self.a.policy_all:
                # The exemption is a HEAD-level claim. If the work tree disagrees
                # with HEAD, the bytes the updater is about to run are company
                # content regardless of who authored the committed version.
                if self.is_dirty(f["path"]):
                    self.r.fail("4", f"{f['rel']} is upstream-provenanced at HEAD "
                                     f"({f.get('provenance','')}) but has uncommitted "
                                     f"changes, so it is checked against the write policy "
                                     f"as company content. Commit it under a company "
                                     f"author, or restore it.",
                                file=f["rel"], rule="dirty-worktree")
                else:
                    self.r.warn("4", f"{f['rel']} is upstream-provenanced ({f.get('provenance','')}); "
                                     f"write policy is not enforced on upstream files by default. "
                                     f"Use --strict-all to enforce it anyway.",
                                file=f["rel"], rule="policy-exempt-origin")
                    continue
            checked_files += 1
            text = f["path"].read_text(encoding="utf-8", errors="replace")
            for stmt_text, off in split_statements(text):
                masked = mask_sql(stmt_text)
                if not masked.strip():
                    continue
                st = parse_statement(stmt_text, masked, off)
                statements += 1
                verb_at = off + (len(masked) - len(masked.lstrip()))
                line = self.line_of(text, verb_at)
                self._policy_statement(f, st, line)
        ref_counts = self.policy_stats.get("upstreamRef", {}) if isinstance(
            self.policy_stats, dict) else {}
        self.policy_stats = {"files": checked_files, "statements": statements,
                             "exemptFiles": len(new_files) - checked_files,
                             "upstreamRef": ref_counts}
        ref_note = ""
        if ref_counts:
            ref = self.upstream_ref().get("ref") or "upstream ref unconfigured"
            ref_note = f"; upstream ref {ref} (" + \
                       ", ".join(f"{k}={v}" for k, v in sorted(ref_counts.items())) + ")"
        self.r.line("4", f"{checked_files} new file(s) under write policy, {statements} statement(s) "
                         f"inspected; reserved bands "
                         f"creature_template.entry/quest_template.ID>="
                         f"{self._band_min('creature_template', 'entry')}, "
                         f"creature.guid>={self._band_min('creature', 'guid')}{ref_note}")
        if checked_files == 0:
            self.r.info("4", "no new company files to policy-check", rule="write-policy")

    def _band_min(self, table, column):
        for b in self.p["baseTables"].get(table, {}).get("bands", []):
            if b["column"].lower() == column.lower():
                return b["min"]
        return "?"

    def _policy_statement(self, f: dict, st: Statement, line: int):
        rel = f["rel"]
        digest = " ".join(st.masked.split())[:110]
        base = self.p["baseTables"]
        custom = {k: v for k, v in self.p["customTables"].items() if not k.startswith("_")}

        def bad(rule, msg):
            self.r.fail("4", f"{rel}:{line}: {msg}", file=rel, line=line, rule=rule)
            self.violations.append({"file": rel, "line": line, "rule": rule,
                                    "detail": msg, "statement": digest})

        if st.verb in ("ALTER", "DROP", "TRUNCATE", "RENAME", "GRANT", "REVOKE", "LOAD",
                       "CALL", "LOCK", "UNLOCK", "HANDLER", "PREPARE", "DEALLOCATE", "KILL",
                       "SHUTDOWN", "OPTIMIZE", "ANALYZE", "REPAIR", "CHECKSUM"):
            bad("forbidden-statement", f"{st.verb} statements are rejected outright by the gate")
            return
        if st.verb == "SET":
            if re.match(r"^\s*SET\s+(?:(?:GLOBAL|PERSIST|PERSIST_ONLY|SESSION|LOCAL)\s+)",
                        st.masked, re.IGNORECASE):
                bad("forbidden-statement", "SET GLOBAL/PERSIST is rejected")
            return
        if not st.verb:
            bad("unparsable", "statement does not begin with a SQL verb — fail closed")
            return
        if st.verb in ("SELECT", "SHOW", "DESCRIBE", "EXPLAIN", "USE", "START", "BEGIN",
                       "COMMIT", "ROLLBACK", "DO", "WITH"):
            return
        if st.verb not in ("DELETE", "UPDATE", "INSERT", "REPLACE", "CREATE"):
            bad("unknown-statement", f"unrecognised statement verb {st.verb!r} — fail closed")
            return

        if st.verb == "CREATE":
            if st.table is None:
                bad("forbidden-statement", "CREATE of a non-table object (view/trigger/routine/user) "
                                           "is rejected")
                return
            if st.table not in custom:
                bad("custom-table-only", f"CREATE of {st.table!r} is not allowed: new schema belongs "
                                         f"in a whitelisted custom table only "
                                         f"({', '.join(sorted(custom))})")
            return

        if st.verb in ("UPDATE", "DELETE"):
            self._policy_multi_table(f, st, line, bad, base, custom)
            return

        # INSERT / REPLACE: a single target table, parsed by parse_statement()
        if st.table is None:
            bad("unparsable", "could not identify the target table — fail closed")
            return
        if not self._check_write_target(bad, st, st.table, st.schema, base, custom):
            return
        entry = self._policy_entry(st.table, base, custom)
        self._policy_insert(f, st, line, digest, bad,
                            [c.lower() for c in entry.get("keyColumns", [])],
                            entry.get("bands", []), entry.get("refs", []),
                            st.table.lower() in {k.lower() for k in custom})

    _LEDGER_TABLES = ("updates", "updates_include", "version", "dbversion")

    def _policy_entry(self, table, base, custom):
        for key in (table, table.lower()):
            if key in base:
                return base[key]
            if key in custom:
                return custom[key]
        return None

    def _check_write_target(self, bad, st, table, schema, base, custom) -> bool:
        """Schema/ledger/allow-list checks for one write target. False if a finding was raised."""
        if schema and schema.lower() not in (self.live_db.lower(), "current_date"):
            bad("cross-schema-write", f"statement qualifies schema {schema!r}; update files must "
                                      f"not write outside the world database")
            return False
        if table.lower() in self._LEDGER_TABLES:
            bad("ledger-table", f"write target {table!r} is updater bookkeeping; only "
                                f"UpdateFetcher may touch it")
            return False
        if self._policy_entry(table, base, custom) is None:
            bad("table-not-allowed",
                f"write target {table!r} is neither a company-approved base table nor a "
                f"whitelisted custom table; add it to ops/preflight/preflight-policy.json and "
                f"get it reviewed")
            return False
        return True

    def _policy_multi_table(self, f, st, line, bad, base, custom):
        """UPDATE/DELETE policy entry point: prove there is exactly one write target.

        The rev-1 scanner only looked at the first table after the verb, so
        `UPDATE creature c JOIN updates u SET u.hash = ...` was checked against
        `creature` alone — it rewrote the ledger hash while the `ledger-table`
        rule reported clean (PRO-179 review B-1). Multi-table writes are now
        rejected outright, which costs nothing in `pending_db_world`, and every
        table the region still names is run through the ledger/allow-list checks
        so a green result never depends on the grammar having anticipated a
        syntax.
        """
        targets, qualifiers, err, region = write_targets(st)
        if err:
            extra = ", ".join(sorted({t for _, t in targets})) or "none identifiable"
            named = f"tables named: {extra}"
            if qualifiers:
                named += f"; other names in region: {', '.join(sorted(qualifiers))}"
            bad("multi-table-write",
                f"{st.verb} is not a single-table write: {err} (write region {region!r}; "
                f"{named}). Split it into one statement per table, each "
                f"WHERE-bounded on that table's natural key.")
            # Still name every table the region mentions in a table position, so the
            # report says *what* the statement was reaching for even though it is
            # rejected. Aliases are excluded: they are names, not write targets.
            for _schema, table in targets:
                self._check_write_target(bad, st, table, _schema, base, custom)
            return
        if len(targets) != 1:
            bad("unparsable", f"could not identify exactly one write target for {st.verb} "
                              f"(found {len(targets)}) — fail closed")
            return
        schema, table = targets[0]
        if not self._check_write_target(bad, st, table, schema, base, custom):
            return
        entry = self._policy_entry(table, base, custom)
        key_cols = [c.lower() for c in entry.get("keyColumns", [])]
        # A partial key is only accepted where policy says so explicitly (keySets);
        # by default the WHOLE natural key has to be bounded (PRO-179 review N1).
        key_sets = [[c.lower() for c in grp] for grp in entry.get("keySets", []) if grp] \
            or ([key_cols] if key_cols else [])
        quals = {table.lower()} | {q.lower() for q in qualifiers} | {self.live_db.lower()}
        if st.verb == "UPDATE":
            self._policy_update(f, st, line, bad, key_cols, entry.get("bands", []),
                                entry.get("refs", []), entry, key_sets, quals)
        else:
            self._policy_delete(st, bad, key_cols, entry.get("bands", []),
                               entry.get("refs", []), key_sets, quals)


    def _policy_insert(self, f, st, line, digest, bad, key_cols, bands, refs, is_custom):
        masked = st.masked
        if re.search(r"(?<![A-Za-z0-9_$])SELECT(?![A-Za-z0-9_$])",
                     masked, re.IGNORECASE):
            bad("insert-select", "INSERT ... SELECT is rejected: the gate cannot prove the ids it "
                                 "will materialise")
            return
        if re.search(r"(?<![A-Za-z0-9_$])ON\s+DUPLICATE\s+KEY\s+UPDATE(?![A-Za-z0-9_$])",
                     masked, re.IGNORECASE):
            bad("on-duplicate-key", "INSERT ... ON DUPLICATE KEY UPDATE is rejected: use the "
                                    "documented DELETE-then-INSERT idempotency pattern")
            return
        # MySQL puts INSERT's PARTITION clause before the column list; the review POC
        # wrote it after, the way UPDATE/DELETE carry it. Accept either position — the
        # clause selects partitions to write to, it cannot change which ids the VALUES
        # rows contain, and every id is still band-checked (PRO-179 review NB-4).
        part = f"(?:{PARTITION_CLAUSE_RE.pattern})?\\s*"
        m = re.match(
            r"^\s*(?:INSERT|REPLACE)\s+(?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY|IGNORE)\s+)*"
            r"(?:INTO\s+)?" + TBL_CAP + r"\s*" + part
            + r"(?:\((?P<cols>[^()]*(?:\([^()]*\)[^()]*)*)\))?\s*" + part
            + r"[^()]*?(?P<how>VALUES|VALUE|SET)\b", masked, re.IGNORECASE)
        if not m:
            bad("unparsable", "could not parse the INSERT target/column list — fail closed")
            return
        cols = [unquote_ident(c).lower() for c in m.group("cols").split(",")] \
            if m.group("cols") else []
        if not cols:
            bad("insert-columns-required", "INSERT without an explicit column list is rejected: "
                                           "band checks need named columns")
            return
        tail = masked[m.end("how"):]
        rows = []
        if m.group("how").upper() == "SET":
            bad("insert-set-syntax", "INSERT ... SET syntax is rejected: use VALUES so every row "
                                     "can be checked")
            return
        for chunk in split_top_level(tail, mask_sql(tail), ","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if not (chunk.startswith("(") and chunk.endswith(")")):
                bad("unparsable", "could not parse a VALUES row — fail closed")
                return
            cells = split_top_level(chunk[1:-1], mask_sql(chunk[1:-1]), ",")
            rows.append(cells)
        if not rows:
            bad("unparsable", "no VALUES rows parsed — fail closed")
            return
        band_cols = {b["column"].lower(): b["min"] for b in bands}
        ref_cols = {r["column"].lower(): r["min"] for r in refs}
        for bc in band_cols:
            if bc not in cols:
                bad("band-column-missing",
                    f"INSERT into {st.table} must set the reserved-band column {bc!r} explicitly")
        for idx, row in enumerate(rows, 1):
            if len(row) != len(cols):
                bad("unparsable",
                    f"VALUES row {idx} has {len(row)} cells for {len(cols)} columns — fail closed")
                return
            for col, cell in zip(cols, row):
                limit = band_cols.get(col, ref_cols.get(col))
                if limit is None:
                    continue
                kind, val = literal_value(cell)
                if kind not in ("int", "float"):
                    bad("band-not-literal", f"{st.table}.{col} in VALUES row {idx} is {kind} "
                                            f"({cell.strip()[:30]!r}); reserved-band ids must be "
                                            f"integer literals")
                    continue
                if val < limit:
                    bad("reserved-band", f"{st.table}.{col}={int(val)} in VALUES row {idx} is below "
                                         f"the company reserved band {limit}+ "
                                         f"(docs/content-playbook.md 2.4)")

    def _policy_update(self, f, st, line, bad, key_cols, bands, refs, entry, key_sets,
                       qualifiers):
        masked = st.masked
        where_positions = top_level_positions(masked, "WHERE")
        if not where_positions:
            bad("unbounded-write", f"UPDATE {st.table} without WHERE is rejected")
            return
        wpos = where_positions[0]
        set_start = [p for p in top_level_positions(masked, "SET") if p < wpos]
        if not set_start:
            bad("unparsable", "could not locate the SET clause of the UPDATE — fail closed")
            return
        set_text = st.text[set_start[-1] + len("SET"):wpos]
        where_text = where_clause(masked, st.text, wpos)
        preds = scan_predicates(where_text, qualifiers)
        self._bounded_on(bad, st, preds, key_sets, "UPDATE",
                         bound=bound_columns(masked, st.text, wpos + len("WHERE"),
                                             wpos + len("WHERE") + len(where_text),
                                             qualifiers),
                         where_text=where_text)
        forbidden_assign = set(key_cols)
        for b in bands:
            forbidden_assign.add(b["column"].lower())
        for piece in split_top_level(set_text, mask_sql(set_text), ","):
            lhs = piece.split("=")[0].strip()
            parts = lhs.split(".")
            if len(parts) > 1:
                qual = unquote_ident(parts[0]).lower()
                if qual not in qualifiers:
                    bad("unparsable",
                        f"UPDATE {st.table} assigns {piece.strip()[:40]!r}, which belongs to "
                        f"{qual!r} — only the written table may be assigned; fail closed")
                    continue
            target = unquote_ident(parts[-1]).lower()
            if target in forbidden_assign:
                bad("key-column-assigned",
                    f"UPDATE {st.table} assigns the natural-key/reserved column {target!r}")
        self._check_literals(bad, st, preds, bands, refs, "UPDATE")

    def _policy_delete(self, st, bad, key_cols, bands, refs, key_sets, qualifiers):
        masked = st.masked
        where_positions = top_level_positions(masked, "WHERE")
        if not where_positions:
            bad("unbounded-write", f"DELETE FROM {st.table} without WHERE is rejected")
            return
        where_text = where_clause(masked, st.text, where_positions[0])
        preds = scan_predicates(where_text, qualifiers)
        self._bounded_on(bad, st, preds, key_sets, "DELETE",
                         bound=bound_columns(masked, st.text,
                                             where_positions[0] + len("WHERE"),
                                             where_positions[0] + len("WHERE") + len(where_text),
                                             qualifiers),
                         where_text=where_text)
        self._check_literals(bad, st, preds, bands, refs, "DELETE")

    def _bounded_on(self, bad, st, preds, key_sets, verb, bound=None, where_text=""):
        """Bound means: EVERY column of at least one key set is equality/IN bound to literals.

        The old rule accepted any single key column, so
        `DELETE FROM module_string WHERE module='mod-x'` passed while deleting rows
        unrelated to the file that shipped them (PRO-179 review N1). keyColumns is the
        whole key and must be bounded as a unit; policy `keySets` lists alternative keys
        only where a partial key really is selective. `bound` is the disjunction-aware
        set from bound_columns(), not the flat predicate list: a key column that is
        only bounded on one side of an OR does not bound the statement.
        """
        if not key_sets:
            bad("no-natural-key-configured",
                f"{verb} on {st.table}: no natural key configured in the policy — fail closed")
            return
        if bound is None:
            bound = {c for c, hits in preds.items()
                     if any(op in ("=", "IN", "<=>") for op, _, _ in hits)
                     and not any(kind == "unknown" for _, kind, _ in hits)}
        if where_text and top_level_disjunction(where_text):
            bad("or-in-where",
                f"{verb} on {st.table}: the WHERE is a disjunction, so no key bounds every "
                f"row the statement can reach; use an explicit IN list of reserved-band "
                f"ids instead")
        for group in key_sets:
            if all(kc.lower() in bound for kc in group):
                return  # every column of this key set bounds the statement
        best: list[str] | None = None
        for group in key_sets:
            problems: list[str] = []
            for kc in group:
                hits = preds.get(kc)
                if not hits:
                    problems.append(f"{kc!r} absent from WHERE")
                    continue
                if not any(op in ("=", "IN", "<=>") for op, _, _ in hits):
                    problems.append(f"{kc!r} only tested with "
                                    f"{sorted({op for op, _, _ in hits})}")
                    continue
                if any(kind == "unknown" for _, kind, _ in hits):
                    problems.append(f"{kc!r} compared against a non-literal operand")
                    continue
                if kc.lower() not in bound:
                    problems.append(f"{kc!r} bounded on only one side of a disjunction")
                    continue
            if not problems:
                break
            if best is None or len(problems) < len(best):
                best = problems
        why = ""
        if where_text:
            scan = blank_subqueries(mask_sql(where_text))
            if has_expression_negation(scan, 0, len(scan)):
                why = ("; a negated group (NOT ...) in the WHERE is the reason: it admits every "
                       "row outside the group, so the key bounds present cannot bound the "
                       "statement. Rewrite without NOT, or name the ids explicitly.")
        bad("unbounded-write",
            f"{verb} on {st.table} is not WHERE-bounded on a complete natural key "
            f"(keys tried: {key_sets}; closest: {'; '.join(best or [])}; "
            f"columns bounded on every branch: {sorted(bound) or 'none'}){why}")

    def _check_literals(self, bad, st, preds, bands, refs, verb):
        rules: dict[str, tuple[int, str]] = {}
        for b in bands:
            rules[b["column"].lower()] = (b["min"], "reserved band")
        for r in refs:
            rules.setdefault(r["column"].lower(), (r["min"], "reserved reference"))
        for col, (limit, what) in rules.items():
            for op, kind, value in preds.get(col, []):
                if kind not in ("int", "float"):
                    bad("band-not-literal",
                        f"{verb} on {st.table}: predicate on {col!r} ({op}) is {kind}; "
                        f"{what} values must be integer literals")
                elif value < limit:
                    bad("reserved-band",
                        f"{verb} on {st.table}: {col!r} {op} {int(value)} is outside the company "
                        f"{what} {limit}+ (docs/content-playbook.md 2.4)")

    # -- check 5: dry run ---------------------------------------------------- #

    def scratch_name(self) -> str:
        name = self.a.scratch_db
        if not SCRATCH_NAME_RE.match(name):
            raise OperationalError(
                f"scratch database name {name!r} must match {SCRATCH_NAME_RE.pattern}. The prefix "
                f"is hard-coded, not a CLI flag, because this run ends with a DROP DATABASE")
        if name.lower() == self.live_db.lower():
            raise OperationalError("refusing to use the live database as the scratch schema")
        if name.lower() in {"acore_world", "acore_auth",
                            "acore_characters", "acore_playerbots", "mysql",
                            "information_schema", "performance_schema", "sys"}:
            raise OperationalError(f"refusing to use {name!r} as the scratch schema")
        return name

    def table_row_counts(self) -> dict[str, int]:
        rows = self.db.query(
            "SELECT table_name, table_rows FROM information_schema.tables "
            f"WHERE table_schema = '{self.live_db}'", None)
        return {r[0]: int(r[1] or 0) for r in rows}

    def written_tables(self, new_files: list[dict]) -> set[str]:
        tables: set[str] = set()
        for f in new_files:
            text = f["path"].read_text(encoding="utf-8", errors="replace")
            for stmt_text, _ in split_statements(text):
                st = parse_statement(stmt_text, mask_sql(stmt_text), 0)
                if st.kind == "write" and st.table:
                    tables.add(st.table)
        return tables

    def live_tables(self) -> list[str]:
        return [r[0] for r in self.db.query(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = "
            f"'{self.live_db}' AND table_type='BASE TABLE' ORDER BY table_name", None)]

    def referenced_tables(self, new_files: list[dict], all_live: set[str]) -> set[str]:
        """Every live table a file mentions — writes, reads, subqueries and JOINs alike."""
        hits: set[str] = set()
        for f in new_files:
            text = f["path"].read_text(encoding="utf-8", errors="replace")
            masked = mask_sql(text)
            for name in BACKTICK_NAME_RE.findall(masked):
                low = name.lower()
                if low in all_live:
                    hits.add(low)
            # most update files write bare identifiers; a clone that missed one of them
            # turns a passing dry run into a false negative (PRO-179 review N6)
            for name in BARE_NAME_RE.findall(masked):
                low = name.lower()
                if low in all_live:
                    hits.add(low)
            for stmt_text, _ in split_statements(text):
                st = parse_statement(stmt_text, mask_sql(stmt_text), 0)
                if st.table and st.table.lower() in all_live:
                    hits.add(st.table.lower())
        return hits

    def _create_like(self, scratch: str, tables: list[str]) -> None:
        """CREATE TABLE ... LIKE for `tables`, sharded over a few connections.

        Each CREATE TABLE LIKE is a metadata-bound operation (~0.4s on this box), so a
        single connection cloning hundreds of tables dominates the run.
        """
        if not tables:
            return
        workers = max(1, min(self.a.clone_workers, len(tables)))
        shards = [tables[i::workers] for i in range(workers)]

        def shard_run(chunk: list[str]):
            script = ["SET FOREIGN_KEY_CHECKS=0;"] + [
                f"CREATE TABLE `{scratch}`.`{t}` LIKE `{self.live_db}`.`{t}`;" for t in chunk]
            return self.db.execute("\n".join(script), None)

        errors: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            for rc, _out, err in pool.map(shard_run, shards):
                if rc != 0:
                    errors.append(err[:400])
        if errors:
            raise OperationalError(f"structural clone failed: {errors[0]}")

    def fk_ancestors(self, seeds: set[str]) -> set[str]:
        """seeds plus every table they reference, transitively (what FK checks will demand)."""
        rows = self.db.query(
            "SELECT table_name, referenced_table_name FROM information_schema.key_column_usage "
            f"WHERE table_schema = '{self.live_db}' AND referenced_table_name IS NOT NULL", None)
        parents: dict[str, set[str]] = {}
        for child, parent in rows:
            parents.setdefault(child, set()).add(parent)
        needed, queue = set(seeds), list(seeds)
        while queue:
            for p in parents.get(queue.pop(), ()):
                if p not in needed:
                    needed.add(p)
                    queue.append(p)
        return needed

    def run_dry_run(self, new_files: list[dict]):
        if not new_files:
            self.r.line("5", "nothing to apply — dry run is a no-op")
            self.r.info("5", "no new files, dry run skipped (nothing could fail)", rule="dry-run")
            return
        scratch = self.scratch_name()
        mode = self.a.clone_mode
        targets = self.written_tables(new_files)
        seeds_extra: set[str] = set()
        self.r.line("5", f"scratch schema {scratch} (clone={mode}); live {self.live_db} is "
                         f"read-only; tables written: {', '.join(sorted(targets)) or 'none'}")
        rc, out, err = self.db.execute(f"DROP DATABASE IF EXISTS `{scratch}`;")
        if rc != 0:
            raise OperationalError(f"cannot drop a stale scratch schema: {err}")
        rc, out, err = self.db.execute(
            f"CREATE DATABASE `{scratch}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;")
        if rc != 0:
            raise OperationalError(f"cannot create the scratch schema: {err}")
        try:
            fk = True
            started = time.monotonic()
            if mode == "full":
                dump = self._dump_argv(self.live_db, False)
                rc, out, err = self.db.restore_pipe(dump, scratch)
                if rc != 0:
                    self.r.fail("5", f"clone of {self.live_db} into {scratch} failed: {err[:800]}",
                                rule="dry-run")
                    return
                self.r.line("5", f"full dump/restore in {time.monotonic() - started:.1f}s")
            else:
                all_live = set(self.live_tables())
                note = "full schema"
                if self.a.clone_tables == "all":
                    tables = sorted(all_live)
                else:
                    refs = self.referenced_tables(new_files, all_live)
                    seeds_extra = set(refs)
                    unknown = sorted(refs - all_live)
                    tables = sorted(self.fk_ancestors(refs))
                    note = f"referenced set: {', '.join(tables) or 'none'}"
                    if unknown:
                        self.r.warn("5", f"statement(s) name table(s) that do not exist in "
                                         f"{self.live_db}: {', '.join(unknown)}. The dry run will "
                                         f"surface the SQL error; a company file writing to an "
                                         f"unknown table is also a policy failure",
                                    rule="dry-run-coverage")
                self._create_like(scratch, tables)
                self.r.line("5", f"{len(tables)} tables created with CREATE TABLE ... LIKE in "
                                 f"{time.monotonic() - started:.1f}s ({note}; "
                                 f"--clone-tables all clones every table)")
                if mode == "auto" and self.a.copy_data:
                    started = time.monotonic()
                    oversized = self._copy_data_for(scratch, targets | seeds_extra)
                    self.r.sections.setdefault("5", [])[-1] += \
                        f" in {time.monotonic() - started:.1f}s"
                    if oversized:
                        fk = False
                        self.r.warn("5", f"FOREIGN_KEY_CHECKS disabled for the dry run because "
                                         f"{len(oversized)} referenced table(s) were left empty "
                                         f"(see --data-copy-max-rows): {', '.join(oversized)}",
                                    rule="dry-run-coverage")
                elif mode == "minimal":
                    fk = False
            applied = 0
            started = time.monotonic()
            for f in sorted(new_files, key=lambda x: x["basename"].encode()):
                rc, out, err = self.db.execute_file(f["path"], scratch, fk_checks=fk)
                if rc != 0:
                    self.r.fail("5", f"dry-run SQL error in {f['rel']} (exit {rc}):\n"
                                     f"{err.strip()[:2000]}", file=f["rel"], rule="dry-run")
                else:
                    applied += 1
                    if err.strip():
                        self.r.warn("5", f"{f['rel']} applied with warnings: {err.strip()[:400]}",
                                    file=f["rel"], rule="dry-run")
            self.r.line("5", f"{applied}/{len(new_files)} file(s) applied to {scratch} with "
                             f"FOREIGN_KEY_CHECKS={'ON' if fk else 'OFF'} in "
                             f"{time.monotonic() - started:.1f}s")
        finally:
            if self.a.keep_scratch:
                self.r.line("5", f"scratch schema {scratch} kept (--keep-scratch)")
            else:
                self.db.execute(f"DROP DATABASE IF EXISTS `{scratch}`;")

    def _dump_argv(self, database: str, no_data: bool) -> list[str]:
        return self.db._dump_argv(database, no_data)

    def _copy_data_for(self, scratch: str, targets: set[str]) -> list[str]:
        """Populate the written tables plus their FK ancestors. Returns tables left empty."""
        needed = self.fk_ancestors(targets)
        counts = self.table_row_counts()
        oversized: list[str] = []
        for t in sorted(needed):
            if t not in counts:
                continue
            if counts[t] > self.a.data_copy_max_rows:
                oversized.append(t)
                self.r.warn("5", f"{scratch}.{t} left empty (~{counts[t]} rows > "
                                 f"--data-copy-max-rows {self.a.data_copy_max_rows}); an "
                                 f"UPDATE/DELETE that matches no row is not an error, so coverage "
                                 f"is schema-only for that table", rule="dry-run-coverage")
                continue
            rc, out, err = self.db.execute(
                f"SET SESSION FOREIGN_KEY_CHECKS=0; INSERT INTO `{scratch}`.`{t}` "
                f"SELECT * FROM `{self.live_db}`.`{t}`;", None)
            if rc != 0:
                self.r.warn("5", f"could not pre-populate {t} in the scratch schema: {err[:300]}",
                            rule="dry-run-coverage")
        self.r.line("5", f"pre-populated {len(needed) - len(oversized)} table(s) "
                         f"(written tables + FK ancestors) server-side")
        return oversized

    # -- driver -------------------------------------------------------------- #

    def run(self) -> int:
        if not self.db.ping():
            raise OperationalError(
                f"cannot reach the database (db-access={self.a.db_access}, "
                f"service={self.a.db_service}). Is ac-database up?")
        dirs = self.included_dirs()
        self.check_wiring()
        files = self.collect_files(dirs)
        if self.a.boot_path:
            self.check_boot_path()
        else:
            self.r.warn("0b", "boot-path verification skipped by --no-boot-path-check. The gate has "
                              "NOT proved that the tree it scanned is the tree the container will "
                              "read.", rule="boot-path-skipped")
        self.check_duplicates(files)
        new_files = self.check_ledger(files)
        self.check_policy(new_files)
        if self.a.dry_run:
            if self.r.failed and not self.a.dry_run_anyway:
                self.r.line("5", "skipped — earlier checks already failed")
            else:
                self.run_dry_run(new_files)
        else:
            # Render must not be able to print "[5] ... ok" for a check that never ran.
            self.r.warn("5", "dry run skipped by --no-dry-run: the restart fast path. Fine for a "
                             "restart that changes no SQL, never for a SQL change — the updater "
                             "was not replayed against a scratch clone of the live schema.",
                        rule="dry-run-skipped")
        return EXIT_FAIL if self.r.failed else EXIT_OK


# --------------------------------------------------------------------------- #
# presentation
# --------------------------------------------------------------------------- #

CHECK_TITLES = {
    "0": "updater wiring",
    "0b": "boot path (mount/env)",
    "1": "duplicate basenames",
    "2": "new files vs ledger",
    "3": "hash vs ledger",
    "4": "write-statement policy",
    "5": "dry run on scratch clone",
}


def render(report: Report, args) -> str:
    out = []
    out.append("PRO-179 world-DB updater pre-flight")
    out.append(f"  compose root : {args.compose_root}")
    out.append(f"  sql root     : {report.data.get('sql_root','')}")
    out.append(f"  live database: {args.live_db} (read-only)")
    out.append(f"  scratch      : {args.scratch_db} (dropped after the run)")
    out.append("")
    for key in sorted(CHECK_TITLES):
        fails = [f for f in report.findings if f.check == key and f.severity == "FAIL"]
        warns = [f for f in report.findings if f.check == key and f.severity == "WARN"]
        status = "FAIL" if fails else ("WARN" if warns else "ok")
        out.append(f"[{key}] {CHECK_TITLES[key]:<24} {status}")
        for text in report.sections.get(key, []):
            out.append("    " + text)
        for f in fails:
            out.append(f"    !! {f.rule}: {f.message}")
        for f in warns:
            out.append(f"    ~  {f.rule}: {f.message}")
        out.append("")
    out.append("RESULT: " + ("FAIL — boot refused" if report.failed else "PASS")
               + f" ({report.data.get('elapsed_s', 0)}s)")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def build_arg_parser() -> argparse.ArgumentParser:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--compose-root", default=os.environ.get("COMPOSE_ROOT", "/mnt/data/wow-server"))
    ap.add_argument("--sql-root", default=None)
    ap.add_argument("--git-root", default=None)
    ap.add_argument("--policy", default=str(here / "preflight-policy.json"))
    ap.add_argument("--live-db", default=None)
    ap.add_argument("--scratch-db", default=None,
                    help=f"scratch schema name; must match {SCRATCH_NAME_RE.pattern}")
    ap.add_argument("--db-access", choices=["docker", "command"], default="docker")
    ap.add_argument("--db-service", default="ac-database")
    ap.add_argument("--db-container", default=None)
    ap.add_argument("--db-user", default="root")
    ap.add_argument("--db-password", default=os.environ.get("DOCKER_DB_ROOT_PASSWORD", "password"))
    ap.add_argument("--mysql-command", default="mysql -uroot",
                    help="command template for --db-access command")
    ap.add_argument("--db-timeout", type=int, default=900)
    ap.add_argument("--compose-project", default=None)
    ap.add_argument("--compose-file", action="append", default=None)
    ap.add_argument("--no-dry-run", dest="dry_run", action="store_false", default=True)
    ap.add_argument("--no-boot-path-check", dest="boot_path", action="store_false", default=True,
                    help="skip the compose/mount verification (audit of a remote tree only; a "
                         "skipped check means the gate never proved which files the boot reads)")
    ap.add_argument("--dry-run-anyway", action="store_true",
                    help="run the dry run even if earlier checks failed")
    ap.add_argument("--clone-mode", choices=["auto", "full", "minimal"], default="auto",
                    help="auto: CREATE TABLE ... LIKE for every table plus server-side copies of "
                         "the written tables and their FK ancestors (fast, FK checks on). "
                         "full: mysqldump of the live schema (slowest, most faithful). "
                         "minimal: structure only, FK checks off.")
    ap.add_argument("--clone-tables", choices=["referenced", "all"], default="referenced",
                    help="referenced (default): clone only the tables the new files mention plus "
                         "their FK ancestors. all: clone every table in the live schema.")
    ap.add_argument("--clone-workers", type=int, default=6,
                    help="parallel connections used for CREATE TABLE ... LIKE")
    ap.add_argument("--no-copy-data", dest="copy_data", action="store_false", default=True)
    ap.add_argument("--data-copy-max-rows", type=int, default=200000)
    ap.add_argument("--keep-scratch", action="store_true")
    ap.add_argument("--strict-all", action="store_true",
                    help="enforce the write policy on upstream-provenanced files too")
    ap.add_argument("--policy-all", action="store_true",
                    help="same as --strict-all but keeps the origin note")
    ap.add_argument("--clean-dead-ref-max-count", type=int, default=3)
    ap.add_argument("--max-dead-list", type=int, default=20)
    ap.add_argument("--json", dest="json_out", default=None)
    ap.add_argument("--json-only", action="store_true")
    return ap


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    # `--compose-file` is `append`, so it is None unless someone passed one.
    args.compose_file = args.compose_file or []
    policy_path = Path(args.policy)
    if not policy_path.is_file():
        print(f"preflight: policy file not found: {policy_path}", file=sys.stderr)
        return EXIT_ERROR
    policy = json.loads(policy_path.read_text())
    if args.live_db is None:
        args.live_db = policy.get("liveDatabase", "acore_world")
    if args.scratch_db is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.scratch_db = f"{SCRATCH_PREFIX}_{os.getpid()}_{stamp}"

    report = Report()
    report.data["started"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    t0 = time.monotonic()
    try:
        pf = Preflight(args, policy, report)
        report.data["sql_root"] = str(pf.sql_root)
        rc = pf.run()
    except OperationalError as exc:
        print(f"preflight: operational error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    report.data["elapsed_s"] = round(time.monotonic() - t0, 1)
    if not args.json_only:
        print(render(report, args))
    if args.json_out:
        payload = {
            "result": "fail" if report.failed else "pass",
            "args": {k: str(v) for k, v in vars(args).items()},
            "data": report.data,
            "sections": report.sections,
            "findings": [f.__dict__ for f in report.findings],
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2, sort_keys=True))
    return rc


if __name__ == "__main__":
    sys.exit(main())
