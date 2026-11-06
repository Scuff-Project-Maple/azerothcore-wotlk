# World-DB updater pre-flight gate (PRO-179)

`preflight_worlddb.py` is the gate that runs **before** `docker compose up -d ac-worldserver`.
It refuses the boot unless the four `db_world` directories that
`acore_world.updates_include` registers are in a state the updater can be trusted with.

    ops/deploy/worldserver-up.sh              # lease check + gate, then compose up
    python3 ops/preflight/preflight_worlddb.py  # gate only (exit 0 pass, 1 fail, 2 operational error)
    PYTHONPATH=ops/preflight python3 ops/preflight/test_preflight_worlddb.py  # policy-engine tests, no DB needed
    bash ops/preflight/test_sync_scripts.sh                 # installer matrix: scripts/lua-sync.sh + scripts/compose-sync.sh

`test_sync_scripts.sh` is the sibling pre-boot assertion's own test, not this gate's. It builds
throwaway repo roots under `$TMPDIR` and runs a copy of each installer against them — never a
real tree, never the stack — covering the matrix PRO-273 review measured (in-sync `0`, orphan `1`
in both modes and never deleted, drift `1` with both md5s plus `diff -u`, missing, empty snapshot
`1`, bad arg `2`) and the cases PRO-282 added: FIFO/socket/symlink/directory at a deployed path
refused with `1` in **both** modes (every run is under `timeout 10`, so the FIFO that used to
hang the installer surfaces as `rc=124` rather than as a wedged test), no `cp` ever attempted
through a symlink (the assertion is that an unrelated file outside the deployed directory is
byte-unchanged), orphans found recursively and over the whole extension set ALE loads, and the
negative controls that keep the widened scan honest (`notes.txt`, `README.LUA`, dotfiles are not
orphans because ALE would not load them). Every case labelled `--check` passes `--check` — the
PRO-293 review found thirteen that were named for check mode but silently ran sync mode, which is
how a check-mode-blind orphan pass stayed green — so each check/sync pair comes from one
`for mode in check sync` loop rather than being written twice. It also gates the **whole** deployed
path, not just its last component: with a tracked `sub/c.lua` in the snapshot and
`…/lua_scripts/sub -> $T/outside/`, the file outside has to be byte-unchanged and must never be
reported `ok`. It needs `bash`, coreutils and `python3` — python3 only for the socket case, which
`mkfifo` cannot produce and `mknod` needs privileges for; device nodes share the `-e && ! -f` gate
instead of being created.

`PREFLIGHT_CHECK_ONLY=1 ops/deploy/worldserver-up.sh` runs the gate without touching the
stack — that is the CI / merge-gate rehearsal form. The wrapper **verifies** the single-writer
lease at `<compose_root>/.stack/lease` and refuses to boot if it is missing, unreadable or
past its `expectedMinutes` window, but it never **takes** it: the gate is read-only against the
live database plus one scratch schema it drops afterwards, and a gate that could grab the lease
would let a CI run starve the host boot path. Whoever boots the stack takes the lease first
(see `docs/dev-environment.md#single-writer-lease`).

Recognised compose flags (`--force-recreate`, `-f/--file`, `-p/--project-name`, `--profile`,
`--scale`, …) are forwarded to `docker compose up -d` and, where they change what the gate
reads, to the gate as well; `COMPOSE_FILE` is honoured the same way. That closes the
"boot straight past the gate" gap the wrapper would otherwise leave.

Everything after a bare `--` is passed to `docker compose` untouched, which is exactly why the
wrapper refuses it by **shape** rather than by exact token: `-f…`, `-p…`, `--file…`, `--env…`,
`--project…` and `--compose-*` are refused in any form — `-- -f=/tmp/evil.yml`, `-- -f/tmp/evil.yml`
and `-- -p=proj` all fail closed, while `-- --force-recreate` and `-- ac-worldserver` pass through
(rev-3 NB-1). A compose file the gate never read must never reach the boot.

## Checks

