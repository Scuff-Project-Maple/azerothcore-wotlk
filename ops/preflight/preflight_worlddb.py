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
        # `input=None` must not leave the child on the gate's own stdin: check [0] runs
        # `docker compose exec -T ac-database mysql ...`, and with no terminal that
        # mysql inherits the gate's stdin and drains it. A caller piped into
        # `ssh host 'bash -s'` then loses the rest of its own script after the first
        # DB check — it prints a marker and silently stops (PRO-307 A4). DEVNULL is the
        # only honest "no stdin" here, and `input` and `stdin` cannot both be passed.
        feed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        try:
            proc = subprocess.run(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=timeout or self.timeout, cwd=str(self.compose_root), **feed,
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
                                  stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                  timeout=self.timeout * 4, cwd=str(self.compose_root))
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
    """(schema, table) for a possibly qualified name token.

    The split is the backtick-aware one in `_ident_parts()`, so a name that contains a
    dot is one component and not two: `` `acore_world`.`my.table` `` is `my.table` in
    schema `acore_world`, not the string ``acore_world`.`my.table`` (PRO-179 review
    rank 1). Callers that must decide whether a schema was *claimed* use
    `_ident_parts()`/`_schema_of()` instead, which still see the quoting.
    """
    parts = _ident_parts(token)
    if len(parts) == 2:
        return unquote_ident(parts[0]), unquote_ident(parts[1])
    if len(parts) == 1:
        return None, unquote_ident(parts[0])
    return None, ".".join(parts)


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


def _ident_parts(token: str) -> list[str]:
    """The identifier components of a possibly qualified name token.

    Split on the dots that sit *outside* backticks. ``token.split(".")`` is wrong: a
    backticked name may contain a dot, and `` `acore_world`.`my.table` `` then yields
    three pieces of which the last is ``table` `` — not quote-balanced, so the bare-name
    test said "heuristic" about a fully quoted name, and the all-quoted test said
    "invented" about a schema the file really did quote (PRO-179 review rank 1). Both
    judgements are made per component, so both have to come from a tokenisation that
    knows where the backticks are. An unbalanced run leaves the remainder in one piece,
    which the region grammar then rejects anyway.
    """
    parts: list[str] = []
    cur: list[str] = []
    quoted = False
    for ch in token:
        if ch == "`":
            quoted = not quoted
            cur.append(ch)
        elif ch == "." and not quoted:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


def _is_quoted(part: str) -> bool:
    return len(part) >= 2 and part.startswith("`") and part.endswith("`")


def _schema_of(parts: list[str]) -> str | None:
    """The schema a qualified name claims, or None when it does not earn being believed.

    A component is believed only when the writer quoted it. Nothing is ever read out of
    bare text: ``a.b.c`` names no schema, while `` `other`.updates `` names `other` even
    though the table half is bare (PRO-179 review rank 1).
    """
    if len(parts) == 2 and _is_quoted(parts[0]):
        return unquote_ident(parts[0])
    return None


def write_targets(st: Statement,
                  known_names: set[str] | None = None) -> tuple[list[tuple[str | None, str]],
                                                                 set[str], str | None, str,
                                                                 bool]:
    """Parse the region between the verb and the first top-level SET/WHERE.

    Returns `([(schema, table), ...], aliases, error, region, heuristic)`. A
    well-formed single-table write yields one target plus any alias, which the caller
    needs so `WHERE c.guid = 9002020` counts as bounding `creature.guid`. Anything
    else yields an error string, the tables the region still names in a table
    position (the ledger among them), the other names it mentions, and whether those
    names came from the bare-name scan rather than from quoted identifiers — see
    `_region_tables()`. `known_names` is the policy/ledger table-name set, which
    keeps a real table that happens to spell a keyword visible.

    Only one write target is accepted. MySQL's multi-table forms (`UPDATE a JOIN
    b`, `UPDATE a, b`, `DELETE a, b FROM ...`, derived tables, index hints) are
    rejected rather than parsed, because a partial reading of them is exactly how
    the rev-1 gate let `UPDATE creature c JOIN updates u SET u.hash = ...`
    through: only the first table was ever examined.
    """
    masked = st.masked
    # Clause keywords are located in a backtick-blanked copy. Backticked content is a
    # name, never syntax, so an identifier spelled `from` cannot be mistaken for the
    # FROM keyword (PRO-179 review NB-2). blank_backticked() is length-preserving, so
    # every offset found in `bt` still slices `masked` correctly.
    bt = blank_backticked(masked)
    from_end: int | None = None
    if st.verb == "UPDATE":
        head = UPDATE_HEAD_RE.match(masked)
        if not head:
            return [], set(), "could not locate the UPDATE keyword", "", False
        stops = top_level_positions(bt, "SET")
        lo, hi = head.end(), (stops[0] if stops else len(masked))
        pre = ""
    elif st.verb == "DELETE":
        head = DELETE_HEAD_RE.match(masked)
        if not head:
            return [], set(), "could not locate the DELETE keyword", "", False
        froms = top_level_positions(bt, "FROM")
        if not froms:
            return [], set(), "DELETE has no FROM clause", "", False
        pre = masked[head.end():froms[0]]
        from_end = froms[0] + len("FROM")
        lo = from_end
        ends = [p for p in (top_level_positions(bt, "WHERE")
                            + top_level_positions(bt, "ORDER")
                            + top_level_positions(bt, "LIMIT")) if p > lo]
        hi = min(ends) if ends else len(masked)
    else:
        return [], set(), None, "", False

    if pre.strip():
        region = masked[head.end():hi]
        tables, others, heuristic = _region_tables(masked, head.end(), hi, from_end, known_names)
        return tables, others, \
            f"table list {pre.strip()[:60]!r} before FROM is multi-table DELETE syntax", \
            region, heuristic
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
        tables, others, heuristic = _region_tables(masked, lo, hi, from_end, known_names)
        return tables, others, err, region, heuristic
    schema, table = split_qualified(m.group("tbl"))
    aliases = {unquote_ident(m.group("alias")).lower()} if m.group("alias") else set()
    return [(schema, table)], aliases, None, region, False


def _region_names(fragment: str) -> list[str]:
    """Identifiers in a region fragment, a qualified name counted as one name."""
    return [m.group(0) for m in REGION_NAME_RE.finditer(fragment)]


