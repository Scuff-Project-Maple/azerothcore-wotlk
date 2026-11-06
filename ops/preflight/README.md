# World-DB updater pre-flight gate (PRO-179)

`preflight_worlddb.py` is the gate that runs **before** `docker compose up -d ac-worldserver`.
It refuses the boot unless the four `db_world` directories that
`acore_world.updates_include` registers are in a state the updater can be trusted with.

    ops/deploy/worldserver-up.sh              # lease check + gate, then compose up
    python3 ops/preflight/preflight_worlddb.py  # gate only (exit 0 pass, 1 fail, 2 operational error)
    PYTHONPATH=ops/preflight python3 ops/preflight/test_preflight_worlddb.py  # policy-engine tests, no DB needed

`PREFLIGHT_CHECK_ONLY=1 ops/deploy/worldserver-up.sh` runs the gate without touching the
stack — the rehearsal form, for a human on a machine that has the live database. The PR
workflow does **not** run it: `.github/workflows/worlddb-preflight.yml` runs only the
pure-Python checks (see "In CI" below), because the boot gate reads the live schema and no
CI job has one.

The wrapper **verifies** the single-writer
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

### The third tag: the tracked upstream ref (PRO-248)

Directory tag plus author tag is a claim about **history**, and history metadata is cheap to
fabricate:

```
git commit --author="Someone <x@azerothcore.org>" data/sql/updates/db_world/2026_11_04_00.sql
```

That keeps the work tree clean and buys the exemption, which is how a company engineer can hand
themselves a `warn` (`policy-exempt-origin`) instead of a check-4 review. So a **new** file (no row
in `acore_world.updates`) sitting in an `origin: "upstream"` directory is exempt only when it also
exists in the ref the tree is tracking — `upstreamProvenance.verifiedRef`, read with local object
lookups only (`git ls-tree`), never a fetch:

| ref answers | classification | finding |
| --- | --- | --- |
| name is in the ref, bytes match | `upstream` | none — the ordinary `git pull` path |
| name is in the ref modulo case, bytes match | `upstream` | `WARN` `upstream-ref-case-divergence` naming the ref path |
| name is in the ref, bytes do not match | `company` | `FAIL` `unverified-upstream-content` naming `pending_db_world` |
| ref resolved, name absent | `company` | `FAIL` `unverified-upstream-origin` naming `pending_db_world` |
| git could not answer | `company` | `FAIL` `unverified-upstream-origin`, failing closed, naming `refreshCommand` |
| ref does not resolve under `refs/remotes/` | `company` | `FAIL` `upstream-ref-not-remote` naming `refreshCommand` |
| ref unset / not present locally | `upstream` | `WARN` `upstream-ref-unverified` naming `refreshCommand` |

A ref that is simply missing is deliberately a `WARN` and not a `FAIL`: refusing every clone that
has not fetched is worse than the pre-PRO-248 status quo, and the report names the command that
restores the check. Every other answer the ref can give is a `FAIL`.

`verifiedRef` is stored fully qualified, as `refs/remotes/origin/Playerbot`. A short
`origin/Playerbot` is ambiguous: git expands `refs/heads/` before `refs/remotes/`, so a single
`git branch origin/Playerbot HEAD` makes rev-parse, `ls-tree` and every object lookup answer for
the shadow branch instead of the remote — and `git rev-parse --symbolic-full-name origin/Playerbot`
then prints **nothing on stdout** (warning on stderr, error on stderr) while still exiting 0. The
remote-tracking test is therefore "exit 0 **and** a name starting with `refs/remotes/`"; anything
else is answered as *not remote-tracking* and refuses the boot, because the person whose forged
`--author` this rule exists to catch can create exactly that shadow branch. `audit_policy_schema.py`
exits 1 when `verifiedRef` is deleted, empty, or does not start with `refs/remotes/`, so the
qualifier cannot be dropped silently.