| # | Check | Failure mode it prevents |
| - | ----- | ------------------------ |
| 0 | `updates_include` is exactly the four approved rows, `Updates.SourceDirectory` matches the tree | module SQL being registered into the world updater; a source directory that points somewhere else |
| 0b | the worldserver service really mounts `<SourceDirectory>/data/sql` read-only from the tree the gate scanned, and its updater env is exactly `DATABASE_WORLD` | the gate passing on a set of files that is not the set the container will apply; the updater being widened beyond `4` |
| 1 | duplicate basenames across the four dirs (`LOG_FATAL`, `UpdateFetcher.cpp:96`) and case-only collisions (`updates.name` is `utf8mb4_unicode_ci`) | worldserver aborts at boot |
| 2 | prints every `.sql` with no `updates` row — exactly the set the next boot applies | surprises about what is about to run |
| 3 | recomputes `SHA1(ReadSQLUpdate(file))` for every file with a ledger row | a drifted file being silently re-applied (`UpdateFetcher.cpp:350-354`), or a rename the updater will answer by renaming the ledger row (`UpdateFetcher.cpp:335-345`) |
| 4 | write-statement policy for company files | out-of-band content ids, unbounded writes, DDL, ledger tampering |
| 5 | dry run of the pending files on a scratch schema cloned from `acore_world` | a file that only fails after the server is down |

Check 0b reads the resolved compose model with `docker compose config --format json` (no
running container needed) and, if the container is already up, cross-checks `docker inspect`
mounts. It requires: bind mount `Destination == <Updates.SourceDirectory>/data/sql`,
`Source` resolving to `<compose_root>/data/sql`, read-only, no shadowing second mount, and
`AC_UPDATES_ENABLE_DATABASES` set with bit `4` (`DATABASE_WORLD`,
`src/server/database/Database/DatabaseLoader.h:48-51`) and no other bit. `--no-boot-path-check`
skips it for offline use; the wrapper refuses that flag unless `PREFLIGHT_CHECK_ONLY=1`, because
a boot that skips check 0b is a boot the gate did not read.

Why the mount is what makes the gate meaningful: the `.sql` files the updater applies come from
the bind mount, not from the image, so a stale image cannot make the scanned set differ from the
applied set — a stale image only means a stale *binary*. On the dev host the mount comes from the
gitignored root `docker-compose.override.yml` (which `scripts/compose-sync.sh` keeps in sync with
the tracked `wrath-unbound/docker-compose.override.yml`), and it is `:ro`. Check 0b asserts that
at run time instead of trusting the file.

## Provenance (who is "company")

Check 4 is enforced on company-owned files. A file is company-owned when **either** its directory
is tagged `company` in `preflight-policy.json` (`custom/db_world`, `updates/pending_db_world`)
**or** its last-commit author address is on a `companyEmailDomains` domain (`paperclip.local`).
The directory tag is a policy statement, so content dropped into a company directory is always
policy-checked — even when it is committed with `--author` crediting an upstream author, which
this repo's `AGENTS.md` requires. `upstream` (and therefore exempt by default) requires **both**
an upstream-tagged directory and a non-company author. Files git cannot attribute — untracked or
unreadable history — are never exempt, and neither is an upstream file with **uncommitted
changes**: provenance is read from HEAD, so a hand edit in the work tree re claims the file for
the policy (`dirty-worktree`) instead of letting the edit ride the exemption.

So **check 4 covers company content only**, by design. `--strict-all` (policy on everything,
warnings too) is not a usable boot-time setting: against this tree it fails ~754 of 861 upstream
files — 77094 reserved-band, 52967 table-not-allowed, 1666 band-not-literal, 1452 unbounded-write,
25 `DROP`, 4 on `updates`, 2 `EXECUTE` — every one of them upstream content the updater has
always applied. `--policy-all` reports the same set without failing; **CI runs `--policy-all`
report-only** and the boot gate runs the default company-only mode. Anything that must hold
against upstream too is a review question, not a boot gate.

## Write policy (check 4)

For a file under policy, every statement must satisfy:

- no `ALTER` / `DROP` / `TRUNCATE` / `RENAME` / `GRANT` / `REVOKE` / `LOAD` / `CALL` /
  `LOCK` / `HANDLER` / `PREPARE` / `SET GLOBAL`, and no writes to `updates`,
  `updates_include`, `version`, `dbversion`;
- writes target only tables in `baseTables` of `preflight-policy.json`, or a table
  explicitly whitelisted in `customTables`; `CREATE TABLE` is allowed for whitelisted
  custom tables only;
