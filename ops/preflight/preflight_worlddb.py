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

IDENT_RE = re.compile(r"^[A-Za-z0-9_$]+$")
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
    """Blank out comments, keep every offset. /*! ... */ stays live (mysql executes it)."""
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
                    out[i + 1] = " "
                    i += 2
                    continue
                state = None
            i += 1
            continue
        if state == "dq":
            if c == "\\":
                i += 2
                continue
            if c == '"':
                if nxt == '"':
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


def top_level_positions(masked: str, word: str) -> list[int]:
    """Offsets of a bare keyword that is not inside quotes and not part of a longer word."""
    pat = re.compile(r"(?<![A-Za-z0-9_$])" + word + r"(?![A-Za-z0-9_$])", re.IGNORECASE)
    return [m.start() for m in pat.finditer(masked)]


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


def scan_predicates(where_text: str) -> dict[str, list[tuple[str, str, object]]]:
    """col -> [(operator, kind, value)] for `col op literal` and `col IN (lit, ...)` shapes."""
    masked = mask_sql(where_text)
    out: dict[str, list[tuple[str, str, object]]] = {}
    pat = re.compile(
        r"(?<![A-Za-z0-9_$.])" + r"(`?[A-Za-z0-9_$]+`?)" +
        r"\s*(<=>|!=|<>|>=|<=|=|<|>|\bIN\b|\bLIKE\b|\bBETWEEN\b|\bIS\b|\bREGEXP\b)",
        re.IGNORECASE,
    )
    for m in pat.finditer(masked):
        col = unquote_ident(m.group(1)).lower()
        op = m.group(2).upper()
        rest_start = m.end()
        if op == "IN":
            while rest_start < len(masked) and masked[rest_start] in " \n\t":
                rest_start += 1
            if rest_start >= len(masked) or masked[rest_start] != "(":
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
            inner = where_text[rest_start + 1:j]
            for piece in split_top_level(inner, mask_sql(inner), ","):
                kind, value = literal_value(piece)
                out.setdefault(col, []).append((op, kind, value))
            continue
        # single operand: read up to the next top-level boolean keyword
        stop = len(where_text)
        for kw in ("AND", "OR", "XOR"):
            for p in top_level_positions(masked[rest_start:], kw):
                stop = min(stop, rest_start + p)
        operand = where_text[rest_start:stop]
        kind, value = literal_value(operand)
        out.setdefault(col, []).append((op, kind, value))
    return out


def has_top_level_or(where_text: str) -> bool:
    """True when an OR sits at bracket depth 0 — the shape that widens a write beyond its key."""
    masked = mask_sql(where_text)
    depth = 0
    for m in re.finditer(r"[()]|(?<![A-Za-z0-9_$])OR(?![A-Za-z0-9_$])", masked, re.IGNORECASE):
        tok = m.group(0)
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0:
            return True
    return False


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
# the checker
# --------------------------------------------------------------------------- #