Content **is** consulted. The ref is listed with `git ls-tree -z -r` — NUL-delimited, so a genuine
upstream filename like `2026_11_04_00_érfar.sql` is compared byte-for-byte instead of against git's
quoted `"...\303\251rfar.sql"` — and each company-authored file under an `origin: "upstream"` tag is
re-hashed with `git hash-object` against the blob id from that listing. A verified name whose bytes
were replaced is company content (`unverified-upstream-content`), whether the rewrite was committed
under a forged `--author` or left sitting dirty in the work tree. A case-only twin stays exempt but
is named in a `WARN`, since it is usually the first sign of a rename that the updater will not
recognise.

Honest limit: an operator with write access to the object database can still
`git update-ref refs/remotes/origin/Playerbot HEAD` and fake the third tag, blob ids included. That
is a deliberate, loud, non-default act — `git reflog refs/remotes/origin/Playerbot` shows it to a
reviewer and the next real fetch moves the ref back — where the author forge was a routine commit
command. It is also no wider a privilege than the ones already in the same file: whoever can
`update-ref` can edit `preflight-policy.json` outright.

Be precise about what the audit does and does not pin (PRO-295 review B1).
`audit_policy_schema.py` checks two things, and the CI job runs exactly it: every
`keyColumns`/`keySets`/`bands`/`refs` **column name** in `baseTables` and `customTables`
against the real `CREATE TABLE` bodies in `data/sql/base/db_world` and the module SQL, and the
`upstreamProvenance` block itself — a `verifiedRef` that is present and starts with
`refs/remotes/`, and a non-empty `refreshCommand`. It is a real pin, and it is now run in CI: the
`Write policy — engine tests and schema audit` job of
`.github/workflows/worlddb-preflight.yml` runs `audit_policy_schema.py --include-modules` and fails
the check on any problem (PRO-285), while step 3 of "Adding a table to the policy" below is what
runs it locally before a push. What it does **not** read is `includedDirs[].origin`,
`companyEmailDomains` or `reservedIdBands`: no machine in this repository checks those three, so
they are review material and nothing more. Deleting `data/sql/updates/pending_db_world` from
`includedDirs`, widening `companyEmailDomains` to `["*"]`, or lowering a reserved band all edit
check 4 into compliance rather than failing anything. The two partial tripwires are the falsification
recipes below (each rule is seen to fire on a crafted file, which a widened exemption stops doing)
and the corpus tallies: the sweep records how many files it attributed to `company`, and
`ci_provenance_guard.py` refuses a tally in which no scanned file is company-attributed at all, so
the `includedDirs` edit turns the check red instead of green. The other two edits still need a
reviewer, and are worth a line in any diff that touches `preflight-policy.json`. An upstream
file that trips the reserved
bands stays exempt from check 4, and the corpus `upstream` column records those 444 726 findings as
review material, not boot failures.

So **check 4 covers company content only**, by design. `--strict-all` (policy on everything,
warnings too) is not a usable boot-time setting: against this tree it fails ~754 of 861 upstream
files — 77094 reserved-band, 52967 table-not-allowed, 1666 band-not-literal, 1452 unbounded-write,
25 `DROP`, 4 on `updates`, 2 `EXECUTE` — every one of them upstream content the updater has
always applied. `--policy-all` reports the same set without failing and is what a reviewer runs by
hand; **nothing in CI runs `--policy-all`** — the CI sweep uses `--strict-all` and gates on the
`company` column only (see "In CI" below). The boot gate runs the default company-only mode.
Anything that must hold against upstream too is a review question, not a boot gate.

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
1, and the failing run prints each company row as `file:line: rule: detail` so the log names the
file and the rule instead of sending the reader to `--rule` (PRO-179 review NB-C, PRO-285).
`--measure-only` returns 0 regardless, which is what a baseline capture needs. The `upstream`
columns are the corpus the gate exempts by default, and are measured with `--strict-all` to see how
much syntax the engine understands; they are **reported, never gated**. `liveDatabase` is read from
the policy file, so `cross-schema-write` is measured against the schema the gate actually uses.

The second invariant the sweep enforces (PRO-179 review rank 3) is printed on its own line:

