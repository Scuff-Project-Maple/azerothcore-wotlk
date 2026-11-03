# World-DB updater pre-flight gate (PRO-179)

`preflight_worlddb.py` is a gate that runs **before** `docker compose up -d ac-worldserver`.
It refuses the boot unless the four `db_world` directories that
`acore_world.updates_include` registers are in a state the updater can be trusted with.

    ops/deploy/worldserver-up.sh              # gate, then compose up
    ops/preflight/preflight_worlddb.py        # gate only (exit 0 pass, 1 fail, 2 operational error)

`PREFLIGHT_CHECK_ONLY=1 ops/deploy/worldserver-up.sh` runs the gate without touching the
stack. The wrapper deliberately does **not** take the single-writer lease at
`.stack/lease` (see `docs/dev-environment.md`); whoever boots the stack takes it, and the
gate is read-only against the live database plus one scratch schema it drops afterwards.

## Checks

| # | Check | Failure mode it prevents |
| - | ----- | ------------------------ |
| 0 | `updates_include` is exactly the four approved rows, `Updates.SourceDirectory` matches the tree | module SQL being registered into the world updater; a source directory that points somewhere else |
| 1 | duplicate basenames across the four dirs (`LOG_FATAL`, `UpdateFetcher.cpp:96`) and case-only collisions (`updates.name` is `utf8mb4_unicode_ci`) | worldserver aborts at boot |
| 2 | prints every `.sql` with no `updates` row — exactly the set the next boot applies | surprises about what is about to run |
| 3 | recomputes `SHA1(ReadSQLUpdate(file))` for every file with a ledger row | a drifted file being silently re-applied (`UpdateFetcher.cpp:350-354`) |
| 4 | write-statement policy for company files | out-of-band content ids, unbounded writes, DDL, ledger tampering |
| 5 | dry run of the pending files on a scratch schema cloned from `acore_world` | a file that only fails after the server is down |

## Write policy (check 4)

Enforced on files classified as company-owned: the `custom/` and `pending_db_world/`
directories, or a last-commit author address on a `companyEmailDomains` domain
(`paperclip.local`). Everything under `archive/db_world` and `updates/db_world` whose last
commit is an upstream address is exempt by default, otherwise a `git pull` of upstream
updates could never boot. `--strict-all` enforces it everywhere.

For a file under policy, every statement must satisfy:

- no `ALTER` / `DROP` / `TRUNCATE` / `RENAME` / `GRANT` / `REVOKE` / `LOAD` / `CALL` /
  `LOCK` / `HANDLER` / `PREPARE` / `SET GLOBAL`, and no writes to `updates`,
  `updates_include`, `version`, `dbversion`;
- writes target only tables in `baseTables` of `preflight-policy.json`, or a table
  explicitly whitelisted in `customTables`; `CREATE TABLE` is allowed for whitelisted
  custom tables only;
- every `UPDATE`/`DELETE` is bounded on the table's natural key: at least one configured
  `keyColumns` entry compared to literals with `=`/`<=>`/`IN`, no top-level `OR`;
- every row that identifies a company creature/quest sits in the reserved band
  (`creature_template.entry` / `quest_template.ID` >= 900000, `creature.guid` >= 9000000,
  plus the reference columns in `refs`), as a literal in a named column;
- no `INSERT ... SELECT`, no `ON DUPLICATE KEY UPDATE`, no `INSERT SET` syntax.

The band, key and table data lives in `preflight-policy.json` — extend it there, not in code.

## Dry run (check 5)

The pending files are replayed, in `std::set`-ordered basename, into
`acore_world_preflight_<pid>_<stamp>` created with `CREATE TABLE ... LIKE`. By default only
the tables the files mention plus their FK ancestors are cloned (`--clone-tables all` for
the full schema) and the written tables are pre-populated server-side so `WHERE` clauses
match real rows; `FOREIGN_KEY_CHECKS` stays on unless a referenced table was too big to
copy (`--data-copy-max-rows`, which then warns under `dry-run-coverage`). The live schema is
only ever read. `--clone-mode full` (mysqldump) and `minimal` (structure only, FK off)
remain for debugging. `--no-dry-run` skips it; the gate still runs checks 0-4.

## Two things reviewers should look at

1. **The `data/sql` bind mount is `rw`, not `ro`.** The worldserver container can write into
   `data/sql/`, and the updater itself writes there when it rehashes. The issue text
   describes it as read-only; `wrath-unbound/docker-compose.override.yml` says otherwise.
   The gate does not depend on the mount being read-only, but the discrepancy is real.
2. **Upstream-provenanced files are exempt from the write policy by default** (see above).
   `--strict-all` is the merge-gate setting; a boot-time gate that enforces it against
   upstream `updates/db_world` will eventually block a legitimate `git pull`.

`Updates.EnableDatabases` in `env/dist/etc/worldserver.conf` is still `7`; the container
environment sets `AC_UPDATES_ENABLE_DATABASES=4`, which is what the gate validates against.
The Playerbots updater (`acore_playerbots`, `AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES=1`) is
out of scope here and untouched.