- every `UPDATE`/`DELETE` writes **one** table. A second table anywhere in the write region —
  `UPDATE creature c JOIN updates u SET ...`, `UPDATE a, b SET ...`, `DELETE a, b FROM a JOIN b` —
  is a `multi-table-write` failure, and every table named in that region is still checked against
  the ledger and the allow-list, so a JOIN cannot smuggle `updates` past check 4;
- **aliases are names, not write targets.** `UPDATE \`creature\` \`c\` SET ...` writes one table; `c`
  is reported as `other names in region: c` and is never tested against the allow-list, so an alias
  cannot manufacture a `table-not-allowed` (rev-3 NB-2). The syntax test for a second table ignores
  what is inside backticks, so an alias spelled like syntax — `UPDATE \`creature_template\` \`join\`
  SET ...`, `DELETE FROM \`creature\` \`using\` WHERE ...` — is still a single-table write (NB-3);
  a real `JOIN` is refused regardless of the quoting;
- every `UPDATE`/`DELETE` is `WHERE`-bounded on a **complete** natural key: every column of one
  of the table's `keySets` groups compared to literals with `=`/`<=>`/`IN`, no `OR` that decides
  which rows are reached. Grouping parentheses do not hide a disjunction
  (`WHERE (source_type=0 OR entryorguid=9002020)` fails), a key bounded only on one side of an
  `OR` does not bound the statement, a column bounded only inside `IN (SELECT ...)` does not
   count at all, and `NOT (entry=...)` bounds nothing — the failure says so, naming the negated
   group as the reason rather than blaming the key columns that are present (rev-3 NB-5).
   `key = 1 AND (x='a' OR x='b')` stays legal.

  Predicates after the key are read up to the first top-level `ORDER BY`/`LIMIT`, so trailing
  clauses cannot make a literal look like a non-literal.
  With no `keySets`, the whole `keyColumns` list is one required group. Bounding on part of a
  composite key is not enough (`DELETE FROM module_string WHERE module='x'` deletes the whole
  module);
- keywords inside string literals are inert: `'Please select your destination, hero;'` in
  `page_text` is data, not a statement separator or an `OR` (the masker blanks literal bodies
  while preserving offsets);
- every row that identifies a company creature/quest sits in the reserved band
  (`creature_template.entry` / `quest_template.ID` >= 900000, `creature.guid` >= 9000000,
  plus the reference columns in `refs`), as a literal in a named column;
- no `INSERT ... SELECT`, no `ON DUPLICATE KEY UPDATE`, no `INSERT SET` syntax.

`INSERT`, `UPDATE` and `DELETE` may all carry a `PARTITION (pN)` clause (`INSERT` puts it before
the column list; both positions are parsed). The clause picks partitions, not rows, so it changes
nothing about which ids a statement touches and every id is still band-checked (rev-3 NB-4).

## Measuring the policy over the whole corpus

`ops/preflight/corpus_sweep.py` runs the same check-4 engine over every `.sql` in the four included
directories (3 060 files / 131 625 statements at the time of writing) and tallies findings per rule,
per provenance class and per directory. It is the only honest way to review a parser change: the
boot gate only policy-checks files the updater has not applied yet, so a regression that starts
refusing upstream syntax is invisible to it until someone tries to boot.

```
python3 ops/preflight/corpus_sweep.py --out /tmp/before.json --measure-only   # baseline capture
# ...change the engine...
python3 ops/preflight/corpus_sweep.py --diff /tmp/before.json   # per-rule + per-rule company deltas
python3 ops/preflight/corpus_sweep.py --rule table-not-allowed  # offending file:line
```

The number that matters is the `company` column: it must stay 0, because a finding in
company-authored SQL is a boot refusal. It is therefore also the exit status — a non-zero company
total, or a `--diff` that shows findings migrating `upstream → company` at constant totals, exits
1 so this can be wired into CI rather than read by eye (PRO-179 review NB-C). `--measure-only`
returns 0 regardless, which is what a baseline capture needs. The `upstream` columns are the corpus
the gate exempts by default, and are measured with `--strict-all` to see how much syntax the engine
understands. `liveDatabase` is read from the policy file, so `cross-schema-write` is measured
against the schema the gate actually uses.

## Dry run (check 5)