```
region-scan consultations=1 multi-table-write=1 unmatched=0
```

`_region_tables()` is the report text for a statement the gate has **already refused**; it is not a
second way to pass. Over the whole corpus it must therefore be consulted exactly once per
`multi-table-write` finding, and a non-zero `unmatched` exits 1: either a multi-table write is now
accepted while still consulting the scan — a policy change wearing a report fix — or a refusal
stopped naming the tables it rejected. The unit-test twin of this property is
`TestRev5RegionScanImpliesRefusal`, whose `REJECTED` list deliberately includes an all-custom-table
`JOIN`, because "let it through when every table is one of ours" is the edit that would otherwise
leave every other literal green.

Both name lists a refusal prints — `tables named` and `other names in region` — come out of the
same bare-name scan, so when that scan is what produced them both carry the
`(heuristic — unquoted region)` suffix, and neither carries it when every name was quoted
(PRO-179 review rank 4). A schema is likewise believed only when the writer quoted the half that
claims one, decided per component from the token's own backticks: `` `other`.updates `` names
`other`, `a.b.c` names nothing, and `` `acore_world`.`my.table` `` is one table called `my.table`
(PRO-179 review rank 1).

A rule that reports 0 everywhere proves nothing until it has been seen to fire, so the provenance
rules are falsified the same way — forge the provenance in a scratch work tree, keep the work tree
clean, and sweep **without** `--strict-all`:

```
git worktree add --detach /tmp/poc HEAD
cp ops/preflight/{preflight_worlddb.py,preflight-policy.json,corpus_sweep.py} \
   /tmp/poc/ops/preflight/                                     # the engine under review
cd /tmp/poc
echo "INSERT INTO \`creature_template\` (\`entry\`,\`name\`) VALUES (1234,'x');" \
  > data/sql/updates/db_world/2026_11_05_99.sql
git add -- data/sql/updates/db_world/2026_11_05_99.sql          # `commit -a` never stages a new file
git commit -q --author="Someone <x@azerothcore.org>" -m "add upstream creature_template row"
python3 ops/preflight/corpus_sweep.py --measure-only            # company column must become non-zero
```

The `git add` is not decoration, and an earlier revision of this recipe omitted it: with the file
left **untracked**, what moves the company column is the untracked/unknown-author path, not
`unverified-upstream-origin`, so the number measures a different rule than the one under test.
Staged and committed as written above, the same tree measures:

| engine (crafted file present) | under-write-policy | company findings |
| --- | --- | --- |
| pre-PRO-248 (`2c4c8dbf9`) | 7 | none — the forged `--author` bought the exemption |
| PRO-248 | 8 | `unverified-upstream-origin` 1, `reserved-band` 1 |
| PRO-248, file returned to the ref | 7 | none |