class Preflight:
    def __init__(self, args, policy, report: Report):
        self.a = args
        self.p = policy
        self.r = report
        self.db = MySQL(args)
        self.live_db = args.live_db
        self.sql_root = Path(args.sql_root) if args.sql_root else Path(args.compose_root, "data", "sql")
        self.git_root = Path(args.git_root) if args.git_root else Path(args.compose_root)
        self.violations: list[dict] = []

    # -- helpers ------------------------------------------------------------- #

    def line_of(self, text: str, offset: int) -> int:
        tail = text[offset:]
        offset += len(tail) - len(tail.lstrip())
        return text.count("\n", 0, offset) + 1

    def git_origin(self, path: Path) -> tuple[str, str]:
        """Provenance of a file inside the compose tree: ('company'|'upstream'|'unknown', detail)."""
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

    def git_tracked(self, path: Path) -> bool:
        rel = os.path.relpath(path, self.git_root)
        try:
            proc = subprocess.run(["git", "ls-files", "--error-unmatch", "--", rel],
                                  cwd=str(self.git_root), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return True
        return proc.returncode == 0

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
            self.r.fail("0", "updates_include does not match the approved four-directory set. "
                             f"unexpected/changed={sorted(extra)} missing={sorted(missing)}",
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
        return {r[0]: {"hash": r[1] or "", "state": r[2], "ts": r[3] if len(r) > 3 else ""}
                for r in rows}

    def check_ledger(self, files: list[dict]) -> list[dict]:
        led = self.ledger()
        new_files = []
        checked = 0
        seen = set()
        for f in files:
            seen.add(f["basename"])
            row = led.get(f["basename"])
            if row is None:
                origin, detail = self.git_origin(f["path"])
                f["origin"] = origin
                f["provenance"] = detail
                if origin == "unknown" and detail == "untracked" and not self.p.get("allowUntrackedFiles"):
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
                    self.r.fail("3", msg + " — ApplyUpdateFile() re-applies the changed file "
                                           "silently (UpdateFetcher.cpp:350).",
                                file=f["rel"], rule="hash-mismatch")
            elif row["state"] != f["dir"]["state"]:
                self.r.info("3", f"{f['rel']} state will move {row['state']} -> "
                                 f"{f['dir']['state']} (harmless UpdateState).", file=f["rel"],
                            rule="state-move")
        dead = sorted(set(led) - seen)
        dead_non_module = [n for n in dead if led[n]["state"] != "MODULE"]
        self.r.line("2", f"{len(files)} file(s) on disk, {checked} already in the ledger, "
                         f"{len(new_files)} will be applied on next boot")
        for f in new_files:
            self.r.line("2", f"  NEW {f['rel']}  sha1={f['sha1'][:12]} size={f['size']}B "
                             f"state={f['dir']['state']} origin={f['origin']}")
        if not new_files:
            self.r.line("2", "  (nothing new — the next boot applies no update files)")
        if dead:
            self.r.line("2", f"  {len(dead)} ledger row(s) have no file on disk "
                             f"({len(dead_non_module)} non-MODULE)")
            max_clean = self.a.clean_dead_ref_max_count
            if dead_non_module and max_clean >= 0 and len(dead_non_module) > max_clean:
                self.r.warn("2", f"{len(dead_non_module)} non-MODULE ledger rows are orphaned but "
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
            if f.get("origin") in exempt and not self.a.policy_all:
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
        self.r.line("4", f"{checked_files} new file(s) under write policy, {statements} statement(s) "
                         f"inspected; reserved bands "
                         f"creature_template.entry/quest_template.ID>="
                         f"{self._band_min('creature_template', 'entry')}, "
                         f"creature.guid>={self._band_min('creature', 'guid')}")
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

        if st.table is None:
            bad("unparsable", "could not identify the target table — fail closed")
            return
        if st.schema and st.schema.lower() not in (self.live_db.lower(), "current_date"):
            bad("cross-schema-write", f"statement qualifies schema {st.schema!r}; update files must "
                                      f"not write outside the world database")
            return

        if st.table.lower() in ("updates", "updates_include", "version", "dbversion"):
            bad("ledger-table", f"write target {st.table!r} is updater bookkeeping; only "
                                f"UpdateFetcher may touch it")
            return

        entry = base.get(st.table) or custom.get(st.table)
        if entry is None:
            bad("table-not-allowed",
                f"write target {st.table!r} is neither a company-approved base table nor a "
                f"whitelisted custom table; add it to ops/preflight/preflight-policy.json and "
                f"get it reviewed")
            return
        is_custom = st.table in custom
        key_cols = [c.lower() for c in entry.get("keyColumns", [])]
        bands = entry.get("bands", [])
        refs = entry.get("refs", [])

        if st.verb in ("INSERT", "REPLACE"):
            self._policy_insert(f, st, line, digest, bad, key_cols, bands, refs, is_custom)
        elif st.verb == "UPDATE":
            self._policy_update(f, st, line, bad, key_cols, bands, refs, entry)
        elif st.verb == "DELETE":
            self._policy_delete(st, bad, key_cols, bands, refs)

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
        m = re.match(
            r"^\s*(?:INSERT|REPLACE)\s+(?:(?:LOW_PRIORITY|DELAYED|HIGH_PRIORITY|IGNORE)\s+)*"
            r"(?:INTO\s+)?" + TBL_CAP + r"\s*(?:\((?P<cols>[^()]*(?:\([^()]*\)[^()]*)*)\))?\s*"
            r"[^()]*?(?P<how>VALUES|VALUE|SET)\b", masked, re.IGNORECASE)
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

    def _policy_update(self, f, st, line, bad, key_cols, bands, refs, entry):
        masked = st.masked
        where_positions = top_level_positions(masked, "WHERE")
        if not where_positions:
            bad("unbounded-write", f"UPDATE {st.table} without WHERE is rejected")
            return
        wpos = where_positions[0]
        set_start = re.search(r"(?<![A-Za-z0-9_$])SET(?![A-Za-z0-9_$])", masked, re.IGNORECASE)
        if not set_start or set_start.end() > wpos:
            bad("unparsable", "could not locate the SET clause of the UPDATE — fail closed")
            return
        set_text = st.text[set_start.end():wpos]
        where_text = st.text[wpos + len("WHERE"):]
        if has_top_level_or(where_text):
            bad("or-in-where", f"UPDATE {st.table}: top-level OR in WHERE is rejected; use an "
                               f"explicit IN list of reserved-band ids")
        preds = scan_predicates(where_text)
        self._bounded_on(bad, st, preds, key_cols, "UPDATE")
        forbidden_assign = set(key_cols)
        for b in bands:
            forbidden_assign.add(b["column"].lower())
        for piece in split_top_level(set_text, mask_sql(set_text), ","):
            target = piece.split("=")[0].strip().lower()
            target = unquote_ident(target.split(".")[-1])
            if target in forbidden_assign:
                bad("key-column-assigned",
                    f"UPDATE {st.table} assigns the natural-key/reserved column {target!r}")
        self._check_literals(bad, st, preds, bands, refs, "UPDATE")

    def _policy_delete(self, st, bad, key_cols, bands, refs):
        masked = st.masked
        where_positions = top_level_positions(masked, "WHERE")
        if not where_positions:
            bad("unbounded-write", f"DELETE FROM {st.table} without WHERE is rejected")
            return
        where_text = st.text[where_positions[0] + len("WHERE"):]
        if has_top_level_or(where_text):
            bad("or-in-where", f"DELETE FROM {st.table}: top-level OR in WHERE is rejected; use an "
                               f"explicit IN list of reserved-band ids")
        preds = scan_predicates(where_text)
        self._bounded_on(bad, st, preds, key_cols, "DELETE")
        self._check_literals(bad, st, preds, bands, refs, "DELETE")

    def _bounded_on(self, bad, st, preds, key_cols, verb):
        """At least one designated natural-key column must be equality/IN bound to literals."""
        if not key_cols:
            bad("no-natural-key-configured",
                f"{verb} on {st.table}: no natural key configured in the policy — fail closed")
            return
        problems = []
        for kc in key_cols:
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
            return  # this key column bounds the statement
        bad("unbounded-write",
            f"{verb} on {st.table} is not WHERE-bounded on the natural key "
            f"({'; '.join(problems)}; bounded columns found: {sorted(preds) or 'none'})")

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
        if not IDENT_RE.match(name):
            raise OperationalError(f"scratch database name {name!r} is not [A-Za-z0-9_$]+")
        if name.lower() == self.live_db.lower():
            raise OperationalError("refusing to use the live database as the scratch schema")
        if name.lower() in {self.live_db.lower(), "acore_world", "acore_auth",
                            "acore_characters", "acore_playerbots", "mysql",
                            "information_schema", "performance_schema", "sys"}:
            raise OperationalError(f"refusing to use {name!r} as the scratch schema")
        if not name.startswith(self.a.scratch_prefix):
            raise OperationalError(
                f"scratch database {name!r} must start with {self.a.scratch_prefix!r} "
                f"so a drop can never target a real schema")
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
        self.check_duplicates(files)
        new_files = self.check_ledger(files)
        self.check_policy(new_files)
        if self.a.dry_run:
            if self.r.failed and not self.a.dry_run_anyway:
                self.r.line("5", "skipped — earlier checks already failed")
            else:
                self.run_dry_run(new_files)
        return EXIT_FAIL if self.r.failed else EXIT_OK


# --------------------------------------------------------------------------- #
# presentation
# --------------------------------------------------------------------------- #

CHECK_TITLES = {
    "0": "updater wiring",
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
    ap.add_argument("--scratch-db", default=None)
    ap.add_argument("--scratch-prefix", default="acore_world_preflight")
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
    policy_path = Path(args.policy)
    if not policy_path.is_file():
        print(f"preflight: policy file not found: {policy_path}", file=sys.stderr)
        return EXIT_ERROR
    policy = json.loads(policy_path.read_text())
    if args.live_db is None:
        args.live_db = policy.get("liveDatabase", "acore_world")
    if args.scratch_db is None:
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        args.scratch_db = f"{args.scratch_prefix}_{os.getpid()}_{stamp}"

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
