# World-DB updater pre-flight gate (PRO-179)

`preflight_worlddb.py` is the gate that runs **before** `docker compose up -d ac-worldserver`.
It refuses the boot unless the four `db_world` directories that
`acore_world.updates_include` registers are in a state the updater can be trusted with.

    ops/deploy/worldserver-up.sh              # lease check + gate, then compose up
    python3 ops/preflight/preflight_worlddb.py  # gate only (exit 0 pass, 1 fail, 2 operational error)

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
skips it for offline use.

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
unreadable history — are never exempt. `--strict-all` enforces the policy on everything, which is
the CI/merge-gate setting; a boot-time gate that enforced it against upstream
`updates/db_world` would eventually block a legitimate `git pull`.

## Write policy (check 4)

For a file under policy, every statement must satisfy:

- no `ALTER` / `DROP` / `TRUNCATE` / `RENAME` / `GRANT` / `REVOKE` / `LOAD` / `CALL` /
  `LOCK` / `HANDLER` / `PREPARE` / `SET GLOBAL`, and no writes to `updates`,
  `updates_include`, `version`, `dbversion`;
- writes target only tables in `baseTables` of `preflight-policy.json`, or a table
  explicitly whitelisted in `customTables`; `CREATE TABLE` is allowed for whitelisted
  custom tables only;
- every `UPDATE`/`DELETE` is `WHERE`-bounded on a **complete** natural key: every column of one
  of the table's `keySets` groups compared to literals with `=`/`<=>`/`IN`, no top-level `OR`.
  With no `keySets`, the whole `keyColumns` list is one required group. Bounding on part of a
  composite key is not enough (`DELETE FROM module_string WHERE module='x'` deletes the whole
  module);
- every row that identifies a company creature/quest sits in the reserved band
  (`creature_template.entry` / `quest_template.ID` >= 900000, `creature.guid` >= 9000000,
  plus the reference columns in `refs`), as a literal in a named column;
- no `INSERT ... SELECT`, no `ON DUPLICATE KEY UPDATE`, no `INSERT SET` syntax.

## Dry run (check 5)

The pending files are replayed, in `std::set`-ordered basename, into
`acore_world_preflight_<pid>_<stamp>` created with `CREATE TABLE ... LIKE`. By default only
the tables the files mention plus their FK ancestors are cloned (`--clone-tables all` for
the full schema) and the written tables are pre-populated server-side so `WHERE` clauses
match real rows; `FOREIGN_KEY_CHECKS` stays on unless a referenced table was too big to
copy (`--data-copy-max-rows`, which then warns under `dry-run-coverage`). The live schema is
only ever read. `--clone-mode full` (mysqldump) and `minimal` (structure only, FK off)
remain for debugging. `--no-dry-run` skips it; the gate still runs checks 0-4.

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
   DDL in `data/sql/base/db_world` and reports drift. Module-created tables are skipped, since
   their DDL is not in that directory — verify those against the module SQL by hand.
4. `python3 -m unittest discover -s ops/preflight` before committing.

## Out of scope

`Updates.EnableDatabases` is `7` in `src/server/apps/worldserver/worldserver.conf.dist:299`; the
container environment sets `AC_UPDATES_ENABLE_DATABASES=4`, which is what the gate validates
against (check 0b). The Playerbots updater (`acore_playerbots`,
`AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES=1`) is a separate updater and is only reported, never
touched.