The 1234 is deliberately out of band, so a second rule fires too; with an in-band id the
provenance rule is the *only* thing that fires, which is the case the rule exists for. The content
half is falsified from the other direction — a file that genuinely is in the ref, with its bytes
replaced (`git hash-object` against the ref's blob id):

```
T=$(git ls-files data/sql/updates/db_world | grep '\.sql$' | head -1)   # not head -1: .gitkeep
printf '\nINSERT INTO `creature_template` (`entry`,`name`) VALUES (900200,"x");\n' >> "$T"
git add -- "$T"; git commit -q --author="Someone <x@azerothcore.org>" -m "tighten defaults"
python3 ops/preflight/corpus_sweep.py --measure-only   # unverified-upstream-content company=1
```

That rewrite registers the same single company finding whether it is committed under a forged
author or left dirty in the work tree, and both return to 0 once the file is restored. Two traps in
a detached work tree: `git reset --hard HEAD` does **not** undo the crafted commit (reset to the
base commit), and that reset also reverts `ops/preflight/` — re-copy the engine files after it or
you will sweep the engine you just replaced.

### In CI (`.github/workflows/worlddb-preflight.yml`)

The sweep is wired up (PRO-285). On a pull request touching `ops/preflight/**` or `data/sql/**`,
two jobs run. Both are pure Python against the checkout: **no MySQL, no Docker, no deploy host**,
and nothing in either job shells out to a database.

| Step | Command | Verdict |
| --- | --- | --- |
| engine tests | `python3 ops/preflight/test_preflight_worlddb.py` | gates |
| schema audit | `python3 ops/preflight/audit_policy_schema.py --include-modules` | gates |
| corpus sweep | `python3 ops/preflight/corpus_sweep.py --strict-all --out "$RUNNER_TEMP/corpus-sweep.json"` | gates on the `company` column |
| provenance guard | `python3 ops/preflight/ci_provenance_guard.py "$RUNNER_TEMP/corpus-sweep.json"` | gates |

Read the verdict column literally.

- **gates** means the check is red when the command fails, and the commands are the ones above,
  verbatim, un-wrapped. Reproduce a failure locally by checking out the PR head and running the
  same line; the log names the rule per finding (`data/sql/updates/pending_db_world/x.sql:12:
  table-not-allowed: ...`).
- The `upstream` columns **report only**. This tree measures ~444 700 upstream findings and always
  will; a number moving there is information for the reviewer, not a merge failure. That is the same
  decision the boot gate makes about upstream content, written down instead of left to whoever reads
  a log.
- The guard is not a second policy verdict — company findings are `corpus_sweep.py`'s to fail on.
  It fails a run whose provenance was never read out of history. `git_author_origin()` asks
  `git log -1 -- <path>` per file, so the **checkout** decides the answer as much as the corpus
  does, and a shallow clone is worse than unattributable: its grafted root answers for *every*
  path, so each file inherits the tip author and the `unknown` column stays empty to warn nobody.
  Measured on a `--depth 1` clone of this repo, all 3 060 scanned files came back `company` and
  444 726 findings landed in the gated column — a red check with the entire upstream corpus in the
  dock. Give the same checkout an upstream-authored tip, which is what `Playerbot` normally has,
  and all 444 726 move to `upstream` and the column the check gates on reads **0**. So the sweep
  records `shallow`, `commits` and `authors` in its tally and the guard refuses a shallow repo, a
  one-commit history, one distinct author across thousands of files, or a corpus with no upstream
  files at all. The sweep job also checks out with `fetch-depth: 0`; the guard is the net, not the
  fix.
- The guard runs **after a red sweep too** (`if: ${{ !cancelled() }}`), because a provenance
  collapse and a genuine policy violation look identical in the log: both print a `company` column
  full of rows. The run that just went red is exactly the run where "is this verdict attributable"
  still has to be answered, so the sweep writes the tally before it exits and the guard reads it
  either way.

What a green check does **not** say: it is not a boot rehearsal. None of these steps reads the
`updates` ledger, hashes pending files, replays them into a scratch schema or checks a compose
mount — those belong to the boot gate (see "Dry run (check 5)"). Green here means the write-policy
engine agrees with itself over the corpus in this checkout, and that the corpus was attributable.

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
   table; it never skips silently. Pass it the **same** `--policy` (and `--repo`) you pass the
   gate: the auditor otherwise reads the policy sitting next to itself, so a gate run with
   `--policy /some/other.json` would be audited against a document it is not enforcing — a
   policy the auditor never opened can pass while the file that decides the boot is not (PRO-248
   review N5). `python3 ops/preflight/audit_policy_schema.py --policy "$POLICY" --repo "$TREE"`.
4. `PYTHONPATH=ops/preflight python3 ops/preflight/test_preflight_worlddb.py` before committing
   (plain `python3 -m unittest discover -s ops/preflight` fails: the directory is not a package).

## Out of scope

`Updates.EnableDatabases` is `7` in `src/server/apps/worldserver/worldserver.conf.dist:299`; the
container environment sets `AC_UPDATES_ENABLE_DATABASES=4`, which is what the gate validates
against (check 0b). The Playerbots updater (`acore_playerbots`,
`AC_PLAYERBOTS_UPDATES_ENABLE_DATABASES=1`) is a separate updater and is only reported, never
touched.