def _region_tables(masked: str, lo: int, hi: int, from_end: int | None = None,
                   known_names: set[str] | None = None) -> tuple[
                       list[tuple[str | None, str]], set[str], bool]:
    """(tables, other names, heuristic) for a rejected write region, in order of first appearance.

    Used only on the rejection path, so the report can name the table a rejected
    statement was reaching for (the ledger among them). The region is split on the
    *syntax* keywords with backticked names blanked, and inside each fragment the
    first name is the table position while the rest are aliases and qualifiers:
    ``UPDATE `creature` `c` JOIN `updates` `u` SET`` names two tables, `creature` and
    `updates`, and `c`/`u` are names, not tables — running them through the
    allow-list check reported a phantom `table-not-allowed` for each (PRO-179
    review NB-2).

    Four refinements, all report fidelity on an already-rejected statement:

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
      `updates` and `creature` instead of `u` and `c` (PRO-179 review NB-B);
    * the third return value says whether any name in the *table list* came from the
      bare-name scan. The bare scan cannot tell a table from an `ON`-clause column or
      an index hint, so a name list built from it is a hint at what to look at, not a
      verdict — the caller labels both lists it returns as such (PRO-179 review NB-3,
      rank 4). Names that only reach the `others` set are noise whatever their
      spelling, so they do not flip it.

    A schema is believed only when the writer quoted the half that claims one, which is
    what `_ident_parts()` / `_schema_of()` decide from the token's own backticks. Where
    the scan invented one from bare tokens, `cross-schema-write` accused files that never
    name a schema, and it is the one accusation in this path that costs an engineer an
    afternoon (PRO-179 review NB-3); where it dropped one the file did quote, the report
    lost the accusation instead (PRO-179 review rank 1). `ledger-table` and
    `table-not-allowed` still fire on the table name, which is what a reviewer acts on.
    A name is never dropped for spelling a keyword when the policy or the ledger actually
    has a table of that name (PRO-179 review NB-4).

    What a bare-name scan cannot tell is an index hint: `USE INDEX (idx)` contributes
    `idx` as a name. That is noise in a message about a statement that is already
    refused, never a silently-passing write — a name this function misses can only
    cost report fidelity, because `multi-table-write` is raised independently of it.
    """
    raw = masked[lo:hi]
    # The PARTITION blank must be length-preserving: the fragment bounds below are
    # derived from `scan` and then used to slice `raw`, so a substitution that shrinks
    # the string puts every name past it out of phase and compares a raw-space offset
    # against a scan-space one (PRO-179 review NB-1).
    scan = blank_backticked(PARTITION_CLAUSE_RE.sub(
        lambda m: " " * (m.end() - m.start()), raw))
    bounds = [0] + [m.end() for m in REGION_SPLIT_RE.finditer(scan)] + [len(scan)]
    from_off = None if from_end is None else from_end - lo
    known = {k.lower() for k in (known_names or ())}

    def record(token: str) -> tuple[str, tuple[str | None, str], bool] | None:
        parts = _ident_parts(token)
        if not parts:
            return None
        table = split_qualified(token)[1]
        if not table:
            return None
        tbl_bare = not _is_quoted(parts[-1])
        # The stopword list removes syntax noise where a bare name is expected. A
        # policy table whose real name spells a keyword is not noise.
        if tbl_bare and table.lower() in REGION_NAME_STOPWORDS and table.lower() not in known:
            return None
        # Believed only when the writer quoted the half that claims a schema; `parts`
        # is the only thing left here that still knows where the backticks were.
        schema = _schema_of(parts)
        return table.lower(), (schema, table), tbl_bare

    parsed: list[tuple[bool, list[tuple[int, str, tuple[str | None, str], bool]]]] = []
    aliases: set[str] = set()
    for a, b in zip(bounds, bounds[1:]):
        in_from = from_off is None or a >= from_off
        hits: list[tuple[int, str, tuple[str | None, str], bool]] = []
        for idx, token in enumerate(_region_names(raw[a:b])):
            hit = record(token)
            if hit is None:
                continue
            key, ident, tbl_bare = hit
            hits.append((idx, key, ident, tbl_bare))
        parsed.append((in_from, hits))
        if in_from:
            aliases.update(key for idx, key, _, _ in hits if idx)

    # One pass in region order, so "in order of first appearance" is what the caller
    # gets: a pre-FROM delete target used to be appended after every FROM-list table
    # (PRO-179 review NB-5). The alias set is known from the pass above, so a delete
    # target that is really an alias is still skipped where it appears.
    tables: dict[str, tuple[str | None, str]] = {}
    seen: set[str] = set()
    heuristic = False
    for in_from, hits in parsed:
        for idx, key, ident, tbl_bare in hits:
            seen.add(key)
            if in_from:
                if idx == 0:
                    if key not in tables and tbl_bare:
                        heuristic = True
                    tables.setdefault(key, ident)
            elif key not in aliases:
                if key not in tables and tbl_bare:
                    heuristic = True
                tables.setdefault(key, ident)
    return list(tables.values()), {k for k in seen if k not in tables}, heuristic


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
        # Through `_git`, not a bare `subprocess.run`: the gate's "nothing in here ever
        # reaches the network" claim is only as wide as the single place git is invoked
        # (PRO-291 review N5).
        rc, out, _err = self._git(["log", "-1", "--format=%ae\t%an\t%s", "--", rel],
                                  timeout=30)
        if rc is None:
            return "unknown", "git unavailable"
        if rc != 0:
            return "unknown", "not a git tree"
        line = out.strip()
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

        stdin is DEVNULL, not inherited: the gate is routinely piped into
        `ssh host 'bash -s'`, and a git child left on the caller's fd 0 drains the rest
        of that script (PRO-307 A4; review N3 for the probes this one missed).
        """
        try:
            proc = subprocess.run(["git", *argv], cwd=str(self.git_root),
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, timeout=timeout)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            return None, "", str(exc)
        return (proc.returncode,
                proc.stdout.decode("utf-8", errors),
                proc.stderr.decode("utf-8", "replace").strip())

    def _reflog_path(self, refname: str) -> str:
        """The path of `refname`'s reflog in this clone, or '' when it holds none.

        The reflog file — not the ref — is the fetch fingerprint. Measured on git
        2.43.0: `git clone` creates `refs/remotes/<remote>/*` refs itself and writes
        no reflog for them, while `git fetch`, `git pull` and `git update-ref` all
        leave one behind (PRO-291 review B1).
        """
        rc, gp, _err = self._git(["rev-parse", "--git-path", f"logs/{refname}"])
        if rc != 0 or not gp.strip():
            return ""
        declared = gp.strip()
        p = Path(declared)
        resolved = p if p.is_absolute() else self.git_root / p
        return declared if resolved.exists() else ""

    def _reflog_present(self, refname: str) -> bool:
        return bool(self._reflog_path(refname))

    # One line per ref a fetch actually brought down: `<sha>\t\tbranch 'Playerbot' of
    # <url>`. A fetch that never reached its remote writes no such line — measured on
    # git 2.43.0 with GIT_ALLOW_PROTOCOL=none it exits 128 and leaves FETCH_HEAD at
    # 0 bytes (sha256 e3b0c44298fc…, the empty-string digest), so `Path.exists()` on
    # that file is not evidence that anything was fetched (PRO-307 A1).
    #
    # 40 or 64 hex digits, because that is all `git fetch` ever writes there: FETCH_HEAD
    # carries full object names, never the abbreviated ones `git log` prints (review
    # N6). A shorter hex run is therefore not a fetched-ref line, and the branch below
    # reports it as content that does not parse rather than counting it as a fetch.
    FETCH_HEAD_ENTRY_RE = re.compile(r"^([0-9a-fA-F]{40,64})(?:\s|$)")

    def _fetch_head_evidence(self) -> tuple[bool, str]:
        """Did a fetch leave anything readable in FETCH_HEAD? (bool, detail) — local only.

        An absent, 0-byte or whitespace-only FETCH_HEAD answers "no". That is the shape
        a *failed* fetch leaves behind, and counting mere existence as fetch evidence
        turned a bootable clone into a refusal whose recovery text blamed the operator
        for a deletion they had not made (PRO-307 A1). Content that names no fetched
        object is still not empty, so it stays evidence — the gate must not hand the
        exemption back just because a file holds something unexpected — but it is
        reported as what it is, never as a fetch this gate observed.

        A file the OS will not hand over is a third answer, and it must not borrow the
        sentence of either of the other two (review N7). `stat` needs no read permission,
        so the gate still asks how big the thing is: 0 bytes is exactly the shape a failed
        fetch leaves, and a non-zero file it cannot read is content somebody put there —
        evidence it cannot inspect, which is not the same as no record.
        """
        rc, fp, _err = self._git(["rev-parse", "--git-path", "FETCH_HEAD"])
        if rc != 0 or not fp.strip():
            return False, ""
        declared = fp.strip()
        p = Path(declared)
        if not p.is_absolute():
            p = self.git_root / p
        try:
            raw = p.read_bytes()
        except OSError as exc:
            try:
                size = p.stat().st_size
            except OSError:
                # Neither readable nor sizeable: the gate observed nothing about the
                # contents, and existence alone was never fetch evidence (A1). Answering
                # "no" here keeps the never-fetched clone's WARN rather than accusing it
                # of a deletion, which is the failure mode this function exists to avoid.
                return False, ""
            if size == 0:
                return False, ""
            return True, (f"{declared} holds {size} byte(s) this process cannot read "
                          f"({exc.strerror}), which is more than a failed fetch leaves but "
                          f"is not a fetch this gate can read")
        if not raw.strip():
            return False, ""
        matched = [m for m in (self.FETCH_HEAD_ENTRY_RE.match(ln)
                               for ln in raw.decode("utf-8", "replace").splitlines()) if m]
        if matched:
            return True, (f"{declared} lists {len(matched)} fetched ref(s), the first at "
                          f"{matched[0].group(1)[:12]}, so a fetch completed in this clone")
        return True, (f"{declared} holds {len(raw)} byte(s) that do not parse as fetched-ref "
                      f"lines, which is more than a failed fetch leaves but is not a fetch "
                      f"this gate can read")

    # -- durable last-verified attestation (PRO-300) ---------------------------- #

    # Where a satisfied gate leaves its proof. `missing_ref_evidence()` reads files a
    # *fetch* writes, which is the right question for "did this clone ever fetch" but
    # it cannot answer "was this ref ever *verified by this gate*", and `git update-ref -d`
    # plus `rm FETCH_HEAD` plus `rm -rf .git/logs/refs/remotes` erases all three signals
    # at once and leaves a never-carried clone byte-identical to a stripped one
    # (PRO-297 review, the residual PRO-291 could not close). This record lives in the
    # common dir instead, where none of those three commands reach.
    ATTESTATION_DIR_NAME = "paperclip-preflight"
    ATTESTATION_FILE_NAME = "last-verified"
    ATTESTATION_VERSION = 1

    ATTEST_ABSENT = "absent"        # no record for this ref -> the WARN path is honest
    ATTEST_RECORDED = "recorded"    # a satisfied boot wrote it for this ref
    ATTEST_UNTRUSTED = "untrusted"  # a record exists that the gate cannot vouch for

    ATTESTATION_LINE_RE = re.compile(
        r"^last-verified\s+(\S+)\s*=\s*([0-9a-fA-F]{7,64})\s*@\s*(\S+)$")

    def git_common_dir(self) -> "Path | None":
        """This tree's git common dir, or None when git will not say.

        The *common* dir and not the work tree: it survives everything the residual
        deletes, a linked worktree shares it with its siblings, and — the part AC 6
        depends on — a fresh `git clone` of a remote that never carried the ref gets a
        common dir with no attestation in it, so the honest answer for that clone is
        still `absent`. Asking git rather than hardcoding `.git` is the same reason
        `_reflog_path()` asks: in a worktree the admin dir is not `.git`.

        No answer means no state directory at all, never a guessed one: an attestation
        written somewhere untrustworthy is worse than none, and a probe nobody answers
        must not become a finding.
        """
        cached = getattr(self, "_git_common_dir_cache", _UNSET)
        if cached is not _UNSET:
            return cached
        rc, cd, _err = self._git(["rev-parse", "--git-common-dir"])
        value = None
        if rc == 0 and cd.strip():
            p = Path(cd.strip())
            value = p if p.is_absolute() else self.git_root / p
        self._git_common_dir_cache = value
        return value

    def attestation_file(self) -> "Path | None":
        """The last-verified file for this tree, or None when it cannot be placed.

        `--preflight-state-dir` names a directory to hold the record instead of the one
        under the common dir (a test or an operator running the gate over a tree they do
        not want written inside); it is read with `getattr` because callers such as
        `corpus_sweep.py` hand-roll an args object that has no such attribute.
        """
        cached = getattr(self, "_attestation_file_cache", _UNSET)
        if cached is not _UNSET:
            return cached
        override = str(getattr(self.a, "preflight_state_dir", "") or "").strip()
        if override:
            p = Path(override)
            # A directory, as its name and its `--help` say, holding one file. The
            # default location below is a file path, so the two branches have to be
            # read carefully: an operator who points the flag at a state directory must
            # not end up with the directory itself overwritten.
            d = p if p.is_absolute() else self.git_root / p
            value = d / self.ATTESTATION_FILE_NAME
        else:
            common = self.git_common_dir()
            value = None if common is None else (
                common / self.ATTESTATION_DIR_NAME / self.ATTESTATION_FILE_NAME)
        self._attestation_file_cache = value
        return value

    def remote_url_for_ref(self, ref: str) -> str:
        """`remote.<name>.url` for the remote `ref` names, or '' when there is none.

        The record is bound to the remote it was earned against, so a record copied in
        from another clone — the obvious way to fake a satisfied past — is
        `untrusted` rather than a pass. '' means "cannot compare", not "empty is fine":
        a clone with no remote configured writes no url line and compares nothing.
        """
        if not ref.startswith("refs/remotes/"):
            return ""
        remote = ref[len("refs/remotes/"):].partition("/")[0]
        if not remote:
            return ""
        rc, out, _err = self._git(["config", "--get", f"remote.{remote}.url"])
        if rc != 0:
            return ""
        return out.strip()

    # Which check refused an untrusted record, so the caller can tell the shapes a boot
    # of this gate could never have produced ("junk", "version", "repo", "url") from the
    # one that is an environment accident ("unreadable"). Only the first group refuses a
    # ref that is otherwise there; see `upstream_ref()`.
    ATTEST_CAUSES = ("unreadable", "junk", "version", "repo", "url")

    def attestation_record(self, ref: str) -> tuple[str, str, str, str, str]:
        """What the last-verified file says about `ref`.

        (status, why, attestedOid, attestedAt, cause) with status in ATTEST_ABSENT /
        ATTEST_RECORDED / ATTEST_UNTRUSTED, and `cause` one of ATTEST_CAUSES (or ''
        when nothing refused).

        Untrusted is deliberate and named, because the whole value of the record is
        that a *satisfied* gate wrote it: a file that is unparsable, unversioned,
        carries a version this gate does not understand, names a different repo, or
        names a different remote url is not that. Absent is different in kind — the
        ref has simply never been verified here, which is the pre-PRO-300 answer and
        still a WARN (AC 6).

        The order of the checks is the answer to review B1. "This file says nothing
        about `ref`" is settled before the repo and remote-url bindings are asked, so a
        record copied in from elsewhere whose lines name only *other* refs cannot turn a
        clone that honestly never verified this one into a FAIL — the operator's only
        way out of that was `rm`, which is the act the record is meant to detect. What
        is still settled first is what a record of ours can never look like: an
        unparsable line, or a version this gate does not write.
        """
        path = self.attestation_file()
        if path is None:
            return (self.ATTEST_ABSENT,
                    "no attestation state directory could be placed for this tree",
                    "", "", "")
        if not path.exists():
            return (self.ATTEST_ABSENT, f"no attestation file at {path}",
                    "", "", "")
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return (self.ATTEST_UNTRUSTED, f"{path} exists but could not be read ({exc})",
                    "", "", "unreadable")
        version: int | None = None
        repo_line = ""
        url_line = ""
        mine: tuple[str, str] | None = None
        junk: list[str] = []
        for line in raw.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("version "):
                try:
                    version = int(s[len("version "):].strip())
                except ValueError:
                    junk.append(s)
                continue
            if s.startswith("repo "):
                repo_line = s[len("repo "):].strip()
                continue
            if s.startswith("remote url "):
                url_line = s[len("remote url "):].strip()
                continue
            m = self.ATTESTATION_LINE_RE.match(s)
            if not m:
                junk.append(s)
            elif m.group(1) == ref:
                mine = (m.group(2), m.group(3))
        if junk:
            return (self.ATTEST_UNTRUSTED,
                    f"{path} holds {len(junk)} line(s) this gate cannot parse, "
                    f"starting {junk[0][:80]!r}", "", "", "junk")
        if version != self.ATTESTATION_VERSION:
            # One branch, two sentences. "It does not say what it is" and "it says
            # something this gate cannot read" are both reasons not to trust the file,
            # and neither is the same answer as "it holds nothing about this ref".
            said = ("names no `version`, so this gate cannot know what it is "
                    "promising" if version is None else
                    f"is version {version}; this gate writes and reads version "
                    f"{self.ATTESTATION_VERSION} only")
            return (self.ATTEST_UNTRUSTED, f"{path} {said}", "", "", "version")
        # Before the binding checks, on purpose (review B1): a record that holds nothing
        # about `ref` says nothing about `ref` no matter whose common dir it names, and
        # reading it as untrusted here would let a foreign file refuse a boot that the
        # record itself never claimed to cover.
        if mine is None:
            return (self.ATTEST_ABSENT,
                    f"{path} holds no `last-verified` line for {ref}", "", "", "")
        # A record is only this tree's history if it says it is. `write_attestation()`
        # refuses to write one it cannot bind for exactly this reason, so a record of
        # ours always carries both lines and a file that carries neither is not one of
        # ours (review N5 / the `repo unknown` nit). '' on either side means git could
        # not say, which is "cannot compare", not "the file is wrong".
        common = self.git_common_dir()
        expect_repo = ""
        if common is not None:
            try:
                expect_repo = str(Path(common).resolve())
            except OSError:
                expect_repo = str(common)
        if expect_repo:
            if not repo_line:
                return (self.ATTEST_UNTRUSTED,
                        f"{path} names no `repo`, so this gate cannot tell which "
                        f"clone it was earned in (this tree is {expect_repo})",
                        "", "", "repo")
            try:
                got_repo = str(Path(repo_line).resolve())
            except OSError:
                got_repo = repo_line
            if got_repo != expect_repo:
                return (self.ATTEST_UNTRUSTED,
                        f"{path} was written for repo {repo_line}, not {expect_repo}: "
                        f"a record earned in another clone is not this one's history",
                        "", "", "repo")
        want_url = self.remote_url_for_ref(ref)
        if want_url and url_line and url_line != want_url:
            # Compared when both sides can name a url, unlike `repo`, which is required:
            # this gate always knows its common dir (it refuses to write a record it
            # cannot bind) but a tree may legitimately have no remote configured, and a
            # record written before `git remote add` is not a forgery.
            return (self.ATTEST_UNTRUSTED,
                    f"{path} was written against remote url {url_line}, but this "
                    f"tree's remote url is {want_url}", "", "", "url")
        return self.ATTEST_RECORDED, f"attested in {path}", mine[0], mine[1], ""

    def write_attestation(self, ref: str, oid: str) -> tuple[str, str]:
        """Record "`ref` = `oid` verified now", keeping other refs' lines.

        Returns (detail, timestamp actually written) — the timestamp is empty when
        nothing was written, so the caller never reports a record it does not have.


        Never a finding: the attestation is evidence *for* a later deletion check and
        must never become a precondition for booting, so a failure to write is an
        informational note (AC 6). Written via a temp file + `os.replace` because a
        record truncated by a crash would then fail closed every later boot.
        """
        if not ref or not oid:
            return "nothing to attest", ""
        path = self.attestation_file()
        if path is None:
            return "no attestation state directory could be placed; not written", ""
        common = self.git_common_dir()
        repo_line = ""
        if common is not None:
            try:
                repo_line = str(Path(common).resolve())
            except OSError:
                repo_line = str(common)
        if not repo_line:
            # An unbound record is a liability rather than a memory: `attestation_record()`
            # reads a record with no `repo` as one it cannot vouch for, so writing one now
            # would only manufacture the next boot's FAIL. No binding, no record — and the
            # boot still stands, because this is evidence *for* a later deletion check.
            return ("could not determine this tree's git common dir; record not written",
                    "")
        url_line = self.remote_url_for_ref(ref)
        keep: list[str] = []
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8",
                                           errors="replace").splitlines():
                    s = line.strip()
                    if not s or s.startswith("#") or s.startswith("version "):
                        continue
                    if s.startswith("repo ") or s.startswith("remote url "):
                        continue
                    m = self.ATTESTATION_LINE_RE.match(s)
                    if not m or m.group(1) != ref:
                        keep.append(s)
            except OSError:
                keep = []
        when = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds")
        body = [f"# paperclip-preflight last-verified (version {self.ATTESTATION_VERSION})",
                f"# written by ops/preflight/preflight_worlddb.py when "
                f"upstreamProvenance.verifiedRef resolved to a remote-tracking "
                f"commit whose tree the gate could read.",
                f"# Deleting this file does not restore the ref; it only removes the "
                f"gate's memory of having verified it.",
                f"version {self.ATTESTATION_VERSION}",
                f"repo {repo_line}"]
        if url_line:
            body.append(f"remote url {url_line}")
        body.extend(keep)
        body.append(f"last-verified {ref} = {oid} @ {when}")
        tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text("\n".join(body) + "\n", encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            return f"could not write {path}: {exc}", ""
        return f"recorded {ref} = {oid[:12]} @ {when} in {path}", when

    def oid_is_ancestor(self, older: str, newer: str):
        """True/False from `git merge-base --is-ancestor`, or None if git cannot answer.

        The attestation names one commit; the ref legitimately moves between boots, so
        "the oid differs" on its own would fault every ordinary `git pull`. What is not
        ordinary is a remote-tracking ref moved to a commit that does not *contain* the
        one this gate last verified: a fetch fast-forwards, and only `git update-ref`
        puts a ref somewhere its previous position is not an ancestor of.
        """
        if not older or not newer:
            return None
        rc, _out, _err = self._git(["merge-base", "--is-ancestor", older, newer])
        if rc == 0:
            return True
        if rc == 1:
            return False
        return None

    def consult_attestation(self, up: dict) -> None:
        """Read the last-verified record for `up['ref']` into `up`.

        Read-only by design. Writing belongs to the one place that has earned it — the
        `ok`+remote-tracking path after the tree could be listed — so a ref that is
        missing, untrustworthy or not a commit can never fabricate a record.

        Two things read here change the *shape* of the run, both of them refusals and
        neither of them an exemption:

        - a ref whose attested commit is not an ancestor of where it points now, with no
          reflog to explain the move, is not the history this gate verified, so
          `upstream_ref()` does not even ask `ls-tree` about it (review N4: never ask a
          ref the gate does not trust for the list of files it vouches for);
        - a record that is not one this gate could have written (`cause` in
          ATTEST_CAUSES, other than `unreadable`) is refused the same way even when the
          ref is there and peels to a commit — an untrusted record must never act as if
          it were simply absent on the path that ends in a write, or appending one junk
          line is all it takes to buy back the exemption and have the forged oid
          stamped as verified (PRO-300 review, finding 1).
        """
        ref = up.get("ref") or ""
        path = self.attestation_file()
        status, why, oid, when, cause = self.attestation_record(ref)
        up["attestation"] = status
        up["attestationDetail"] = why
        up["attestedOid"] = oid
        up["attestedWhen"] = when
        up["attestationCause"] = cause
        up["attestationPath"] = "" if path is None else str(path)
        current = up.get("oid") or ""
        if status != self.ATTEST_RECORDED or not oid or not current or oid == current:
            return
        ancestor = self.oid_is_ancestor(oid, current)
        if ancestor is True:
            return
        # A fetch fast-forwards a remote-tracking ref, and a genuine force-push leaves a
        # reflog. Nothing else explains a ref whose last verified position is not an
        # ancestor of where it points now — `git update-ref` putting the tracked ref on
        # commits of the operator's choosing is exactly that shape.
        up["oidMismatch"] = {
            "attested": oid,
            "current": current,
            "why": ("git could not answer whether it is an ancestor of it"
                    if ancestor is None else
                    "it is not an ancestor of the commit the ref resolves to now"),
            "explained": self._reflog_present(ref),
        }

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

    def allow_missing_upstream_ref(self) -> bool:
        """Did the operator explicitly opt into the missing-ref exemption?

        Off by default on the boot path. The permissive answer to "the tracked
        upstream ref is not here" used to be inherited by simply saying nothing;
        `--allow-missing-upstream-ref` makes it a decision that has to be typed
        (PRO-248 review N1, opening 2).
        """
        return bool(getattr(self.a, "allow_missing_upstream_ref", False))

    def missing_ref_evidence(self, ref: str) -> tuple[bool, str, str]:
        """Has this clone ever held `ref`? (evidence, what was found, certainty) — local only.

        `missing` is the one ref answer that keeps the upstream exemption, so the
        gate has to tell "this clone has never fetched" from "the ref was here and
        someone deleted it". Both used to produce the same WARN, which put exactly
        one command — `git update-ref -d refs/remotes/origin/Playerbot` — between
        the boot gate and the pre-PRO-248 directory+author exemption the author
        forger can satisfy (PRO-248 review N1, opening 2; the same class of hole B1
        closed for the ambiguous-name case).

        Three local signals, none of which touch the network. What they test is
        *fetching*, so each one is a file a fetch writes and `git clone` does not:

        - the ref's own reflog, at `git rev-parse --git-path logs/<ref>`. Measured on
          git 2.43.0: `git update-ref` *creates* that file and `git update-ref -d`
          deletes it again, so on its own it only catches a ref removed some other
          way (a loose-ref `rm`, a rename). It is the signal the review named, so it
          is checked; it is not sufficient, which is why the other two are here.
        - `FETCH_HEAD`, but only what a fetch actually **wrote**. `git fetch` opens the
          file before it has anything to record, so a fetch that fails on transport
          leaves it present and empty; existence alone is not evidence and a 0-byte or
          whitespace-only file counts as none (PRO-307 A1).
        - any sibling ref under the same `refs/remotes/<remote>/` namespace other
          than `<remote>/HEAD` that carries **its own reflog**. The reflog is the
          point: `git clone` populates `refs/remotes/<remote>/*` itself, so a sibling
          *ref* says only that the remote had that branch when the clone was made —
          it is not evidence that anyone ever fetched, and counting it as such
          refused a boot to an untouched clone (PRO-291 review B1).

        A genuinely never-fetched clone has none of the three and keeps its `WARN`.
        FETCH_HEAD is also not monotone: a good fetch writes it and one later failed
        fetch truncates it back to 0 bytes, erasing that signal on its own. That is
        why it is one of three probes rather than the answer, and why the durable
        attestation below exists.

        `certainty` says how far the evidence actually reaches: 'held' when this
        clone holds `ref`'s own reflog, which is a record of *this* ref having been
        here; 'fetched' when the strongest signal is someone else's — a FETCH_HEAD
        that names fetched refs, or a sibling reflog — which proves the clone has
        fetch history but not that the configured ref ever arrived. Callers must not
        assert a deletion on 'fetched'.
        """
        found: list[str] = []
        certainty = ""
        own = self._reflog_path(ref)
        if own:
            found.append(f"its reflog ({own}) is still in this clone")
            certainty = "held"
        fetch_head, fh_detail = self._fetch_head_evidence()
        if fetch_head:
            found.append(fh_detail)
        if ref.startswith("refs/remotes/"):
            remote = ref[len("refs/remotes/"):].partition("/")[0]
            ns = f"refs/remotes/{remote}/"
            # `for-each-ref`, not `git show-ref refs/remotes/<remote>/`: show-ref matches
            # its arguments as anchored patterns against whole ref names, so a bare
            # namespace prefix answers "no refs" (measured on git 2.43.0) and the
            # namespace probe would never fire.
            rc, body, _err = self._git(["for-each-ref", "--format=%(refname)", ns])
            if rc == 0:
                sibs = [ln.strip() for ln in body.splitlines() if ln.strip()]
                sibs = [s for s in sibs if s != f"{ns}HEAD" and s != ref]
                # B1: a cloned sibling is not fetch evidence. Keep only siblings whose
                # own reflog survives — the file clone leaves absent and fetch leaves.
                sibs = [s for s in sibs if self._reflog_present(s)]
                if sibs:
                    found.append(f"{len(sibs)} other {ns}* refs carry a reflog in this "
                                 f"clone, including {sibs[0]}")
        if found and not certainty:
            certainty = "fetched"
        return (bool(found), "; ".join(found), certainty if found else "")

    def upstream_ref(self) -> dict:
        """Resolve the tracked upstream ref once per run.

        Returns {state, ref, refresh, paths, blobs, pathsLower, pathsByLower, detail,
        remoteTracked, remoteDetail, wasTracked, wasTrackedDetail,
        wasTrackedCertainty, objectType, oid, attestation, attestationDetail,
        attestationCause, attestationRefused, attestedOid, attestedWhen,
        attestationPath, attestationNote, oidMismatch, treeSkipped} where state is 'ok'
        (`paths` is the set of tree paths, `blobs` maps a tree path to its blob id),
        'unconfigured', 'missing', 'not-a-commit' or 'error'. A missing ref is not an
        error: this must never need the network, and a clone that has not fetched yet
        still has to boot. A ref that resolves to something which is *not a commit* is
        not missing either — see PRO-248 review N1 opening 1.

        `wasTracked` answers "did this clone ever have the ref?" for a 'missing' ref:
        local history only, no network — see `missing_ref_evidence()` (review N1
        opening 2). `wasTrackedCertainty` says how far that history reaches: 'held' is
        a record of this very ref having been here, 'fetched' is proof of fetching
        only, and the two must not be reported to the operator as the same sentence
        (review B1/N4).

        `remoteTracked` answers "is verifiedRef under refs/remotes/?" and it fails
        closed (PRO-248 review B1). `git rev-parse --symbolic-full-name` prints nothing
        and reports failure for a short name that is ambiguous — which is exactly what
        `git branch origin/Playerbot HEAD` creates — while every other read of that same
        name silently resolves refs/heads/origin/Playerbot, the local branch the
        `--author` forger can rewrite. An unanswered question therefore means "not a
        remote-tracking ref", never the default "yes".

        `oid` is the commit `ref` peeled to on the 'ok' path, and the `attestation*`
        keys carry what the durable last-verified record says about it — see
        `attestation_record()` and `consult_attestation()` (PRO-300). `oidMismatch` is
        {attested, current, why, explained} when the ref now names a commit the attested
        one is not an ancestor of; `explained` is whether the ref's own reflog survives
        to account for it as a force-push, and when it does not, `treeSkipped` says the
        tree was never read — `upstream_ref_state()` turns that into a FAIL.
        """
        cached = getattr(self, "_upstream_ref_cache", _UNSET)
        if cached is not _UNSET:
            return cached
        ref, refresh = self.upstream_ref_setting()
        out = {"state": "ok", "ref": ref, "refresh": refresh, "paths": None,
               "blobs": None, "pathsLower": None, "pathsByLower": None,
               "detail": "", "remoteTracked": True, "remoteDetail": "",
               "wasTracked": False, "wasTrackedDetail": "",
               "wasTrackedCertainty": "", "objectType": "", "oid": "",
               "attestation": "", "attestationDetail": "", "attestationCause": "",
               "attestationRefused": False,
               "attestedOid": "", "attestedWhen": "", "attestationPath": "",
               "attestationNote": "",
               "oidMismatch": None, "treeSkipped": False}
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
                rc, peeled, err = self._git(["rev-parse", "--verify", "--quiet",
                                             f"{ref}^{{commit}}"])
                if rc == 0 and peeled.strip():
                    # Keep the commit the peel actually resolved: the attestation has to
                    # name the object this gate trusted, not re-resolve an untrusted name
                    # afterwards (same reason review N3 pinned the cat-file probe to the
                    # oid the existence check had already settled on).
                    out["oid"] = peeled.strip()
                if rc is None:
                    out.update(state="error", detail=f"git unavailable: {err}", paths=None)
                elif rc != 0:
                    # `^{commit}` failing says one of two different things, and only one
                    # of them is "this clone does not have the ref". `verifiedRef` aimed at
                    # a tree or a blob — `refs/tags/<annotated>^{tree}`, or a mistyped
                    # object name — fails the peel because the object it names is not a
                    # commit, and `git rev-parse --symbolic-full-name <tree>` exits 0
                    # printing nothing, so a tree also arrives here with remoteTracked
                    # already False. Filing that as `missing` handed back the pre-PRO-248
                    # directory+author exemption on a WARN (PRO-248 review N1, opening 1:
                    # Rev's POC was a tree object and a forged-author file that booted).
                    # Ask the separate question: does the name resolve to *any* object?
                    rc2, oid, err2 = self._git(["rev-parse", "--verify", "--quiet", ref])
                    if rc2 is None:
                        out.update(state="error", detail=f"git unavailable: {err2}",
                                   paths=None)
                    elif rc2 == 0 and oid.strip():
                        out.update(state="not-a-commit")
                        # Ask the type of the object the existence probe actually
                        # resolved rather than re-resolving the name: the oid is what
                        # the gate decided on, and naming it also removes a second
                        # resolution of an untrusted name (review N3).
                        rc3, otype, _e3 = self._git(["cat-file", "-t", oid.strip()])
                        out["objectType"] = (otype.strip().lower()
                                             if rc3 == 0 and otype.strip()
                                             else "non-commit object")
                        out["detail"] = (f"{ref} resolves to {oid.strip()[:12]}, which is "
                                         f"a {out['objectType']}, not a commit")
                    else:
                        # Nothing to find at all: not a ref, not an object name.
                        out.update(state="missing",
                                   detail=f"{ref} does not exist in this clone")
            if out["state"] == "missing":
                # Review N1, opening 2: 'missing' is a WARN that keeps the exemption, so
                # "never fetched" and "was here and got deleted" must not share it.
                existed, why, certainty = self.missing_ref_evidence(ref)
                out.update(wasTracked=existed, wasTrackedDetail=why,
                           wasTrackedCertainty=certainty if existed else "")
                # PRO-300: those three signals answer "has this clone ever fetched".
                # None of them can answer "did this gate ever verify this ref", and
                # `git update-ref -d` + `rm FETCH_HEAD` + `rm -rf
                # .git/logs/refs/remotes` erases all three at once, which is the
                # residual PRO-291 left open. The durable record is asked here, and
                # read-only: a ref that is not there is never a reason to write one.
                self.consult_attestation(out)
            if out["state"] == "ok" and out["remoteTracked"]:
                # PRO-300: ask the durable question before reading anything off the ref.
                # `ls-tree` is the answer the whole exemption is built on, so it must not
                # be asked of a ref this gate cannot vouch for — and a ref moved off the
                # commit it was verified at, with no reflog to call that a force-push,
                # is exactly such a ref.
                self.consult_attestation(out)
                mism = out.get("oidMismatch")
                if mism and not mism["explained"]:
                    out["treeSkipped"] = True
                    out["detail"] = (
                        f"{ref} resolves to {mism['current'][:12]}, but "
                        f"{out['attestationPath'] or 'the last-verified record'} "
                        f"records it verified at {mism['attested'][:12]} on "
                        f"{out['attestedWhen']} and, since {mism['why']}, this ref "
                        f"holds no reflog of its own to explain the move")
                cause = out.get("attestationCause") or ""
                if (out.get("attestation") == self.ATTEST_UNTRUSTED and cause
                        and cause != "unreadable"):
                    # PRO-300 review, finding 1: an untrusted record must not act as if it
                    # were simply absent on the path that ends in a write. If it did, one
                    # appended junk line would buy back the exemption and the forged oid
                    # would then be stamped `last-verified` by this very run. `unreadable`
                    # is the exception and stays inert — a file the OS will not hand over
                    # is an environment accident, and this record must never become a
                    # precondition for booting (AC 6); what the exemption rests on here is
                    # the tree comparison below, not the record.
                    out["treeSkipped"] = True
                    out["attestationRefused"] = True
                    out["detail"] = (
                        f"{ref} resolves to {(out.get('oid') or '')[:12]}, but the "
                        f"last-verified record at "
                        f"{out.get('attestationPath') or '(unknown)'} is not one this "
                        f"gate can trust: {out.get('attestationDetail')}. Its tree was "
                        f"not read and nothing was recorded")
            if out["state"] == "ok" and out["remoteTracked"] and not out.get(
                    "treeSkipped"):
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
                    # PRO-300: the tree could be read, so this gate is entitled to say
                    # it verified the ref — and that is what turns a later `.git`
                    # stripped of every fetch fingerprint into a FAIL instead of the WARN
                    # a never-carried clone still gets. Written on every satisfied boot,
                    # which is also how a fast-forwarded or force-pushed ref moves the
                    # record forward instead of stranding it on an old commit.
                    note, written_at = self.write_attestation(ref, out["oid"])
                    out["attestationNote"] = note
                    if written_at:
                        # The record this run just wrote *is* the record now, and the
                        # summary has to say so rather than repeating what was on disk
                        # when the run started.
                        out["attestation"] = self.ATTEST_RECORDED
                        out["attestedOid"] = out["oid"]
                        out["attestedWhen"] = written_at
        self._upstream_ref_cache = out
        return out

    def upstream_ref_state(self, path: Path) -> tuple[str, str, str]:
        """Did `path` arrive with the tracked upstream history? (state, detail, cause).

        `cause` is the machine-readable reason behind the state so the report can name
        the right recovery: 'verified', 'absent', 'content-diverged', 'case-diverged',
        'not-remote', 'not-a-commit', 'ref-deleted', 'attested-missing',
        'attestation-untrusted', 'oid-mismatch', 'unreadable', 'uncomparable',
        'outside-tree', 'unhashable', 'unconfigured', 'missing' or 'missing-allowed'.

        Three of these answer the same question — "is the ref there?" — and they are
        not interchangeable, which is what PRO-248 review N1 was about:

        - 'missing' / REF_UNCHECKED: git cannot find the name at all and this clone has
          no local history for it. A clone that has genuinely never fetched still
          boots, on a WARN that names `refreshCommand`.
        - 'ref-deleted' / REF_INDETERMINATE: git cannot find the ref but this clone has
          fetch history — `ref`'s own reflog, or a FETCH_HEAD or a sibling reflog that
          only proves somebody fetched. Fail closed; `--allow-missing-upstream-ref` is
          the only way back to the WARN, and it is a decision someone has to type. A
          plain `git clone` leaves none of the three, so a clone that has never fetched
          still lands on 'missing' (review B1).
        - 'not-a-commit' / REF_INDETERMINATE: the name resolves to a tree, blob or other
          non-commit object. Fail closed. It is not missing, and calling it missing was
          the false statement that kept the exemption.
        - 'attested-missing' / REF_INDETERMINATE: git cannot find the ref, no fetch
          fingerprint survives anywhere, and the durable last-verified record says this
          gate verified the ref at a named commit on a named date. That is exactly the
          pair PRO-291 could not tell apart — a never-carried clone has no record to
          lose, so the record is what separates them. Fail closed; the recovery is
          `refreshCommand`, or `--allow-missing-upstream-ref` to opt into the exemption
          by typing it (PRO-300).
        - 'attestation-untrusted' / REF_INDETERMINATE: a record exists and this gate
          cannot verify it — an unparsable line, a version it does not understand, no
          `repo` binding at all, or a `repo`/`remote url` naming somewhere else. Two
          shapes reach it. The ref is missing and the record that would have said it was
          ever here is unreadable: fail closed, and `--allow-missing-upstream-ref` does
          not cover it, or corrupting a file would be the cheapest way back to the WARN.
          Or the ref is *present* and the record that made its word worth taking is not
          one this gate could have written: fail closed the same way, and `ls-tree` was
          never asked of it. One exception, deliberate — a file the OS will not hand over
          is inert on that second path, because the record is evidence for a later
          deletion check and must never become a precondition for booting (AC 6).
        - 'oid-mismatch' / REF_INDETERMINATE: the ref is here but names a commit the
          attested one is not an ancestor of, and holds no reflog to explain the move as a
          force-push. A fetch fast-forwards a remote-tracking ref, so this is the shape
          `git update-ref` leaves: a tracked ref pointing at commits of the operator's
          choosing.
        """
        up = self.upstream_ref()
        ref = up["ref"] or "the configured upstreamProvenance.verifiedRef"
        if up["state"] == "unconfigured":
            return self.REF_UNCHECKED, (f"upstream ref not configured; set "
                                        f"upstreamProvenance.verifiedRef or run "
                                        f"{up['refresh']}"), "unconfigured"
        if up["state"] == "missing":
            if up.get("wasTracked"):
                # Review B1/N4: only `ref`'s own reflog records *this* ref having been
                # here. FETCH_HEAD and a sibling reflog prove the clone fetched, which
                # is enough to fail closed but not enough to accuse anyone of deleting
                # something — the alternative is that the remote never had the branch.
                certainty = up.get("wasTrackedCertainty") or "fetched"
                why = up.get("wasTrackedDetail", "")
                if certainty == "held":
                    verdict = (f"{ref} is not present locally, but this clone holds "
                               f"{ref}'s own reflog ({why}), so it was deleted rather "
                               f"than never fetched")
                else:
                    verdict = (f"{ref} is not present locally and has no reflog of its "
                               f"own, while this clone does have fetch history ({why}), "
                               f"so the ref was either deleted after it arrived or "
                               f"never existed in this clone")
                if self.allow_missing_upstream_ref():
                    return (self.REF_UNCHECKED,
                            f"{verdict}; --allow-missing-upstream-ref was passed, which "
                            f"opts into the pre-PRO-248 exemption on that answer",
                            "missing-allowed")
                return (self.REF_INDETERMINATE, verdict, "ref-deleted")
            attest = up.get("attestation") or ""
            if attest == self.ATTEST_RECORDED:
                # PRO-300, the PRO-291 residual. The three signals above prove somebody
                # fetched; a record proves this gate *verified* the ref, at a named commit
                # at a named time. A clone with no reflog, no FETCH_HEAD, no sibling
                # reflog and an attestation is not one that never fetched — it is one
                # whose fetch fingerprints were removed after the fact.
                verdict = (f"{ref} is not present locally, but "
                           f"{up.get('attestationPath') or 'the last-verified record'} "
                           f"attests that an earlier run of this gate verified it at "
                           f"{(up.get('attestedOid') or '')[:12]} on "
                           f"{up.get('attestedWhen')}, while this clone holds no reflog, "
                           f"no FETCH_HEAD that names a fetched ref and no sibling reflog "
                           f"either. A clone that "
                           f"has genuinely never fetched has no attestation to lose, so "
                           f"this is a stripped one")
                if self.allow_missing_upstream_ref():
                    return (self.REF_UNCHECKED,
                            f"{verdict}; --allow-missing-upstream-ref was passed, which "
                            f"opts into the pre-PRO-248 exemption on that answer",
                            "missing-allowed")
                return (self.REF_INDETERMINATE, verdict, "attested-missing")
            if attest == self.ATTEST_UNTRUSTED:
                # Failing closed here rather than falling through to the WARN is the
                # point: "the record is unreadable" and "there is no record" cannot be
                # the same sentence, or truncating a file is all it takes to buy back
                # the exemption the record was blocking.
                return (self.REF_INDETERMINATE,
                        f"{ref} is not present locally and the last-verified record for "
                        f"it is not one this gate can trust: "
                        f"{up.get('attestationDetail')}. An attestation the gate cannot "
                        f"verify is not a pass, and it is not the same answer as having "
                        f"never verified the ref",
                        "attestation-untrusted")
            return (self.REF_UNCHECKED,
                    f"{ref} is not present locally; run {up['refresh']}", "missing")
        if up["state"] == "not-a-commit":
            # The name is here, it is just not a commit, so it cannot say what arrived
            # with upstream history. That is git failing to answer, not a clone that has
            # not fetched (PRO-248 review N1, opening 1).
            return (self.REF_INDETERMINATE,
                    f"{up['detail'] or (ref + ' does not peel to a commit')}; the "
                    f"upstream exemption is a claim about commit history and a "
                    f"{up.get('objectType') or 'non-commit'} cannot make it",
                    "not-a-commit")
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
        if up.get("attestationRefused"):
            # PRO-300 review, finding 1. The ref is present and peels to a commit, so
            # every PRO-291 check passes; what fails here is the record that made the
            # ref's word worth taking. A file this gate could not have written is not
            # entitled to a tree read, and must not be treated as the same answer as
            # having no record at all on the path that ends in writing one.
            return (self.REF_INDETERMINATE,
                    up.get("detail") or (f"the last-verified record for {ref} is not one "
                                         f"this gate can trust"),
                    "attestation-untrusted")
        mism = up.get("oidMismatch")
        if mism and not mism.get("explained"):
            # PRO-300: "the ref is there" is not the claim the exemption rests on. The
            # claim is that this ref *is* the upstream history this gate checked before,
            # and a ref moved outside the commit it was verified at no longer makes it.
            # A fetch fast-forwards a remote-tracking ref, so an ordinary `git pull`
            # between boots is cleared by `oid_is_ancestor()` and never reaches here;
            # `git update-ref` is what puts a ref where its verified position is not an
            # ancestor. A real force-push leaves a reflog, so the missing reflog is what
            # separates the forged move from the legitimate one.
            return (self.REF_INDETERMINATE,
                    f"{ref} resolves to {mism['current'][:12]} now, but "
                    f"{up.get('attestationPath') or 'the last-verified record'} records "
                    f"this gate verifying {mism['attested'][:12]} on "
                    f"{up.get('attestedWhen')} and, since {mism['why']}, this ref holds "
                    f"no reflog of its own either — a fetch would have fast-forwarded "
                    f"it and a real force-push would have left one, so this reads as "
                    f"`git update-ref` pointing the tracked ref at commits of the "
                    f"operator's choosing",
                    "oid-mismatch")
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
                f"granted on {ref}: {detail}. A ref outside refs/remotes/ is a branch, and "
                f"a branch is rewritten by the same `git commit` / "
                f"`git update-ref` this rule constrains, so it cannot vouch "
                f"for anything. Point upstreamProvenance.verifiedRef at "
                f"refs/remotes/<remote>/<branch> and re-run; if that ref is "
                f"the right one and is merely behind, refresh it with "
                f"`{up['refresh']}` first. If this file is company content it "
                f"belongs in {pending}.",
                )
        elif state == self.REF_INDETERMINATE and cause == "not-a-commit":
            # Review N1, opening 1. `git rev-parse --verify <ref>^{commit}` cannot tell
            # "no such ref" from "the ref names a tree", and the second one used to be
            # filed as `missing`, which is a WARN that keeps the exemption.
            bad("upstream-ref-not-a-commit",
                f"{f['rel']} is new in the upstream-tagged directory "
                f"{db_path} and the upstream exemption was about to be "
                f"granted on {ref}: {detail}. A ref that does not peel to a "
                f"commit cannot say what arrived with upstream history, and "
                f"it is not a clone that has simply not fetched — `missing` "
                f"would be a false statement about it. Point "
                f"upstreamProvenance.verifiedRef at a remote-tracking commit "
                f"ref, or refresh it with `{up['refresh']}`, and re-run. If "
                f"this file is company content it belongs in {pending}.",
                )
        elif state == self.REF_INDETERMINATE and cause == "ref-deleted":
            # Review N1, opening 2. Without this branch the file keeps the exemption on
            # the word of a ref that is one `git update-ref -d` away from being gone.
            # Review B1/N4: the sentence has to match the evidence. `ref`'s own reflog is
            # a record of *this* ref having been here; a FETCH_HEAD or a sibling reflog
            # only proves somebody fetched, which leaves "the remote never had this
            # branch" standing. The gate must not report a deletion it did not observe.
            if up.get("wasTrackedCertainty") == "held":
                evidence = "a ref that this clone demonstrably had and no longer does " \
                           "is not that"
            else:
                # PRO-307 A1: the strongest signal here may be a FETCH_HEAD that only
                # proves somebody fetched, which is not the same as this ref having
                # arrived. The refusal stays fail-closed; the sentence stops claiming
                # a fetch it did not observe.
                evidence = "this clone has fetch history, so the ref either arrived and " \
                           "was deleted or never existed here, and the gate will not " \
                           "grant the exemption on the difference"
            bad("upstream-ref-deleted",
                f"{f['rel']} is new in the upstream-tagged directory "
                f"{db_path} and the upstream exemption was about to be "
                f"granted on {ref}, which is not present in this clone: {detail}. "
                f"A clone that has genuinely never fetched still boots on a "
                f"WARN; {evidence}. Restore it with `{up['refresh']}` and "
                f"re-run; if you are deliberately running without the ref, "
                f"pass --allow-missing-upstream-ref so the exemption is "
                f"opted into rather than inherited. If this file is company "
                f"content it belongs in {pending}.",
                )
        elif state == self.REF_INDETERMINATE and cause == "attested-missing":
            # PRO-300, the residual PRO-291 recorded and could not close: after
            # `git update-ref -d <ref>` plus `rm FETCH_HEAD` plus `rm -rf
            # .git/logs/refs/remotes`, `.git` is byte-identical to a clone that never
            # carried the ref, so the directory+author exemption came back on the WARN.
            # The durable record is the only thing in the tree that those three commands
            # do not reach, and it names the commit and the date this gate last checked.
            attested = (up.get("attestedOid") or "")[:12]
            bad("upstream-ref-attested-missing",
                f"{f['rel']} is new in the upstream-tagged directory "
                f"{db_path} and the upstream exemption was about to be "
                f"granted on {ref}, which is not present in this clone: {detail}. "
                f"The last-verified record at {up.get('attestationPath') or '(unknown)'} "
                f"is what tells this apart from a clone that never fetched — this "
                f"gate verified {ref} at `{attested}` on {up.get('attestedWhen')}, "
                f"and a clone that has never fetched has no such record to delete. "
                f"Restore the ref with `{up['refresh']}` and re-run; if you are "
                f"deliberately running without it, pass "
                f"--allow-missing-upstream-ref so the exemption is opted into "
                f"rather than inherited. Deleting the record does not restore the "
                f"ref, and on a clone that still has the ref the next satisfied boot "
                f"writes it again; if the ref has genuinely gone from the remote as "
                f"well, say so in review instead of removing the gate's memory of "
                f"having verified it. If this file is company content it belongs in "
                f"{pending}.",
                )
        elif state == self.REF_INDETERMINATE and cause == "attestation-untrusted":
            # Fail closed with a named basis. An unverifiable record must not be read as
            # "no record", because that makes corrupting one the cheapest route back to
            # the exemption the record was blocking. Two shapes land here and they need
            # different sentences: the ref is gone and the record that would have said so
            # is unreadable, or the ref is here and the record behind it is a forgery.
            path_note = up.get("attestationPath") or "(state file)"
            if up.get("attestationRefused"):
                bad("upstream-ref-attestation-untrusted",
                    f"{f['rel']} is new in the upstream-tagged directory "
                    f"{db_path} and the upstream exemption was about to be "
                    f"granted on {ref}, which this clone does resolve, but the record "
                    f"that makes its word worth taking is not one this gate can trust: "
                    f"{detail}. The gate will not read an unverifiable record as no "
                    f"record at all, so it did not ask that ref for a file listing and "
                    f"did not stamp this boot as verified. Remove the record with "
                    f"`rm {path_note}` and re-run: a satisfied boot writes a fresh one "
                    f"naming this tree. Say so in review either way — an untrusted "
                    f"record is not the same evidence state as a missing one, and only "
                    f"the missing one is what a never-verified clone looks like. If this "
                    f"file is company content it belongs in {pending}.",
                    )
            else:
                bad("upstream-ref-attestation-untrusted",
                    f"{f['rel']} is new in the upstream-tagged directory "
                    f"{db_path} and the upstream exemption was about to be "
                    f"granted on {ref}, which is not present in this clone, and the "
                    f"record that would have said whether it was ever verified here is "
                    f"not one this gate can trust: {detail}. The gate will not read an "
                    f"unverifiable record as no record at all. Restore the ref with "
                    f"`{up['refresh']}` and remove the record with `rm {path_note}` — a "
                    f"boot does not repair a record it cannot parse, it keeps the lines "
                    f"it was given — then let the next satisfied boot write a fresh one. "
                    f"Say so in review, since an absent record is the same evidence "
                    f"state as never having verified the ref. If this file is company "
                    f"content it belongs in {pending}.",
                    )
        elif state == self.REF_INDETERMINATE and cause == "oid-mismatch":
            # PRO-300. The ref is present, which is why PRO-291's checks all passed: it
            # is simply no longer the history this gate verified, and a tree read off a
            # ref someone pointed elsewhere with `git update-ref` vouches for nothing.
            bad("upstream-ref-oid-mismatch",
                f"{f['rel']} is new in the upstream-tagged directory "
                f"{db_path} and the upstream exemption was about to be "
                f"granted on {ref}: {detail}. The tracked ref is no longer the "
                f"commit this gate verified, so whatever its tree says about this "
                f"file is a claim about a history the gate never checked. Refresh "
                f"it with `{up['refresh']}` — if the remote genuinely force-pushed, "
                f"the fetch restores the ref along with the reflog that documents "
                f"the force-push — and re-run. If this file is company content it "
                f"belongs in {pending}.",
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
        # Through `_git` so the "nothing in the gate reaches the network" assertion
        # actually covers this call too (review N5).
        rc, out, _err = self._git(["status", "--porcelain",
                                   "--ignore-submodules=none"], timeout=60)
        if rc is None or rc != 0:
            paths = None
        else:
            for entry in out.splitlines():
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
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  stdin=subprocess.DEVNULL, timeout=90)
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
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL, timeout=60)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return
        cid = ids.stdout.decode("utf-8", "replace").strip().splitlines()
        if ids.returncode != 0 or not cid or not cid[0].strip():
            self.r.line("0b", f"no existing {svc_name} container; asserted against the compose "
                              f"config only")
            return
        try:
            insp = subprocess.run(["docker", "inspect", cid[0].strip()],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  stdin=subprocess.DEVNULL, timeout=60)
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
        attestation_note = ""
        if ref_counts:
            up = self.upstream_ref()
            ref = up.get("ref") or "upstream ref unconfigured"
            ref_note = f"; upstream ref {ref} (" + \
                       ", ".join(f"{k}={v}" for k, v in sorted(ref_counts.items())) + ")"
            # PRO-300: the operator has to be able to see which of the two answers the
            # ref verdict rested on — a record this gate wrote, or the absence of one.
            status = up.get("attestation") or ""
            if up.get("attestationRefused"):
                # The summary must not read as a satisfied boot: nothing was recorded
                # this run and the ref's tree was never read.
                ref_note += (f", last-verified refused (record untrusted: "
                             f"{up.get('attestationCause') or 'unknown reason'})")
            elif status == self.ATTEST_RECORDED:
                ref_note += (f", last-verified {(up.get('attestedOid') or '')[:12]}"
                             f"@{up.get('attestedWhen')}")
            elif status:
                ref_note += f", last-verified {status}"
            if up.get("attestationNote"):
                attestation_note = up["attestationNote"]
        self.r.line("4", f"{checked_files} new file(s) under write policy, {statements} statement(s) "
                         f"inspected; reserved bands "
                         f"creature_template.entry/quest_template.ID>="
                         f"{self._band_min('creature_template', 'entry')}, "
                         f"creature.guid>={self._band_min('creature', 'guid')}{ref_note}")
        if attestation_note:
            # Informational, never a finding: failing to write the record must not be
            # able to stop a boot that the ref itself still justifies (AC 6).
            self.r.info("4", f"upstream ref attestation: {attestation_note}",
                        rule="upstream-ref-attestation")
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
        known = {k.lower() for k in base} | {k.lower() for k in custom} \
            | {t.lower() for t in self._LEDGER_TABLES}
        targets, qualifiers, err, region, heuristic = write_targets(st, known)
        if err:
            extra = ", ".join(sorted({t for _, t in targets})) or "none identifiable"
            # Both name lists come out of the same bare-name scan, so both carry the
            # same caveat: it cannot tell a table from an `ON`-clause column or an index
            # hint. Labelling only the table list left `other names in region` reading
            # as parsed fact (PRO-179 review rank 4).
            suffix = " (heuristic — unquoted region)" if heuristic else ""
            named = f"tables named{suffix}: {extra}"
            if qualifiers:
                named += f"; other names in region{suffix}: {', '.join(sorted(qualifiers))}"
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
    out.append(f"  sql root     : {report.data.get('sql_root','')} "
               f"(what check 0b compares to the container's sql bind)")
    out.append(f"  scanned from : {report.data.get('scanned_from','')} "
               f"(the $-relative updates_include paths the scan walked resolve here)")
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
    ap.add_argument("--sql-root", default=None,
                    help="the directory the boot-path mount check (check 0b) compares against "
                         "the container's /azerothcore/data/sql bind, and the base the report "
                         "prints as \"sql root\". It does NOT choose which files are scanned: "
                         "the scan walks the `updates_include` paths from the live database, "
                         "and those $-relative paths resolve against --git-root. Pointing "
                         "--sql-root somewhere else while --git-root names another tree prints "
                         "a banner for a tree the gate never walked (PRO-307 A3). Defaults to "
                         "<compose-root>/data/sql.")
    ap.add_argument("--git-root", default=None,
                    help="the tracked tree the gate reads: git runs here, and the $-relative "
                         "`updates_include` paths the file scan walks are resolved against it "
                         "($/data/sql/db_world -> <git-root>/data/sql/db_world). This is what "
                         "decides which files the gate actually looked at. Defaults to "
                         "<compose-root>.")
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
    ap.add_argument("--allow-missing-upstream-ref", action="store_true",
                    help="grant the upstream exemption even when the tracked "
                         "upstreamProvenance.verifiedRef has been deleted from this clone "
                         "(local history says it was here). Off by default: the boot path "
                         "refuses, and a clone that has genuinely never fetched still "
                         "boots on a WARN without it (PRO-248 review N1).")
    ap.add_argument("--preflight-state-dir", dest="preflight_state_dir", default=None,
                    help="directory holding the durable last-verified attestation for "
                         "upstreamProvenance.verifiedRef (default: "
                         "<git common dir>/paperclip-preflight/last-verified). The "
                         "attestation is what lets a later boot tell a ref that was "
                         "verified and then deleted from a clone that never carried it "
                         "(PRO-300); it is never a precondition for booting, and a "
                         "missing one is still a WARN.")
    ap.add_argument("--policy-all", action="store_true",
                    help="same as --strict-all but keeps the origin note")
    ap.add_argument("--clean-dead-ref-max-count", type=int, default=3)
    ap.add_argument("--max-dead-list", type=int, default=20)
    ap.add_argument("--json", dest="json_out", default=None,
                    help="write the machine-readable report to this path; the parent "
                         "directory must already exist (PRO-307 A5)")
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
    # Fail before the run rather than after it: a `--json` path under a directory that
    # does not exist used to raise a raw traceback at the last line of main(), after a
    # gate that had already done all of its work (PRO-307 A5). It still exits non-zero;
    # this only makes the answer an operational error instead of a stack trace.
    if args.json_out:
        parent = Path(args.json_out).parent
        if not parent.is_dir():
            print(f"preflight: operational error: --json path {args.json_out} has no "
                  f"parent directory {parent}; create it or name a path that exists",
                  file=sys.stderr)
            return EXIT_ERROR

    report = Report()
    report.data["started"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    t0 = time.monotonic()
    try:
        pf = Preflight(args, policy, report)
        report.data["sql_root"] = str(pf.sql_root)
        # The banner has to say which tree the walk was rooted in, not only which one
        # `--sql-root` was pointed at — those are different directories and only one of
        # them decides what the gate read (PRO-307 A3).
        report.data["scanned_from"] = str(pf.git_root)
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
        try:
            Path(args.json_out).write_text(json.dumps(payload, indent=2, sort_keys=True))
        except OSError as exc:
            print(f"preflight: operational error: cannot write --json path "
                  f"{args.json_out}: {exc}", file=sys.stderr)
            return EXIT_ERROR
    return rc


if __name__ == "__main__":
    sys.exit(main())