The pending files are replayed, in `std::set`-ordered basename, into
`acore_world_preflight_<pid>_<stamp>` created with `CREATE TABLE ... LIKE`. By default only
the tables the files mention plus their FK ancestors are cloned (`--clone-tables all` for
the full schema) and the written tables are pre-populated server-side so `WHERE` clauses
match real rows; `FOREIGN_KEY_CHECKS` stays on unless a referenced table was too big to
copy (`--data-copy-max-rows`, which then warns under `dry-run-coverage`). The live schema is
only ever read. `--clone-mode full` (mysqldump) and `minimal` (structure only, FK off)
remain for debugging. `--no-dry-run` skips it and says so — check 5 renders a `dry-run-skipped`
warning rather than an `ok`, so a report can never read as though the updater had been replayed.

`--keep-scratch` leaves `acore_world_preflight_<pid>_<stamp>` in place after the run instead of
dropping it, so a failed dry run can be queried afterwards; nothing else reads that schema, and
the next run drops it before creating its own.

The scratch name is validated against a hard-coded prefix (`acore_world_preflight_`) plus the
live database name and the reserved MySQL schemas **before** the `DROP DATABASE` that opens the
dry run. There is no operator-settable scratch prefix.

## Ledger and hash semantics

- `updates.name` is a `utf8mb4_unicode_ci` primary key, so the gate looks files up
  case-insensitively, exactly like the updater.
- An empty `updates.hash` is a warning, not a failure (`hashCheck.emptyLedgerHashIsWarning`):
  the updater re-hashes and re-inserts such a row (`MODE_REHASH`) rather than re-applying it.
- A hash mismatch where the bytes match *another* ledger row is reported as `hash-rename`:
  upstream answers that with `RenameEntry()` (`UpdateFetcher.cpp:335-345`), not a re-apply.
  A mismatch with genuinely new bytes is `hash-mismatch` and is the silent re-apply case
  (`UpdateFetcher.cpp:350-354`).
- Orphaned ledger rows (rows with no file on disk) are listed for the non-`MODULE` ones, but the
  `--clean-dead-ref-max-count` threshold counts all of them, because `UpdateFetcher` counts all
  of them before it deletes anything (`UpdateFetcher.cpp:409-416`).

## Adding a table to the policy

`baseTables` / `customTables` are the only place to extend this; there is no code change.

1. Find the real DDL — `data/sql/base/db_world/<table>.sql`, or the module SQL that creates a
   custom table. `keyColumns` must be the actual primary key columns; use `keySets` when a
   partial key is legitimately selective (e.g. `creature` → `[["guid"], ["id1","map"]]`).
2. Add `bands` for columns that carry a company content id and `refs` for columns that reference
   one.
3. `python3 ops/preflight/audit_policy_schema.py` checks every policy table/column against the
   DDL in `data/sql/base/db_world` **and** the module SQL that creates the `customTables`
   (`modules/`, `wrath-unbound/modules/`, `nodeps/modules/`, discovered with `git ls-files` so a
   clean clone sees them too). `--no-modules` skips the module half and says so per unchecked
   table; it never skips silently.
4. `PYTHONPATH=ops/preflight python3 ops/preflight/test_preflight_worlddb.py` before committing
   (plain `python3 -m unittest discover -s ops/preflight` fails: the directory is not a package).
5. `bash ops/preflight/test_sync_scripts.sh` before committing any change to `scripts/lua-sync.sh`
   or `scripts/compose-sync.sh`. It is the only thing standing between an edit and the PRO-273
   failure modes coming back, and it fails loudly on the pre-fix scripts — extract one with
   `git show origin/Playerbot:scripts/lua-sync.sh` into a throwaway tree (the harness derives
   both script paths from its own location, so put it at
   `<tmp>/ops/preflight/test_sync_scripts.sh` next to `<tmp>/scripts/`) if you doubt a case
   is real. `HEAD` is the wrong reference to name here: on a branch that already carries the
   fix it re-tests the fix and proves nothing.

## Out of scope

`Updates.EnableDatabases` is `7` in `src/server/apps/worldserver/worldserver.conf.dist:299`; the
container environment sets `AC_UPDATES_ENABLE_DATABASES=4`, which is what the gate validates
against (check 0b). The Playerbots updater (`acore_playerbots`,
`AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES=1`) is a separate updater and is only reported, never
touched.
